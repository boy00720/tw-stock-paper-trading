"""automation/sim/advisor/daily_brief.py — 純聚合 markdown 日報（M6 階段二 區塊 J）

設計取向：
- 純資料 aggregation；不依賴 LLM / Any Router / cc-switch
- 不會 panic、不燒 token、產出單一 markdown 檔
- 使用者可在任意 LLM（Claude Code / Cursor / Web GUI）讀取後自行評估

聚合區塊（依序）：
1. Header — 日期、since 起算
2. 四件套 — metrics.compute_four_pillars（含 0050 baseline）
3. 帳戶快照 — equity_curve 最後一筆 + 持倉
4. 今日 fills — symbol/side/qty/price 表格
5. Equity 曲線變化 — today / 7d / 30d 高低
6. Advisor 今日決策摘要 — advisor_audit decision_round
7. 風險觀察 — 熔斷、blocked、error 計數

對外兩個函數：
- render_daily_brief(*, since=None, brief_date=None, ...) -> str
- write_daily_brief(*, output_path=None, **kwargs) -> Path

since=None 取近 30 個自然日；brief_date=None 取 today。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sim.advisor.auto_stop import AUTO_STOP_NOTE_PREFIX
from sim.advisor.watch_only import evaluate_watch_only_action, load_watch_only

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_FILLS_PATH = _BASE_DIR / "logs" / "sim" / "fills.jsonl"
DEFAULT_EQUITY_CURVE_PATH = _BASE_DIR / "logs" / "sim" / "equity_curve.jsonl"
DEFAULT_ADVISOR_AUDIT_PATH = _BASE_DIR / "logs" / "sim" / "advisor_audit.jsonl"
DEFAULT_ORDERS_PATH = _BASE_DIR / "logs" / "sim" / "orders.jsonl"
DEFAULT_WATCH_ONLY_PATH = _BASE_DIR / "config" / "sim_watch_only.yaml"
DEFAULT_OUTPUT_DIR = _BASE_DIR / "logs" / "sim" / "daily_brief"


# ─────────────────────────────────────────────────────
# 區塊 1：Header
# ─────────────────────────────────────────────────────


def _render_header(brief_date: date, since: Optional[date]) -> str:
    since_label = since.isoformat() if since else "全期"
    return (
        f"# 模擬交易日報 {brief_date.isoformat()}\n\n"
        f"- 報告日期：{brief_date.isoformat()}\n"
        f"- 起算範圍：{since_label}\n"
        f"- 產出時間：{datetime.now().isoformat(timespec='seconds')}\n"
    )


# ─────────────────────────────────────────────────────
# 區塊 2：四件套
# ─────────────────────────────────────────────────────


def _render_four_pillars(four: dict[str, Any]) -> str:
    wr = four.get("win_rate", {})
    pl = four.get("profit_loss", {})
    bl = four.get("baseline", {})
    bl_pct = bl.get("return_pct")
    bl_str = f"{bl_pct * 100:+.2f}%" if isinstance(bl_pct, (int, float)) else "—"

    rate = wr.get("rate") or 0.0
    ratio = pl.get("ratio", 0.0)
    ratio_str = "∞" if ratio == float("inf") else f"{ratio:.2f}"

    return (
        "## 四件套指標\n\n"
        f"- **勝率**：{rate * 100:.1f}%（{wr.get('win', 0)}/{wr.get('total', 0)}）\n"
        f"- **累積已實現 PnL**：{four.get('cum_realized_pnl', 0.0):+,.0f}\n"
        f"- **最大回撤**：{four.get('max_drawdown_pct', 0.0) * 100:.2f}%\n"
        f"- **盈虧比**：{ratio_str}（avg_win={pl.get('avg_win', 0.0):,.0f}, avg_loss={pl.get('avg_loss', 0.0):,.0f}, wins={pl.get('wins', 0)}, losses={pl.get('losses', 0)}）\n"
        f"- **0050.TW baseline**：{bl_str}\n"
    )


def _render_strategy_segments() -> str:
    """M6 績效分段提醒：四件套為累積口徑，策略切點前後不可混為一條績效線。

    純靜態註記（不改任何資料）；驗收時以最後一個策略切點重新起算 20 交易日窗口。
    """
    return (
        "## 策略切點（M6 績效分段提醒）\n\n"
        "> 上方四件套為**累積**口徑；下列切點前後策略已改變，20 交易日驗收請以"
        "**最後一個策略切點**重新起算，勿把切點前後混成同一條「策略未變」績效線。\n\n"
        "- **2026-05-14**：cooldown gate 上線（同檔砍完回補 / 連續加倉防火牆）— 策略變更\n"
        "- **2026-05-29**：no-new-positions 防守 gate + prompt 轉保守觀察 — 策略性轉防守（驗收重切點）\n"
        "- **2026-06-04**：auto-trail 四捨五入修正 — 控制面修正（非策略，單獨標記）\n"
        "- **2026-06-05**：綜合績效口徑 + probe 漸進放寬 scaffolding（預設 defensive、未啟用）\n"
    )


# ─────────────────────────────────────────────────────
# 區塊 3：帳戶快照
# ─────────────────────────────────────────────────────


def _last_equity_snapshot(equity_curve_path: Path) -> Optional[dict[str, Any]]:
    """讀 equity_curve.jsonl 最後一筆。"""
    try:
        from sim.advisor.metrics import _iter_jsonl  # 重用
    except ImportError:
        return None
    last = None
    for record in _iter_jsonl(equity_curve_path):
        last = record
    return last


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(__import__("json").loads(line))
                except Exception:
                    continue
    except Exception:
        logger.exception("[daily_brief] 讀 jsonl 失敗: %s", path)
    return rows


def _build_stop_status(
    positions: dict[str, Any],
    orders_path: Path,
    fills_path: Path,
    watch_only_path: Path,
) -> dict[str, Any]:
    open_positions = positions.get("positions", {}) if isinstance(positions, dict) else {}
    orders = _load_jsonl(orders_path)
    fills = _load_jsonl(fills_path)
    watch_only = load_watch_only(watch_only_path)
    fills_by_order_id = {row.get("order_id"): row for row in fills if row.get("order_id")}

    quotes: dict[str, float] = {}
    if watch_only:
        try:
            from sim.feed import QuoteFeed
            feed = QuoteFeed()
            quotes = feed.get_quotes(list(watch_only.keys())) or {}
        except Exception:
            logger.exception("[daily_brief] watch-only quotes 讀取失敗")
            quotes = {}

    pending_auto: dict[str, dict[str, Any]] = {}
    last_auto_state: dict[str, dict[str, Any]] = {}
    for row in orders:
        oid = row.get("order_id")
        if not oid:
            continue
        if not str(row.get("notes") or "").startswith(AUTO_STOP_NOTE_PREFIX):
            continue
        last_auto_state[oid] = row
        if row.get("status") == "PENDING":
            pending_auto[row.get("symbol") or ""] = row

    protected = 0
    missing: list[dict[str, Any]] = []
    for sym, info in sorted(open_positions.items()):
        if not isinstance(info, dict) or not info.get("qty"):
            continue
        auto = pending_auto.get(sym)
        if auto:
            protected += 1
        else:
            note = watch_only.get(sym) or {}
            avg_cost = float(info.get("avg_cost", 0.0) or 0.0)
            action = None
            if note:
                action = evaluate_watch_only_action(
                    sym,
                    avg_cost=avg_cost,
                    quote=quotes.get(sym),
                    config=note,
                )
            missing.append({
                "symbol": sym,
                "qty": info.get("qty", 0),
                "avg_cost": avg_cost,
                "reason": note.get("reason"),
                "review_rule": note.get("review_rule"),
                "action": action,
            })

    recent_events: list[dict[str, Any]] = []
    for oid, row in last_auto_state.items():
        status = row.get("status")
        if status not in {"FILLED", "CANCELLED", "REJECTED"}:
            continue
        fill = fills_by_order_id.get(oid)
        recent_events.append({
            "symbol": row.get("symbol"),
            "status": status,
            "qty": row.get("qty"),
            "stop_price": row.get("price"),
            "event_time": (fill or {}).get("filled_at") or row.get("created_at"),
            "fill_price": (fill or {}).get("price"),
            "order_id": oid,
        })
    recent_events.sort(key=lambda x: x.get("event_time") or "")

    return {
        "protected": protected,
        "missing": missing,
        "recent_events": recent_events[-3:],
        "pending_auto": pending_auto,
        "positions_total": sum(1 for _, info in open_positions.items() if isinstance(info, dict) and info.get("qty")),
    }


def _render_stop_status(stop_status: dict[str, Any]) -> str:
    protected = int(stop_status.get("protected", 0) or 0)
    missing = list(stop_status.get("missing") or [])
    recent_events = list(stop_status.get("recent_events") or [])
    total = int(stop_status.get("positions_total", 0) or 0)

    lines = [
        "### Stop 覆蓋\n",
        f"- protected / total：{protected}/{total}",
        f"- missing coverage：{len(missing)}",
        f"- recent auto-stop events：{len(recent_events)}",
    ]

    if missing:
        lines.append("\n#### 缺保護持倉")
        for row in missing:
            lines.append(
                f"- {row['symbol']}：{row['qty']} 股 @ avg_cost {row['avg_cost']:.2f}"
            )
            if row.get("reason"):
                lines.append(f"  - 觀察原因：{row['reason']}")
            if row.get("review_rule"):
                lines.append(f"  - 轉補條件：{row['review_rule']}")
            action = row.get("action") or {}
            if action.get("action_needed"):
                lines.append(f"  - **ACTION NEEDED**：{action.get('message')}")
            elif action.get("message"):
                lines.append(f"  - action_check：{action.get('message')}")
            if action.get("quote") is not None:
                lines.append(f"  - quote：{action.get('quote'):.2f}")
            if action.get("computed_stop") is not None:
                lines.append(f"  - computed_stop：{action.get('computed_stop'):.2f}")

    if recent_events:
        lines.append("\n#### 最近 auto-stop 事件")
        for ev in recent_events:
            fill_part = f" / fill={ev['fill_price']:.2f}" if isinstance(ev.get('fill_price'), (int, float)) else ""
            lines.append(
                f"- {ev.get('event_time', '—')} {ev.get('symbol', '—')} {ev.get('status', '—')} qty={ev.get('qty', 0)} stop={ev.get('stop_price', 0.0):.2f}{fill_part}"
            )

    return "\n".join(lines) + "\n"


def _parse_snapshot_date(equity_snap: Optional[dict[str, Any]]) -> Optional[date]:
    if not equity_snap:
        return None
    raw = equity_snap.get("time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except (TypeError, ValueError):
        return None


def _build_replay_account_snapshot(
    positions: dict[str, Any],
    *,
    fallback_equity_snap: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """用 fills replay 結果重建帳戶快照，避免 equity_curve 停更時日報顯示舊值。

    replay_positions_from_fills 回傳 cash_delta / total_realized_pnl / positions；若缺 cash_delta，
    則無法可靠重建 cash，回 None 讓 caller 明確標示不可用。
    """
    if not isinstance(positions, dict):
        return None
    if "cash_delta" not in positions:
        return None
    initial_cash = 1_000_000.0
    try:
        if fallback_equity_snap and fallback_equity_snap.get("cash") is not None and fallback_equity_snap.get("position_value") is not None:
            # 若歷史 equity snap 可用，initial_cash 仍以 sim 預設 1M 為主；保留此分支只避免型別錯。
            initial_cash = 1_000_000.0
        cash = initial_cash + float(positions.get("cash_delta", 0.0) or 0.0)
        open_positions = positions.get("positions", {}) or {}
        position_value = 0.0
        for info in open_positions.values():
            if isinstance(info, dict):
                position_value += float(info.get("qty", 0) or 0) * float(info.get("avg_cost", 0.0) or 0.0)
        return {
            "time": "fills_replay",
            "cash": cash,
            "position_value": position_value,
            "equity": cash + position_value,
            "total_realized_pnl": float(positions.get("total_realized_pnl", 0.0) or 0.0),
            "source": "fills_replay",
        }
    except Exception:
        logger.exception("[daily_brief] replay account snapshot 建立失敗")
        return None


def _format_money(value: Any, *, signed: bool = False) -> str:
    parsed = _safe_float(value)
    if parsed is None:
        return "—"
    return f"{parsed:+,.0f}" if signed else f"{parsed:,.0f}"


def _format_price(value: Any) -> str:
    parsed = _safe_float(value)
    if parsed is None:
        return "—"
    return f"{parsed:,.2f}"


def _format_pct(value: Any, *, signed: bool = True) -> str:
    parsed = _safe_float(value)
    if parsed is None:
        return "—"
    prefix = "+" if signed else ""
    return f"{parsed:{prefix}.2f}%"


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_quote_map(symbols: list[str] | set[str] | tuple[str, ...]) -> dict[str, float]:
    unique = sorted({str(sym) for sym in symbols if sym})
    if not unique:
        return {}
    try:
        from sim.feed import QuoteFeed
        return QuoteFeed().get_quotes(unique) or {}
    except Exception:
        logger.exception("[daily_brief] quote 讀取失敗: %s", unique)
        return {}


def _open_position_rows(positions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    open_positions = positions.get("positions", {}) if isinstance(positions, dict) else {}
    rows: dict[str, dict[str, Any]] = {}
    for sym, info in sorted(open_positions.items()):
        if not isinstance(info, dict):
            continue
        try:
            qty = int(info.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        avg_cost = _safe_float(info.get("avg_cost"))
        if qty <= 0 or avg_cost is None or avg_cost <= 0:
            continue
        rows[str(sym)] = {
            "qty": qty,
            "avg_cost": avg_cost,
            "cost_value": qty * avg_cost,
        }
    return rows


def _latest_pending_auto_stops(orders_path: Path) -> dict[str, dict[str, Any]]:
    """回傳每檔最新一張 PENDING auto-stop；僅供展示保護線，不改訂單。"""
    last_by_id: dict[str, dict[str, Any]] = {}
    for row in _load_jsonl(orders_path):
        oid = row.get("order_id")
        if oid:
            last_by_id[str(oid)] = row

    pending_by_symbol: dict[str, dict[str, Any]] = {}
    for row in last_by_id.values():
        if row.get("status") != "PENDING":
            continue
        if row.get("side") != "SELL" or row.get("type") != "STOP":
            continue
        if not str(row.get("notes") or "").startswith(AUTO_STOP_NOTE_PREFIX):
            continue
        sym = str(row.get("symbol") or "")
        if not sym:
            continue
        prev = pending_by_symbol.get(sym)
        if prev is None or str(row.get("created_at") or "") >= str(prev.get("created_at") or ""):
            pending_by_symbol[sym] = row
    return pending_by_symbol


def _build_position_performance(
    positions: dict[str, Any],
    *,
    orders_path: Path,
    cash: Any,
) -> dict[str, Any]:
    """建立 sim 持倉 MTM / 未實現 / stop-floor 展示資料。

    報價失敗時不硬湊未實現 PnL；MTM equity 若僅部分有 quote，缺 quote 的持倉退回成本基礎並明確註記。
    stop-floor 是用目前有效 auto-stop 粗估保護線，未扣未來平倉費稅。
    """
    open_positions = _open_position_rows(positions)
    quotes = _fetch_quote_map(set(open_positions))
    pending_auto = _latest_pending_auto_stops(orders_path)
    cash_value = _safe_float(cash)

    rows: list[dict[str, Any]] = []
    cost_total = 0.0
    market_value_with_cost_fallback = 0.0
    quoted_cost_total = 0.0
    quoted_market_total = 0.0
    stop_floor_value = 0.0
    stop_floor_complete = bool(open_positions)
    missing_quote_symbols: list[str] = []
    missing_stop_symbols: list[str] = []

    for sym, pos in open_positions.items():
        qty = int(pos["qty"])
        avg_cost = float(pos["avg_cost"])
        cost_value = float(pos["cost_value"])
        cost_total += cost_value

        quote = _safe_float(quotes.get(sym))
        market_value = None
        unrealized_pnl = None
        unrealized_pct = None
        if quote is not None and quote > 0:
            market_value = qty * quote
            unrealized_pnl = market_value - cost_value
            unrealized_pct = unrealized_pnl / cost_value * 100 if cost_value else None
            quoted_cost_total += cost_value
            quoted_market_total += market_value
            market_value_with_cost_fallback += market_value
        else:
            missing_quote_symbols.append(sym)
            market_value_with_cost_fallback += cost_value

        auto_stop = pending_auto.get(sym)
        stop_price = _safe_float((auto_stop or {}).get("price"))
        try:
            stop_qty = int((auto_stop or {}).get("qty", 0) or 0)
        except (TypeError, ValueError):
            stop_qty = 0
        stop_covers_full_qty = bool(stop_price is not None and stop_price > 0 and stop_qty >= qty)
        stop_floor_for_position = qty * stop_price if stop_covers_full_qty else None
        if stop_floor_for_position is not None:
            stop_floor_value += stop_floor_for_position
        else:
            stop_floor_complete = False
            missing_stop_symbols.append(sym)

        rows.append({
            "symbol": sym,
            "qty": qty,
            "avg_cost": avg_cost,
            "cost_value": cost_value,
            "quote": quote,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pct": unrealized_pct,
            "stop_price": stop_price,
            "stop_qty": stop_qty,
            "stop_floor_value": stop_floor_for_position,
        })

    quoted_unrealized = quoted_market_total - quoted_cost_total if quoted_cost_total > 0 else None
    mtm_equity = None
    if cash_value is not None and rows and quoted_cost_total > 0:
        mtm_equity = cash_value + market_value_with_cost_fallback
    stop_floor_equity = None
    if cash_value is not None and rows and stop_floor_complete:
        stop_floor_equity = cash_value + stop_floor_value

    return {
        "rows": rows,
        "cost_total": cost_total,
        "quoted_unrealized_pnl": quoted_unrealized,
        "quoted_count": sum(1 for row in rows if row.get("quote") is not None),
        "missing_quote_symbols": missing_quote_symbols,
        "mtm_equity": mtm_equity,
        "stop_floor_equity": stop_floor_equity,
        "missing_stop_symbols": missing_stop_symbols,
    }


def _render_position_performance(perf: dict[str, Any], *, realized_pnl: Any) -> str:
    rows = list(perf.get("rows") or [])
    lines = ["### 綜合績效口徑", ""]
    lines.append(f"- realized_pnl（fills replay）：{_format_money(realized_pnl, signed=True)}")
    if not rows:
        lines.append("- 目前無 sim 持倉，無未實現 PnL。")
        return "\n".join(lines) + "\n"

    unrealized = perf.get("quoted_unrealized_pnl")
    if unrealized is None:
        lines.append("- unrealized_pnl：—（持倉 quote 不可用）")
    else:
        note = ""
        missing = perf.get("missing_quote_symbols") or []
        if missing:
            note = f"；缺 quote：{', '.join(missing)}（MTM equity 對缺 quote 持倉退回成本基礎）"
        lines.append(f"- unrealized_pnl（有 quote 持倉）：{_format_money(unrealized, signed=True)}{note}")

    mtm_equity = perf.get("mtm_equity")
    if mtm_equity is None:
        lines.append("- mark_to_market_equity：—（quote 不足）")
    else:
        lines.append(f"- mark_to_market_equity：{_format_money(mtm_equity)}")

    stop_floor_equity = perf.get("stop_floor_equity")
    if stop_floor_equity is None:
        missing_stop = perf.get("missing_stop_symbols") or []
        reason = f"缺完整 auto-stop 覆蓋：{', '.join(missing_stop)}" if missing_stop else "stop 資料不足"
        lines.append(f"- stop_floor_equity：—（{reason}）")
    else:
        lines.append(f"- stop_floor_equity：{_format_money(stop_floor_equity)}（按有效 auto-stop 粗估，未扣未來平倉費稅）")

    lines.extend([
        "",
        "#### 持倉 MTM 明細",
        "| 標的 | 股數 | 成本 | quote | 未實現 | stop |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in rows:
        quote = _format_price(row.get("quote")) if row.get("quote") is not None else "—"
        unrealized_s = "—"
        if row.get("unrealized_pnl") is not None:
            unrealized_s = f"{_format_money(row.get('unrealized_pnl'), signed=True)} / {_format_pct(row.get('unrealized_pct'))}"
        stop_s = _format_price(row.get("stop_price")) if row.get("stop_price") is not None else "—"
        lines.append(
            f"| {row.get('symbol', '—')} | {row.get('qty', 0):,} | "
            f"{row.get('avg_cost', 0.0):,.2f} | {quote} | {unrealized_s} | {stop_s} |"
        )
    return "\n".join(lines) + "\n"


def _manual_live_watch_rows(
    watch_only_path: Path,
    *,
    positions: Optional[dict[str, Any]] = None,
    orders_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    watch_only = load_watch_only(watch_only_path)
    manual = {
        str(sym): cfg for sym, cfg in watch_only.items()
        if isinstance(cfg, dict) and (cfg.get("source") == "manual_live_position" or cfg.get("entry_price") is not None)
    }
    if not manual:
        return []

    sim_positions = _open_position_rows(positions or {})
    pending_auto = _latest_pending_auto_stops(orders_path) if orders_path else {}
    quote_symbols = set(manual) | set(sim_positions)
    quotes = _fetch_quote_map(quote_symbols)
    rows: list[dict[str, Any]] = []
    for sym, cfg in sorted(manual.items()):
        entry_price = _safe_float(cfg.get("entry_price"))
        quote = _safe_float(quotes.get(sym))
        position_state = str(cfg.get("position_state") or "holding").strip().lower()
        exit_price = _safe_float(cfg.get("exit_price"))
        return_pct = None
        if (
            position_state == "flat"
            and exit_price is not None and exit_price > 0
            and entry_price is not None and entry_price > 0
        ):
            # 空手：實盤報酬欄改顯示本輪結算（entry→exit）；對空手而言 quote 浮盈已無意義
            return_pct = (exit_price - entry_price) / entry_price * 100
        elif entry_price is not None and entry_price > 0 and quote is not None and quote > 0:
            return_pct = (quote - entry_price) / entry_price * 100
        action = evaluate_watch_only_action(
            sym,
            avg_cost=entry_price or 0.0,
            quote=quote,
            config=cfg,
        )
        sim_pos = sim_positions.get(sym) or {}
        sim_qty = int(sim_pos.get("qty", 0) or 0) if sim_pos else 0
        sim_avg = _safe_float(sim_pos.get("avg_cost")) if sim_pos else None
        sim_return_pct = None
        if sim_avg is not None and sim_avg > 0 and quote is not None and quote > 0:
            sim_return_pct = (quote - sim_avg) / sim_avg * 100
        auto_stop = pending_auto.get(sym) or {}
        rows.append({
            "symbol": sym,
            "entry_price": entry_price,
            "quote": quote,
            "return_pct": return_pct,
            "position_state": position_state,
            "exit_price": exit_price,
            "reason": cfg.get("reason"),
            "review_rule": cfg.get("review_rule"),
            "action": action,
            "sim_qty": sim_qty,
            "sim_avg_cost": sim_avg,
            "sim_return_pct": sim_return_pct,
            "sim_stop": _safe_float(auto_stop.get("price")),
        })
    return rows


def _render_manual_live_watch(
    watch_only_path: Path,
    *,
    positions: Optional[dict[str, Any]] = None,
    orders_path: Optional[Path] = None,
) -> str:
    rows = _manual_live_watch_rows(watch_only_path, positions=positions, orders_path=orders_path)
    if not rows:
        return ""

    has_flat = any(row.get("position_state") == "flat" for row in rows)
    basis_header = "實盤基準" if has_flat else "實盤成本"
    lines = [
        "### 00631L / 人工實盤 vs sim 策略觀察",
        "",
        "- 實盤軌與 sim 軌為**兩本帳**：實盤人工基準不覆蓋 sim avg_cost；sim orders/fills 也不代表真實帳戶操作。",
        f"| 標的 | {basis_header} | quote | 實盤報酬 | 實盤狀態 | sim 狀態 | sim 報酬 | sim stop |",
        "|---|---:|---:|---:|---|---|---:|---:|",
    ]
    for row in rows:
        action = row.get("action") or {}
        status = action.get("message") or action.get("suggestion") or "—"
        if action.get("action_needed"):
            status = f"ACTION NEEDED：{status}"
        is_flat = row.get("position_state") == "flat"
        sym_label = row.get("symbol", "—")
        if is_flat:
            sym_label = f"{sym_label}（已平倉）"
        sim_qty = int(row.get("sim_qty") or 0)
        sim_avg = row.get("sim_avg_cost")
        if sim_qty > 0 and sim_avg is not None:
            sim_status = f"已持有 {sim_qty:,} 股 @ {sim_avg:,.2f}"
        else:
            sim_status = "sim 尚未進場"
        if is_flat:
            basis = "—"
            if row.get("entry_price") is not None and row.get("exit_price") is not None:
                basis = f"{row.get('entry_price'):,.2f}→{row.get('exit_price'):,.2f}"
            elif row.get("entry_price") is not None:
                basis = f"上輪進場 {row.get('entry_price'):,.2f}"
        else:
            basis = f"{row.get('entry_price', 0.0):,.2f}"
        lines.append(
            f"| {sym_label} | "
            f"{basis} | "
            f"{_format_price(row.get('quote'))} | "
            f"{_format_pct(row.get('return_pct'))} | {status} | "
            f"{sim_status} | {_format_pct(row.get('sim_return_pct'))} | {_format_price(row.get('sim_stop'))} |"
        )
        if row.get("reason"):
            lines.append(f"  - 實盤 reason：{row['reason']}")
        if row.get("review_rule"):
            lines.append(f"  - 實盤 review：{row['review_rule']}")
        if action.get("status_level"):
            sl = action.get("status_level")
            sl_txt = "已平倉空手（再進場由盤中雷達觀察）" if sl == "flat" else sl
            lines.append(f"  - 實盤狀態燈：{sl_txt}")
    return "\n".join(lines) + "\n"


def _render_account_snapshot(
    equity_snap: Optional[dict[str, Any]],
    positions: dict[str, Any],
    *,
    orders_path: Path,
    fills_path: Path,
    watch_only_path: Path,
    brief_date: Optional[date] = None,
) -> str:
    snap_date = _parse_snapshot_date(equity_snap)
    is_fresh_equity = bool(equity_snap and brief_date and snap_date == brief_date)
    display_snap = equity_snap if is_fresh_equity else _build_replay_account_snapshot(
        positions, fallback_equity_snap=equity_snap,
    )
    if not display_snap:
        if not equity_snap:
            return "## 帳戶快照\n\n_無 equity_curve 資料，且 fills replay 快照不可用_\n"
        return (
            "## 帳戶快照\n\n"
            f"_equity_curve 快照非報告日（快照時間：{equity_snap.get('time', '—')}），且 fills replay 快照不可用，故不顯示舊帳戶數值。_\n"
        )

    open_positions = positions.get("positions", {}) if isinstance(positions, dict) else {}
    pos_lines = []
    for sym, info in sorted(open_positions.items()):
        if not isinstance(info, dict):
            continue
        qty = info.get("qty", 0)
        avg_cost = info.get("avg_cost", 0.0)
        if qty:
            pos_lines.append(f"  - {sym}：{qty} 股 @ avg_cost {avg_cost:.2f}")

    pos_block = "\n".join(pos_lines) if pos_lines else "  - _無持倉_"
    stop_status = _build_stop_status(positions, orders_path, fills_path, watch_only_path)
    stop_block = _render_stop_status(stop_status)
    performance_block = _render_position_performance(
        _build_position_performance(
            positions,
            orders_path=orders_path,
            cash=display_snap.get("cash"),
        ),
        realized_pnl=display_snap.get("total_realized_pnl"),
    )
    manual_live_watch_block = _render_manual_live_watch(
        watch_only_path,
        positions=positions,
        orders_path=orders_path,
    )

    source = display_snap.get("source") or "equity_curve"
    stale_note = ""
    if not is_fresh_equity and equity_snap:
        stale_note = (
            f"- ⚠️ equity_curve 最後快照非報告日（最後時間：{equity_snap.get('time', '—')}），"
            "以下帳戶數值改用 fills replay 重建。\n"
        )
    valuation_note = ""
    if source == "fills_replay":
        valuation_note = "- ⚠️ position_value / equity 為持倉成本估算，非即時市值；未納入最新報價浮盈虧。\n"

    return (
        "## 帳戶快照\n\n"
        f"{stale_note}"
        f"{valuation_note}"
        f"- source：{source}\n"
        f"- equity：{display_snap.get('equity', 0.0):,.0f}\n"
        f"- cash：{display_snap.get('cash', 0.0):,.0f}\n"
        f"- position_value：{display_snap.get('position_value', 0.0):,.0f}\n"
        f"- total_realized_pnl：{display_snap.get('total_realized_pnl', 0.0):+,.0f}\n"
        f"- 快照時間：{display_snap.get('time', '—')}\n\n"
        f"### 持倉\n{pos_block}\n\n"
        f"{stop_block}\n"
        f"{performance_block}\n"
        f"{manual_live_watch_block}"
    )


# ─────────────────────────────────────────────────────
# 區塊 4：今日 fills
# ─────────────────────────────────────────────────────


def _render_today_fills(fills_path: Path, brief_date: date) -> str:
    try:
        from sim.advisor.metrics import _iter_jsonl, _parse_iso_or_none
    except ImportError:
        return "## 今日成交\n\n_metrics helpers 不可用_\n"

    today_fills = []
    for fill in _iter_jsonl(fills_path):
        t = _parse_iso_or_none(fill.get("filled_at"))
        if t is None or t.date() != brief_date:
            continue
        today_fills.append(fill)

    if not today_fills:
        return "## 今日成交\n\n_今日無成交_\n"

    lines = ["## 今日成交\n", "| 時間 | 標的 | 方向 | 數量 | 價格 | 手續費 |", "|---|---|---|---|---|---|"]
    for fill in today_fills:
        t = fill.get("filled_at", "—")
        t_short = t.split("T")[1][:8] if "T" in str(t) else t
        lines.append(
            f"| {t_short} | {fill.get('symbol', '—')} | {fill.get('side', '—')} | "
            f"{fill.get('qty', 0)} | {fill.get('price', 0.0):.2f} | {fill.get('fee', 0.0):.0f} |"
        )
    lines.append(f"\n共 {len(today_fills)} 筆")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────
# 區塊 5：Equity 曲線變化
# ─────────────────────────────────────────────────────


def _render_equity_change(equity_curve_path: Path, brief_date: date) -> str:
    try:
        from sim.advisor.metrics import _iter_jsonl, _parse_iso_or_none
    except ImportError:
        return "## Equity 曲線\n\n_helpers 不可用_\n"

    today_eq: list[float] = []
    last_7_eq: list[float] = []
    last_30_eq: list[float] = []
    seven_ago = brief_date - timedelta(days=7)
    thirty_ago = brief_date - timedelta(days=30)

    for record in _iter_jsonl(equity_curve_path):
        t = _parse_iso_or_none(record.get("time"))
        if t is None:
            continue
        try:
            eq = float(record.get("equity"))
        except (TypeError, ValueError):
            continue
        d = t.date()
        if d == brief_date:
            today_eq.append(eq)
        if d >= seven_ago:
            last_7_eq.append(eq)
        if d >= thirty_ago:
            last_30_eq.append(eq)

    def _stats(series: list[float]) -> str:
        if not series:
            return "—"
        return f"high={max(series):,.0f} / low={min(series):,.0f} / 末值={series[-1]:,.0f}（n={len(series)}）"

    return (
        "## Equity 曲線變化\n\n"
        f"- 今日：{_stats(today_eq)}\n"
        f"- 近 7 日：{_stats(last_7_eq)}\n"
        f"- 近 30 日：{_stats(last_30_eq)}\n"
    )


# ─────────────────────────────────────────────────────
# 區塊 6：Advisor 今日決策摘要
# ─────────────────────────────────────────────────────


def _render_advisor_summary(advisor_audit_path: Path, brief_date: date) -> str:
    try:
        from sim.advisor.metrics import _iter_jsonl, _parse_iso_or_none
    except ImportError:
        return "## Advisor 今日決策\n\n_helpers 不可用_\n"

    rounds = []
    for rec in _iter_jsonl(advisor_audit_path):
        if rec.get("type") != "decision_round":
            continue
        t = _parse_iso_or_none(rec.get("time"))
        if t is None or t.date() != brief_date:
            continue
        rounds.append(rec)

    if not rounds:
        return "## Advisor 今日決策\n\n_今日無 decision_round 紀錄_\n"

    lines = ["## Advisor 今日決策\n", f"今日共 {len(rounds)} 輪。\n"]
    # 只展開最後一輪的細節（其餘列表）
    last = rounds[-1]
    lines.append(
        f"### 最末輪 @ {last.get('time', '—')}\n\n"
        f"- status：{last.get('status', '—')}\n"
        f"- model：{last.get('model', '—')}（provider={last.get('provider_id', '—')}）\n"
        f"- decided/passed/submitted/blocked/hold："
        f"{last.get('decided', 0)}/{last.get('passed', 0)}/{last.get('submitted', 0)}/"
        f"{last.get('blocked', 0)}/{last.get('hold', 0)}\n"
        f"- circuit_triggered：{last.get('circuit_triggered_this_round', False)}\n"
    )
    if last.get("summary"):
        lines.append(f"\n**summary**：{last['summary']}\n")
    if last.get("metrics_self_eval"):
        lines.append(f"\n**self_eval**：{last['metrics_self_eval']}\n")
    if last.get("error"):
        lines.append(f"\n**error**：{last['error']}\n")

    decisions = last.get("decisions_log", []) or []
    if decisions:
        lines.append("\n#### decisions_log\n")
        for d in decisions:
            if not isinstance(d, dict):
                continue
            lines.append(
                f"- {d.get('action', '?')} {d.get('symbol', '?')} {d.get('qty', '?')} "
                f"({d.get('type', '?')}) → {d.get('result', '?')}：{d.get('reason', '')}"
            )

    if len(rounds) > 1:
        lines.append("\n#### 其餘輪次\n")
        for r in rounds[:-1]:
            lines.append(
                f"- {r.get('time', '—')}：status={r.get('status', '—')} "
                f"submitted={r.get('submitted', 0)} blocked={r.get('blocked', 0)}"
            )

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────
# 區塊 7：風險觀察
# ─────────────────────────────────────────────────────


def _render_risk_watch(advisor_audit_path: Path, brief_date: date) -> str:
    try:
        from sim.advisor.metrics import _iter_jsonl, _parse_iso_or_none
    except ImportError:
        return "## 風險觀察\n\n_helpers 不可用_\n"

    circuit_count = 0
    blocked_total = 0
    error_count = 0
    failed_status = 0
    for rec in _iter_jsonl(advisor_audit_path):
        if rec.get("type") != "decision_round":
            continue
        t = _parse_iso_or_none(rec.get("time"))
        if t is None or t.date() != brief_date:
            continue
        if rec.get("circuit_triggered_this_round"):
            circuit_count += 1
        blocked_total += int(rec.get("blocked", 0) or 0)
        if rec.get("error"):
            error_count += 1
        if rec.get("status") and rec["status"] != "ok":
            failed_status += 1

    return (
        "## 風險觀察\n\n"
        f"- 日內熔斷觸發：{circuit_count} 次\n"
        f"- blocked 決策累計：{blocked_total} 筆\n"
        f"- 非 ok status 輪次：{failed_status}\n"
        f"- error 輪次：{error_count}\n"
    )


# ─────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────


def render_daily_brief(
    *,
    fills_path: Path | str = DEFAULT_FILLS_PATH,
    equity_curve_path: Path | str = DEFAULT_EQUITY_CURVE_PATH,
    advisor_audit_path: Path | str = DEFAULT_ADVISOR_AUDIT_PATH,
    orders_path: Path | str = DEFAULT_ORDERS_PATH,
    watch_only_path: Path | str = DEFAULT_WATCH_ONLY_PATH,
    since: Optional[date] = None,
    brief_date: Optional[date] = None,
    baseline_symbol: str = "0050.TW",
) -> str:
    """聚合所有區塊，回 markdown 字串。"""
    brief_date = brief_date or date.today()
    if since is None:
        since = brief_date - timedelta(days=30)

    # 區塊 2：四件套
    try:
        from sim.advisor.metrics import compute_four_pillars
        four = compute_four_pillars(
            fills_path, equity_curve_path,
            since=since, baseline_symbol=baseline_symbol,
        )
    except Exception:
        logger.exception("[daily_brief] compute_four_pillars 失敗")
        four = {
            "win_rate": {"total": 0, "win": 0, "rate": 0.0},
            "cum_realized_pnl": 0.0,
            "max_drawdown_pct": 0.0,
            "profit_loss": {"wins": 0, "losses": 0, "avg_win": 0.0, "avg_loss": 0.0, "ratio": 0.0},
            "baseline": {"symbol": baseline_symbol, "return_pct": None},
        }

    # 區塊 3：帳戶快照 + 持倉
    equity_snap = _last_equity_snapshot(Path(equity_curve_path))
    try:
        from sim.reports import replay_positions_from_fills
        positions = replay_positions_from_fills(fills_path)
    except Exception:
        logger.exception("[daily_brief] replay_positions_from_fills 失敗")
        positions = {}

    parts = [
        _render_header(brief_date, since),
        _render_four_pillars(four),
        _render_strategy_segments(),
        _render_account_snapshot(
            equity_snap,
            positions,
            orders_path=Path(orders_path),
            fills_path=Path(fills_path),
            watch_only_path=Path(watch_only_path),
            brief_date=brief_date,
        ),
        _render_today_fills(Path(fills_path), brief_date),
        _render_equity_change(Path(equity_curve_path), brief_date),
        _render_advisor_summary(Path(advisor_audit_path), brief_date),
        _render_risk_watch(Path(advisor_audit_path), brief_date),
    ]
    return "\n".join(parts) + "\n"


def write_daily_brief(
    *,
    output_path: Optional[Path | str] = None,
    brief_date: Optional[date] = None,
    **kwargs: Any,
) -> Path:
    """產出 markdown 寫入檔案、回傳實際路徑。

    output_path=None → DEFAULT_OUTPUT_DIR/brief_YYYY-MM-DD.md
    """
    brief_date = brief_date or date.today()
    if output_path is None:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = DEFAULT_OUTPUT_DIR / f"brief_{brief_date.isoformat()}.md"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    md = render_daily_brief(brief_date=brief_date, **kwargs)
    output_path.write_text(md, encoding="utf-8")
    logger.info("[daily_brief] 寫入 %s（%d bytes）", output_path, len(md))
    return output_path
