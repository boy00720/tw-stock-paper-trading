"""sim/tests/test_advisor_stop_alerts.py — stop 風控提醒聚合與去重。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.stop_alerts import (
    collect_stop_alerts,
    format_stop_alerts,
    mark_alerts_sent,
)


def _append_jsonl(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def test_collect_auto_stop_filled_event_and_dedupe(tmp_path):
    orders = tmp_path / "orders.jsonl"
    fills = tmp_path / "fills.jsonl"
    state = tmp_path / "state.json"

    _append_jsonl(orders, {
        "order_id": "ord-stop", "symbol": "2330.TW", "side": "SELL", "type": "STOP",
        "qty": 100, "price": 900.0, "status": "PENDING", "notes": "[auto-stop] test",
        "created_at": "2026-05-11T09:00:00",
    })
    _append_jsonl(orders, {
        "order_id": "ord-stop", "symbol": "2330.TW", "side": "SELL", "type": "STOP",
        "qty": 100, "price": 900.0, "status": "FILLED", "notes": "[auto-stop] test",
        "created_at": "2026-05-11T09:00:00",
    })
    _append_jsonl(fills, {
        "order_id": "ord-stop", "symbol": "2330.TW", "side": "SELL",
        "qty": 100, "price": 895.0, "filled_at": "2026-05-11T09:05:00",
    })

    alerts, st = collect_stop_alerts(orders_path=orders, fills_path=fills, state_path=state)
    # 這裡可能還會收真實 watch-only alerts；只檢查 auto-stop 事件存在
    stop_alerts = [a for a in alerts if a.get("key") == "auto_stop:ord-stop:FILLED"]
    assert len(stop_alerts) == 1
    assert stop_alerts[0]["fill_price"] == 895.0

    mark_alerts_sent(stop_alerts, st, state_path=state)
    alerts2, _ = collect_stop_alerts(orders_path=orders, fills_path=fills, state_path=state)
    assert not [a for a in alerts2 if a.get("key") == "auto_stop:ord-stop:FILLED"]


def test_format_stop_alerts_includes_action_needed():
    text = format_stop_alerts([
        {
            "kind": "watch_only_action",
            "symbol": "2379.TW",
            "message": "現價跌破 avg_cost 1% 以上",
            "qty": 330,
            "avg_cost": 593.60,
            "quote": 584.0,
        },
        {
            "kind": "auto_stop_terminal",
            "symbol": "3034.TW",
            "status": "FILLED",
            "qty": 400,
            "stop_price": 428.87,
            "fill_price": 428.0,
            "order_id": "ord-x",
        },
    ])
    assert "ACTION NEEDED 2379.TW" in text
    assert "AUTO-STOP FILLED 3034.TW" in text
