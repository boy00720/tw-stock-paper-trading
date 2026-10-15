"""sim/tests/test_advisor_auto_stop.py — M6 階段二 P1：BUY 成交後自動掛 STOP SELL"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.auto_stop import (
    AUTO_STOP_NOTE_PREFIX,
    sync_auto_stops_after_fills,
)
from sim.advisor.runner import run_advisor_once
from sim.engine import MatchEngine
from sim.models import Order, OrderSide, OrderType, Position


# ─── fixtures / helpers ───

@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture(autouse=True)
def allow_new_positions_for_auto_stop_e2e(monkeypatch):
    """本檔整合測試需驗證 BUY 後 auto-stop；保守禁新倉另有專測。"""
    monkeypatch.setenv("SIM_ADVISOR_NO_NEW_POSITIONS_MODE", "0")


class FakeFeed:
    def __init__(self, fixed):
        self.fixed = dict(fixed)

    def get_quotes(self, symbols):
        return {s: self.fixed[s] for s in symbols if s in self.fixed}


class FakeClient:
    def __init__(self, raw, *, provider_id="fake", model="fake-m"):
        self.provider_id = provider_id
        self.model = model
        self._raw = raw

    def decide(self, system_prompt, user_payload, **kw):
        return self._raw


def _make_engine(tmp_dir, watchlist=None):
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    if watchlist is not None:
        e.config["watchlist"] = list(watchlist)
    return e


def _make_watchlist(tmp_dir, *, short=None, long=None, cross=None):
    p = tmp_dir / "wl.json"
    p.write_text(json.dumps({
        "formal_pools": {"short_term": short or [], "long_term": long or []},
        "cross_signals": cross or {},
    }), encoding="utf-8")
    return p


def _auto_stop_orders(engine, symbol=None):
    return [
        o for o in engine.pending_orders.values()
        if o.side == OrderSide.SELL
        and o.type == OrderType.STOP
        and (o.notes or "").startswith(AUTO_STOP_NOTE_PREFIX)
        and (symbol is None or o.symbol == symbol)
    ]


# ─── 單元測試：sync_auto_stops_after_fills ───

def test_buy_fill_submits_new_auto_stop(tmp_dir):
    """BUY 成交後該 symbol pending_orders 出現一筆 auto-stop SELL，qty=持倉量、price=stop_loss。"""
    e = _make_engine(tmp_dir)
    # 模擬 BUY 已被 apply：手動建持倉
    e.account.positions["2330.TW"] = Position(
        symbol="2330.TW", qty=100, avg_cost=920.0,
    )
    # 模擬剛產生的 fill
    from sim.models import Fill
    fill = Fill(
        order_id="ord-x", symbol="2330.TW", side=OrderSide.BUY,
        qty=100, price=920.0, fee=20.0, tax=0.0,
    )
    records = sync_auto_stops_after_fills(e, [fill], {"2330.TW": 900.0})

    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "2330.TW"
    assert r["old_stop"] is None
    assert r["qty_after_fill"] == 100
    assert r["new_stop"] == 900.0
    assert r["new_order_status"] == "PENDING"

    autos = _auto_stop_orders(e, "2330.TW")
    assert len(autos) == 1
    assert autos[0].qty == 100
    assert autos[0].price == 900.0


def test_second_buy_replaces_old_auto_stop(tmp_dir):
    """同檔加倉：舊 auto-stop 取消、新 auto-stop 用新 stop_loss + 新總持倉量。"""
    from sim.models import Fill
    e = _make_engine(tmp_dir)

    # 第一次 BUY 100 @ 920
    e.account.positions["2330.TW"] = Position("2330.TW", 100, 920.0)
    fill1 = Fill("ord-1", "2330.TW", OrderSide.BUY, 100, 920.0, 20.0, 0.0)
    sync_auto_stops_after_fills(e, [fill1], {"2330.TW": 900.0})
    autos = _auto_stop_orders(e, "2330.TW")
    assert len(autos) == 1
    first_oid = autos[0].order_id
    assert autos[0].price == 900.0

    # 第二次 BUY 50 @ 930 → 持倉變 150；新 stop_loss 880
    e.account.positions["2330.TW"].qty = 150
    e.account.positions["2330.TW"].avg_cost = 923.33
    fill2 = Fill("ord-2", "2330.TW", OrderSide.BUY, 50, 930.0, 20.0, 0.0)
    records = sync_auto_stops_after_fills(e, [fill2], {"2330.TW": 880.0})

    r = records[0]
    assert r["old_stop"] == 900.0
    assert r["qty_after_fill"] == 150
    assert r["new_stop"] == 880.0

    autos = _auto_stop_orders(e, "2330.TW")
    assert len(autos) == 1, "舊 auto-stop 應已 cancel，只剩一筆新的"
    assert autos[0].order_id != first_oid
    assert autos[0].qty == 150
    assert autos[0].price == 880.0


def test_partial_sell_realigns_auto_stop_qty_keeps_price(tmp_dir):
    """部分 SELL 後 auto-stop qty 對齊新持倉量、price 沿用舊 auto-stop（無新 stop_loss 來源）。"""
    from sim.models import Fill
    e = _make_engine(tmp_dir)

    # 先掛上既有 auto-stop（模擬上一輪 BUY 後的狀態）
    e.account.positions["2330.TW"] = Position("2330.TW", 200, 920.0)
    e.submit_order(Order(
        symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP,
        qty=200, price=900.0, notes=f"{AUTO_STOP_NOTE_PREFIX} prior",
    ))
    assert len(_auto_stop_orders(e, "2330.TW")) == 1

    # 部分 SELL 50 → 持倉 150
    e.account.positions["2330.TW"].qty = 150
    sell_fill = Fill("ord-s1", "2330.TW", OrderSide.SELL, 50, 1000.0, 20.0, 3.0)
    records = sync_auto_stops_after_fills(e, [sell_fill], {})  # SELL 沒新 stop_loss

    r = records[0]
    assert r["old_stop"] == 900.0
    assert r["qty_after_fill"] == 150
    assert r["new_stop"] == 900.0, "SELL 後 fallback 到舊 auto-stop 觸發價"

    autos = _auto_stop_orders(e, "2330.TW")
    assert len(autos) == 1
    assert autos[0].qty == 150
    assert autos[0].price == 900.0


def test_full_sell_cancels_auto_stop_no_resubmit(tmp_dir):
    """全平倉後 auto-stop 被取消、不重掛。"""
    from sim.models import Fill
    e = _make_engine(tmp_dir)

    e.account.positions["2330.TW"] = Position("2330.TW", 100, 920.0)
    e.submit_order(Order(
        symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP,
        qty=100, price=900.0, notes=f"{AUTO_STOP_NOTE_PREFIX} prior",
    ))

    # 全 SELL → 持倉歸零（模擬 apply_fills 已跑過）
    del e.account.positions["2330.TW"]
    sell_fill = Fill("ord-s", "2330.TW", OrderSide.SELL, 100, 1000.0, 20.0, 3.0)
    records = sync_auto_stops_after_fills(e, [sell_fill], {})

    r = records[0]
    assert r["old_stop"] == 900.0
    assert r["qty_after_fill"] == 0
    assert r["new_stop"] is None
    assert r["skip_reason"] == "position_closed"
    assert len(_auto_stop_orders(e, "2330.TW")) == 0


def test_does_not_touch_user_manual_stop(tmp_dir):
    """使用者手動掛的 STOP（notes 沒有 [auto-stop] prefix）不應被同步機制動到。"""
    from sim.models import Fill
    e = _make_engine(tmp_dir)
    e.account.positions["2330.TW"] = Position("2330.TW", 100, 920.0)
    user_stop = e.submit_order(Order(
        symbol="2330.TW", side=OrderSide.SELL, type=OrderType.STOP,
        qty=100, price=850.0, notes="使用者手動掛",
    ))

    fill = Fill("ord-b", "2330.TW", OrderSide.BUY, 100, 920.0, 20.0, 0.0)
    sync_auto_stops_after_fills(e, [fill], {"2330.TW": 905.0})

    # 使用者 STOP 還在
    assert user_stop.order_id in e.pending_orders
    # 同時新增了一筆 auto-stop
    autos = _auto_stop_orders(e, "2330.TW")
    assert len(autos) == 1
    assert autos[0].price == 905.0


def test_no_stop_loss_available_emits_skip_record(tmp_dir):
    """既無新 stop_loss 也無舊 auto-stop → skip + 紀錄 reason。"""
    from sim.models import Fill
    e = _make_engine(tmp_dir)
    e.account.positions["2330.TW"] = Position("2330.TW", 100, 920.0)
    fill = Fill("ord-b", "2330.TW", OrderSide.BUY, 100, 920.0, 20.0, 0.0)
    records = sync_auto_stops_after_fills(e, [fill], {})  # 沒給 stop_loss

    r = records[0]
    assert r["skip_reason"] == "no_stop_loss_available"
    assert len(_auto_stop_orders(e, "2330.TW")) == 0


# ─── 整合：run_advisor_once 完整流程 ───

def test_run_advisor_buy_then_quote_drop_triggers_auto_stop(tmp_dir):
    """端到端：advisor BUY → fill → auto-stop 掛上 → 下一輪 quote 跌破 → STOP 觸發 → SELL fill 落地。

    這是 P1 的命中目標：不再裸奔，跌破自動砍。
    """
    e = _make_engine(tmp_dir, watchlist=["2330.TW"])
    feed = FakeFeed({"2330.TW": 920.0, "0050.TW": 200.0})
    wl = _make_watchlist(tmp_dir, short=[{"symbol": "2330.TW", "score": 85}])

    raw = {
        "decisions": [
            {"action": "buy", "symbol": "2330.TW", "qty": 100, "type": "MKT",
             "stop_loss": 860.0, "reason": "test buy"},
        ],
    }
    client = FakeClient(raw)
    result = run_advisor_once(
        e, feed, client, dry_run=False,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=tmp_dir / "audit.jsonl",
    )

    assert result["matched_immediately"] == 1
    assert result["auto_stop_sync"], "auto_stop_sync 應有記錄"
    assert result["auto_stop_sync"][0]["new_stop"] == 860.0

    # auto-stop 已在 pending
    autos = _auto_stop_orders(e, "2330.TW")
    assert len(autos) == 1
    assert autos[0].qty == 100
    assert autos[0].price == 860.0

    # 模擬下一輪 quote 跌破 860 → match 觸發 SELL
    fills = e.match({"2330.TW": 855.0})
    assert len(fills) == 1
    assert fills[0].side == OrderSide.SELL
    e.apply_fills(fills)
    assert "2330.TW" not in e.account.positions, "全平倉後 position 應消失"
    assert e.account.total_realized_pnl < 0, "停損觸發 → 已實現損益為負"


def test_run_advisor_dry_run_does_not_create_auto_stop(tmp_dir):
    """dry_run 模式不撮合、不掛 auto-stop。"""
    e = _make_engine(tmp_dir, watchlist=["2330.TW"])
    feed = FakeFeed({"2330.TW": 920.0, "0050.TW": 200.0})
    wl = _make_watchlist(tmp_dir, short=[{"symbol": "2330.TW", "score": 85}])

    raw = {
        "decisions": [
            {"action": "buy", "symbol": "2330.TW", "qty": 100, "type": "MKT",
             "stop_loss": 860.0, "reason": "test"},
        ],
    }
    result = run_advisor_once(
        e, feed, FakeClient(raw), dry_run=True,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=tmp_dir / "audit.jsonl",
    )
    assert result["matched_immediately"] == 0
    assert result["auto_stop_sync"] == []
    assert len(_auto_stop_orders(e)) == 0


def test_run_advisor_audit_includes_auto_stop_sync(tmp_dir):
    """decision_round audit log 必須帶上 auto_stop_sync 欄位。"""
    e = _make_engine(tmp_dir, watchlist=["2330.TW"])
    feed = FakeFeed({"2330.TW": 920.0, "0050.TW": 200.0})
    wl = _make_watchlist(tmp_dir, short=[{"symbol": "2330.TW", "score": 85}])
    audit = tmp_dir / "audit.jsonl"

    raw = {
        "decisions": [
            {"action": "buy", "symbol": "2330.TW", "qty": 100, "type": "MKT",
             "stop_loss": 860.0, "reason": "test"},
        ],
    }
    run_advisor_once(
        e, feed, FakeClient(raw), dry_run=False,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=audit,
    )

    rec = json.loads(audit.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "auto_stop_sync" in rec
    assert len(rec["auto_stop_sync"]) == 1
    assert rec["auto_stop_sync"][0]["symbol"] == "2330.TW"
    assert rec["auto_stop_sync"][0]["new_stop"] == 860.0
