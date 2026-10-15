"""
automation/sim/commands.py — 飛書 bot 結構化指令解析與分派（M3）

提供三個對外函數：
- `is_sim_prefix(text)`：快速判斷是否為 sim 指令（feishu_bot 路由用）
- `parse_command(text)`：解析成 ParsedCommand；非 sim 指令回 None；前綴對但格式錯回 _invalid
- `dispatch(parsed, engine, feed)`：執行對應 engine/feed 操作，回傳格式化 reply 字串

設計原則：
- 純函數、不依賴飛書 API，便於單測（mock engine + feed 即可）
- 引擎 thread-safety 由呼叫端負責（`MatchEngine` 內部已用 RLock 包鎖；dispatch
  不必額外加鎖）
- 格式化輸出走純文字（飛書 send_message 用 text 模式）；表格用空白對齊，行內
  emoji 給狀態提示

REQUIREMENTS §8 涵蓋的指令：
    /buy <symbol> <qty>                    # MKT buy
    /buy <symbol> <qty> limit <price>      # LMT buy
    /buy <symbol> <qty> stop <price>       # STOP buy
    /sell ...                              # 同 buy 但 SELL
    /cancel <order_id>
    /close_all
    /portfolio  /p
    /orders
    /pnl
    /equity
    /quote <symbol>
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from sim.engine import MatchEngine
from sim.feed import QuoteFeed
from sim.models import Order, OrderSide, OrderStatus, OrderType


# 即時成交 callback 型別（與 runner.run_loop 的 on_fill 同簽名）
# Caller 應傳入跟 runner 同一個 callback，這樣 MKT 立即成交與 runner 撮合
# 兩條路徑的即時通知都走同一條推送邏輯。
_OnFillCb = Callable[[dict, dict], None]


# ─────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────

@dataclass
class ParsedCommand:
    cmd: str                                # buy / sell / cancel / portfolio / ...
    args: dict = field(default_factory=dict)
    raw: str = ""                           # 原始輸入（debug / audit 用）


# 已知 sim 指令前綴（小寫）；is_sim_prefix / parse_command 都用這個
_SIM_PREFIXES: set[str] = {
    "/buy", "/sell", "/cancel", "/close_all",
    "/portfolio", "/p",
    "/orders", "/pnl", "/equity", "/quote",
}

# 報價延遲提示（yfinance ~15 分鐘）
_QUOTE_DISCLAIMER = "（yfinance ~15 分鐘延遲）"

# 使用者常會把 placeholder 標記（<>、「」、《》、[]、`、引號）連同內容一起貼上來，
# 例如 `/cancel <ord-xxx>` 直接複製文件範例。為了少一次重打，這裡把這些字元
# 從頭尾剝掉。中間出現不剝（避免改動正常輸入）。
_PLACEHOLDER_WRAP_CHARS = "<>「」《》[]`'\""


def _strip_placeholder_wrap(s: str) -> str:
    """剝掉首尾常見 placeholder 包裝字元；中間保留。"""
    if not s:
        return s
    return s.strip(_PLACEHOLDER_WRAP_CHARS).strip()


# ─────────────────────────────────────────────────────
# 解析
# ─────────────────────────────────────────────────────

def is_sim_prefix(text: str) -> bool:
    """快速判斷：第一個 token 是否為已知 sim 指令前綴（小寫比對）。"""
    if not text:
        return False
    first = text.strip().split(maxsplit=1)[0].lower()
    return first in _SIM_PREFIXES


def parse_command(text: str) -> Optional[ParsedCommand]:
    """
    解析使用者輸入。
    - 非 sim 前綴 → None（讓 feishu_bot fall-through 到 LLM）
    - 前綴對、格式錯 → ParsedCommand(cmd='_invalid', args={'reason': ...})
    """
    if not text:
        return None
    raw = text.strip()
    tokens = raw.split()
    if not tokens:
        return None

    head = tokens[0].lower()
    if head not in _SIM_PREFIXES:
        return None

    rest = tokens[1:]

    if head in ("/buy", "/sell"):
        return _parse_order(head, rest, raw)
    if head == "/cancel":
        return _parse_cancel(rest, raw)
    if head == "/close_all":
        return ParsedCommand(cmd="close_all", raw=raw)
    if head in ("/portfolio", "/p"):
        return ParsedCommand(cmd="portfolio", raw=raw)
    if head == "/orders":
        return ParsedCommand(cmd="orders", raw=raw)
    if head == "/pnl":
        return ParsedCommand(cmd="pnl", raw=raw)
    if head == "/equity":
        return ParsedCommand(cmd="equity", raw=raw)
    if head == "/quote":
        return _parse_quote(rest, raw)

    # 不該到這裡（_SIM_PREFIXES 已涵蓋全部）
    return _invalid(raw, f"未實作的指令: {head}")


def _parse_order(head: str, rest: list[str], raw: str) -> ParsedCommand:
    """
    解析 /buy 或 /sell：
      /buy SYM QTY                  → MKT
      /buy SYM QTY limit PRICE      → LMT
      /buy SYM QTY stop PRICE       → STOP
    """
    side = OrderSide.BUY if head == "/buy" else OrderSide.SELL

    if len(rest) < 2:
        return _invalid(raw, f"用法：{head} <symbol> <qty> [limit|stop <price>]")

    symbol = _strip_placeholder_wrap(rest[0])
    if not _is_valid_symbol(symbol):
        return _invalid(raw, f"無效的 symbol: {symbol}")

    qty = _parse_int(rest[1])
    if qty is None or qty <= 0:
        return _invalid(raw, f"qty 必須為正整數: {rest[1]}")

    # 沒指定 limit/stop → 市價
    if len(rest) == 2:
        return ParsedCommand(
            cmd="order",
            args={"side": side, "type": OrderType.MKT, "symbol": symbol, "qty": qty, "price": None},
            raw=raw,
        )

    # 第三個 token 必須是 limit 或 stop
    if len(rest) != 4:
        return _invalid(raw, f"用法：{head} <symbol> <qty> [limit|stop <price>]")

    kind = rest[2].lower()
    price = _parse_float(rest[3])
    if price is None or price <= 0:
        return _invalid(raw, f"price 必須為正數: {rest[3]}")

    if kind == "limit":
        otype = OrderType.LMT
    elif kind == "stop":
        otype = OrderType.STOP
    else:
        return _invalid(raw, f"未知的訂單類型: {kind}（僅支援 limit / stop）")

    return ParsedCommand(
        cmd="order",
        args={"side": side, "type": otype, "symbol": symbol, "qty": qty, "price": price},
        raw=raw,
    )


def _parse_cancel(rest: list[str], raw: str) -> ParsedCommand:
    if len(rest) != 1:
        return _invalid(raw, "用法：/cancel <order_id>")
    order_id = _strip_placeholder_wrap(rest[0])
    if not order_id:
        return _invalid(raw, "用法：/cancel <order_id>")
    return ParsedCommand(cmd="cancel", args={"order_id": order_id}, raw=raw)


def _parse_quote(rest: list[str], raw: str) -> ParsedCommand:
    if len(rest) != 1:
        return _invalid(raw, "用法：/quote <symbol>")
    sym = _strip_placeholder_wrap(rest[0])
    if not _is_valid_symbol(sym):
        return _invalid(raw, f"無效的 symbol: {sym}")
    return ParsedCommand(cmd="quote", args={"symbol": sym}, raw=raw)


def _invalid(raw: str, reason: str) -> ParsedCommand:
    return ParsedCommand(cmd="_invalid", args={"reason": reason}, raw=raw)


# ─────────────────────────────────────────────────────
# 解析 helpers
# ─────────────────────────────────────────────────────

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")


def _is_valid_symbol(s: str) -> bool:
    return bool(_SYMBOL_RE.match(s))


def _parse_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────
# 分派執行
# ─────────────────────────────────────────────────────

def dispatch(
    parsed: Optional[ParsedCommand],
    engine: MatchEngine,
    feed: QuoteFeed,
    *,
    on_fill: Optional[_OnFillCb] = None,
) -> str:
    """
    依 parsed.cmd 執行對應 engine / feed 操作，回傳格式化 reply 字串。
    parsed=None 不應該發生（feishu_bot 應先用 is_sim_prefix 過濾）；防呆回提示。

    on_fill（M5）：MKT 訂單與 close_all 在 dispatch 內立即撮合（M3 即時回饋
    設計），這條路徑跟 runner.run_loop 是兩條獨立成交點。caller 傳入同一個
    callback 確保兩條路徑的即時通知都走同一條推送邏輯（避免 _do_order 立即
    成交時漏推 DM）。callback 拋例外不影響回覆字串生成（包 try/except）。
    """
    if parsed is None:
        return "❓ 無法解析指令。"

    if parsed.cmd == "_invalid":
        reason = parsed.args.get("reason", "格式錯誤")
        return f"❌ {reason}"

    if parsed.cmd == "order":
        return _do_order(parsed, engine, feed, on_fill=on_fill)
    if parsed.cmd == "cancel":
        return _do_cancel(parsed, engine)
    if parsed.cmd == "close_all":
        return _do_close_all(engine, feed, on_fill=on_fill)
    if parsed.cmd == "portfolio":
        return _do_portfolio(engine, feed)
    if parsed.cmd == "orders":
        return _do_orders(engine)
    if parsed.cmd == "pnl":
        return _do_pnl(engine, feed)
    if parsed.cmd == "equity":
        return _do_equity(engine, feed)
    if parsed.cmd == "quote":
        return _do_quote(parsed, feed)

    return f"❓ 未知指令：{parsed.cmd}"


# ─────────────────────────────────────────────────────
# 個別指令執行
# ─────────────────────────────────────────────────────

def _invoke_on_fill(
    on_fill: Optional[_OnFillCb],
    fills: list,
    engine: MatchEngine,
    quotes: dict[str, float],
) -> None:
    """共用：把成交 fills 餵給 on_fill callback；callback 拋例外吞掉不傳播。

    snapshot 用本次撮合的 quotes（跟 runner.run_loop 行為一致）。
    """
    if on_fill is None or not fills:
        return
    try:
        snapshot = engine.snapshot(quotes)
    except Exception:
        snapshot = {}
    for fill in fills:
        try:
            on_fill(fill.to_dict(), snapshot)
        except Exception:
            # 不阻擋回覆字串生成；feishu_bot 端 callback 自己有 try/except + log
            pass

def _do_order(
    parsed: ParsedCommand,
    engine: MatchEngine,
    feed: QuoteFeed,
    *,
    on_fill: Optional[_OnFillCb] = None,
) -> str:
    """處理 /buy 或 /sell。"""
    side: OrderSide = parsed.args["side"]
    otype: OrderType = parsed.args["type"]
    symbol: str = parsed.args["symbol"]
    qty: int = parsed.args["qty"]
    price: Optional[float] = parsed.args.get("price")

    order = Order(symbol=symbol, side=side, type=otype, qty=qty, price=price)
    submitted = engine.submit_order(order)

    if submitted.status == OrderStatus.REJECTED:
        return f"❌ 下單失敗：{submitted.notes}\n  symbol={symbol} side={side.value} type={otype.value} qty={qty}"

    side_zh = "買" if side == OrderSide.BUY else "賣"
    type_zh = {
        OrderType.MKT: "市價",
        OrderType.LMT: "限價",
        OrderType.STOP: "停損",
        OrderType.TP: "停利",
    }.get(otype, otype.value)

    head = f"✅ 已下單 {submitted.order_id}\n  {type_zh}{side_zh} {symbol} {qty} 股"
    if price is not None:
        head += f" @ {price}"

    # 市價單：嘗試立刻撮合一次（reply 包含成交結果，給用戶即時回饋）
    # 限價/停損單：runner 30 秒後或下次互動會撮合，此處先回 PENDING
    if otype == OrderType.MKT:
        quotes = feed.get_quotes([symbol])
        if symbol in quotes:
            fills = engine.match(quotes)
            engine.apply_fills(fills)
            # M5：dispatch 立即撮合也觸發 on_fill callback（否則 MKT 成交漏推 DM）
            _invoke_on_fill(on_fill, fills, engine, quotes)
            # 找對應這張單的 fill
            for fill in fills:
                if fill.order_id == submitted.order_id:
                    return (
                        f"{head}\n"
                        f"  → 成交 @ {fill.price:.2f}（含滑價，手續費 {fill.fee:.2f}"
                        + (f"、稅 {fill.tax:.2f}" if fill.tax else "")
                        + f"）\n"
                        f"  → 剩餘現金 {engine.account.cash:,.2f}"
                    )
            # 拉到報價但 match 沒成交（理論上 MKT 必成交，除非 quote None）
            return f"{head}\n  ⏳ 已掛單，等候撮合"
        else:
            return f"{head}\n  ⚠️ 無法取得 {symbol} 即時報價，已掛單等候 runner 撮合"

    # 限價、停損
    return f"{head}\n  ⏳ 等候撮合（背景每 {engine.match_interval_sec()} 秒檢查一次）"


def _do_cancel(parsed: ParsedCommand, engine: MatchEngine) -> str:
    order_id = parsed.args["order_id"]
    ok = engine.cancel_order(order_id)
    if ok:
        return f"✅ 已取消 {order_id}"
    return f"❌ 取消失敗：找不到 pending 訂單 {order_id}"


def _do_close_all(
    engine: MatchEngine,
    feed: QuoteFeed,
    *,
    on_fill: Optional[_OnFillCb] = None,
) -> str:
    submitted = engine.close_all_positions()
    if not submitted:
        return "✅ 目前無持倉，無需平倉。"

    # 嘗試立刻撮合（市價單）
    symbols = list({o.symbol for o in submitted})
    quotes = feed.get_quotes(symbols)
    if quotes:
        fills = engine.match(quotes)
        engine.apply_fills(fills)
        # M5：立即撮合觸發 on_fill callback
        _invoke_on_fill(on_fill, fills, engine, quotes)
        # 對應 fills
        fill_by_id = {f.order_id: f for f in fills}
    else:
        fill_by_id = {}

    lines = [f"✅ 全平倉送出 {len(submitted)} 張市價賣單："]
    for o in submitted:
        line = f"  {o.order_id} {o.symbol} {o.qty} 股"
        f = fill_by_id.get(o.order_id)
        if f is not None:
            line += f" → 成交 @ {f.price:.2f}"
        else:
            line += " ⏳ 等候撮合"
        lines.append(line)
    lines.append(f"  剩餘現金 {engine.account.cash:,.2f}")
    return "\n".join(lines)


def _do_portfolio(engine: MatchEngine, feed: QuoteFeed) -> str:
    positions = engine.account.positions
    if not positions:
        return f"📊 目前無持倉。\n  現金 {engine.account.cash:,.2f}"

    quotes = feed.get_quotes(list(positions.keys()))
    lines = [f"📊 持倉摘要 {_QUOTE_DISCLAIMER}"]
    lines.append(f"  {'symbol':<10} {'qty':>6} {'avg_cost':>10} {'now':>10} {'unrealized':>12}")
    for sym, pos in positions.items():
        now = quotes.get(sym)
        now_s = f"{now:.2f}" if now is not None else "—"
        if now is not None:
            unrealized = (now - pos.avg_cost) * pos.qty
            unrealized_s = f"{unrealized:+,.2f}"
        else:
            unrealized_s = "—"
        lines.append(
            f"  {sym:<10} {pos.qty:>6} {pos.avg_cost:>10.2f} {now_s:>10} {unrealized_s:>12}"
        )
    lines.append(f"  現金 {engine.account.cash:,.2f}")
    return "\n".join(lines)


def _do_orders(engine: MatchEngine) -> str:
    if not engine.pending_orders:
        return "📋 目前無待成交委託。"

    lines = ["📋 待成交委託："]
    lines.append(f"  {'order_id':<22} {'side':<5} {'type':<5} {'symbol':<10} {'qty':>6} {'price':>10} {'status':<14}")
    for o in engine.pending_orders.values():
        price_s = f"{o.price:.2f}" if o.price is not None else "—"
        extra = ""
        if o.oco_group_id:
            extra += f" oco={o.oco_group_id[:14]}"
        if o.parent_order_id:
            extra += f" parent={o.parent_order_id}"
        lines.append(
            f"  {o.order_id:<22} {o.side.value:<5} {o.type.value:<5} "
            f"{o.symbol:<10} {o.qty:>6} {price_s:>10} {o.status.value:<14}{extra}"
        )
    return "\n".join(lines)


def _do_pnl(engine: MatchEngine, feed: QuoteFeed) -> str:
    """已實現損益（今日 / 30 日 / lifetime）+ 未實現損益（M5 補今日 / 30 日切片）。"""
    from sim import reports as sim_reports

    realized = engine.account.total_realized_pnl
    positions = engine.account.positions
    unrealized = 0.0
    if positions:
        quotes = feed.get_quotes(list(positions.keys()))
        for sym, pos in positions.items():
            q = quotes.get(sym)
            if q is not None:
                unrealized += (q - pos.avg_cost) * pos.qty

    total = realized + unrealized

    # M5：今日 / 30 日切片（從 fills.jsonl replay 算，bot 重啟後仍正確）
    fills_path = engine.fills_log_path
    today_pnl = sim_reports.today_realized_pnl(fills_path)
    last30_pnl = sim_reports.last_n_days_realized_pnl(fills_path, 30)

    return (
        "💰 損益摘要\n"
        f"  今日已實現：{today_pnl:+,.2f}\n"
        f"  近 30 日已實現：{last30_pnl:+,.2f}\n"
        f"  累計已實現：{realized:+,.2f}\n"
        f"  未實現損益：{unrealized:+,.2f} {_QUOTE_DISCLAIMER}\n"
        f"  合計：{total:+,.2f}"
    )


def _do_equity(engine: MatchEngine, feed: QuoteFeed) -> str:
    """權益摘要（當下值 + 近 30 日 equity_curve 最高 / 最低 / 30 日變化）。"""
    from datetime import datetime, timedelta
    from sim import reports as sim_reports

    positions = engine.account.positions
    quotes: dict[str, float] = {}
    if positions:
        quotes = feed.get_quotes(list(positions.keys()))
    equity = engine.account.equity(quotes)
    position_value = equity - engine.account.cash
    initial = engine.account.initial_cash
    return_pct = (equity - initial) / initial * 100 if initial else 0.0

    # M5：近 30 日 equity_curve 摘要（從 logs/sim/equity_curve.jsonl 抓）
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=30)
    eqs: list[float] = []
    for record in sim_reports._iter_jsonl(sim_reports.DEFAULT_EQUITY_CURVE_PATH):
        ts_raw = record.get("time")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            continue
        if start_dt <= ts <= end_dt:
            try:
                eqs.append(float(record.get("equity")))
            except (TypeError, ValueError):
                continue

    head = (
        f"💎 權益摘要 {_QUOTE_DISCLAIMER}\n"
        f"  現金：{engine.account.cash:,.2f}\n"
        f"  持倉市值：{position_value:,.2f}\n"
        f"  總權益：{equity:,.2f}\n"
        f"  總報酬率（vs 初始 {initial:,.0f}）：{return_pct:+.2f}%"
    )
    if not eqs:
        return head + "\n  近 30 日權益曲線：無記錄"
    return (
        head + "\n"
        f"  近 30 日 equity 區間：[{min(eqs):,.0f} ~ {max(eqs):,.0f}]"
        f"（{len(eqs)} 筆）\n"
        f"  30 日變化：{(eqs[-1] - eqs[0]):+,.0f}"
    )


def _do_quote(parsed: ParsedCommand, feed: QuoteFeed) -> str:
    sym = parsed.args["symbol"]
    quotes = feed.get_quotes([sym])
    if sym not in quotes:
        # 從沒拿到過報價（含 fallback 也沒）
        return f"❌ 無法取得 {sym} 報價（symbol 不存在或網路異常）"
    price = quotes[sym]
    return f"💹 {sym} = {price:.2f} {_QUOTE_DISCLAIMER}"
