"""
M2 驗收測試：進階訂單類型（STOP / TP / OCO / IFD）。

執行：
    python3 sim/tests/test_advanced_orders.py
    python3 -m pytest sim/tests/test_advanced_orders.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.engine import MatchEngine
from sim.models import Order, OrderSide, OrderStatus, OrderType


def _make_engine(tmp_dir: Path) -> MatchEngine:
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    return e


def _buy_position(e: MatchEngine, symbol: str, qty: int, price: float) -> None:
    """測試 helper：建立持倉"""
    e.submit_order(Order(symbol=symbol, side=OrderSide.BUY, type=OrderType.MKT, qty=qty))
    e.apply_fills(e.match({symbol: price}))


# ─────────────────────────────────────────────────────
# STOP（停損 / 突破）
# ─────────────────────────────────────────────────────

def test_stop_sell_triggered_when_breakdown(tmp_dir):
    """SELL STOP：持有部位、報價跌破停損價 → 觸發市價賣"""
    e = _make_engine(tmp_dir)
    _buy_position(e, "2330.TW", 100, 600.0)

    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP, qty=100, price=580.0)
    )
    # 報價 590 > 580，未觸發
    assert e.match({"2330.TW": 590.0}) == []
    assert o.status == OrderStatus.PENDING

    # 報價 575 ≤ 580，觸發 → 成交在 575 × (1 - 0.001) = 574.425
    fills = e.match({"2330.TW": 575.0})
    assert len(fills) == 1
    assert abs(fills[0].price - 574.425) < 1e-3
    e.apply_fills(fills)
    assert "2330.TW" not in e.account.positions  # 全平倉


def test_stop_buy_triggered_when_breakout(tmp_dir):
    """BUY STOP：報價突破上方觸發價 → 觸發市價買"""
    e = _make_engine(tmp_dir)

    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.STOP, qty=100, price=620.0)
    )
    assert e.match({"2330.TW": 615.0}) == []  # 未突破
    fills = e.match({"2330.TW": 625.0})  # 突破
    assert len(fills) == 1
    assert abs(fills[0].price - 625.625) < 1e-3  # 625 × 1.001


def test_stop_not_triggered_no_change(tmp_dir):
    """STOP 在觸發價反方向波動 → 維持 PENDING"""
    e = _make_engine(tmp_dir)
    _buy_position(e, "2330.TW", 100, 600.0)

    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP, qty=100, price=580.0)
    )
    e.match({"2330.TW": 610.0})  # 報價往上漲，停損未觸發
    e.match({"2330.TW": 605.0})
    assert o.status == OrderStatus.PENDING


# ─────────────────────────────────────────────────────
# TP（停利 / 達標）
# ─────────────────────────────────────────────────────

def test_tp_sell_triggered_when_target_reached(tmp_dir):
    """SELL TP：持有部位、報價達到目標 → 觸發市價獲利賣"""
    e = _make_engine(tmp_dir)
    _buy_position(e, "2330.TW", 100, 600.0)

    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=650.0)
    )
    assert e.match({"2330.TW": 640.0}) == []  # 未達標
    fills = e.match({"2330.TW": 655.0})  # 達標
    assert len(fills) == 1
    assert abs(fills[0].price - 654.345) < 1e-3  # 655 × 0.999
    e.apply_fills(fills)
    # 已實現損益應為正
    assert e.account.total_realized_pnl > 0


def test_tp_buy_triggered_at_dip(tmp_dir):
    """BUY TP（少見）：報價跌到目標 → 撿便宜買進"""
    e = _make_engine(tmp_dir)

    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.TP, qty=100, price=550.0)
    )
    assert e.match({"2330.TW": 560.0}) == []  # 還沒跌到
    fills = e.match({"2330.TW": 545.0})  # 跌到目標
    assert len(fills) == 1
    assert abs(fills[0].price - 545.545) < 1e-3  # 545 × 1.001


def test_tp_not_triggered_keeps_pending(tmp_dir):
    e = _make_engine(tmp_dir)
    _buy_position(e, "2330.TW", 100, 600.0)
    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=650.0)
    )
    e.match({"2330.TW": 620.0})
    e.match({"2330.TW": 640.0})
    assert o.status == OrderStatus.PENDING


# ─────────────────────────────────────────────────────
# OCO（One-Cancels-Other）
# ─────────────────────────────────────────────────────

def test_oco_stop_fires_first_cancels_tp(tmp_dir):
    """OCO 配對：停損先觸發，停利自動取消"""
    e = _make_engine(tmp_dir)
    _buy_position(e, "2330.TW", 100, 600.0)

    sl = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP, qty=100, price=580.0)
    tp = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=650.0)
    sl_o, tp_o = e.submit_oco(sl, tp)

    # 兩張同 group
    assert sl_o.oco_group_id == tp_o.oco_group_id
    assert sl_o.order_id in e.pending_orders
    assert tp_o.order_id in e.pending_orders

    # 報價跌到 575：觸發 SL
    fills = e.match({"2330.TW": 575.0})
    e.apply_fills(fills)

    assert sl_o.status == OrderStatus.FILLED
    assert tp_o.status == OrderStatus.CANCELLED
    assert "OCO sibling filled" in tp_o.notes
    # 兩張都不再 pending
    assert sl_o.order_id not in e.pending_orders
    assert tp_o.order_id not in e.pending_orders


def test_oco_tp_fires_first_cancels_stop(tmp_dir):
    """OCO 配對：停利先觸發，停損自動取消"""
    e = _make_engine(tmp_dir)
    _buy_position(e, "2330.TW", 100, 600.0)

    sl = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP, qty=100, price=580.0)
    tp = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=650.0)
    sl_o, tp_o = e.submit_oco(sl, tp)

    # 報價漲到 660：觸發 TP
    fills = e.match({"2330.TW": 660.0})
    e.apply_fills(fills)

    assert tp_o.status == OrderStatus.FILLED
    assert sl_o.status == OrderStatus.CANCELLED
    assert "OCO sibling filled" in sl_o.notes


def test_oco_neither_triggers_both_pending(tmp_dir):
    """OCO 配對：價格在 SL/TP 之間波動，兩張都維持 pending"""
    e = _make_engine(tmp_dir)
    _buy_position(e, "2330.TW", 100, 600.0)

    sl = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP, qty=100, price=580.0)
    tp = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=650.0)
    sl_o, tp_o = e.submit_oco(sl, tp)

    e.match({"2330.TW": 605.0})
    e.match({"2330.TW": 595.0})
    e.match({"2330.TW": 620.0})

    assert sl_o.status == OrderStatus.PENDING
    assert tp_o.status == OrderStatus.PENDING


# ─────────────────────────────────────────────────────
# IFD（If-Done）
# ─────────────────────────────────────────────────────

def test_ifd_child_inactive_until_parent_fills(tmp_dir):
    """IFD：parent 未成交時 child 不參與撮合"""
    e = _make_engine(tmp_dir)

    parent = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=580.0)
    child = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=620.0)
    p, c = e.submit_ifd(parent, child)

    # parent 進 pending、child 進 _waiting_children
    assert p.order_id in e.pending_orders
    assert c.status == OrderStatus.PENDING_PARENT
    assert c.parent_order_id == p.order_id
    assert c.order_id not in e.pending_orders
    assert p.order_id in e._waiting_children

    # 報價 590 > 580，parent 不成交，child 也不該被撮合
    e.match({"2330.TW": 590.0})
    assert p.status == OrderStatus.PENDING
    assert c.status == OrderStatus.PENDING_PARENT
    # 即便報價 ≥ 620（child TP 條件），child 仍因 parent 未成交不撮合
    e.match({"2330.TW": 625.0})
    assert c.status == OrderStatus.PENDING_PARENT


def test_ifd_child_activates_after_parent_fills(tmp_dir):
    """IFD：parent 成交後 child 升級為 PENDING，後續達條件可成交"""
    e = _make_engine(tmp_dir)

    parent = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=580.0)
    child = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=620.0)
    p, c = e.submit_ifd(parent, child)

    # 報價跌到 575：parent 成交（限價 580）
    fills = e.match({"2330.TW": 575.0})
    e.apply_fills(fills)

    assert p.status == OrderStatus.FILLED
    assert c.status == OrderStatus.PENDING        # child 升級
    assert c.order_id in e.pending_orders
    assert p.order_id not in e._waiting_children   # waiting 清空

    # 報價漲到 625：child TP 觸發
    fills2 = e.match({"2330.TW": 625.0})
    e.apply_fills(fills2)
    assert c.status == OrderStatus.FILLED


def test_ifd_parent_rejected_child_also_rejected(tmp_dir):
    """IFD：parent 被 reject（如 LMT 缺 price），child 也應 REJECTED"""
    e = _make_engine(tmp_dir)

    bad_parent = Order(
        symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=None
    )  # LMT 缺 price → reject
    child = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=100, price=620.0)
    p, c = e.submit_ifd(bad_parent, child)

    assert p.status == OrderStatus.REJECTED
    assert c.status == OrderStatus.REJECTED
    assert "parent rejected" in c.notes
    assert p.order_id not in e._waiting_children  # 沒進 waiting


def test_ifd_child_rejected_on_activate_if_holding_insufficient(tmp_dir):
    """IFD：parent 買 100、但 child SELL 200 → 升級時 reject（持倉不足）"""
    e = _make_engine(tmp_dir)

    parent = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100)
    child = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.TP, qty=200, price=620.0)
    p, c = e.submit_ifd(parent, child)

    # parent 在 600 成交
    e.apply_fills(e.match({"2330.TW": 600.0}))
    assert p.status == OrderStatus.FILLED

    # child 升級時被 reject（only held 100，要賣 200）
    assert c.status == OrderStatus.REJECTED
    assert "insufficient holding" in c.notes
    assert c.order_id not in e.pending_orders


# ─────────────────────────────────────────────────────
# 自包執行
# ─────────────────────────────────────────────────────

if __name__ == "__main__":
    g = globals()
    tests = [name for name, obj in g.items() if name.startswith("test_") and callable(obj)]
    failed = []
    with tempfile.TemporaryDirectory() as tmp_root:
        root = Path(tmp_root)
        for t in tests:
            try:
                sub = root / t
                sub.mkdir()
                g[t](sub)
                print(f"  ✅ {t}")
            except Exception as e:
                import traceback
                print(f"  ❌ {t}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed.append(t)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通過")
    sys.exit(0 if not failed else 1)


try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass
