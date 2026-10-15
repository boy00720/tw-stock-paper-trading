"""
M1 Day 1 驗收測試：資料模型基本功能。

執行方式：
    cd /Users/w/Cursor/automation
    python3 sim/tests/test_models.py        # 自包執行（不依賴 pytest）
    python3 -m pytest sim/tests/test_models.py   # pytest 也行
"""

import json
import sys
from pathlib import Path

# 確保能 import sim
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.models import (
    Order, Fill, Position, Account,
    OrderSide, OrderType, OrderStatus,
)


# ─── Order ───

def test_order_default_id_and_status():
    o = Order(symbol="2330", side=OrderSide.BUY, type=OrderType.MKT, qty=100)
    assert o.order_id.startswith("ord-"), f"order_id 應以 ord- 開頭：{o.order_id}"
    assert o.status == OrderStatus.PENDING
    assert o.qty == 100
    assert o.price is None  # MKT 無限價


def test_order_to_dict_json_serializable():
    o = Order(symbol="2330", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=600.0)
    d = o.to_dict()
    assert d["side"] == "BUY"
    assert d["type"] == "LMT"
    assert d["status"] == "PENDING"
    # 必須能直接 json 序列化（後續寫 orders.jsonl 用）
    s = json.dumps(d, ensure_ascii=False)
    assert "BUY" in s


# ─── Fill ───

def test_fill_basic():
    f = Fill(
        order_id="ord-abc",
        symbol="2330",
        side=OrderSide.BUY,
        qty=100,
        price=600.6,
        fee=85.59,
        tax=0,
    )
    assert f.fill_id.startswith("fill-")
    assert f.gross_value == 60060.0
    # 買單 net_cost = gross + fee
    assert abs(f.net_cost - (60060.0 + 85.59)) < 0.01


def test_fill_sell_proceeds():
    """賣單 net_proceeds = gross - fee - tax"""
    f = Fill(
        order_id="ord-xyz",
        symbol="2330",
        side=OrderSide.SELL,
        qty=100,
        price=620.0,
        fee=88.32,    # 0.1425% × 62000
        tax=186.0,    # 0.3% × 62000
    )
    expected = 62000.0 - 88.32 - 186.0
    assert abs(f.net_proceeds - expected) < 0.01


# ─── Position ───

def test_position_cost_basis():
    p = Position(symbol="2330", qty=100, avg_cost=600.5)
    assert p.cost_basis == 60050.0


def test_position_market_value_and_unrealized():
    p = Position(symbol="2330", qty=100, avg_cost=600.0)
    # 報價 620，市值 62000，未實現 +2000
    assert p.market_value(620.0) == 62000.0
    assert p.unrealized_pnl(620.0) == 2000.0
    # 報價 580，未實現 -2000
    assert p.unrealized_pnl(580.0) == -2000.0


# ─── Account ───

def test_account_initial_state():
    a = Account(cash=1_000_000.0, initial_cash=1_000_000.0)
    assert a.cost_value == 0.0
    assert a.equity() == 1_000_000.0
    assert a.total_return_pct() == 0.0


def test_account_equity_with_position_no_quotes():
    """無 quotes 時用 cost_basis 估算"""
    a = Account(cash=940_000.0, initial_cash=1_000_000.0)
    a.positions["2330"] = Position(symbol="2330", qty=100, avg_cost=600.0)
    # cost_value = 60000，equity = 940000 + 60000 = 1_000_000
    assert a.equity() == 1_000_000.0


def test_account_equity_with_quotes_mark_to_market():
    """有 quotes 時走 market value"""
    a = Account(cash=940_000.0, initial_cash=1_000_000.0)
    a.positions["2330"] = Position(symbol="2330", qty=100, avg_cost=600.0)
    # 報價 620 → market_value 62000 → equity 1_002_000
    assert a.equity({"2330": 620.0}) == 1_002_000.0


def test_account_total_return_with_realized_pnl():
    """總報酬 = (equity + realized_pnl - initial) / initial × 100"""
    a = Account(cash=1_050_000.0, initial_cash=1_000_000.0)
    a.total_realized_pnl = 50_000.0
    # 持倉空、equity=1_050_000、realized=50_000，總報酬 = (1_050_000 + 50_000 - 1_000_000) / 1_000_000 × 100 = 10%
    assert abs(a.total_return_pct() - 10.0) < 1e-6


def test_account_to_dict_serializable():
    a = Account(cash=900_000.0, initial_cash=1_000_000.0)
    a.positions["2330"] = Position(symbol="2330", qty=100, avg_cost=600.0)
    d = a.to_dict({"2330": 620.0})
    s = json.dumps(d, ensure_ascii=False)
    assert "2330" in s
    assert d["equity"] == 962_000.0  # 900000 + 100*620
    assert d["positions"]["2330"]["quote"] == 620.0
    assert d["positions"]["2330"]["unrealized_pnl"] == 2_000.0
    assert d["total_return_pct"] != 0  # 應該有變動


# ─── 自包執行（不依賴 pytest）───

if __name__ == "__main__":
    g = globals()
    tests = [name for name, obj in g.items() if name.startswith("test_") and callable(obj)]
    failed = []
    for t in tests:
        try:
            g[t]()
            print(f"  ✅ {t}")
        except Exception as e:
            print(f"  ❌ {t}: {type(e).__name__}: {e}")
            failed.append(t)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通過")
    sys.exit(0 if not failed else 1)
