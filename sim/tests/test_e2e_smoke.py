"""
M1 Day 3 整合測試：模擬 e2e 流程 + Feed 行為測試。

測試策略：
- 不打真實 yfinance（避免 CI / 離線跑時失敗）
- 用 MockFeed / FakeFeedWithFailures 替代
- 整合測試聚焦：訂單 → 撮合 → 套用 → 帳戶狀態 全鏈路

執行：
    python3 sim/tests/test_e2e_smoke.py        # 自包執行
    python3 -m pytest sim/tests/test_e2e_smoke.py
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.engine import MatchEngine
from sim.feed import QuoteFeed
from sim.models import Order, OrderSide, OrderStatus, OrderType
from sim.runner import run_loop, run_once


def _make_engine(tmp_dir: Path) -> MatchEngine:
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    return e


# ─────────────────────────────────────────────────────
# Mock feeds
# ─────────────────────────────────────────────────────

class MockFeed:
    """直接吞固定 quotes dict 的假 feed，可動態改變 quotes 模擬報價變動。"""
    def __init__(self, quotes: dict):
        self.quotes = dict(quotes)
        self.call_count = 0

    def get_quotes(self, symbols):
        self.call_count += 1
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


class FakeFeedWithFailures(QuoteFeed):
    """繼承 QuoteFeed，覆寫 _fetch 模擬失敗 + last_known fallback 行為"""
    def __init__(self, fixed: dict, fail_symbols: set = None):
        super().__init__(cache_ttl_sec=0.0)  # 不用快取，每次都呼叫 _fetch
        self.fixed = fixed
        self.fail_symbols = set(fail_symbols or [])
        self.fetch_calls = 0

    def _fetch(self, symbols):
        self.fetch_calls += 1
        return {s: (None if s in self.fail_symbols else self.fixed.get(s)) for s in symbols}


# ─────────────────────────────────────────────────────
# E2E：完整下單 → 撮合 → 持倉
# ─────────────────────────────────────────────────────

def test_e2e_market_buy_flow(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 600.0})

    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100)
    )
    n = run_once(e, feed)

    assert n == 1
    assert o.status == OrderStatus.FILLED
    assert "2330.TW" in e.account.positions
    assert e.account.positions["2330.TW"].qty == 100


def test_e2e_limit_waits_then_fills(tmp_dir):
    """限價單先不成交，價格到位後成交"""
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 590.0})

    o = e.submit_order(
        Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.LMT, qty=100, price=580)
    )

    # 第 1 輪：590 > 580，不成交
    assert run_once(e, feed) == 0
    assert o.order_id in e.pending_orders

    # 報價跌到 575 → 第 2 輪成交
    feed.quotes["2330.TW"] = 575.0
    assert run_once(e, feed) == 1
    assert "2330.TW" in e.account.positions
    assert o.status == OrderStatus.FILLED


def test_e2e_buy_then_partial_sell(tmp_dir):
    """買 100 → 賣 50 → 剩 50、已實現損益累積"""
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 600.0})

    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    run_once(e, feed)

    feed.quotes["2330.TW"] = 620.0
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.SELL, type=OrderType.MKT, qty=50))
    run_once(e, feed)

    assert e.account.positions["2330.TW"].qty == 50
    assert e.account.total_realized_pnl > 0


def test_run_loop_stops_after_iterations(tmp_dir):
    """run_loop 用 stop_after_iterations 控制圈數，interval=0 不睡"""
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 600.0})

    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    run_loop(e, feed, interval_sec=0, stop_after_iterations=3, ignore_market_hours=True)

    # 第 1 輪就會撮合，後 2 輪 pending 為空，呼叫 feed 也 OK
    assert "2330.TW" in e.account.positions
    assert feed.call_count == 3


def test_run_once_no_watchlist_returns_zero(tmp_dir):
    """watchlist 為空時 run_once 直接回 0，不打 feed"""
    e = _make_engine(tmp_dir)
    e.config["watchlist"] = []
    feed = MockFeed({"2330.TW": 600.0})

    assert run_once(e, feed) == 0
    assert feed.call_count == 0


def test_run_loop_swallows_per_iteration_exception(tmp_dir):
    """單輪內例外不應中斷 run_loop"""
    e = _make_engine(tmp_dir)

    class BoomFeed:
        def __init__(self):
            self.call = 0
        def get_quotes(self, symbols):
            self.call += 1
            if self.call == 1:
                raise RuntimeError("simulated network glitch")
            return {"2330.TW": 600.0}

    feed = BoomFeed()
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    # 跑 2 輪：第 1 輪炸、第 2 輪正常
    run_loop(e, feed, interval_sec=0, stop_after_iterations=2, ignore_market_hours=True)

    assert "2330.TW" in e.account.positions  # 第 2 輪救回來


# ─────────────────────────────────────────────────────
# M5：on_fill callback + equity_curve.jsonl
# ─────────────────────────────────────────────────────

def test_run_loop_invokes_on_fill_callback(tmp_dir):
    """成交時 on_fill 被呼叫，收到 fill_dict + snapshot。"""
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 600.0})
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))

    captured: list[tuple[dict, dict]] = []

    def _on_fill(fill_dict, snapshot):
        captured.append((fill_dict, snapshot))

    run_loop(e, feed, interval_sec=0, stop_after_iterations=1, on_fill=_on_fill, ignore_market_hours=True)

    assert len(captured) == 1
    fill_dict, snapshot = captured[0]
    assert fill_dict["symbol"] == "2330.TW"
    assert fill_dict["side"] == "BUY"
    assert fill_dict["qty"] == 100
    assert "fill_id" in fill_dict
    # snapshot 結構應對齊 engine.snapshot()
    assert "account" in snapshot
    assert snapshot["account"]["cash"] < e.account.initial_cash


def test_run_loop_on_fill_exception_does_not_break_loop(tmp_dir):
    """on_fill 拋例外時 runner 仍能跑完並繼續下一輪。"""
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 600.0})
    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))

    def _bad_on_fill(fill_dict, snapshot):
        raise RuntimeError("intentional callback failure")

    # 應正常 return（不 propagate）
    run_loop(e, feed, interval_sec=0, stop_after_iterations=2, on_fill=_bad_on_fill, ignore_market_hours=True)
    # 第 1 輪成交且 callback 例外被捕，position 仍正常
    assert "2330.TW" in e.account.positions


def test_run_loop_writes_equity_curve(tmp_dir):
    """傳 equity_curve_path 跑 2 圈，jsonl 應有 2 行欄位完整記錄。"""
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 600.0})
    eq_path = tmp_dir / "equity_curve.jsonl"

    e.submit_order(Order(symbol="2330.TW", side=OrderSide.BUY, type=OrderType.MKT, qty=100))
    run_loop(
        e, feed,
        interval_sec=0,
        stop_after_iterations=2,
        equity_curve_path=eq_path,
        ignore_market_hours=True,
    )

    assert eq_path.exists()
    with open(eq_path, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 2
    for rec in lines:
        assert "time" in rec
        assert "cash" in rec
        assert "equity" in rec
        assert "position_value" in rec
        assert "total_realized_pnl" in rec
    # 第 2 圈時已成交，position_value 應 > 0（mark-to-market 用 quote 600）
    assert lines[1]["position_value"] > 0


def test_run_loop_writes_equity_curve_even_without_fills(tmp_dir):
    """沒成交那輪也寫 equity_curve（避免「持倉浮動但曲線不動」鋸齒；PLAN 決策 D2）。"""
    e = _make_engine(tmp_dir)
    feed = MockFeed({"2330.TW": 600.0})
    eq_path = tmp_dir / "equity_curve.jsonl"

    # 不下單 → 沒成交
    run_loop(e, feed, interval_sec=0, stop_after_iterations=3, equity_curve_path=eq_path, ignore_market_hours=True)
    with open(eq_path, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 3  # 每輪都寫，包含沒成交那輪


# ─────────────────────────────────────────────────────
# Feed：快取與 fallback
# ─────────────────────────────────────────────────────

def test_feed_cache_within_ttl(tmp_dir):
    """快取內不重複打 _fetch"""
    feed = FakeFeedWithFailures({"2330.TW": 600.0})
    feed.cache_ttl = 999.0  # 永遠在快取內

    feed.get_quotes(["2330.TW"])
    feed.get_quotes(["2330.TW"])
    feed.get_quotes(["2330.TW"])

    assert feed.fetch_calls == 1  # 只打一次


def test_feed_fallback_to_last_known(tmp_dir):
    """拉失敗時 fallback 到 last_known"""
    feed = FakeFeedWithFailures({"2330.TW": 600.0}, fail_symbols=set())
    # 第 1 次成功 → 建立 last_known
    r1 = feed.get_quotes(["2330.TW"])
    assert r1["2330.TW"] == 600.0

    # 第 2 次模擬失敗 → 應 fallback
    feed.fail_symbols.add("2330.TW")
    r2 = feed.get_quotes(["2330.TW"])
    assert r2["2330.TW"] == 600.0  # fallback 到 last_known


def test_feed_first_fetch_fail_no_entry(tmp_dir):
    """從沒成功過的 symbol 第一次就失敗 → result 不含此 key"""
    feed = FakeFeedWithFailures({"2330.TW": 600.0}, fail_symbols={"BAD.TW"})
    result = feed.get_quotes(["2330.TW", "BAD.TW"])
    assert "2330.TW" in result
    assert "BAD.TW" not in result


def test_feed_last_known_helper(tmp_dir):
    """last_known() 能查到上次成功的價"""
    feed = FakeFeedWithFailures({"2330.TW": 600.0})
    feed.get_quotes(["2330.TW"])
    assert feed.last_known("2330.TW") == 600.0
    assert feed.last_known("UNKNOWN.TW") is None


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


# pytest fixture
try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass
