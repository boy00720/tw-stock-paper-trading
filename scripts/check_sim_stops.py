#!/usr/bin/env python3
"""check_sim_stops.py — 檢查模擬盤持倉與 auto-stop 覆蓋情況。

輸出四段：
1. 當前持倉 + 對應 auto-stop
2. 缺保護持倉
3. 最近觸發 / 取消的 auto-stop 紀錄
4. 所有 pending auto-stop 明細

用法：
  cd /Users/w/Cursor/automation
  python3 scripts/check_sim_stops.py
  python3 scripts/check_sim_stops.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sim.advisor.auto_stop import AUTO_STOP_NOTE_PREFIX
from sim.advisor.runner import init_engine_with_pending
from sim.advisor.watch_only import evaluate_watch_only_action, load_watch_only
from sim.feed import QuoteFeed

ORDERS_JSONL = ROOT / "logs" / "sim" / "orders.jsonl"
FILLS_JSONL = ROOT / "logs" / "sim" / "fills.jsonl"


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


def _fmt_num(v: Any, digits: int = 2) -> str:
    if isinstance(v, (int, float)):
        return f"{v:.{digits}f}"
    return "-"


def _is_auto_stop_row(row: dict[str, Any]) -> bool:
    return str(row.get("notes") or "").startswith(AUTO_STOP_NOTE_PREFIX)


def _load_recent_auto_stop_events(limit: int) -> list[dict[str, Any]]:
    orders = _load_jsonl(ORDERS_JSONL)
    fills = _load_jsonl(FILLS_JSONL)
    fills_by_order_id = {row.get("order_id"): row for row in fills if row.get("order_id")}

    last_state: dict[str, dict[str, Any]] = {}
    for row in orders:
        oid = row.get("order_id")
        if not oid or not _is_auto_stop_row(row):
            continue
        last_state[oid] = row

    events: list[dict[str, Any]] = []
    for oid, row in last_state.items():
        status = row.get("status")
        if status not in {"FILLED", "CANCELLED", "REJECTED"}:
            continue
        fill = fills_by_order_id.get(oid)
        events.append(
            {
                "order_id": oid,
                "symbol": row.get("symbol"),
                "status": status,
                "qty": row.get("qty"),
                "stop_price": row.get("price"),
                "created_at": row.get("created_at"),
                "filled_at": fill.get("filled_at") if fill else None,
                "fill_price": fill.get("price") if fill else None,
                "notes": row.get("notes") or "",
            }
        )

    events.sort(key=lambda x: (x.get("filled_at") or x.get("created_at") or ""))
    return events[-limit:]


def main() -> int:
    parser = argparse.ArgumentParser(description="檢查模擬盤 auto-stop 覆蓋與最近事件")
    parser.add_argument("--limit", type=int, default=10, help="最近事件顯示筆數（預設 10）")
    args = parser.parse_args()

    engine = init_engine_with_pending()
    watch_only = load_watch_only()
    feed = QuoteFeed()
    pending_auto = {
        oid: o
        for oid, o in engine.pending_orders.items()
        if (o.notes or "").startswith(AUTO_STOP_NOTE_PREFIX)
    }

    watch_symbols = [s for s in engine.account.positions.keys() if s in watch_only]
    try:
        watch_quotes = feed.get_quotes(watch_symbols) if watch_symbols else {}
    except Exception:
        watch_quotes = {}

    pos_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    for sym, pos in sorted(engine.account.positions.items()):
        auto = next((o for o in pending_auto.values() if o.symbol == sym), None)
        row = {
            "symbol": sym,
            "qty": pos.qty,
            "avg_cost": pos.avg_cost,
            "auto_stop": auto.price if auto else None,
            "auto_stop_qty": auto.qty if auto else None,
            "auto_stop_order_id": auto.order_id if auto else None,
            "coverage": "protected" if auto else "missing",
        }
        pos_rows.append(row)
        if auto is None:
            note = watch_only.get(sym) or {}
            row["watch_only_reason"] = note.get("reason")
            row["watch_only_review_rule"] = note.get("review_rule")
            if note:
                row["watch_only_action"] = evaluate_watch_only_action(
                    sym,
                    avg_cost=float(pos.avg_cost),
                    quote=watch_quotes.get(sym),
                    config=note,
                )
            missing_rows.append(row)

    recent_events = _load_recent_auto_stop_events(max(args.limit, 1))

    print("=== sim stop coverage ===")
    print(f"positions={len(pos_rows)} pending_auto_stops={len(pending_auto)} missing_coverage={len(missing_rows)}")
    print()

    print("=== positions + auto-stop ===")
    if not pos_rows:
        print("(no positions)")
    else:
        print(f"{'symbol':<10}{'qty':>8}{'avg_cost':>12}{'stop':>12}{'stop_qty':>10}  coverage  order_id")
        for row in pos_rows:
            print(
                f"{row['symbol']:<10}{row['qty']:>8}{_fmt_num(row['avg_cost']):>12}"
                f"{_fmt_num(row['auto_stop']):>12}{str(row['auto_stop_qty'] or '-'):>10}"
                f"  {row['coverage']:<9} {row['auto_stop_order_id'] or '-'}"
            )
    print()

    print("=== missing coverage ===")
    if not missing_rows:
        print("(none)")
    else:
        for row in missing_rows:
            line = f"- {row['symbol']} qty={row['qty']} avg_cost={_fmt_num(row['avg_cost'])} auto_stop=MISSING"
            if row.get("watch_only_reason"):
                line += f"\n    reason: {row['watch_only_reason']}"
            if row.get("watch_only_review_rule"):
                line += f"\n    review: {row['watch_only_review_rule']}"
            action = row.get("watch_only_action") or {}
            if action.get("action_needed"):
                line += f"\n    ACTION NEEDED: {action.get('message')}"
            elif action.get("message"):
                line += f"\n    action_check: {action.get('message')}"
            if action.get("quote") is not None:
                line += f"\n    quote: {_fmt_num(action.get('quote'))}"
            if action.get("computed_stop") is not None:
                line += f" / computed_stop: {_fmt_num(action.get('computed_stop'))}"
            print(line)
    print()

    print("=== recent auto-stop events ===")
    if not recent_events:
        print("(none)")
    else:
        for ev in recent_events:
            when = ev.get("filled_at") or ev.get("created_at") or "-"
            extra = f" fill_price={_fmt_num(ev['fill_price'])}" if ev.get("fill_price") is not None else ""
            print(
                f"- {when} {ev['symbol']} {ev['status']} qty={ev['qty']} stop={_fmt_num(ev['stop_price'])}{extra}"
                f" order_id={ev['order_id']}"
            )
    print()

    print("=== pending auto-stop details ===")
    if not pending_auto:
        print("(none)")
    else:
        for oid, o in sorted(pending_auto.items(), key=lambda kv: (kv[1].symbol, kv[1].price or 0)):
            print(
                f"- {o.symbol} qty={o.qty} stop={_fmt_num(o.price)} status={o.status.value}"
                f" order_id={oid} notes={o.notes}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
