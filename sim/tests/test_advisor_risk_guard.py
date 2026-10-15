"""sim/tests/test_advisor_risk_guard.py — 區塊 D 測試"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor._types import Decision
from sim.advisor.risk_guard import RiskGuard
from sim.engine import MatchEngine


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _make_engine(tmp_dir):
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    return e


def _make_guard(tmp_dir):
    e = _make_engine(tmp_dir)
    g = RiskGuard(
        e,
        audit_path=tmp_dir / "audit.jsonl",
        state_path=tmp_dir / "state.json",
    )
    return e, g


# ─── hold / sell ───

def test_hold_passes(tmp_dir):
    e, g = _make_guard(tmp_dir)
    r = g.evaluate(Decision("hold"))
    assert r.passed
    assert r.reason == "passed_hold"


def test_sell_passes(tmp_dir):
    e, g = _make_guard(tmp_dir)
    r = g.evaluate(Decision("sell", symbol="2330.TW", qty=500, type="MKT"))
    assert r.passed
    assert r.reason == "passed_sell"


# ─── BUY 第一層：單筆風險 ───

def test_buy_within_risk_limit(tmp_dir):
    e, g = _make_guard(tmp_dir)
    # equity=1M, max_risk=2% = 20K
    # qty=100, ref=920, stop=900 → risk = 20*100 = 2K << 20K
    r = g.evaluate(
        Decision("buy", symbol="2330.TW", qty=100, type="MKT", stop_loss=900),
        current_quote=920,
    )
    assert r.passed, f"reason={r.reason}"


def test_buy_risk_too_high(tmp_dir):
    e, g = _make_guard(tmp_dir)
    # qty=2000, risk = 20×2000 = 40K > 20K
    r = g.evaluate(
        Decision("buy", symbol="2330.TW", qty=2000, type="MKT", stop_loss=900),
        current_quote=920,
    )
    assert not r.passed
    assert "single_risk_too_high" in r.reason


def test_buy_no_stop_uses_5pct(tmp_dir):
    e, g = _make_guard(tmp_dir)
    # 沒 stop_loss → max_risk_no_stop_pct=5%
    # qty=300, ref=920 → risk = 920×300×5% = 13800 < 20K → 過
    r = g.evaluate(
        Decision("buy", symbol="2330.TW", qty=300, type="MKT"),
        current_quote=920,
    )
    # 集中度判斷：300×920 = 276K > 200K → 仍會被集中度擋
    assert not r.passed
    assert "concentration_too_high" in r.reason


def test_buy_no_ref_price_rejects(tmp_dir):
    e, g = _make_guard(tmp_dir)
    r = g.evaluate(Decision("buy", symbol="2330.TW", qty=100, type="MKT"))
    assert not r.passed
    assert "no_ref_price_for_risk_calc" in r.reason


# ─── BUY 第二層：集中度 ───

def test_buy_concentration_too_high(tmp_dir):
    e, g = _make_guard(tmp_dir)
    # 300×920 = 276K > equity×20% = 200K
    r = g.evaluate(
        Decision("buy", symbol="2330.TW", qty=300, type="MKT", stop_loss=900),
        current_quote=920,
    )
    assert not r.passed
    assert "concentration_too_high" in r.reason


def test_buy_concentration_with_existing(tmp_dir):
    """既有持倉 + 新買加總超過集中度上限"""
    from sim.models import Position
    e, g = _make_guard(tmp_dir)
    # 模擬真實買進：position + 扣 cash，equity 維持 1M
    e.account.positions["2330.TW"] = Position(symbol="2330.TW", qty=150, avg_cost=900)
    e.account.cash -= 150 * 900
    # equity ≈ 1M, conc_limit = 200K
    # 既有 cost_basis = 135K + 新買 100×920=92K → 227K > 200K → reject
    r = g.evaluate(
        Decision("buy", symbol="2330.TW", qty=100, type="MKT", stop_loss=900),
        current_quote=920,
    )
    assert not r.passed
    assert "concentration_too_high" in r.reason


# ─── BUY 邊界 ───

def test_buy_zero_qty_rejects(tmp_dir):
    e, g = _make_guard(tmp_dir)
    r = g.evaluate(
        Decision("buy", symbol="2330.TW", qty=0, type="MKT"),
        current_quote=920,
    )
    assert not r.passed
    assert "qty_non_positive" in r.reason


def test_buy_no_symbol_rejects(tmp_dir):
    e, g = _make_guard(tmp_dir)
    r = g.evaluate(
        Decision("buy", qty=100, type="MKT", stop_loss=900),
        current_quote=920,
    )
    assert not r.passed
    assert "buy_without_symbol" in r.reason


# ─── 熔斷 ───

def test_circuit_not_broken_initially(tmp_dir):
    e, g = _make_guard(tmp_dir)
    assert not g.is_circuit_broken_today()


def test_circuit_trigger_then_blocks_buy(tmp_dir):
    e, g = _make_guard(tmp_dir)
    g.trigger_circuit_breaker()
    assert g.is_circuit_broken_today()
    r = g.evaluate(
        Decision("buy", symbol="2330.TW", qty=100, type="MKT", stop_loss=900),
        current_quote=920,
    )
    assert not r.passed
    assert r.reason == "circuit_breaker_active_today"


def test_circuit_blocks_even_hold(tmp_dir):
    e, g = _make_guard(tmp_dir)
    g.trigger_circuit_breaker()
    # 熔斷後連 hold 都被擋（防呆，避免某條 race 路徑下漏判）
    r = g.evaluate(Decision("hold"))
    assert not r.passed
    assert r.reason == "circuit_breaker_active_today"


def test_audit_log_on_reject(tmp_dir):
    e, g = _make_guard(tmp_dir)
    g.evaluate(
        Decision("buy", symbol="2330.TW", qty=2000, type="MKT", stop_loss=900),
        current_quote=920,
    )
    audit = (tmp_dir / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(audit) == 1
    rec = json.loads(audit[0])
    assert rec["type"] == "risk_reject"
    assert "single_risk_too_high" in rec["reason"]


def test_check_and_maybe_trigger_circuit_below_threshold(tmp_dir):
    """fills 不足以打到 -3% → 不觸發"""
    e, g = _make_guard(tmp_dir)
    triggered = g.check_and_maybe_trigger_circuit()
    assert triggered is False
    assert not g.is_circuit_broken_today()


# ─── unknown action ───

def test_unknown_action_rejects(tmp_dir):
    e, g = _make_guard(tmp_dir)
    r = g.evaluate(Decision("yolo"))
    assert not r.passed
    assert "unknown_action" in r.reason
