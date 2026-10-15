"""sim/tests/test_advisor_signals.py — 區塊 B 測試"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.signals import (
    DEFAULT_FILLS_PATH,
    DEFAULT_WATCHLIST_PATH,
    _compress_payload,
    _empty_pools,
    _load_pending_orders_summary,
    _tail_fills,
    build_decision_context,
    load_watchlist_pools,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class FakeFeed:
    def __init__(self, fixed=None, raise_on_call=False):
        self.fixed = fixed or {}
        self.raise_on_call = raise_on_call
        self.calls = 0

    def get_quotes(self, symbols):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("simulated feed failure")
        return {s: self.fixed.get(s, 100.0) for s in symbols}


# ─── load_watchlist_pools ───

def test_load_pools_default(tmp_dir):
    """讀真實 logs/watchlist/current.json 應拿到 31 短線 + 12 長線"""
    if not DEFAULT_WATCHLIST_PATH.exists():
        pytest.skip("default watchlist 不存在")
    pools = load_watchlist_pools()
    assert isinstance(pools["short_term"], list)
    assert isinstance(pools["long_term"], list)
    assert isinstance(pools["cross_signals"], dict)
    # schema 完整
    assert "summary" in pools
    assert "updated_at" in pools


def test_load_pools_missing_path(tmp_dir):
    """檔不存在 → 回空池 dict 不 raise"""
    pools = load_watchlist_pools(tmp_dir / "nope.json")
    assert pools == _empty_pools()


def test_load_pools_corrupt_json(tmp_dir):
    """JSON 損壞 → 回空池 dict 不 raise"""
    p = tmp_dir / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    pools = load_watchlist_pools(p)
    assert pools["short_term"] == []
    assert pools["cross_signals"] == {}


def test_load_pools_minimal(tmp_dir):
    p = tmp_dir / "min.json"
    p.write_text(json.dumps({
        "formal_pools": {"short_term": [{"symbol": "2330.TW", "score": 70}]},
        "auxiliary_signals": {"leveraged_etf": [{"symbol": "00631L.TW", "score": 90}]},
        "cross_signals": {"2330.TW": {"pools": ["short_term", "long_term"]}},
    }), encoding="utf-8")
    pools = load_watchlist_pools(p)
    assert len(pools["short_term"]) == 1
    assert pools["leveraged_etf"][0]["symbol"] == "00631L.TW"
    assert pools["cross_signals"]["2330.TW"]["pools"] == ["short_term", "long_term"]


# ─── build_decision_context ───

def test_build_context_no_engine_no_holdings(tmp_dir):
    p = tmp_dir / "wl.json"
    p.write_text(json.dumps({
        "formal_pools": {"short_term": [{"symbol": "1101.TW"}]},
        "auxiliary_signals": {"leveraged_etf": [{"symbol": "00631L.TW", "score": 90}]},
    }), encoding="utf-8")
    feed = FakeFeed({"1101.TW": 50.0, "00631L.TW": 37.5, "0050.TW": 200.0})
    ctx = build_decision_context(None, feed, watchlist_path=p, fills_path=tmp_dir / "fills.jsonl")
    assert ctx["account"] == {}
    assert "1101.TW" in ctx["quotes"]
    assert "00631L.TW" in ctx["quotes"]
    assert ctx["market_context"]["baseline_price"] == 200.0


def test_build_context_with_baseline_and_holdings(tmp_dir):
    """含持倉 + baseline 候選一起拉報價"""
    from sim.engine import MatchEngine
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    from sim.models import Position
    e.account.positions["2330.TW"] = Position(symbol="2330.TW", qty=1000, avg_cost=900.0)

    p = tmp_dir / "wl.json"
    p.write_text(json.dumps({
        "formal_pools": {"short_term": [{"symbol": "1101.TW"}]},
    }), encoding="utf-8")

    feed = FakeFeed({"2330.TW": 920.0, "1101.TW": 50.0, "0050.TW": 200.0})
    ctx = build_decision_context(e, feed, watchlist_path=p, fills_path=e.fills_log_path)
    assert ctx["account"]["positions"]["2330.TW"]["qty"] == 1000
    assert ctx["quotes"]["2330.TW"] == 920.0
    assert ctx["quotes"]["0050.TW"] == 200.0  # baseline 自動加入
    assert ctx["market_context"]["baseline_price"] == 200.0


def test_build_context_feed_failure(tmp_dir):
    """feed 拋例外 → quotes 為空，但 ctx 仍可組成（不 raise）"""
    p = tmp_dir / "wl.json"
    p.write_text("{}", encoding="utf-8")
    feed = FakeFeed(raise_on_call=True)
    ctx = build_decision_context(None, feed, watchlist_path=p, fills_path=tmp_dir / "fills.jsonl")
    assert ctx["quotes"] == {}


# ─── _tail_fills ───

def test_tail_fills_empty(tmp_dir):
    assert _tail_fills(tmp_dir / "no.jsonl", 10) == []


def test_tail_fills_truncate(tmp_dir):
    p = tmp_dir / "f.jsonl"
    lines = [json.dumps({"i": i}) for i in range(50)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tail = _tail_fills(p, 5)
    assert len(tail) == 5
    assert [t["i"] for t in tail] == [45, 46, 47, 48, 49]


def test_tail_fills_skip_corrupt_line(tmp_dir):
    p = tmp_dir / "f.jsonl"
    p.write_text(
        json.dumps({"i": 1}) + "\n"
        + "garbage line\n"
        + json.dumps({"i": 2}) + "\n",
        encoding="utf-8",
    )
    tail = _tail_fills(p, 10)
    assert [t["i"] for t in tail] == [1, 2]


# ─── _load_pending_orders_summary ───

def test_pending_summary_missing_file(tmp_dir):
    assert _load_pending_orders_summary(tmp_dir / "no.jsonl") == []


def test_pending_summary_last_status_wins(tmp_dir):
    """orders.jsonl 是 append log；某 order_id 先 PENDING 後 FILLED 不該入列。"""
    p = tmp_dir / "orders.jsonl"
    p.write_text(
        json.dumps({"order_id": "o1", "symbol": "2330.TW", "side": "buy", "type": "LMT",
                    "qty": 1000, "price": 900.0, "status": "PENDING",
                    "created_at": "2026-05-10T13:25:00", "notes": ""}) + "\n"
        + json.dumps({"order_id": "o1", "symbol": "2330.TW", "side": "buy", "type": "LMT",
                      "qty": 1000, "price": 900.0, "status": "FILLED",
                      "created_at": "2026-05-10T13:25:00", "notes": ""}) + "\n"
        + json.dumps({"order_id": "o2", "symbol": "1101.TW", "side": "buy", "type": "LMT",
                      "qty": 2000, "price": 50.0, "status": "PENDING",
                      "created_at": "2026-05-10T14:00:00", "notes": "carry overnight"}) + "\n"
        + json.dumps({"order_id": "o3", "symbol": "2454.TW", "side": "sell", "type": "STOP",
                      "qty": 1000, "price": 1100.0, "status": "PENDING",
                      "created_at": "2026-05-10T14:30:00", "notes": "[auto-stop] 2330 保護"}) + "\n",
        encoding="utf-8",
    )
    summary = _load_pending_orders_summary(p)
    ids = sorted(s["order_id"] for s in summary)
    assert ids == ["o2", "o3"]
    by_id = {s["order_id"]: s for s in summary}
    assert by_id["o2"]["symbol"] == "1101.TW"
    assert by_id["o3"]["notes"].startswith("[auto-stop]")
    # 確保只挑允許欄位（不會洩漏 status）
    for s in summary:
        assert "status" not in s


def test_pending_summary_corrupt_lines_skipped(tmp_dir):
    p = tmp_dir / "orders.jsonl"
    p.write_text(
        "garbage\n"
        + json.dumps({"order_id": "o1", "symbol": "2330.TW", "side": "buy", "type": "LMT",
                      "qty": 1000, "price": 900.0, "status": "PENDING",
                      "created_at": "x", "notes": ""}) + "\n",
        encoding="utf-8",
    )
    summary = _load_pending_orders_summary(p)
    assert len(summary) == 1
    assert summary[0]["order_id"] == "o1"


def test_build_context_injects_pending_orders(tmp_dir):
    """build_decision_context 應把 PENDING 注入 payload.pending_orders。"""
    wl = tmp_dir / "wl.json"
    wl.write_text("{}", encoding="utf-8")
    orders = tmp_dir / "orders.jsonl"
    orders.write_text(
        json.dumps({"order_id": "p1", "symbol": "2330.TW", "side": "buy", "type": "LMT",
                    "qty": 1000, "price": 900.0, "status": "PENDING",
                    "created_at": "2026-05-10T13:25:00", "notes": ""}) + "\n",
        encoding="utf-8",
    )
    feed = FakeFeed({"0050.TW": 200.0})
    ctx = build_decision_context(
        None, feed,
        watchlist_path=wl,
        fills_path=tmp_dir / "fills.jsonl",
        orders_log_path=orders,
    )
    assert "pending_orders" in ctx
    assert len(ctx["pending_orders"]) == 1
    assert ctx["pending_orders"][0]["symbol"] == "2330.TW"


# ─── risk_state（P0-1：讓 LLM 不再把累計 PnL 誤判為日內熔斷） ───

def test_build_context_risk_state_no_engine(tmp_dir):
    """無 engine 時 risk_state 仍存在；today/threshold/remaining 為 None，circuit_pct 帶上"""
    wl = tmp_dir / "wl.json"
    wl.write_text("{}", encoding="utf-8")
    feed = FakeFeed({"0050.TW": 200.0})
    ctx = build_decision_context(
        None, feed,
        watchlist_path=wl,
        fills_path=tmp_dir / "fills.jsonl",
        circuit_pct=0.025,
        circuit_active_today=False,
    )
    rs = ctx["risk_state"]
    assert rs["circuit_pct"] == 0.025
    assert rs["circuit_active_today"] is False
    assert rs["circuit_threshold"] is None
    assert rs["circuit_remaining"] is None
    # 無 fills 檔 → today_pnl 走 fallback 0.0
    assert rs["today_realized_pnl"] == 0.0


def test_build_context_risk_state_with_engine_no_fills(tmp_dir):
    """有 engine、無今日 fills → today_pnl=0；threshold/remaining 用 initial_cash 算"""
    from sim.engine import MatchEngine
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"

    wl = tmp_dir / "wl.json"
    wl.write_text("{}", encoding="utf-8")
    feed = FakeFeed({"0050.TW": 200.0})
    ctx = build_decision_context(
        e, feed,
        watchlist_path=wl,
        fills_path=e.fills_log_path,
        circuit_pct=0.03,
    )
    rs = ctx["risk_state"]
    assert rs["today_realized_pnl"] == 0.0
    # initial_cash=1,000,000（sim_config 預設） × 0.03 = 30,000 → threshold = -30000
    assert rs["circuit_threshold"] == pytest.approx(-30000.0)
    # remaining = today_pnl - threshold = 0 - (-30000) = 30000（還有 30k 緩衝）
    assert rs["circuit_remaining"] == pytest.approx(30000.0)
    assert rs["circuit_active_today"] is False


def test_build_context_risk_state_active_today(tmp_dir):
    """circuit_active_today=True 必須真實出現在 payload"""
    from sim.engine import MatchEngine
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    wl = tmp_dir / "wl.json"
    wl.write_text("{}", encoding="utf-8")
    feed = FakeFeed({"0050.TW": 200.0})
    ctx = build_decision_context(
        e, feed,
        watchlist_path=wl,
        fills_path=e.fills_log_path,
        circuit_active_today=True,
    )
    assert ctx["risk_state"]["circuit_active_today"] is True


# ─── _compress_payload ───

def test_compress_strips_short_term_breakdown():
    payload = {
        "pools": {
            "short_term": [
                {"symbol": "2330.TW", "score": 70, "breakdown": {"vol": 8, "rsi": 0}},
                {"symbol": "1101.TW", "score": 60, "breakdown": {"vol": 5, "rsi": 5}},
            ],
            "long_term": [{"symbol": "2357.TW"}],
        },
    }
    out = _compress_payload(payload)
    for item in out["pools"]["short_term"]:
        assert "breakdown" not in item
        assert "score" in item  # 其他欄位保留
    # long_term 不動
    assert out["pools"]["long_term"][0]["symbol"] == "2357.TW"
