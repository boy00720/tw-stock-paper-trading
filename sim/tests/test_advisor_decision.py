"""sim/tests/test_advisor_decision.py — 區塊 C 測試"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor._types import Decision
from sim.advisor.decision import SYSTEM_PROMPT, _parse_decisions, _parse_one


# ─── happy paths ───

def test_parse_buy_full():
    d = _parse_one({
        "action": "buy", "symbol": "2330.TW", "qty": 1000,
        "type": "MKT", "stop_loss": 900,
        "reason": "cross", "source_pools": ["short_term", "cross_signals"],
    })
    assert d.action == "buy"
    assert d.symbol == "2330.TW"
    assert d.qty == 1000
    assert d.type == "MKT"
    assert d.stop_loss == 900.0
    assert d.source_pools == ["short_term", "cross_signals"]


def test_parse_hold_minimal():
    d = _parse_one({"action": "hold", "reason": "noise"})
    assert d.action == "hold"
    assert d.symbol is None
    assert d.qty is None
    assert d.reason == "noise"


def test_parse_sell_lmt():
    d = _parse_one({
        "action": "sell", "symbol": "2330.TW",
        "qty": 500, "type": "LMT", "price": 925,
        "reason": "trim",
    })
    assert d.action == "sell"
    assert d.type == "LMT"
    assert d.price == 925.0


# ─── parse failure recovery ───

def test_parse_unknown_action_raises():
    import pytest
    with pytest.raises(ValueError):
        _parse_one({"action": "yolo"})


def test_parse_decisions_mixed():
    """一筆壞掉但其他 OK 應各自解析正確 / 降級"""
    raw = {
        "decisions": [
            {"action": "buy", "symbol": "2330.TW", "qty": 1000, "stop_loss": "oops"},
            {"action": "hold", "reason": "ok"},
            {"action": "sell", "symbol": "1101.TW", "qty": 500, "type": "MKT"},
        ],
    }
    out = _parse_decisions(raw)
    assert len(out) == 3
    # 第 0 筆降級為 hold
    assert out[0].action == "hold"
    assert "parse_decision[0]_failed" in out[0].reason
    # 第 1 筆正常 hold
    assert out[1].action == "hold"
    assert out[1].reason == "ok"
    # 第 2 筆 sell
    assert out[2].action == "sell"


def test_parse_empty_list():
    out = _parse_decisions({"decisions": []})
    assert len(out) == 1
    assert out[0].action == "hold"
    assert out[0].reason == "empty_decisions_array"


def test_parse_missing_decisions_key():
    out = _parse_decisions({"summary": "x"})
    assert len(out) == 1
    assert out[0].action == "hold"
    assert "missing_decisions_key" in out[0].reason


def test_parse_decisions_not_list():
    out = _parse_decisions({"decisions": "not a list"})
    assert len(out) == 1
    assert out[0].action == "hold"
    assert "decisions_not_list" in out[0].reason


def test_parse_raw_not_dict():
    out = _parse_decisions("garbage string")
    assert len(out) == 1
    assert out[0].action == "hold"
    assert "raw_response_not_dict" in out[0].reason


# ─── P0：buy 必填 stop_loss ───

def test_parse_buy_missing_stop_loss_raises():
    """buy 漏帶 stop_loss → _parse_one raise ValueError，由 _parse_decisions 降級為 hold。"""
    import pytest
    with pytest.raises(ValueError, match="buy_missing_stop_loss"):
        _parse_one({
            "action": "buy", "symbol": "2330.TW", "qty": 1000,
            "type": "MKT", "reason": "reasoning 寫了停損但欄位漏填",
        })


def test_parse_buy_zero_stop_loss_raises():
    """stop_loss <= 0 視同無效，避免 LLM 用 0 假裝有填。"""
    import pytest
    with pytest.raises(ValueError, match="buy_missing_stop_loss"):
        _parse_one({
            "action": "buy", "symbol": "2330.TW", "qty": 1000,
            "type": "MKT", "stop_loss": 0,
        })


def test_parse_decisions_buy_missing_stop_loss_degrades_to_hold():
    """整批解析時，buy 缺 stop_loss 那筆降級為 hold；其他筆不受影響。"""
    raw = {
        "decisions": [
            {"action": "buy", "symbol": "2345.TW", "qty": 75, "type": "MKT",
             "reason": "短線評分高，停損 2379.75"},  # ← 模擬實際 incident：reasoning 寫了停損但欄位漏填
            {"action": "hold", "reason": "其他標的訊號雜訊高"},
            {"action": "buy", "symbol": "2330.TW", "qty": 1000, "type": "MKT",
             "stop_loss": 900.0, "reason": "正常 buy"},
        ],
    }
    out = _parse_decisions(raw)
    assert len(out) == 3
    assert out[0].action == "hold"
    assert "buy_missing_stop_loss" in out[0].reason
    assert out[1].action == "hold"
    assert out[2].action == "buy"
    assert out[2].stop_loss == 900.0


def test_parse_sell_without_stop_loss_ok():
    """sell 不需要 stop_loss。"""
    d = _parse_one({
        "action": "sell", "symbol": "2330.TW", "qty": 500,
        "type": "MKT", "reason": "trim",
    })
    assert d.action == "sell"
    assert d.stop_loss is None


def test_system_prompt_includes_winner_protection_rules():
    """P2：prompt 必須明確要求保護 winner、避免砍勝留虧。"""
    assert "砍勝留虧" in SYSTEM_PROMPT
    assert "已有持倉時，先算浮盈虧" in SYSTEM_PROMPT
    assert "讓 winner 跑，不要手動早賣" in SYSTEM_PROMPT
    assert "階梯式 trailing stop" in SYSTEM_PROMPT
    assert "優先保留 winner、先處理 loser" in SYSTEM_PROMPT
    assert "winner_protection" in SYSTEM_PROMPT
