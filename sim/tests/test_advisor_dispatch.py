"""sim/tests/test_advisor_dispatch.py — M6.1 dispatch 子命令 / replay PENDING 測試

驗證：
1. load_pending_orders 只回最後狀態為 PENDING 的訂單（FILLED/CANCELLED 過濾掉）
2. load_pending_orders 解析格式錯誤的行不會 raise（warn + skip）
3. init_engine_with_pending 同時 replay fills 重建持倉 + replay PENDING orders
4. dispatch match 邏輯：replay 後 engine.match 能撮合 LMT 觸發成交
5. dispatch cancel-all-pending：所有 PENDING orders 被改成 CANCELLED 寫進 orders.jsonl
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.runner import (
    _order_from_dict,
    init_engine_with_pending,
    load_pending_orders,
)
from sim.engine import MatchEngine
from sim.models import Order, OrderSide, OrderStatus, OrderType


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _order_dict(order_id, status, *, side="BUY", type_="MKT", symbol="2330.TW",
                qty=100, price=None):
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "type": type_,
        "qty": qty,
        "price": price,
        "status": status,
        "created_at": "2026-05-06T10:00:00",
        "notes": "",
        "oco_group_id": None,
        "parent_order_id": None,
    }


# ─── _order_from_dict ───

def test_order_from_dict_happy():
    o = _order_from_dict(_order_dict("o1", "PENDING", type_="LMT", price=900.0))
    assert o is not None
    assert o.order_id == "o1"
    assert o.type == OrderType.LMT
    assert o.price == 900.0
    assert o.status == OrderStatus.PENDING


def test_order_from_dict_invalid_returns_none():
    """缺 order_id / 不合法 enum 值 → None，不 raise"""
    assert _order_from_dict({}) is None
    assert _order_from_dict(_order_dict("o1", "BAD_STATUS")) is None
    assert _order_from_dict(_order_dict("o1", "PENDING", side="WRONG")) is None


# ─── load_pending_orders ───

def test_load_pending_orders_only_keeps_last_state(tmp_dir):
    """同 order_id 寫 PENDING → FILLED → load_pending_orders 應回空"""
    orders = tmp_dir / "orders.jsonl"
    _write_jsonl(orders, [
        _order_dict("o1", "PENDING"),
        _order_dict("o1", "FILLED"),
    ])
    pending = load_pending_orders(orders)
    assert pending == {}


def test_load_pending_orders_returns_pending_only(tmp_dir):
    """3 筆訂單：1 PENDING / 1 FILLED / 1 CANCELLED → 只回 PENDING 那筆"""
    orders = tmp_dir / "orders.jsonl"
    _write_jsonl(orders, [
        _order_dict("o-pending", "PENDING", type_="LMT", price=900.0),
        _order_dict("o-filled-init", "PENDING"),
        _order_dict("o-filled-init", "FILLED"),
        _order_dict("o-cancelled", "PENDING"),
        _order_dict("o-cancelled", "CANCELLED"),
    ])
    pending = load_pending_orders(orders)
    assert set(pending.keys()) == {"o-pending"}
    assert pending["o-pending"].type == OrderType.LMT
    assert pending["o-pending"].price == 900.0


def test_load_pending_orders_skips_malformed_lines(tmp_dir):
    """格式錯誤的行直接跳過、不 raise"""
    orders = tmp_dir / "orders.jsonl"
    orders.write_text(
        json.dumps(_order_dict("o1", "PENDING")) + "\n"
        "not-a-json-line\n"
        "\n"  # blank line
        + json.dumps(_order_dict("o2", "PENDING")) + "\n",
        encoding="utf-8",
    )
    pending = load_pending_orders(orders)
    assert set(pending.keys()) == {"o1", "o2"}


def test_load_pending_orders_missing_file_returns_empty(tmp_dir):
    assert load_pending_orders(tmp_dir / "no_such.jsonl") == {}


# ─── init_engine_with_pending ───

def test_init_engine_with_pending_combines_fills_replay_and_orders(tmp_dir, monkeypatch):
    """fills.jsonl 重建持倉 + orders.jsonl PENDING 重建 pending_orders。"""
    # 用 monkeypatch 把 MatchEngine 的 default 路徑指到 tmp_dir
    fills = tmp_dir / "fills.jsonl"
    orders = tmp_dir / "orders.jsonl"
    _write_jsonl(fills, [
        {"order_id": "f1", "fill_id": "fid1", "symbol": "2330.TW", "side": "BUY",
         "qty": 100, "price": 900.0, "fee": 142.5, "tax": 0,
         "filled_at": "2026-05-04T10:00:00"},
    ])
    _write_jsonl(orders, [
        _order_dict("o-lmt-pending", "PENDING", type_="LMT", price=950.0,
                    side="SELL", qty=100),
    ])

    # patch MatchEngine 路徑
    def _fake_init(self, *args, **kwargs):
        # 呼叫真的 __init__ 後改路徑
        MatchEngine.__init_orig__(self, *args, **kwargs)
        self.fills_log_path = fills
        self.orders_log_path = orders

    if not hasattr(MatchEngine, "__init_orig__"):
        MatchEngine.__init_orig__ = MatchEngine.__init__
    monkeypatch.setattr(MatchEngine, "__init__", _fake_init)

    e = init_engine_with_pending()
    # 持倉 replay：2330.TW 100 股
    assert "2330.TW" in e.account.positions
    assert e.account.positions["2330.TW"].qty == 100
    # PENDING replay：1 筆 LMT SELL
    assert len(e.pending_orders) == 1
    pending_order = list(e.pending_orders.values())[0]
    assert pending_order.type == OrderType.LMT
    assert pending_order.side == OrderSide.SELL
    assert pending_order.price == 950.0


# ─── dispatch match：LMT 觸發撮合 ───

def test_engine_match_lmt_buy_triggered_after_replay(tmp_dir, monkeypatch):
    """Replay PENDING LMT BUY @ 900；當 quote 落到 ≤ 900 時 match 應撮合。"""
    fills = tmp_dir / "fills.jsonl"
    orders = tmp_dir / "orders.jsonl"
    fills.touch()
    _write_jsonl(orders, [
        _order_dict("o-lmt-buy", "PENDING", type_="LMT", price=900.0,
                    side="BUY", qty=100, symbol="2330.TW"),
    ])

    def _fake_init(self, *args, **kwargs):
        MatchEngine.__init_orig__(self, *args, **kwargs)
        self.fills_log_path = fills
        self.orders_log_path = orders
    if not hasattr(MatchEngine, "__init_orig__"):
        MatchEngine.__init_orig__ = MatchEngine.__init__
    monkeypatch.setattr(MatchEngine, "__init__", _fake_init)

    e = init_engine_with_pending()
    assert len(e.pending_orders) == 1

    # quote = 895 ≤ 900 → 撮合
    matched_fills = e.match({"2330.TW": 895.0})
    assert len(matched_fills) == 1
    e.apply_fills(matched_fills)
    # 撮合後持倉 100 股
    assert e.account.positions["2330.TW"].qty == 100
    # pending_orders 已清空
    assert len(e.pending_orders) == 0
    # fills.jsonl 寫進新一筆
    fill_lines = fills.read_text(encoding="utf-8").strip().splitlines()
    assert len(fill_lines) == 1
    assert "o-lmt-buy" in fill_lines[0]


# ─── cancel-all-pending：手動把所有 PENDING 改 CANCELLED ───

def test_engine_cancel_all_after_replay_writes_cancelled_status(tmp_dir, monkeypatch):
    """Replay 3 筆 PENDING + cancel_order 全部 → orders.jsonl 多 3 筆 CANCELLED。"""
    fills = tmp_dir / "fills.jsonl"
    orders = tmp_dir / "orders.jsonl"
    fills.touch()
    _write_jsonl(orders, [
        _order_dict("o1", "PENDING", type_="MKT"),
        _order_dict("o2", "PENDING", type_="LMT", price=900.0),
        _order_dict("o3", "PENDING", type_="MKT", side="SELL"),
    ])

    def _fake_init(self, *args, **kwargs):
        MatchEngine.__init_orig__(self, *args, **kwargs)
        self.fills_log_path = fills
        self.orders_log_path = orders
    if not hasattr(MatchEngine, "__init_orig__"):
        MatchEngine.__init_orig__ = MatchEngine.__init__
    monkeypatch.setattr(MatchEngine, "__init__", _fake_init)

    # SELL replay 需要持倉 — 加一筆持倉避免 cancel 時誤判
    from sim.models import Position
    e = init_engine_with_pending()
    e.account.positions["2330.TW"] = Position(
        symbol="2330.TW", qty=200, avg_cost=900.0, realized_pnl=0.0,
    )

    cancelled = 0
    for oid in list(e.pending_orders.keys()):
        if e.cancel_order(oid):
            cancelled += 1
    assert cancelled == 3

    # orders.jsonl 應有 3 筆 PENDING + 3 筆 CANCELLED = 6 行
    lines = orders.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 6
    final_states = {}
    for line in lines:
        d = json.loads(line)
        final_states[d["order_id"]] = d["status"]
    assert final_states == {"o1": "CANCELLED", "o2": "CANCELLED", "o3": "CANCELLED"}

    # 再 load_pending_orders 應回空
    pending = load_pending_orders(orders)
    assert pending == {}


# ─── cancel-stale-pending：EOD 清理只動日內 LMT/MKT，保留 STOP/TP 與 [auto-stop] ───

def test_cancel_stale_pending_skips_stop_and_auto_stop(tmp_dir, monkeypatch):
    """EOD 清理應留下：(a) STOP/TP 單；(b) notes 以 [auto-stop] 開頭的單。
    清掉：(c) 一般 LMT/MKT 殘留。
    """
    from sim.advisor.__main__ import cmd_dispatch
    from sim.models import Position

    fills = tmp_dir / "fills.jsonl"
    orders = tmp_dir / "orders.jsonl"
    fills.touch()
    _write_jsonl(orders, [
        # (c) 應被清：advisor 殘留的 LMT BUY
        _order_dict("stale-lmt-1", "PENDING", type_="LMT", price=240.0),
        _order_dict("stale-lmt-2", "PENDING", type_="LMT", price=241.5, qty=300),
        # (c) 應被清：MKT 殘留（罕見但邏輯要對）
        _order_dict("stale-mkt", "PENDING", type_="MKT", symbol="1101.TW"),
        # (a) 應保留：STOP SELL 持倉保護
        _order_dict("stop-protect", "PENDING", type_="STOP", side="SELL",
                    symbol="3034.TW", qty=400, price=428.87),
        # (b) 應保留：notes 標記 [auto-stop] 的 LMT（即使是 LMT 也雙重保險）
        {**_order_dict("auto-stop-lmt", "PENDING", type_="LMT",
                       symbol="2727.TW", qty=800, price=225.36),
         "notes": "[auto-stop] manual_backfill conservative_3pct"},
    ])

    def _fake_init(self, *args, **kwargs):
        MatchEngine.__init_orig__(self, *args, **kwargs)
        self.fills_log_path = fills
        self.orders_log_path = orders
    if not hasattr(MatchEngine, "__init_orig__"):
        MatchEngine.__init_orig__ = MatchEngine.__init__
    monkeypatch.setattr(MatchEngine, "__init__", _fake_init)

    # SELL/STOP replay 需要持倉
    from sim.advisor.runner import init_engine_with_pending as _real_init
    # 提早 init 一次取得 audit path 並注入持倉
    e_probe = _real_init()
    e_probe.account.positions["3034.TW"] = Position(
        symbol="3034.TW", qty=400, avg_cost=440.0, realized_pnl=0.0)
    e_probe.account.positions["2727.TW"] = Position(
        symbol="2727.TW", qty=800, avg_cost=230.0, realized_pnl=0.0)

    # 把 audit 寫到 tmp 避免污染真實 advisor_audit.jsonl
    audit_path = tmp_dir / "audit.jsonl"
    import sim.advisor.risk_guard as _rg
    monkeypatch.setattr(_rg, "DEFAULT_AUDIT_PATH", audit_path)

    # init_engine_with_pending 內部會 init 新 engine 並 replay PENDING；
    # SELL/STOP 驗證需要持倉，monkeypatch account.positions
    from sim.advisor import runner as _runner
    _orig_init_engine = _runner.init_engine_with_pending
    def _patched():
        e = _orig_init_engine()
        e.account.positions["3034.TW"] = Position(
            symbol="3034.TW", qty=400, avg_cost=440.0, realized_pnl=0.0)
        e.account.positions["2727.TW"] = Position(
            symbol="2727.TW", qty=800, avg_cost=230.0, realized_pnl=0.0)
        return e
    monkeypatch.setattr(_runner, "init_engine_with_pending", _patched)

    class _Args:
        force = True  # 繞過 is_trading_day（unit test 不應受日期影響）
        cancel_all_pending = False
        cancel_stale_pending = True
    rc = cmd_dispatch(_Args())
    assert rc == 0

    # 最終狀態：3 筆 CANCELLED（stale-*），2 筆仍 PENDING（stop-protect / auto-stop-lmt）
    pending_after = load_pending_orders(orders)
    surviving_ids = set(pending_after.keys())
    assert surviving_ids == {"stop-protect", "auto-stop-lmt"}, surviving_ids

    # audit 應記錄 mode=cancel_stale_pending + skipped_reasons
    audit_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert audit_lines, "audit 應至少寫一筆"
    last_audit = json.loads(audit_lines[-1])
    assert last_audit["mode"] == "cancel_stale_pending"
    assert last_audit["cancelled"] == 3
    assert last_audit["skipped"] == 2
    assert last_audit["skipped_reasons"]["is_stop_or_tp"] == 1
    assert last_audit["skipped_reasons"]["is_auto_stop"] == 1


def test_cancel_stale_pending_empty_pending_is_noop(tmp_dir, monkeypatch):
    """無 PENDING 時 cancel-stale-pending 應正常 noop 退出（不 raise、cancelled=0）。"""
    from sim.advisor.__main__ import cmd_dispatch

    fills = tmp_dir / "fills.jsonl"
    orders = tmp_dir / "orders.jsonl"
    fills.touch()
    orders.touch()

    def _fake_init(self, *args, **kwargs):
        MatchEngine.__init_orig__(self, *args, **kwargs)
        self.fills_log_path = fills
        self.orders_log_path = orders
    if not hasattr(MatchEngine, "__init_orig__"):
        MatchEngine.__init_orig__ = MatchEngine.__init__
    monkeypatch.setattr(MatchEngine, "__init__", _fake_init)

    audit_path = tmp_dir / "audit.jsonl"
    import sim.advisor.risk_guard as _rg
    monkeypatch.setattr(_rg, "DEFAULT_AUDIT_PATH", audit_path)

    class _Args:
        force = True  # 繞過 is_trading_day（unit test 不應受日期影響）
        cancel_all_pending = False
        cancel_stale_pending = True
    assert cmd_dispatch(_Args()) == 0
    last_audit = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last_audit["cancelled"] == 0
    assert last_audit["skipped"] == 0


# ─── M6.2 init_engine_with_pending 重算 reserved_cash ───

def test_init_engine_replays_reserved_cash_for_pending_buys(tmp_dir, monkeypatch):
    """replay PENDING BUY LMT → reserved_cash 應重算（不依賴 orders.jsonl 持久化此欄位）。"""
    fills = tmp_dir / "fills.jsonl"
    orders = tmp_dir / "orders.jsonl"
    fills.touch()
    _write_jsonl(orders, [
        _order_dict("buy-1", "PENDING", type_="LMT", price=900.0, qty=1000),
        _order_dict("buy-2", "PENDING", type_="LMT", price=50.0, qty=200, symbol="1101.TW"),
        # SELL 不該計入 reserved_cash
        _order_dict("sell-1", "PENDING", type_="LMT", side="SELL",
                    symbol="2330.TW", qty=100, price=950.0),
        # MKT BUY 沒 price 不該計入
        _order_dict("mkt-1", "PENDING", type_="MKT", qty=100, symbol="2454.TW"),
    ])

    def _fake_init(self, *args, **kwargs):
        MatchEngine.__init_orig__(self, *args, **kwargs)
        self.fills_log_path = fills
        self.orders_log_path = orders
    if not hasattr(MatchEngine, "__init_orig__"):
        MatchEngine.__init_orig__ = MatchEngine.__init__
    monkeypatch.setattr(MatchEngine, "__init__", _fake_init)

    # SELL replay 需要持倉
    from sim.models import Position
    from sim.advisor import runner as _runner
    _orig = _runner.init_engine_with_pending
    def _patched():
        e = _orig()
        e.account.positions["2330.TW"] = Position(
            symbol="2330.TW", qty=100, avg_cost=900.0, realized_pnl=0.0)
        return e

    e = init_engine_with_pending()
    # buy-1: 900×1000 + max(1282.5, 20) = 901282.5
    # buy-2: 50×200 + max(14.25, 20) = 10020
    # 合計 911302.5
    assert e.account.reserved_cash == pytest.approx(911302.5, rel=1e-4)
    assert "buy-1" in e._reserved_by_order
    assert "buy-2" in e._reserved_by_order
    assert "sell-1" not in e._reserved_by_order
    assert "mkt-1" not in e._reserved_by_order
