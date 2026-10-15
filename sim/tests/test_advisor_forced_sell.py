"""sim/tests/test_advisor_forced_sell.py — P0-3 退池浮虧持倉強制 sell sweep

驗證 _build_forced_sells_for_stale_losers：
1. 持倉檔在 short_term / medium_term / long_term / cross_signals 任一池內 → 不 inject
2. 持倉檔退池 + 浮虧 → inject sell（MKT、qty=full、reason=forced_sell_out_of_pool_loser）
3. 持倉檔退池 + 浮盈（winner）→ 不 inject（交由 LLM 自決 hold or sell）
4. 持倉檔退池 + LLM 本輪已 sell → 不重複 inject
5. engine=None / positions 空 → 回 ([], [])
6. feed.get_quotes 例外 → skip 該檔但不 raise
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor._types import Decision
from sim.advisor.runner import _build_forced_sells_for_stale_losers
from sim.engine import MatchEngine
from sim.models import Position


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class FakeFeed:
    def __init__(self, fixed=None, raise_for=None):
        self.fixed = fixed or {}
        self.raise_for = raise_for or set()

    def get_quotes(self, symbols):
        for s in symbols:
            if s in self.raise_for:
                raise RuntimeError(f"feed boom on {s}")
        return {s: self.fixed.get(s, 100.0) for s in symbols}


def _make_engine(tmp_dir: Path, positions: dict[str, Position]) -> MatchEngine:
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    for sym, pos in positions.items():
        e.account.positions[sym] = pos
    return e


def _wl(tmp_dir: Path, *, short=None, medium=None, long=None, cross=None) -> Path:
    p = tmp_dir / "wl.json"
    data = {
        "formal_pools": {
            "short_term": [{"symbol": s} for s in (short or [])],
            "medium_term": [{"symbol": s} for s in (medium or [])],
            "long_term": [{"symbol": s} for s in (long or [])],
        },
        "auxiliary_signals": {"intraday": []},
        "cross_signals": {s: {"pools": ["short_term", "long_term"]} for s in (cross or [])},
    }
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ─── case 1 ───

def test_in_pool_not_forced(tmp_dir):
    """持倉在 short_term 池內 → 不 inject sell（即便浮虧）"""
    e = _make_engine(tmp_dir, {"2330.TW": Position(symbol="2330.TW", qty=1000, avg_cost=900.0)})
    wl = _wl(tmp_dir, short=["2330.TW"])
    feed = FakeFeed({"2330.TW": 850.0})  # 浮虧 -5.5%
    forced, log = _build_forced_sells_for_stale_losers(e, feed, wl, [], {})
    assert forced == []
    assert log == []


# ─── case 2 ───

def test_out_of_pool_loser_forced(tmp_dir):
    """持倉退池 + 浮虧 → inject MKT sell full qty，reason 帶 forced_sell_out_of_pool_loser"""
    e = _make_engine(tmp_dir, {"2345.TW": Position(symbol="2345.TW", qty=75, avg_cost=2505.0)})
    wl = _wl(tmp_dir, short=["1101.TW"], long=["2330.TW"])  # 2345 不在任一池
    feed = FakeFeed({"2345.TW": 2400.0})  # 浮虧
    forced, log = _build_forced_sells_for_stale_losers(e, feed, wl, [], {})
    assert len(forced) == 1
    d = forced[0]
    assert d.action == "sell"
    assert d.symbol == "2345.TW"
    assert d.qty == 75
    assert d.type == "MKT"
    assert d.price is None
    assert d.reason == "forced_sell_out_of_pool_loser"
    assert len(log) == 1
    assert log[0]["symbol"] == "2345.TW"
    assert log[0]["avg_cost"] == 2505.0
    assert log[0]["current_quote"] == 2400.0


# ─── case 3 ───

def test_out_of_pool_winner_not_forced(tmp_dir):
    """持倉退池但浮盈（winner）→ 不 inject，交給 LLM 自決"""
    e = _make_engine(tmp_dir, {"2408.TW": Position(symbol="2408.TW", qty=200, avg_cost=283.5)})
    wl = _wl(tmp_dir, short=["1101.TW"])
    feed = FakeFeed({"2408.TW": 312.0})  # 浮盈 +10%
    forced, log = _build_forced_sells_for_stale_losers(e, feed, wl, [], {})
    assert forced == []


# ─── case 4 ───

def test_already_sell_not_duplicated(tmp_dir):
    """LLM 本輪已對該檔 sell → 不重複 inject"""
    e = _make_engine(tmp_dir, {"2303.TW": Position(symbol="2303.TW", qty=1000, avg_cost=104.6)})
    wl = _wl(tmp_dir, short=["1101.TW"])
    feed = FakeFeed({"2303.TW": 99.0})  # 浮虧
    llm_decisions = [Decision(action="sell", symbol="2303.TW", qty=1000, type="MKT")]
    forced, log = _build_forced_sells_for_stale_losers(e, feed, wl, llm_decisions, {})
    assert forced == []


# ─── case 5 ───

def test_no_engine(tmp_dir):
    wl = _wl(tmp_dir, short=["1101.TW"])
    feed = FakeFeed({})
    assert _build_forced_sells_for_stale_losers(None, feed, wl, [], {}) == ([], [])


def test_empty_positions(tmp_dir):
    e = _make_engine(tmp_dir, {})
    wl = _wl(tmp_dir, short=["1101.TW"])
    feed = FakeFeed({})
    assert _build_forced_sells_for_stale_losers(e, feed, wl, [], {}) == ([], [])


# ─── case 6 ───

def test_feed_failure_skips_symbol(tmp_dir):
    """feed.get_quotes 例外某檔 → 略過該檔不 raise；其他檔仍走流程"""
    e = _make_engine(tmp_dir, {
        "BOOM.TW": Position(symbol="BOOM.TW", qty=100, avg_cost=500.0),
        "2345.TW": Position(symbol="2345.TW", qty=75, avg_cost=2505.0),
    })
    wl = _wl(tmp_dir, short=[])
    feed = FakeFeed({"2345.TW": 2400.0}, raise_for={"BOOM.TW"})
    forced, _ = _build_forced_sells_for_stale_losers(e, feed, wl, [], {})
    syms = [d.symbol for d in forced]
    assert "2345.TW" in syms
    assert "BOOM.TW" not in syms


# ─── case 7：cross_signals 內也算池內 ───

def test_in_cross_signals_not_forced(tmp_dir):
    e = _make_engine(tmp_dir, {"3034.TW": Position(symbol="3034.TW", qty=100, avg_cost=500.0)})
    wl = _wl(tmp_dir, short=[], cross=["3034.TW"])
    feed = FakeFeed({"3034.TW": 470.0})  # 浮虧
    forced, _ = _build_forced_sells_for_stale_losers(e, feed, wl, [], {})
    assert forced == []


# ─── case 8：quote_cache 命中時不再呼叫 feed ───

def test_uses_quote_cache(tmp_dir):
    e = _make_engine(tmp_dir, {"2345.TW": Position(symbol="2345.TW", qty=75, avg_cost=2505.0)})
    wl = _wl(tmp_dir, short=[])
    feed = FakeFeed({"2345.TW": 9999.0})  # 若被呼叫會浮盈
    cache = {"2345.TW": 2400.0}  # 用 cache 就是浮虧
    forced, _ = _build_forced_sells_for_stale_losers(e, feed, wl, [], cache)
    assert len(forced) == 1
    assert forced[0].symbol == "2345.TW"
