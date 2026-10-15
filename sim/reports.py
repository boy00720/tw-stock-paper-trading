"""
automation/sim/reports.py — 通知 / 日報 / 週報 / equity_curve（M5）

對外 API（區塊 A + B；區塊 E CLI / 區塊 F /pnl /equity 切片留 M5 後續）：

資料層（純函數，從 jsonl 讀 + 計算）：
- iter_fills(path) → Iterator[dict]
- iter_fills_in_period(path, start, end) → list[dict]
- today_realized_pnl(path, *, now=None) → float
- last_n_days_realized_pnl(path, n_days, *, now=None) → float
- win_rate(fills) → dict{total, win, rate}
- replay_positions_from_fills(path) → dict（包 _position_math 純函數）
- equity_curve_append(path, **kwargs) → None

格式化層：
- fmt_realtime_fill(fill, account_after) → str
- fmt_daily_report(date, fills_today, positions, account) → str
- fmt_weekly_report(week_range, fills_week, equity_curve_week, lifetime_stats) → str

設計原則：
- 純函數，不依賴飛書 / lark API（CLI 與通知串接留 sim/feishu_bot 處理）
- 時間欄位用 naive isoformat（fills.jsonl 內 filled_at 格式對齊；本地時區
  假設 UTC+8 台灣，跨日界用本地 datetime）
- jsonl JSON parse 失敗的行略過（與 nl_query._tail_jsonl 一致）
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from sim._position_math import replay_positions_from_fills as _replay_positions_pure

logger = logging.getLogger(__name__)


# 模組級常數（給 CLI 用；資料層函數不依賴）
AUTOMATION_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FILLS_PATH = AUTOMATION_DIR / "logs" / "sim" / "fills.jsonl"
DEFAULT_ORDERS_PATH = AUTOMATION_DIR / "logs" / "sim" / "orders.jsonl"
DEFAULT_ADVISOR_AUDIT_PATH = AUTOMATION_DIR / "logs" / "sim" / "advisor_audit.jsonl"
DEFAULT_EQUITY_CURVE_PATH = AUTOMATION_DIR / "logs" / "sim" / "equity_curve.jsonl"
DEFAULT_LARK_CONFIG = AUTOMATION_DIR / "config" / "lark_config.yaml"
DEFAULT_SIM_CONFIG = AUTOMATION_DIR / "config" / "sim_config.yaml"


# ─────────────────────────────────────────────────────
# 資料層：jsonl 讀取與時間過濾
# ─────────────────────────────────────────────────────

def iter_fills(path: Path | str) -> Iterator[dict[str, Any]]:
    """streaming 讀 fills.jsonl，逐行 yield dict。

    壞行（JSONDecodeError、空行）略過；檔案不存在時直接 return（產生空 iterator）。
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        logger.warning("[sim.reports] iter_fills %s 失敗: %s", p, exc)
        return


