"""
M1 Day 2 驗收測試：撮合引擎。

執行：
    cd /Users/w/Cursor/automation
    python3 sim/tests/test_engine.py        # 自包執行
    python3 -m pytest sim/tests/test_engine.py
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.engine import MatchEngine
from sim.models import Order, OrderSide, OrderStatus, OrderType


def _make_engine(tmp_dir: Path) -> MatchEngine:
    """建立隔離環境的 engine（log 路徑指向 tmp_dir）"""
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    return e


# ─── 撮合：市價單 ───

def test_market_buy_basic(tmp_dir):
    """市價買 100 @ 600，成交價 = 600 × 1.001，手續費攤入 avg_cost"""
    e = _make_engine(tmp_dir)
    initial_cash = e.account.cash

    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100)
    e.submit_order(o)
    fills = e.match({"2330.TW": 600.0})
    e.apply_fills(fills)

    assert len(fills) == 1
    fill = fills[0]
    assert abs(fill.price - 600.6) < 1e-6, f"成交價應為 600.6 (含 0.1% 滑價)，實際 {fill.price}"
    expected_fee = 600.6 * 100 * 0.001425  # 85.5855
    assert abs(fill.fee - expected_fee) < 0.01
    assert fill.tax == 0  # 買單無稅

    p = e.account.positions["2330.TW"]
    assert p.qty == 100
    expected_avg = (600.6 * 100 + expected_fee) / 100
    assert abs(p.avg_cost - expected_avg) < 0.01

    expected_cash = initial_cash - 60060 - expected_fee
    assert abs(e.account.cash - expected_cash) < 0.01


def test_market_buy_no_quote_skipped(tmp_dir):
    """quotes 不含此 symbol → 不撮合，order 留 pending"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100)
    e.submit_order(o)
    fills = e.match({"OTHER.TW": 100.0})  # 沒有 2330 報價
    assert len(fills) == 0
    assert o.order_id in e.pending_orders


# ─── 撮合：限價單 ───

def test_limit_buy_not_filled(tmp_dir):
    """限價 580 買，報價 590 → 不成交"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=580)
    e.submit_order(o)
    fills = e.match({"2330.TW": 590.0})
    assert len(fills) == 0
    assert o.order_id in e.pending_orders


def test_limit_buy_filled_at_limit_price(tmp_dir):
    """限價 580 買，報價 575 → 成交在 580（不取更優價）"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=580)
    e.submit_order(o)
    fills = e.match({"2330.TW": 575.0})
    assert len(fills) == 1
    assert fills[0].price == 580.0
    assert o.order_id not in e.pending_orders


def test_limit_sell_not_filled(tmp_dir):
    """限價 620 賣，報價 615 → 不成交"""
    e = _make_engine(tmp_dir)
    # 先買進
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    # 限價賣
    sell = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.LMT, qty=100, price=620)
    e.submit_order(sell)
    fills = e.match({"2330.TW": 615.0})
    assert len(fills) == 0


def test_limit_sell_filled(tmp_dir):
    """限價 620 賣，報價 625 → 成交在 620"""
    e = _make_engine(tmp_dir)
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    sell = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.LMT, qty=100, price=620)
    e.submit_order(sell)
    fills = e.match({"2330.TW": 625.0})
    assert len(fills) == 1
    assert fills[0].price == 620.0


# ─── 持倉管理 ───

def test_partial_close_keeps_avg_cost(tmp_dir):
    """部分平倉：剩餘 avg_cost 不變、realized_pnl > 0"""
    e = _make_engine(tmp_dir)

    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    avg_cost_after_buy = e.account.positions["2330.TW"].avg_cost

    e.submit_order(Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.MKT, qty=50))
    e.apply_fills(e.match({"2330.TW": 620.0}))

    p = e.account.positions["2330.TW"]
    assert p.qty == 50
    assert abs(p.avg_cost - avg_cost_after_buy) < 1e-6
    assert p.realized_pnl > 0
    assert abs(e.account.total_realized_pnl - p.realized_pnl) < 1e-6


def test_full_close_removes_position(tmp_dir):
    """全平倉：position 從 dict 移除，total_realized_pnl 仍保留"""
    e = _make_engine(tmp_dir)

    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 620.0}))

    assert "2330.TW" not in e.account.positions
    assert e.account.total_realized_pnl != 0


