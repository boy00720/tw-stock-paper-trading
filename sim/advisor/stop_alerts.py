"""automation/sim/advisor/stop_alerts.py — stop 風控提醒聚合與去重。

提醒策略（低噪音）：
- 推 watch-only 的 ACTION NEEDED
- 推 auto-stop 的 FILLED / REJECTED
- 不推單純 missing coverage
- 用 state json 去重，同一事件不重複推
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sim.advisor.auto_stop import AUTO_STOP_NOTE_PREFIX
from sim.advisor.runner import init_engine_from_fills
from sim.advisor.watch_only import evaluate_watch_only_action, load_watch_only
from sim.feed import QuoteFeed

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_ORDERS_PATH = _BASE_DIR / "logs" / "sim" / "orders.jsonl"
DEFAULT_FILLS_PATH = _BASE_DIR / "logs" / "sim" / "fills.jsonl"
DEFAULT_STATE_PATH = _BASE_DIR / "logs" / "sim" / "stop_alert_state.json"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sent_keys": []}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {"sent_keys": []}
    except Exception:
        return {"sent_keys": []}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _auto_stop_terminal_events(orders_path: Path, fills_path: Path) -> list[dict[str, Any]]:
    orders = _load_jsonl(orders_path)
    fills = _load_jsonl(fills_path)
    fills_by_order_id = {row.get("order_id"): row for row in fills if row.get("order_id")}

    last_state: dict[str, dict[str, Any]] = {}
    for row in orders:
        oid = row.get("order_id")
        if not oid:
            continue
        if not str(row.get("notes") or "").startswith(AUTO_STOP_NOTE_PREFIX):
            continue
        last_state[oid] = row

    events: list[dict[str, Any]] = []
    for oid, row in last_state.items():
        status = row.get("status")
        if status not in {"FILLED", "REJECTED"}:
            continue
        fill = fills_by_order_id.get(oid)
        events.append({
            "key": f"auto_stop:{oid}:{status}",
            "kind": "auto_stop_terminal",
            "order_id": oid,
            "symbol": row.get("symbol"),
            "status": status,
            "qty": row.get("qty"),
            "stop_price": row.get("price"),
            "fill_price": fill.get("price") if fill else None,
            "event_time": (fill or {}).get("filled_at") or row.get("created_at"),
        })
    events.sort(key=lambda x: x.get("event_time") or "")
    return events


def _watch_only_action_events() -> list[dict[str, Any]]:
    engine = init_engine_from_fills()
    watch_only = load_watch_only()
    symbols = [
        s for s, cfg in watch_only.items()
        if isinstance(cfg, dict) and (
            s in engine.account.positions
            or cfg.get("source") == "manual_live_position"
            or cfg.get("entry_price") is not None
        )
    ]
    if not symbols:
        return []

    try:
        quotes = QuoteFeed().get_quotes(symbols) or {}
    except Exception:
        quotes = {}

    events: list[dict[str, Any]] = []
    for sym in sorted(symbols):
        cfg = watch_only.get(sym) or {}
        pos = engine.account.positions.get(sym)
        avg_cost = float(getattr(pos, "avg_cost", 0.0) or 0.0)
        qty = int(getattr(pos, "qty", 0) or 0)
        if (avg_cost <= 0 or qty <= 0) and cfg.get("entry_price") is not None:
            try:
                avg_cost = float(cfg.get("entry_price") or 0.0)
            except (TypeError, ValueError):
                avg_cost = 0.0
            qty = int(cfg.get("qty") or 0)
        action = evaluate_watch_only_action(
            sym,
            avg_cost=avg_cost,
            quote=quotes.get(sym),
            config=cfg,
        )
        if not action.get("action_needed"):
            continue
        rule_type = action.get("rule_type") or "unknown"
        events.append({
            "key": f"watch_only:{sym}:{rule_type}",
            "kind": "watch_only_action",
            "symbol": sym,
            "qty": qty,
            "avg_cost": avg_cost,
            "quote": action.get("quote"),
            "computed_stop": action.get("computed_stop"),
            "message": action.get("message"),
            "event_time": datetime.now().isoformat(timespec="seconds"),
        })
    return events


def collect_stop_alerts(
    *,
    orders_path: Path | str = DEFAULT_ORDERS_PATH,
    fills_path: Path | str = DEFAULT_FILLS_PATH,
    state_path: Path | str = DEFAULT_STATE_PATH,
    include_sent: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """回傳待推送 alerts 與 state；include_sent=True 時不做去重過濾。"""
    state_p = Path(state_path)
    state = _load_state(state_p)
    sent = set(state.get("sent_keys") or [])

    alerts = _watch_only_action_events()
    alerts.extend(_auto_stop_terminal_events(Path(orders_path), Path(fills_path)))
    alerts.sort(key=lambda x: (x.get("kind") or "", x.get("event_time") or ""))

    if not include_sent:
        alerts = [a for a in alerts if a.get("key") not in sent]
    return alerts, state


def format_stop_alerts(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return "目前沒有新的 stop 風控提醒。"

    lines = ["sim stop 風控提醒", ""]
    for a in alerts:
        if a.get("kind") == "watch_only_action":
            lines.append(
                f"- ACTION NEEDED {a.get('symbol')}: {a.get('message')} "
                f"qty={a.get('qty')} avg_cost={a.get('avg_cost'):.2f} "
                f"quote={a.get('quote'):.2f}"
            )
            if isinstance(a.get("computed_stop"), (int, float)):
                lines[-1] += f" computed_stop={a.get('computed_stop'):.2f}"
        elif a.get("kind") == "auto_stop_terminal":
            fill = ""
            if isinstance(a.get("fill_price"), (int, float)):
                fill = f" fill={a.get('fill_price'):.2f}"
            lines.append(
                f"- AUTO-STOP {a.get('status')} {a.get('symbol')}: "
                f"qty={a.get('qty')} stop={a.get('stop_price'):.2f}{fill} "
                f"order_id={a.get('order_id')}"
            )
    return "\n".join(lines)


def mark_alerts_sent(alerts: list[dict[str, Any]], state: dict[str, Any], state_path: Path | str = DEFAULT_STATE_PATH) -> None:
    sent = list(dict.fromkeys(list(state.get("sent_keys") or []) + [a["key"] for a in alerts if a.get("key")]))
    state["sent_keys"] = sent[-500:]
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_state(Path(state_path), state)