def _parse_fill_time(fill: dict[str, Any]) -> Optional[datetime]:
    """從 fill dict 取 filled_at 時間欄位 → naive datetime；解析失敗回 None。"""
    raw = fill.get("filled_at") or fill.get("time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def iter_fills_in_period(
    path: Path | str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """讀 fills.jsonl 中 filled_at 在 [start, end) 範圍的記錄。

    無時區的 naive datetime 比較（與 fills.jsonl 寫入格式一致）。
    """
    out: list[dict[str, Any]] = []
    for fill in iter_fills(path):
        ts = _parse_fill_time(fill)
        if ts is None:
            continue
        if start <= ts < end:
            out.append(fill)
    return out


def today_realized_pnl(
    path: Path | str,
    *,
    now: Optional[datetime] = None,
) -> float:
    """今日（local 0:00 起、不含明日 0:00）已實現損益總和。"""
    n = now or datetime.now()
    start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    fills = iter_fills_in_period(path, start, end)
    return _sum_realized_from_fills(path, fills)


def last_n_days_realized_pnl(
    path: Path | str,
    n_days: int,
    *,
    now: Optional[datetime] = None,
) -> float:
    """近 N 日（含今日）已實現損益總和。"""
    n = now or datetime.now()
    end = (n.replace(hour=0, minute=0, second=0, microsecond=0)
           + timedelta(days=1))
    start = end - timedelta(days=n_days)
    fills = iter_fills_in_period(path, start, end)
    return _sum_realized_from_fills(path, fills)


def _sum_realized_from_fills(path: Path | str, fills_in_window: list[dict[str, Any]]) -> float:
    """計算「期間內已實現損益」。

    複雜度：fill 只記 price/qty/fee/tax，沒有當下的 avg_cost。要算 realized_pnl
    必須 replay 整個歷史（直到該 fill）。M5 取折衷：對全歷史 fills 跑 replay，
    但只 sum「期間內 fills 對應的 realized」。

    具體做法：iter 全歷史 fills，每處理一筆 SELL 計算 realized；若該 SELL 的
    filled_at 在期間內，加進 sum；否則略過。
    """
    if not fills_in_window:
        return 0.0
    in_window_ids = {f.get("fill_id") for f in fills_in_window if f.get("fill_id")}
    if not in_window_ids:
        # fallback：沒有 fill_id 就退回用 (filled_at, order_id) 配對
        in_window_keys = {(f.get("filled_at"), f.get("order_id")) for f in fills_in_window}
    else:
        in_window_keys = None

    positions: dict[str, dict[str, Any]] = {}
    total = 0.0

    for fill in iter_fills(path):
        symbol = fill.get("symbol")
        side = (fill.get("side") or "").upper()
        if not symbol or side not in ("BUY", "SELL"):
            continue
        try:
            price = float(fill.get("price", 0))
            qty = int(fill.get("qty", 0))
            fee = float(fill.get("fee", 0) or 0)
            tax = float(fill.get("tax", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        gross = price * qty

        if side == "BUY":
            existing = positions.get(symbol)
            if existing is None:
                positions[symbol] = {"qty": qty, "avg_cost": (gross + fee) / qty}
            else:
                old_total_cost = existing["qty"] * existing["avg_cost"]
                new_total_cost = old_total_cost + gross + fee
                new_qty = existing["qty"] + qty
                existing["qty"] = new_qty
                existing["avg_cost"] = new_total_cost / new_qty
        else:  # SELL
            existing = positions.get(symbol)
            if existing is None or existing["qty"] < qty:
                continue
            net_proceeds = gross - fee - tax
            cost_of_sold = qty * existing["avg_cost"]
            realized = net_proceeds - cost_of_sold

            in_window = False
            if in_window_keys is not None:
                in_window = (fill.get("filled_at"), fill.get("order_id")) in in_window_keys
            else:
                in_window = fill.get("fill_id") in in_window_ids
            if in_window:
                total += realized

            existing["qty"] -= qty
            if existing["qty"] == 0:
                del positions[symbol]

    return total


def win_rate(fills: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """簡化勝率（同 nl_query._compute_lifetime_stats）。

    定義：realized 筆數 = realized_pnl 不為 0/null 的 fill 數；勝率 =
    realized_pnl > 0 的筆數 / realized 筆數。fills 應該是已含 realized_pnl
    欄位的 dict（reports CLI 時 fill jsonl 沒這欄位 — 實作此函數的 caller
    應自行先補 realized_pnl）。
    """
    realized: list[dict[str, Any]] = []
    for f in fills:
        rp = f.get("realized_pnl") if isinstance(f, dict) else None
        if rp is None:
            continue
        try:
            if float(rp) != 0.0:
                realized.append(f)
        except (TypeError, ValueError):
            continue

    win = 0
    for f in realized:
        try:
            if float(f.get("realized_pnl", 0)) > 0:
                win += 1
        except (TypeError, ValueError):
            pass

    total = len(realized)
    return {
        "total": total,
        "win": win,
        "rate": (win / total) if total > 0 else None,
    }


def replay_positions_from_fills(path: Path | str) -> dict[str, Any]:
    """從 fills.jsonl 重建持倉。包 _position_math 純函數，便於測試與 CLI 共用。"""
    return _replay_positions_pure(iter_fills(path))


# ─────────────────────────────────────────────────────
# equity_curve append
# ─────────────────────────────────────────────────────

def equity_curve_append(
    path: Path | str,
    *,
    time: str,
    cash: float,
    position_value: float,
    equity: float,
    total_realized_pnl: float,
) -> None:
    """append 一筆權益曲線記錄到 equity_curve.jsonl。

    用 keyword-only 強制呼叫端寫清欄位名（避免位置誤插）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": time,
        "cash": cash,
        "position_value": position_value,
        "equity": equity,
        "total_realized_pnl": total_realized_pnl,
    }
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ─────────────────────────────────────────────────────
# 格式化層（對齊 M3 commands._fmt_* 風格）
# ─────────────────────────────────────────────────────

def _fmt_signed_money(value: Any) -> str:
    try:
        return f"{float(value):+,.0f}"
    except (TypeError, ValueError):
        return "—"


def _format_position_pnl_summary(account: dict[str, Any], *, max_items: int = 5) -> str:
    positions = account.get("positions") or {}
    rows: list[str] = []
    for sym, pos in sorted(positions.items()):
        if not isinstance(pos, dict):
            continue
        unrealized = pos.get("unrealized_pnl")
        quote = pos.get("quote")
        if unrealized is None or quote is None:
            continue
        pct = pos.get("unrealized_pct")
        pct_s = f" / {float(pct):+.2f}%" if isinstance(pct, (int, float)) else ""
        rows.append(f"{sym} {_fmt_signed_money(unrealized)}{pct_s} @ {float(quote):.2f}")
    if not rows:
        return ""
    suffix = "" if len(rows) <= max_items else f"；另 {len(rows) - max_items} 檔略"
    return "  持倉浮盈/浮虧：" + "；".join(rows[:max_items]) + suffix


def _order_notes_by_id(path: Path | str = DEFAULT_ORDERS_PATH) -> dict[str, dict[str, Any]]:
    """orders append log 依 order_id 取最後狀態，回 notes/side/type/price 等摘要。"""
    out: dict[str, dict[str, Any]] = {}
    for rec in _iter_jsonl(path):
        oid = rec.get("order_id")
        if oid:
            out[str(oid)] = rec
    return out


def _parse_advisor_order_notes(notes: Any) -> dict[str, Any]:
    text = str(notes or "")
    if text.startswith("[advisor] "):
        raw = text[len("[advisor] "):]
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {"reason": raw[:500]}
    if text.startswith("[auto-stop]"):
        return {"exit_reason": text, "auto_stop": True}
    return {"reason": text[:500]} if text else {}


def enrich_fill_with_order_context(
    fill: dict[str, Any],
    *,
    orders_path: Path | str = DEFAULT_ORDERS_PATH,
) -> dict[str, Any]:
    """把 fill 對應 order 的 notes/source/type 合併進 fill（淺拷貝）。"""
    oid = fill.get("order_id")
    if not oid:
        return dict(fill)
    order = _order_notes_by_id(orders_path).get(str(oid)) or {}
    if not order:
        return dict(fill)
    enriched = dict(fill)
    notes = order.get("notes")
    parsed = _parse_advisor_order_notes(notes)
    if parsed.get("reason"):
        enriched["trade_reason"] = parsed.get("reason")
    if parsed.get("exit_reason"):
        enriched["exit_reason"] = parsed.get("exit_reason")
    if parsed.get("source_pools"):
        enriched["source_pools"] = parsed.get("source_pools")
    if parsed.get("stop_loss") is not None:
        enriched["planned_stop_loss"] = parsed.get("stop_loss")
    if notes:
        enriched["order_notes"] = notes
    enriched["order_type"] = order.get("type")
    enriched["order_price"] = order.get("price")
    return enriched


def _truncate_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_REASON_PHRASES: dict[str, str] = {
    "forced_sell_out_of_pool_loser": "標的已退出觀察池且仍處於虧損，先主動降倉，避免弱勢部位繼續拖累帳戶。",
    "risk_off": "風險狀態轉弱，先降低部位曝險，保留現金等待更清楚的訊號。",
    "take_profit_hit": "已觸及預定停利條件，先把獲利落袋，避免回吐。",
    "winner_protection": "部位已有獲利但動能轉弱，先保護勝利果實，避免盈利單變回虧損單。",
    "signal_broken": "原本的進場訊號已被破壞，先出場等待重新站回條件。",
    "time_stop": "持有時間已到但走勢沒有按預期推進，依時間停損規則退出。",
    "stop_loss": "停損條件觸發，依風控紀律出場，避免虧損擴大。",
}


def _format_price_for_reason(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _humanize_trade_reason(value: Any, *, side: str) -> str:
    """把 advisor/auto-stop 機器原因翻成交易員口吻；未知原因保留原文。"""
    text = str(value or "").strip()
    if not text:
        return ""

    # auto-trail：`[auto-stop] trail raised from 430.00`，order.price 會另外顯示在成交價，
    # 這裡只說清楚它是「上移後的保護停損」被觸發，不把它當成一般主動賣出。
    m = re.search(r"^\[auto-stop\]\s+trail raised from\s+([0-9.]+)", text)
    if m:
        old_stop = _format_price_for_reason(m.group(1))
        return f"移動停損觸發；這筆原本的保護停損曾從 {old_stop} 上調，用來鎖住已累積的獲利並控制回撤。"

    m = re.search(r"^\[auto-stop\]\s+buy_synced\s+sl=([0-9.]+)", text)
    if m:
        stop = _format_price_for_reason(m.group(1))
        return f"保護性停損觸發；這是買進後同步掛上的停損單，觸發價 {stop}，依原定風控出場。"

    m = re.search(r"^\[auto-stop\]\s+sell_resync\s+sl=([0-9.]+)", text)
    if m:
        stop = _format_price_for_reason(m.group(1))
        return f"保護性停損觸發；先前減倉後停損單已重新對齊剩餘持倉，觸發價 {stop}，依風控出場。"

    if text.startswith("[auto-stop]"):
        raw = text[len("[auto-stop]"):].strip()
        if raw:
            return f"自動停損單觸發；依既有保護單出場。（原始標記：{_truncate_text(raw, 80)}）"
        return "自動停損單觸發；依既有保護單出場。"

    normalized = text.strip().lower()
    if normalized in _REASON_PHRASES:
        return _REASON_PHRASES[normalized]

    # LLM reason 有時會把分類字串寫在自然語言裡；只在明確命中時補一句人話，不覆蓋原說明。
    matched: list[str] = []
    for key, phrase in _REASON_PHRASES.items():
        if key in normalized:
            matched.append(phrase)
    if matched:
        base = matched[0]
        if text == normalized or len(text) <= len(next((k for k in _REASON_PHRASES if k in normalized), "")) + 8:
            return base
        return f"{base}（advisor 補充：{_truncate_text(text, 100)}）"

    return _truncate_text(text)


def fmt_realtime_fill(fill: dict[str, Any], account_after: dict[str, Any]) -> str:
    """即時成交通知：成交 + 本筆 SELL PnL + 持倉浮盈/浮虧摘要。"""
    side = (fill.get("side") or "").upper()
    side_zh = "買進" if side == "BUY" else "賣出"
    symbol = fill.get("symbol", "?")
    qty = fill.get("qty", "?")
    price = fill.get("price")
    price_s = f"{float(price):.2f}" if price is not None else "—"
    fee = float(fill.get("fee") or 0)
    tax = float(fill.get("tax") or 0)
    cost_text = f"手續費 {fee:.0f}"
    if tax > 0:
        cost_text += f"、稅 {tax:.0f}"

    account = account_after.get("account", account_after) or {}
    cash = account.get("cash")
    cash_s = f"{float(cash):,.0f}" if cash is not None else "—"
    realized = account.get("total_realized_pnl")
    realized_s = f"{float(realized):+,.0f}" if realized is not None else "—"

    head = f"⚡ 成交 {symbol} {side_zh} {qty} 股 @ {price_s}（{cost_text}）"
    lines = [head]
    trade_reason = fill.get("exit_reason") if side == "SELL" else fill.get("trade_reason")
    if not trade_reason and side == "SELL":
        trade_reason = fill.get("trade_reason")
    if trade_reason:
        label = "出場原因" if side == "SELL" else "買進理由"
        lines.append(f"  {label}：{_humanize_trade_reason(trade_reason, side=side)}")
    pools = fill.get("source_pools") or []
    if pools:
        lines.append(f"  觸發來源：{', '.join(str(p) for p in pools)}")
    planned_stop = fill.get("planned_stop_loss")
    if planned_stop is not None:
        try:
            lines.append(f"  計畫停損：{float(planned_stop):.2f}")
        except (TypeError, ValueError):
            lines.append(f"  計畫停損：{planned_stop}")
    if side == "SELL":
        realized_this = fill.get("realized_pnl")
        entry_avg = fill.get("entry_avg_cost")
        entry_time = fill.get("entry_time")
        holding_period = fill.get("holding_period") or _format_holding_seconds(fill.get("holding_seconds"))
        if entry_avg is not None or entry_time or holding_period != "—":
            entry_avg_s = f"{float(entry_avg):.2f}" if entry_avg is not None else "—"
            lines.append(f"  開單均價：{entry_avg_s}　入場：{entry_time or '—'}　持有：{holding_period}")
        if realized_this is not None:
            try:
                label = "盈利" if float(realized_this) >= 0 else "虧損"
            except (TypeError, ValueError):
                label = "盈虧"
            lines.append(f"  本筆已實現：{_fmt_signed_money(realized_this)}（{label}）")
        else:
            lines.append("  本筆已實現：—（缺少 avg_cost/replay，請看盤後日報核對）")
    pos_summary = _format_position_pnl_summary(account)
    if pos_summary:
        lines.append(pos_summary)
    lines.append(f"  累計已實現 {realized_s}　剩餘現金 {cash_s}")
    return "\n".join(lines)


def fmt_daily_report(
    date: str,
    fills_today: list[dict[str, Any]],
    positions: dict[str, dict[str, Any]],
    account: dict[str, Any],
    today_pnl: float,
    *,
    pending_orders: Optional[list[dict[str, Any]]] = None,
    advisor_stats: Optional[dict[str, Any]] = None,
) -> str:
    """每日 17:00 盤後交易彙總：成交、持倉、損益、pending、advisor 決策。"""
    pending_orders = pending_orders or []
    advisor_stats = advisor_stats or {}
    lines = [f"📅 sim 每日交易彙總 {date}"]

    lines.append("\n一、今日成交")
    if not fills_today:
        lines.append("- 今日無成交。")
    else:
        buy_count = sum(1 for f in fills_today if (f.get("side") or "").upper() == "BUY")
        sell_count = sum(1 for f in fills_today if (f.get("side") or "").upper() == "SELL")
        lines.append(f"- 今日成交 {len(fills_today)} 筆（買 {buy_count} / 賣 {sell_count}）")
        for f in fills_today[:20]:
            t = str(f.get("filled_at") or "")
            hhmm = t[11:16] if len(t) >= 16 else "--:--"
            sym = f.get("symbol", "?")
            side = (f.get("side") or "").upper()
            side_zh = "買進" if side == "BUY" else "賣出"
            qty = f.get("qty", "?")
            price = f.get("price")
            price_s = f"{float(price):.2f}" if price is not None else "—"
            lines.append(f"  - {hhmm} {side_zh} {sym} {qty} 股 @ {price_s}")
        if len(fills_today) > 20:
            lines.append(f"  - ...（其餘 {len(fills_today) - 20} 筆略）")

    lines.append("\n二、目前持倉")
    if not positions:
        lines.append("- 目前無持倉。")
    else:
        lines.append(f"- 持倉 {len(positions)} 檔")
        for sym, pos in list(positions.items())[:20]:
            qty = pos.get("qty", 0)
            avg_cost = float(pos.get("avg_cost") or 0)
            lines.append(f"  - {sym} {qty} 股，成本 {avg_cost:.2f}")
    cash = float(account.get("cash") or 0)
    equity = account.get("equity")
    equity_s = f"{float(equity):,.0f}" if equity is not None else "—"
    lines.append(f"- 現金 {cash:,.0f}　總權益 {equity_s}")

    lines.append("\n三、已實現損益")
    total_realized = float(account.get("total_realized_pnl") or 0)
    fees = sum(float(f.get("fee") or 0) for f in fills_today)
    taxes = sum(float(f.get("tax") or 0) for f in fills_today)
    lines.append(f"- 今日已實現損益：{today_pnl:+,.0f}")
    lines.append(f"- 累計已實現損益：{total_realized:+,.0f}")
    lines.append(f"- 今日手續費 / 稅：{fees:,.0f} / {taxes:,.0f}")

    lines.append("\n四、未成交掛單")
    if not pending_orders:
        lines.append("- 目前無 PENDING 掛單。")
    else:
        lines.append(f"- PENDING {len(pending_orders)} 筆")
        for o in pending_orders[:20]:
            side = o.get("side", "?")
            typ = o.get("type", "?")
            price = o.get("price")
            price_s = f"{float(price):.2f}" if price is not None else "—"
            lines.append(f"  - {o.get('symbol', '?')} {side} {typ} {o.get('qty', '?')} @ {price_s}")
        if len(pending_orders) > 20:
            lines.append(f"  - ...（其餘 {len(pending_orders) - 20} 筆略）")

    lines.append("\n五、advisor 決策狀況")
    if advisor_stats:
        lines.append(f"- decision_round={advisor_stats.get('decision_round', 0)}")
        lines.append(f"- dispatch_round={advisor_stats.get('dispatch_round', 0)}")
        lines.append(f"- risk_reject={advisor_stats.get('risk_reject', 0)}")
        latest = advisor_stats.get("latest_summary")
        if latest:
            lines.append(f"- 最新摘要：{latest}")
    else:
        lines.append("- 無 advisor audit 資料。")

    return "\n".join(lines)


def fmt_weekly_report(
    week_range: str,
    fills_week: list[dict[str, Any]],
    week_realized_pnl: float,
    week_win_rate: dict[str, Any],
    lifetime_stats: dict[str, Any],
) -> str:
    """每週五盤後週報（5 區塊）。"""
    lines = [f"📊 sim 模擬盤週報 {week_range}"]

    # 1. 週成交筆數
    lines.append(f"  本週成交 {len(fills_week)} 筆")

    # 2. 週已實現
    lines.append(f"  本週已實現損益：{week_realized_pnl:+,.2f}")

    # 3. 週勝率
    rate = week_win_rate.get("rate")
    if rate is None:
        lines.append(f"  本週勝率：N/A（{week_win_rate.get('total', 0)} 筆已實現）")
    else:
        lines.append(
            f"  本週勝率：{rate * 100:.1f}%"
            f"（{week_win_rate.get('win', 0)} / {week_win_rate.get('total', 0)} 筆）"
        )

    # 4. lifetime 累計
    lt_realized = float(lifetime_stats.get("total_realized_pnl") or 0)
    lt_winrate = lifetime_stats.get("win_rate")
    lt_winrate_s = f"{lt_winrate * 100:.1f}%" if lt_winrate is not None else "N/A"
    lines.append(
        f"  累計已實現：{lt_realized:+,.2f}　累計勝率：{lt_winrate_s}"
    )

    # 5. equity 變化（週起始 vs 週結束）
    # 由 caller 在 lifetime_stats 中提供 equity_start / equity_end
    eq_start = lifetime_stats.get("equity_start")
    eq_end = lifetime_stats.get("equity_end")
    if eq_start is not None and eq_end is not None:
        delta = float(eq_end) - float(eq_start)
        delta_pct = (delta / float(eq_start) * 100) if float(eq_start) else 0.0
        lines.append(
            f"  週權益：{float(eq_start):,.0f} → {float(eq_end):,.0f}"
            f"（{delta:+,.0f}, {delta_pct:+.2f}%）"
        )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────
# CLI entry（M5 區塊 E）
# ─────────────────────────────────────────────────────
#
# python3 -m sim.reports daily              → 今日日報，推 W 個人 DM
# python3 -m sim.reports daily --date YYYY-MM-DD → 重產任意日期（dry-run / 補推）
# python3 -m sim.reports daily --no-send    → 只 print 不推
# python3 -m sim.reports weekly             → 本週週報，推 DM
# python3 -m sim.reports weekly --no-send

def _read_initial_cash() -> float:
    """從 sim_config.yaml 讀 initial_cash；讀失敗回預設 1M。"""
    try:
        import yaml
        with open(DEFAULT_SIM_CONFIG, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return float(cfg.get("initial_cash", 1_000_000))
    except Exception as exc:
        logger.warning("讀 sim_config 失敗，用預設 initial_cash=1M: %s", exc)
        return 1_000_000.0


def _build_account_dict(
    positions: dict[str, dict[str, Any]],
    cash: float,
    total_realized: float,
) -> dict[str, Any]:
    """模擬 engine.snapshot()['account'] 格式給 fmt_daily_report 用。

    沒有 quotes 時 equity 退用 cost_basis（對齊 `Account.equity(None)` 邏輯）。
    """
    cost_value = sum(
        p.get("qty", 0) * float(p.get("avg_cost") or 0)
        for p in positions.values()
    )
    equity = cash + cost_value
    return {
        "cash": cash,
        "total_realized_pnl": total_realized,
        "equity": equity,
    }


def _format_holding_seconds(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}天{hours}小時"
    if hours > 0:
        return f"{hours}小時{minutes}分"
    return f"{minutes}分"


def _per_fill_exit_context(path: Path | str) -> dict[str, dict[str, Any]]:
    """跑 fills 全歷史，回 SELL fill_id 對應的出場上下文。

    多次加倉/部分平倉無法對應單一買單，因此使用「賣出當下持倉加權均價」與
    「該批仍持有部位最早入場時間」作為開單價格/持有時間口徑。
    """
    positions: dict[str, dict[str, Any]] = {}
    out: dict[str, dict[str, Any]] = {}
    for fill in iter_fills(path):
        symbol = fill.get("symbol")
        side = (fill.get("side") or "").upper()
        if not symbol or side not in ("BUY", "SELL"):
            continue
        try:
            price = float(fill.get("price", 0))
            qty = int(fill.get("qty", 0))
            fee = float(fill.get("fee", 0) or 0)
            tax = float(fill.get("tax", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        ts = _parse_fill_time(fill)
        gross = price * qty
        if side == "BUY":
            existing = positions.get(symbol)
            if existing is None:
                positions[symbol] = {
                    "qty": qty,
                    "avg_cost": (gross + fee) / qty,
                    "entry_time": ts,
                }
            else:
                old_total_cost = existing["qty"] * existing["avg_cost"]
                new_total_cost = old_total_cost + gross + fee
                new_qty = existing["qty"] + qty
                existing["qty"] = new_qty
                existing["avg_cost"] = new_total_cost / new_qty
                if existing.get("entry_time") is None or (ts is not None and ts < existing["entry_time"]):
                    existing["entry_time"] = ts
        else:
            existing = positions.get(symbol)
            if existing is None or existing["qty"] < qty:
                continue
            net_proceeds = gross - fee - tax
            cost_of_sold = qty * existing["avg_cost"]
            realized = net_proceeds - cost_of_sold
            entry_time = existing.get("entry_time")
            holding_seconds = None
            if ts is not None and entry_time is not None:
                holding_seconds = int((ts - entry_time).total_seconds())
            fid = fill.get("fill_id")
            if fid:
                out[fid] = {
                    "realized_pnl": realized,
                    "entry_avg_cost": existing["avg_cost"],
                    "entry_time": entry_time.isoformat(timespec="seconds") if entry_time else None,
                    "holding_seconds": holding_seconds,
                    "holding_period": _format_holding_seconds(holding_seconds),
                }
            existing["qty"] -= qty
            if existing["qty"] == 0:
                del positions[symbol]
    return out


def _per_fill_realized(path: Path | str) -> dict[str, float]:
    """跑 fills 全歷史，回 {fill_id: realized_pnl}（僅 SELL 有 entry）。

    用途：win_rate 需要 fills 含 `realized_pnl` 欄位，但 jsonl 沒寫；CLI 內必須
    先 enrich。replay 邏輯與 `_sum_realized_from_fills` 完全相同（fee 攤入 avg_cost、
    SELL 用既有 avg_cost、avg_cost 部分平倉不變、全平倉刪 position）。

    M5 接受少量重複（< 30 行）換取既有 _sum_realized_from_fills 簽名不動。
    """
    positions: dict[str, dict[str, Any]] = {}
    out: dict[str, float] = {}
    for fill in iter_fills(path):
        symbol = fill.get("symbol")
        side = (fill.get("side") or "").upper()
        if not symbol or side not in ("BUY", "SELL"):
            continue
        try:
            price = float(fill.get("price", 0))
            qty = int(fill.get("qty", 0))
            fee = float(fill.get("fee", 0) or 0)
            tax = float(fill.get("tax", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        gross = price * qty
        if side == "BUY":
            existing = positions.get(symbol)
            if existing is None:
                positions[symbol] = {"qty": qty, "avg_cost": (gross + fee) / qty}
            else:
                old_total_cost = existing["qty"] * existing["avg_cost"]
                new_total_cost = old_total_cost + gross + fee
                new_qty = existing["qty"] + qty
                existing["qty"] = new_qty
                existing["avg_cost"] = new_total_cost / new_qty
        else:
            existing = positions.get(symbol)
            if existing is None or existing["qty"] < qty:
                continue
            net_proceeds = gross - fee - tax
            cost_of_sold = qty * existing["avg_cost"]
            realized = net_proceeds - cost_of_sold
            fid = fill.get("fill_id")
            if fid:
                out[fid] = realized
            existing["qty"] -= qty
            if existing["qty"] == 0:
                del positions[symbol]
    return out


def _enrich_fills_with_realized(
    fills: list[dict[str, Any]],
    realized_map: dict[str, float],
) -> list[dict[str, Any]]:
    """把 realized_map 對應的 realized_pnl 注入 fill dict（淺拷貝不改原物件）。"""
    out = []
    for f in fills:
        fid = f.get("fill_id")
        if fid and fid in realized_map:
            f2 = dict(f)
            f2["realized_pnl"] = realized_map[fid]
            out.append(f2)
        else:
            out.append(f)
    return out


def _iter_jsonl(path: Path | str) -> Iterator[dict[str, Any]]:
    """通用 jsonl streaming 讀（給 equity_curve / orders / audit 用；行為與 iter_fills 一致）。"""
    return iter_fills(path)


def _pending_orders(path: Path | str = DEFAULT_ORDERS_PATH) -> list[dict[str, Any]]:
    """orders.jsonl 是 append log；以 order_id 最後狀態統計仍為 PENDING 的掛單。"""
    last_by_id: dict[str, dict[str, Any]] = {}
    for rec in _iter_jsonl(path):
        oid = rec.get("order_id")
        if oid:
            last_by_id[oid] = rec
    return [o for o in last_by_id.values() if o.get("status") == "PENDING"]


def _parse_dt_or_none(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _advisor_stats_for_day(
    day_start: datetime,
    day_end: datetime,
    path: Path | str = DEFAULT_ADVISOR_AUDIT_PATH,
) -> dict[str, Any]:
    """彙總單日 advisor audit：decision / dispatch / risk_reject 與最新 summary。"""
    counts: dict[str, int] = {}
    latest_summary: Optional[str] = None
    for rec in _iter_jsonl(path):
        t = _parse_dt_or_none(rec.get("time"))
        if t is None or not (day_start <= t < day_end):
            continue
        kind = rec.get("type", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "decision_round" and rec.get("summary"):
            latest_summary = str(rec.get("summary"))
    return {
        "decision_round": counts.get("decision_round", 0),
        "dispatch_round": counts.get("dispatch_round", 0),
        "risk_reject": counts.get("risk_reject", 0),
        "latest_summary": latest_summary,
    }


def _send_dm(title: str, content: str) -> bool:
    """推 W 個人 DM；FEISHU_USER_OPEN_ID 未設則 log warning return False。

    走 utils.lark_notifier.LarkNotifier（CLI 是獨立 process，沒有 feishu_bot 的
    lark_oapi client；webhook-based 推送是這條 CLI 唯一可用通道）。
    """
    import os
    open_id = os.getenv("FEISHU_USER_OPEN_ID")
    if not open_id:
        logger.warning("FEISHU_USER_OPEN_ID 未設定，跳過推送")
        return False
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier(str(DEFAULT_LARK_CONFIG))
        return bool(notifier.send_message(
            receive_id=open_id,
            title=title,
            content=content,
            receive_id_type="open_id",
        ))
    except Exception:
        logger.exception("推送 DM 失敗")
        return False


def _cmd_daily(args) -> int:
    """daily 子指令。"""
    if not getattr(args, "force", False) and not getattr(args, "no_send", False):
        from sim.calendar import is_trading_day
        if not is_trading_day():
            print("[sim.reports daily] 今日非交易日（週末或台股假日）— skip（用 --force 繞過）")
            return 0
    fills_path = DEFAULT_FILLS_PATH
    try:
        day = datetime.now() if not args.date else datetime.fromisoformat(args.date)
    except ValueError:
        print(f"❌ --date 格式錯誤: {args.date}（需 YYYY-MM-DD）")
        return 2

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    fills_today = iter_fills_in_period(fills_path, start, end)
    positions_data = replay_positions_from_fills(fills_path)
    positions = positions_data.get("positions", {})
    total_realized = positions_data.get("total_realized_pnl", 0.0)
    cash = _read_initial_cash() + positions_data.get("cash_delta", 0.0)
    today_pnl = today_realized_pnl(fills_path, now=day)
    account = _build_account_dict(positions, cash, total_realized)
    pending_orders = _pending_orders()
    advisor_stats = _advisor_stats_for_day(start, end)

    date_s = start.strftime("%Y-%m-%d")
    report_text = fmt_daily_report(
        date_s,
        fills_today,
        positions,
        account,
        today_pnl,
        pending_orders=pending_orders,
        advisor_stats=advisor_stats,
    )
    title = f"📅 sim 每日交易彙總 {date_s}"

    if args.no_send:
        print(title)
        print()
        print(report_text)
        return 0

    if _send_dm(title, report_text):
        print(f"✅ 日報已推送 ({date_s})")
        return 0
    print(f"❌ 日報推送失敗 ({date_s})")
    return 1


def _cmd_weekly(args) -> int:
    """weekly 子指令：本週一 0:00 ~ 下週一 0:00（local naive，weekday: Mon=0）。"""
    if not getattr(args, "force", False) and not getattr(args, "no_send", False):
        from sim.calendar import is_trading_day
        if not is_trading_day():
            print("[sim.reports weekly] 今日非交易日（週末或台股假日）— skip（用 --force 繞過）")
            return 0
    fills_path = DEFAULT_FILLS_PATH
    equity_path = DEFAULT_EQUITY_CURVE_PATH

    now = datetime.now()
    today_0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_0 - timedelta(days=today_0.weekday())  # 本週一
    end = start + timedelta(days=7)

    fills_week = iter_fills_in_period(fills_path, start, end)
    realized_map = _per_fill_realized(fills_path)
    fills_week_enriched = _enrich_fills_with_realized(fills_week, realized_map)
    week_realized = sum(
        realized_map.get(f.get("fill_id"), 0.0)
        for f in fills_week
        if f.get("fill_id") in realized_map
    )
    week_winrate = win_rate(fills_week_enriched)

    # lifetime stats
    positions_data = replay_positions_from_fills(fills_path)
    lt_realized = positions_data.get("total_realized_pnl", 0.0)
    all_enriched = _enrich_fills_with_realized(list(iter_fills(fills_path)), realized_map)
    lt_winrate = win_rate(all_enriched).get("rate")

    # 週首 / 週尾 equity（從 equity_curve.jsonl 抓本週區間第一筆 / 最後一筆）
    equity_start = equity_end = None
    for record in _iter_jsonl(equity_path):
        ts_raw = record.get("time")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            continue
        if start <= ts < end:
            if equity_start is None:
                equity_start = record.get("equity")
            equity_end = record.get("equity")

    lifetime_stats = {
        "total_realized_pnl": lt_realized,
        "win_rate": lt_winrate,
        "equity_start": equity_start,
        "equity_end": equity_end,
    }

    week_range = f"{start.strftime('%Y-%m-%d')} ~ {(end - timedelta(days=1)).strftime('%Y-%m-%d')}"
    report_text = fmt_weekly_report(
        week_range, fills_week_enriched, week_realized, week_winrate, lifetime_stats
    )
    title = f"📊 sim 模擬盤週報 {week_range}"

    if args.no_send:
        print(title)
        print()
        print(report_text)
        return 0

    if _send_dm(title, report_text):
        print(f"✅ 週報已推送 ({week_range})")
        return 0
    print(f"❌ 週報推送失敗 ({week_range})")
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    """sim.reports CLI 入口。回 exit code（0=成功 / 1=推送失敗 / 2=參數錯誤）。"""
    import argparse
    # CLI 模式必須先載入 .env（feishu_bot 走 start_feishu_bot.sh 已載；CLI 從
    # launchd / 手動執行則需自己載，否則 FEISHU_USER_OPEN_ID 等取不到）
    try:
        from dotenv import load_dotenv
        load_dotenv(AUTOMATION_DIR / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(
        prog="python3 -m sim.reports",
        description="sim 模擬盤日報 / 週報 CLI（推 W 個人 DM）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_daily = sub.add_parser("daily", help="今日日報（或指定日期）")
    p_daily.add_argument("--date", help="指定日期 YYYY-MM-DD（預設今天）")
    p_daily.add_argument("--no-send", action="store_true", help="只 print 不推送")
    p_daily.add_argument("--force", action="store_true", help="繞過休市日檢查（假日仍推）")
    p_daily.set_defaults(func=_cmd_daily)

    p_weekly = sub.add_parser("weekly", help="本週週報")
    p_weekly.add_argument("--no-send", action="store_true", help="只 print 不推送")
    p_weekly.add_argument("--force", action="store_true", help="繞過休市日檢查（假日仍推）")
    p_weekly.set_defaults(func=_cmd_weekly)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    return args.func(args)


if __name__ == "__main__":
    import sys
    sys.exit(main())