def test_add_to_position_avg_cost_weighted(tmp_dir):
    """加倉：均價加權"""
    e = _make_engine(tmp_dir)

    # 第一筆：100 @ 600
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    avg1 = e.account.positions["2330.TW"].avg_cost

    # 第二筆：100 @ 700 → 加倉
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 700.0}))

    p = e.account.positions["2330.TW"]
    assert p.qty == 200
    # 新 avg_cost 應該介於兩筆之間且偏向加權
    assert avg1 < p.avg_cost
    assert p.avg_cost < 701  # 含費用攤提後也不會超過 700+滑價+手續費邊界


# ─── 費率規則 ───

def test_min_commission_20_for_small_trade(tmp_dir):
    """小額交易手續費最低 20 元"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=1)
    e.submit_order(o)
    fills = e.match({"2330.TW": 10.0})  # 1 股 × 10 元 = 10 元，0.1425% × 10 = 0.01 → floor 到 20
    assert fills[0].fee == 20.0


def test_etf_tax_rate_lower_than_stock(tmp_dir):
    """ETF 證交稅 0.1% < 台股 0.3%"""
    e = _make_engine(tmp_dir)
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.submit_order(Order(symbol="0050.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0, "0050.TW": 100.0}))

    e.submit_order(Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.MKT, qty=100))
    e.submit_order(Order(symbol="0050.TW", side=OrderSide.SELL, type=OrderType.MKT, qty=100))
    fills = e.match({"2330.TW": 620.0, "0050.TW": 110.0})

    fill_stock = next(f for f in fills if f.symbol == "2330.TW")
    fill_etf = next(f for f in fills if f.symbol == "0050.TW")

    # 台股稅率 0.3%、ETF 0.1%
    stock_tax_rate = fill_stock.tax / fill_stock.gross_value
    etf_tax_rate = fill_etf.tax / fill_etf.gross_value
    assert abs(stock_tax_rate - 0.003) < 1e-6
    assert abs(etf_tax_rate - 0.001) < 1e-6


# ─── 訂單驗證 ───

def test_reject_zero_qty(tmp_dir):
    """qty=0 直接 REJECT"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=0)
    e.submit_order(o)
    assert o.status == OrderStatus.REJECTED
    assert "qty" in o.notes.lower()


def test_reject_limit_without_price(tmp_dir):
    """LMT 缺 price 直接 REJECT"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=None)
    e.submit_order(o)
    assert o.status == OrderStatus.REJECTED
    assert "requires price" in o.notes.lower()


def test_reject_sell_more_than_held(tmp_dir):
    """賣超持倉 REJECT"""
    e = _make_engine(tmp_dir)
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    o = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.MKT, qty=200)
    e.submit_order(o)
    assert o.status == OrderStatus.REJECTED
    assert "insufficient holding" in o.notes


def test_reject_sell_no_position(tmp_dir):
    """完全沒持倉就賣 REJECT"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.MKT, qty=100)
    e.submit_order(o)
    assert o.status == OrderStatus.REJECTED


def test_match_rejects_orphan_sell_stop_without_fill(tmp_dir):
    """跨進程 replay 出來的孤兒 SELL STOP 觸發時，不應產生 phantom fill。"""
    e = _make_engine(tmp_dir)
    orphan = Order(
        symbol="2330.TW",
        side=OrderSide.SELL,
        type=OrderType.STOP,
        qty=100,
        price=90.0,
        notes="[auto-stop] orphan",
    )
    # 直接注入 pending，模擬 orders.jsonl replay 後已無對應持倉的舊 stop。
    e.pending_orders[orphan.order_id] = orphan

    fills = e.match({"2330.TW": 80.0})
    e.apply_fills(fills)

    assert fills == []
    assert orphan.order_id not in e.pending_orders
    assert orphan.status == OrderStatus.REJECTED
    assert "insufficient holding at match" in orphan.notes
    assert not e.fills_log_path.exists()
    lines = e.orders_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["status"] == "REJECTED"


