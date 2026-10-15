"""sim/tests/test_advisor_buy_gate.py — P0-2 低質量 buy 降級 hold

驗證 _gate_low_quality_buys：
- cross_signals 命中 → pass
- short_term score ≥ 85 → pass
- short_term score < 85 且 non-cross → 降級 hold + reason 帶 buy_signal_below_threshold
- 不在任一池 → 降級 hold
- sell / hold 一律不動
- short_term score 缺失 → 視為不通過
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor._types import Decision
from sim.advisor.runner import _gate_low_quality_buys


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _wl(tmp_dir: Path, short_items=None, cross=None) -> Path:
    p = tmp_dir / "wl.json"
    data = {
        "formal_pools": {
            "short_term": list(short_items or []),
            "medium_term": [],
            "long_term": [],
        },
        "auxiliary_signals": {"intraday": []},
        "cross_signals": {s: {"pools": ["short_term", "long_term"]} for s in (cross or [])},
    }
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _buy(sym, score_hint=""):
    return Decision(action="buy", symbol=sym, qty=1000, type="LMT", price=100.0,
                    stop_loss=95.0, reason=f"test buy {score_hint}")


def test_cross_signals_pass(tmp_dir):
    wl = _wl(tmp_dir, short_items=[{"symbol": "3034.TW", "score": 50}], cross=["3034.TW"])
    out, gated = _gate_low_quality_buys([_buy("3034.TW")], wl)
    assert len(out) == 1 and out[0].action == "buy"
    assert gated == []


def test_high_score_short_term_pass(tmp_dir):
    wl = _wl(tmp_dir, short_items=[{"symbol": "2330.TW", "score": 85}])
    out, gated = _gate_low_quality_buys([_buy("2330.TW")], wl)
    assert out[0].action == "buy"
    assert gated == []


def test_low_score_downgraded(tmp_dir):
    """3231 5/7 池內 score=74 → 應被降級"""
    wl = _wl(tmp_dir, short_items=[{"symbol": "3231.TW", "score": 74}])
    out, gated = _gate_low_quality_buys([_buy("3231.TW")], wl)
    assert out[0].action == "hold"
    assert "buy_signal_below_threshold" in out[0].reason
    assert "score=74" in out[0].reason
    assert len(gated) == 1
    assert gated[0]["symbol"] == "3231.TW"
    assert gated[0]["short_term_score"] == 74
    assert gated[0]["threshold"] == 85


def test_not_in_any_pool_downgraded(tmp_dir):
    """完全不在 short_term 池且不在 cross → 降級"""
    wl = _wl(tmp_dir, short_items=[{"symbol": "1101.TW", "score": 90}])
    out, gated = _gate_low_quality_buys([_buy("2603.TW")], wl)
    assert out[0].action == "hold"
    assert gated[0]["short_term_score"] is None


def test_missing_score_downgraded(tmp_dir):
    """score 缺失（None）→ 降級（避免漏網）"""
    wl = _wl(tmp_dir, short_items=[{"symbol": "2330.TW", "score": None}])
    out, gated = _gate_low_quality_buys([_buy("2330.TW")], wl)
    assert out[0].action == "hold"
    assert gated[0]["short_term_score"] is None


def test_score_exactly_85_pass(tmp_dir):
    wl = _wl(tmp_dir, short_items=[{"symbol": "2881.TW", "score": 85}])
    out, gated = _gate_low_quality_buys([_buy("2881.TW")], wl)
    assert out[0].action == "buy"
    assert gated == []


def test_score_80_now_blocked(tmp_dir):
    """score=80 低於新門檻 85 → 降級"""
    wl = _wl(tmp_dir, short_items=[{"symbol": "2881.TW", "score": 80}])
    out, gated = _gate_low_quality_buys([_buy("2881.TW")], wl)
    assert out[0].action == "hold"
    assert len(gated) == 1


def test_sell_and_hold_untouched(tmp_dir):
    wl = _wl(tmp_dir, short_items=[])
    decs = [
        Decision(action="sell", symbol="2330.TW", qty=100, type="MKT"),
        Decision(action="hold", reason="watching"),
    ]
    out, gated = _gate_low_quality_buys(decs, wl)
    assert [d.action for d in out] == ["sell", "hold"]
    assert gated == []


def test_mixed_batch(tmp_dir):
    """混合：cross 過、低分擋、高分過、sell 不動"""
    wl = _wl(tmp_dir,
             short_items=[{"symbol": "3231.TW", "score": 74}, {"symbol": "3034.TW", "score": 92}],
             cross=["8069.TWO"])
    decs = [
        _buy("8069.TWO"),  # cross → pass
        _buy("3231.TW"),   # 低分 → 降
        _buy("3034.TW"),   # 高分 → pass
        Decision(action="sell", symbol="2330.TW", qty=100, type="MKT"),  # 不動
    ]
    out, gated = _gate_low_quality_buys(decs, wl)
    actions = [d.action for d in out]
    assert actions == ["buy", "hold", "buy", "sell"]
    assert len(gated) == 1
    assert gated[0]["symbol"] == "3231.TW"


def test_corrupt_watchlist_passes_through(tmp_dir):
    """壞 watchlist → 不擋（fail-open，避免整輪停擺）"""
    p = tmp_dir / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    decs = [_buy("3231.TW")]
    out, gated = _gate_low_quality_buys(decs, p)
    # load_watchlist_pools 對壞 json 回空池（不 raise），所以 buy 會被當「不在池」→ 降級
    # 這比放行更保守，符合「不確定就 hold」原則
    assert out[0].action == "hold"
