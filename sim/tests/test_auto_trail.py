"""sim/tests/test_auto_trail.py — 階梯式 trailing stop 自動上移

驗證 auto_trail.maybe_raise_trailing_stops：
- 浮盈 < 5% → 不動
- 浮盈 5% → stop 上移到 cost
- 浮盈 8% → stop 上移到 +3%
- 浮盈 12% → stop 上移到 +7%
- 浮盈 15% → stop 上移到 +10%
- 只升不降：已有更高 stop 不會被覆蓋
- 無 auto-stop 的持倉不動
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.auto_trail import _calc_trail_stop, maybe_raise_trailing_stops
from sim.engine import MatchEngine
from sim.models import Order, OrderSide, OrderType


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _make_engine(tmp_dir):
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    return e


def _buy_and_set_stop(engine, symbol, qty, buy_price, stop_price):
    """模擬 BUY 成交 + 掛 auto-stop。"""
    from sim.models import Fill
    fill = Fill(order_id="test", symbol=symbol, side=OrderSide.BUY, qty=qty,
                price=buy_price, fee=0.0, tax=0.0)
    engine.apply_fills([fill])
    order = Order(
        symbol=symbol,
        side=OrderSide.SELL,
        type=OrderType.STOP,
        qty=qty,
        price=stop_price,
        notes="[auto-stop] initial",
    )
    engine.submit_order(order)
    return order


# ─── _calc_trail_stop 單元測試 ───

def test_calc_trail_no_gain():
    assert _calc_trail_stop(100, 100) is None
    assert _calc_trail_stop(100, 95) is None


def test_calc_trail_below_5pct():
    assert _calc_trail_stop(100, 104) is None


def test_calc_trail_at_5pct():
    result = _calc_trail_stop(100, 105)
    assert result == pytest.approx(100.0)  # cost


def test_calc_trail_at_8pct():
    result = _calc_trail_stop(100, 108)
    assert result == pytest.approx(103.0)  # +3%


def test_calc_trail_at_12pct():
    result = _calc_trail_stop(100, 112)
    assert result == pytest.approx(107.0)  # +7%


def test_calc_trail_at_15pct():
    result = _calc_trail_stop(100, 115)
    assert result == pytest.approx(110.0)  # +10%


def test_calc_trail_between_steps():
    # 6% → 只達第一階梯 (+5%)，stop at cost
    result = _calc_trail_stop(100, 106)
    assert result == pytest.approx(100.0)


# ─── 整合測試 ───

def test_trail_raises_stop_at_5pct_gain(tmp_dir):
    e = _make_engine(tmp_dir)
    _buy_and_set_stop(e, "2330.TW", 100, 100.0, 93.0)

    records = maybe_raise_trailing_stops(e, {"2330.TW": 105.0})
    assert len(records) == 1
    assert records[0]["old_stop"] == 93.0
    assert records[0]["new_stop"] == 100.0


def test_trail_no_action_below_5pct(tmp_dir):
    e = _make_engine(tmp_dir)
    _buy_and_set_stop(e, "2330.TW", 100, 100.0, 93.0)

    records = maybe_raise_trailing_stops(e, {"2330.TW": 104.0})
    assert records == []


def test_trail_only_up_never_down(tmp_dir):
    """若既有 stop 已高於計算值，不動。"""
    e = _make_engine(tmp_dir)
    _buy_and_set_stop(e, "2330.TW", 100, 100.0, 102.0)  # stop 已在 +2%

    # 浮盈 5% → 新 stop 應是 100（cost），但 102 > 100 → 不動
    records = maybe_raise_trailing_stops(e, {"2330.TW": 105.0})
    assert records == []


def test_trail_no_action_when_rounded_stop_unchanged(tmp_dir):
    """raw stop 略高但四捨五入後沒變時，不應取消重掛。"""
    e = _make_engine(tmp_dir)
    existing = _buy_and_set_stop(e, "2891.TW", 3300, 57.83754484252549, 63.62)

    records = maybe_raise_trailing_stops(e, {"2891.TW": 67.7})

    assert records == []
    assert set(e.pending_orders.keys()) == {existing.order_id}
    assert e.pending_orders[existing.order_id].price == 63.62


def test_trail_raises_when_rounded_stop_really_increases(tmp_dir):
    """四捨五入後的新 stop 確實更高時才替換。"""
    e = _make_engine(tmp_dir)
    existing = _buy_and_set_stop(e, "2891.TW", 3300, 57.83754484252549, 63.61)

    records = maybe_raise_trailing_stops(e, {"2891.TW": 67.7})

    assert len(records) == 1
    assert records[0]["old_stop"] == 63.61
    assert records[0]["new_stop"] == 63.62
    assert existing.order_id not in e.pending_orders


def test_trail_multiple_symbols(tmp_dir):
    e = _make_engine(tmp_dir)
    _buy_and_set_stop(e, "2330.TW", 100, 100.0, 93.0)
    _buy_and_set_stop(e, "2881.TW", 200, 50.0, 46.0)

    records = maybe_raise_trailing_stops(e, {
        "2330.TW": 108.0,  # +8% → stop at 103
        "2881.TW": 52.0,   # +4% → no action
    })
    assert len(records) == 1
    assert records[0]["symbol"] == "2330.TW"
    assert records[0]["new_stop"] == 103.0


def test_trail_no_auto_stop_skipped(tmp_dir):
    """持倉沒有 auto-stop 的不動。"""
    e = _make_engine(tmp_dir)
    from sim.models import Fill
    fill = Fill(order_id="test", symbol="2330.TW", side=OrderSide.BUY,
                qty=100, price=100.0, fee=0.0, tax=0.0)
    e.apply_fills([fill])
    # no auto-stop submitted

    records = maybe_raise_trailing_stops(e, {"2330.TW": 110.0})
    assert records == []