def test_match_rejects_second_over_qty_sell_stop_without_extra_fill(tmp_dir):
    """多張 SELL STOP 合計超過持倉時，只允許本輪可賣數量內的訂單成交。"""
    from sim.models import Position

    e = _make_engine(tmp_dir)
    e.account.positions["2330.TW"] = Position(
        symbol="2330.TW", qty=100, avg_cost=100.0, realized_pnl=0.0)
    first = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP,
                  qty=100, price=90.0, notes="[auto-stop] first")
    second = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP,
                   qty=100, price=90.0, notes="[auto-stop] duplicate")
    e.pending_orders[first.order_id] = first
    e.pending_orders[second.order_id] = second

    fills = e.match({"2330.TW": 80.0})
    e.apply_fills(fills)

    assert len(fills) == 1
    assert fills[0].order_id == first.order_id
    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.REJECTED
    assert "2330.TW" not in e.account.positions
    fill_lines = e.fills_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(fill_lines) == 1
    assert json.loads(fill_lines[0])["order_id"] == first.order_id


# ─── 取消委託 ───

def test_cancel_pending_order(tmp_dir):
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=580)
    e.submit_order(o)
    assert e.cancel_order(o.order_id) is True
    assert o.order_id not in e.pending_orders
    assert o.status == OrderStatus.CANCELLED


def test_cancel_nonexistent_order(tmp_dir):
    e = _make_engine(tmp_dir)
    assert e.cancel_order("ord-nonexistent") is False


# ─── M6.2 cash 預扣（reserved_cash / available_cash） ───

def test_lmt_buy_reserves_cash_estimate(tmp_dir):
    """LMT BUY submit → reserved_cash += price × qty + commission；available_cash 同步降。"""
    e = _make_engine(tmp_dir)
    cash_before = e.account.cash
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT,
              qty=1000, price=900.0)
    e.submit_order(o)
    assert o.status == OrderStatus.PENDING
    # 預期 reserve ≈ 900 × 1000 + max(900×1000×0.001425, 20) = 900000 + 1282.5
    assert e.account.reserved_cash == pytest.approx(900000.0 + 1282.5, rel=1e-4)
    assert e.account.available_cash == pytest.approx(cash_before - 901282.5, rel=1e-4)
    # cash 本身不動（要等 FILL）
    assert e.account.cash == cash_before


def test_mkt_buy_does_not_reserve(tmp_dir):
    """MKT BUY 無 price → 不預扣（依賴撮合輪立即吃掉）。"""
    e = _make_engine(tmp_dir)
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100)
    e.submit_order(o)
    assert o.status == OrderStatus.PENDING
    assert e.account.reserved_cash == 0.0


def test_cancel_lmt_buy_releases_reserve(tmp_dir):
    """cancel LMT BUY → reserved_cash 歸 0、available_cash 完全還原。"""
    e = _make_engine(tmp_dir)
    cash_before = e.account.cash
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT,
              qty=500, price=900.0)
    e.submit_order(o)
    assert e.account.reserved_cash > 0
    assert e.cancel_order(o.order_id) is True
    assert e.account.reserved_cash == 0.0
    assert e.account.available_cash == cash_before


def test_fill_lmt_buy_releases_reserve_and_deducts_actual_cash(tmp_dir):
    """LMT BUY 成交 → reserved_cash 歸 0、cash 實扣 (gross + actual_fee)。"""
    e = _make_engine(tmp_dir)
    cash_before = e.account.cash
    o = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT,
              qty=1000, price=900.0)
    e.submit_order(o)
    e.apply_fills(e.match({"2330.TW": 895.0}))  # 報價低於限價，會成交
    assert o.status == OrderStatus.FILLED
    assert e.account.reserved_cash == 0.0
    # cash 扣的是實際成交 gross + fee（不是預扣那個數）
    assert e.account.cash < cash_before
    assert e.account.available_cash == e.account.cash  # 無 PENDING 預扣


def test_lmt_buy_rejected_when_insufficient_available_cash(tmp_dir):
    """available_cash 不足 → LMT BUY 直接 REJECTED；reserved_cash 不變。"""
    e = _make_engine(tmp_dir)
    # 先掛一筆吃掉幾乎全部 cash
    big = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT,
                qty=1000, price=900.0)  # ≈901,282.5
    e.submit_order(big)
    assert big.status == OrderStatus.PENDING
    reserve_before = e.account.reserved_cash

    # 再掛一筆會超 available_cash 的單
    big2 = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT,
                 qty=1000, price=900.0)
    e.submit_order(big2)
    assert big2.status == OrderStatus.REJECTED
    assert "insufficient available cash" in big2.notes
    # reserved_cash 不該增加
    assert e.account.reserved_cash == reserve_before


