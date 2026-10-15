"""sim/tests/test_advisor_watch_only.py — watch-only 半自動提醒規則。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.watch_only import evaluate_watch_only_action


def test_below_avg_cost_pct_triggers():
    cfg = {
        "action_rule": {
            "type": "below_avg_cost_pct",
            "pct": 0.01,
            "message": "跌破 1%",
        }
    }
    out = evaluate_watch_only_action("2379.TW", avg_cost=100.0, quote=98.9, config=cfg)
    assert out["action_needed"] is True
    assert out["message"] == "跌破 1%"
    assert out["threshold"] == 99.0


def test_below_avg_cost_pct_not_triggered():
    cfg = {"action_rule": {"type": "below_avg_cost_pct", "pct": 0.01}}
    out = evaluate_watch_only_action("2379.TW", avg_cost=100.0, quote=99.5, config=cfg)
    assert out["action_needed"] is False


def test_stop_distance_from_quote_pct_triggers():
    cfg = {
        "action_rule": {
            "type": "stop_distance_from_quote_pct",
            "stop_pct_from_avg_cost": 0.03,
            "min_distance_pct": 0.02,
            "message": "距離足夠",
        }
    }
    out = evaluate_watch_only_action("6446.TW", avg_cost=100.0, quote=100.0, config=cfg)
    assert out["action_needed"] is True
    assert out["computed_stop"] == 97.0
    assert out["distance_pct"] == 0.03


def test_stop_distance_from_quote_pct_not_triggered_when_too_close():
    cfg = {
        "action_rule": {
            "type": "stop_distance_from_quote_pct",
            "stop_pct_from_avg_cost": 0.03,
            "min_distance_pct": 0.02,
        }
    }
    out = evaluate_watch_only_action("6446.TW", avg_cost=100.0, quote=98.0, config=cfg)
    assert out["action_needed"] is False
    assert out["computed_stop"] == 97.0


def test_missing_quote_returns_non_actionable_message():
    cfg = {"action_rule": {"type": "below_avg_cost_pct", "pct": 0.01}}
    out = evaluate_watch_only_action("2379.TW", avg_cost=100.0, quote=None, config=cfg)
    assert out["action_needed"] is False
    assert "缺少有效 quote" in out["message"]


def test_leveraged_etf_watch_flat_settlement():
    """position_state=flat → 回報本輪結算 + 空手狀態，不報跌破成本。"""
    cfg = {
        "position_state": "flat",
        "entry_price": 37.08,
        "exit_price": 39.0,
        "action_rule": {"type": "leveraged_etf_watch"},
    }
    out = evaluate_watch_only_action("00631L.TW", avg_cost=37.08, quote=36.0, config=cfg)
    assert out["status_level"] == "flat"
    assert out["action_needed"] is False
    assert round(out["settle_pct"] * 100, 2) == 5.18
    assert "本輪實盤結算" in out["message"]
    assert "目前空手" in out["message"]


def test_leveraged_etf_watch_flat_settles_without_quote():
    """flat 結算只需 entry+exit；盤後無報價仍要算得出本輪結算。"""
    cfg = {
        "position_state": "flat",
        "entry_price": 37.08,
        "exit_price": 39.0,
        "action_rule": {"type": "leveraged_etf_watch"},
    }
    out = evaluate_watch_only_action("00631L.TW", avg_cost=37.08, quote=None, config=cfg)
    assert out["status_level"] == "flat"
    assert out["return_pct_display"] is not None


def test_leveraged_etf_watch_holding_unchanged():
    """position_state 缺省（=holding）時維持跌破成本的下行風控。"""
    cfg = {
        "entry_price": 37.08,
        "action_rule": {"type": "leveraged_etf_watch", "strong_red_loss_pct": 0.03},
    }
    out = evaluate_watch_only_action("00631L.TW", avg_cost=37.08, quote=35.8, config=cfg)
    assert out["status_level"] == "strong_red"
    assert out["action_needed"] is True