def test_available_cash_reflects_multi_pending_reserves(tmp_dir):
    """多筆 PENDING BUY 並存：reserved_cash = 所有預扣總和。"""
    e = _make_engine(tmp_dir)
    cash_before = e.account.cash
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY,
                         type=OrderType.LMT, qty=100, price=900.0))
    e.submit_order(Order(symbol="1101.TW", side=OrderSide.BUY,
                         type=OrderType.LMT, qty=200, price=50.0))
    # 兩筆 reserve = (900×100 + commission_min=128.25→實際 max(...,20)=128.25)
    # + (50×200 + max(...,20)=20)  = 90128.25 + 10020
    assert e.account.reserved_cash == pytest.approx(90128.25 + 10020.0, rel=1e-4)
    assert e.account.available_cash == pytest.approx(cash_before - e.account.reserved_cash, rel=1e-4)


def test_sell_does_not_reserve_cash(tmp_dir):
    """SELL 影響持倉而非 cash 上限；reserved_cash 不該動。"""
    e = _make_engine(tmp_dir)
    # 先建倉
    e.apply_fills(e.match({"2330.TW": 900.0}))  # 沒 pending 跳過
    from sim.models import Position
    e.account.positions["2330.TW"] = Position(
        symbol="2330.TW", qty=1000, avg_cost=900.0)
    reserve_before = e.account.reserved_cash
    o = Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.LMT,
              qty=500, price=920.0)
    e.submit_order(o)
    assert o.status == OrderStatus.PENDING
    assert e.account.reserved_cash == reserve_before


def test_oco_sibling_cancel_releases_reserve(tmp_dir):
    """OCO 兩個 BUY LMT（理論罕用，但邏輯要對）：一筆成交另一筆被 OCO 取消時，預扣應釋放。"""
    import uuid
    e = _make_engine(tmp_dir)
    group = f"oco-{uuid.uuid4().hex[:6]}"
    o1 = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT,
               qty=100, price=900.0, oco_group_id=group)
    o2 = Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT,
               qty=100, price=850.0, oco_group_id=group)
    e.submit_order(o1)
    e.submit_order(o2)
    total_reserved = e.account.reserved_cash
    assert total_reserved > 0
    # 讓 o1 成交（901 報價低於 o1 限價，但高於 o2，所以只 o1 成交）
    e.apply_fills(e.match({"2330.TW": 850.0}))
    # 兩筆都離 pending：o1 FILLED，o2 OCO 取消
    assert o1.order_id not in e.pending_orders
    assert o2.order_id not in e.pending_orders
    # 兩筆預扣都釋放
    assert e.account.reserved_cash == 0.0


# ─── jsonl 持久化 ───

def test_orders_jsonl_records_lifecycle(tmp_dir):
    """orders.jsonl 應記錄 PENDING → FILLED 兩筆"""
    e = _make_engine(tmp_dir)
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))

    lines = e.orders_log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == "PENDING"
    assert json.loads(lines[1])["status"] == "FILLED"


def test_fills_jsonl_records_each_fill(tmp_dir):
    e = _make_engine(tmp_dir)
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))

    lines = e.fills_log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["symbol"] == "2330.TW"
    assert rec["side"] == "BUY"
    assert rec["qty"] == 100


# ─── snapshot ───

def test_snapshot_format(tmp_dir):
    e = _make_engine(tmp_dir)
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=580))
    snap = e.snapshot()
    assert "account" in snap
    assert "pending_orders" in snap
    assert len(snap["pending_orders"]) == 1
    assert snap["account"]["cash"] == 1_000_000.0


def test_snapshot_with_quotes_uses_market_value(tmp_dir):
    e = _make_engine(tmp_dir)
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    e.apply_fills(e.match({"2330.TW": 600.0}))
    snap_no_quote = e.snapshot()
    snap_with_quote = e.snapshot({"2330.TW": 700.0})
    # 給更高報價 → equity 應該更高
    assert snap_with_quote["account"]["equity"] > snap_no_quote["account"]["equity"]


# ─── 自包執行 ───

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


# 給 pytest 用的 fixture（自包執行用不到）
try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass
