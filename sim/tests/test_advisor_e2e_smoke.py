"""sim/tests/test_advisor_e2e_smoke.py — 區塊 E 整合測試（mock 全鏈路）"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.runner import run_advisor_once, _decision_to_order, should_run_now
from sim.advisor._types import Decision
from sim.engine import MatchEngine
from sim.models import OrderType


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture(autouse=True)
def allow_new_positions_for_e2e(monkeypatch):
    """本檔測 buy plumbing；保守模式另有專測，這裡關閉避免遮蔽原測試目的。"""
    monkeypatch.setenv("SIM_ADVISOR_NO_NEW_POSITIONS_MODE", "0")


class FakeFeed:
    def __init__(self, fixed):
        self.fixed = fixed
    def get_quotes(self, symbols):
        return {s: self.fixed[s] for s in symbols if s in self.fixed}


class FakeClient:
    def __init__(self, raw_response, *, provider_id="fake", model="fake-model"):
        self.provider_id = provider_id
        self.model = model
        self._raw = raw_response
        self.calls = 0
    def decide(self, system_prompt, user_payload, **kw):
        self.calls += 1
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
        "formal_pools": {
            "short_term": short or [],
            "long_term": long or [],
        },
        "cross_signals": cross or {},
    }), encoding="utf-8")
    return p


# ─── _decision_to_order ───

def test_decision_to_order_buy_mkt():
    o = _decision_to_order(Decision(
        "buy", "2330.TW", 1000, "MKT",
        stop_loss=900.0,
        reason="短線突破且 cross_signals 命中",
        source_pools=["short_term", "cross_signals"],
    ))
    assert o is not None and o.qty == 1000 and o.type == OrderType.MKT
    assert o.notes.startswith("[advisor]")
    assert "短線突破" in o.notes
    assert "cross_signals" in o.notes


def test_decision_to_order_sell_lmt():
    o = _decision_to_order(Decision("sell", "2330.TW", 500, "LMT", price=920))
    assert o is not None and o.type == OrderType.LMT and o.price == 920


def test_decision_to_order_lmt_no_price_rejects():
    """LMT 沒 price → None"""
    o = _decision_to_order(Decision("buy", "2330.TW", 100, "LMT"))
    assert o is None


def test_decision_to_order_hold_returns_none():
    assert _decision_to_order(Decision("hold")) is None


def test_decision_to_order_zero_qty_rejects():
    assert _decision_to_order(Decision("buy", "2330.TW", 0, "MKT")) is None


# ─── should_run_now ───

def test_should_run_now_inside_session(tmp_dir):
    from datetime import datetime
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays: []\n", encoding="utf-8")
    assert should_run_now(datetime(2026, 5, 6, 10, 30), calendar_path=cal)  # Wed


def test_should_run_now_weekend_blocks(tmp_dir):
    from datetime import datetime
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays: []\n", encoding="utf-8")
    assert not should_run_now(datetime(2026, 5, 9, 10, 30), calendar_path=cal)  # Sat


def test_should_run_now_after_close(tmp_dir):
    from datetime import datetime
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays: []\n", encoding="utf-8")
    assert not should_run_now(datetime(2026, 5, 6, 14, 0), calendar_path=cal)


def test_should_run_now_holiday_blocks(tmp_dir):
    from datetime import datetime
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays:\n  - '2026-05-06'\n", encoding="utf-8")
    assert not should_run_now(datetime(2026, 5, 6, 10, 30), calendar_path=cal)


# ─── is_trading_day ───

def test_is_trading_day_weekday(tmp_dir):
    from datetime import date
    from sim.calendar import is_trading_day
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays: []\n", encoding="utf-8")
    assert is_trading_day(date(2026, 5, 6), calendar_path=cal)  # Wed


def test_is_trading_day_weekend(tmp_dir):
    from datetime import date
    from sim.calendar import is_trading_day
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays: []\n", encoding="utf-8")
    assert not is_trading_day(date(2026, 5, 9), calendar_path=cal)  # Sat
    assert not is_trading_day(date(2026, 5, 10), calendar_path=cal)  # Sun


def test_is_trading_day_holiday(tmp_dir):
    from datetime import date
    from sim.calendar import is_trading_day
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays:\n  - '2026-05-01'\n", encoding="utf-8")
    assert not is_trading_day(date(2026, 5, 1), calendar_path=cal)  # 勞動節


def test_is_trading_day_accepts_datetime(tmp_dir):
    from datetime import datetime
    from sim.calendar import is_trading_day
    cal = tmp_dir / "cal.yaml"
    cal.write_text("tw_holidays: []\n", encoding="utf-8")
    assert is_trading_day(datetime(2026, 5, 6, 17, 0), calendar_path=cal)  # Wed 17:00 — date only


def test_is_trading_day_no_calendar_file(tmp_dir):
    from datetime import date
    from sim.calendar import is_trading_day
    cal = tmp_dir / "nonexistent.yaml"
    assert is_trading_day(date(2026, 5, 6), calendar_path=cal)  # no file → no holidays → True


# ─── e2e dry-run happy ───

def test_e2e_dry_run_buy_passes_risk_guard(tmp_dir):
    e = _make_engine(tmp_dir)
    feed = FakeFeed({"2330.TW": 920.0, "0050.TW": 200.0})
    # score=85 才能通過 P0-2 buy quality gate（score ≥ 85 才放行）
    wl = _make_watchlist(tmp_dir, short=[{"symbol": "2330.TW", "score": 85}])

    raw = {
        "decisions": [
            {"action": "buy", "symbol": "2330.TW", "qty": 100, "type": "MKT",
             "stop_loss": 870, "reason": "test", "source_pools": ["short_term"]},
            {"action": "hold", "reason": "noise"},
        ],
        "summary": "test round",
    }
    client = FakeClient(raw)
    result = run_advisor_once(
        e, feed, client, dry_run=True,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=tmp_dir / "audit.jsonl",
    )
    assert result["status"] == "ok"
    assert result["decided"] == 2
    assert result["passed"] == 1
    assert result["hold"] == 1
    assert result["submitted"] == 0  # dry-run 不 submit
    assert result["blocked"] == 0


def test_e2e_dry_run_buy_blocked_by_concentration(tmp_dir):
    """單筆超 20% 集中度被擋"""
    e = _make_engine(tmp_dir)
    feed = FakeFeed({"2330.TW": 920.0, "0050.TW": 200.0})
    # 須先讓 buy 通過 P0-2 質量閘；用 cross_signals 命中放行
    wl = _make_watchlist(
        tmp_dir,
        short=[{"symbol": "2330.TW", "score": 85}],
        cross={"2330.TW": {"pools": ["short_term", "long_term"]}},
    )

    raw = {
        "decisions": [
            # 300×920 = 276K > 20% × 1M = 200K → reject
            {"action": "buy", "symbol": "2330.TW", "qty": 300, "type": "MKT",
             "stop_loss": 870, "reason": "test"},
        ],
    }
    client = FakeClient(raw)
    result = run_advisor_once(
        e, feed, client, dry_run=True,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=tmp_dir / "audit.jsonl",
    )
    assert result["blocked"] == 1
    assert result["passed"] == 0
    log = result["decisions_log"][0]
    assert log["result"] == "blocked"
    assert "concentration" in log["reason"]


def test_e2e_decide_failure_skips_round(tmp_dir):
    """client.decide 拋例外 → 整輪 skip + status=decide_failed"""
    e = _make_engine(tmp_dir)
    feed = FakeFeed({"0050.TW": 200.0})
    wl = _make_watchlist(tmp_dir)

    class FailingClient:
        provider_id = "fake"
        model = "fake-m"
        def decide(self, *a, **kw):
            raise RuntimeError("simulated API failure")

    result = run_advisor_once(
        e, feed, FailingClient(), dry_run=True,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=tmp_dir / "audit.jsonl",
    )
    assert result["status"] == "decide_failed"
    assert "simulated API failure" in result["error"]


def test_e2e_audit_written_with_provider_model(tmp_dir):
    """audit log 必須記 provider_id + model 給 retro 用"""
    e = _make_engine(tmp_dir)
    feed = FakeFeed({"2330.TW": 920.0, "0050.TW": 200.0})
    wl = _make_watchlist(tmp_dir)

    raw = {"decisions": [{"action": "hold", "reason": "x"}]}
    client = FakeClient(raw, provider_id="provX", model="modelY")
    audit = tmp_dir / "audit.jsonl"
    run_advisor_once(
        e, feed, client, dry_run=True,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=audit,
    )
    lines = audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    rec = json.loads(lines[-1])
    assert rec["type"] == "decision_round"
    assert rec["provider_id"] == "provX"
    assert rec["model"] == "modelY"
    assert rec["dry_run"] is True


# ─── M6.1：post-submit 立即撮合 ───

def test_e2e_post_submit_immediate_match_mkt_buy(tmp_dir):
    """live mode submit MKT BUY → 同一輪 match + apply_fills 寫進 fills.jsonl，
    持倉與 cash 立即更新（解 advisor / feishu_bot 跨進程撮合斷層）。
    """
    e = _make_engine(tmp_dir, watchlist=["2330.TW"])
    feed = FakeFeed({"2330.TW": 920.0, "0050.TW": 200.0})
    wl = _make_watchlist(tmp_dir, short=[{"symbol": "2330.TW", "score": 85}])

    raw = {
        "decisions": [
            {"action": "buy", "symbol": "2330.TW", "qty": 100, "type": "MKT",
             "stop_loss": 870, "reason": "MKT live submit"},
        ],
    }
    client = FakeClient(raw)

    cash_before = e.account.cash
    result = run_advisor_once(
        e, feed, client, dry_run=False,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=tmp_dir / "audit.jsonl",
    )

    assert result["status"] == "ok"
    assert result["submitted"] == 1
    assert result["matched_immediately"] == 1
    # fills.jsonl 寫了一筆
    fill_lines = e.fills_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(fill_lines) == 1
    fill = json.loads(fill_lines[0])
    assert fill["symbol"] == "2330.TW"
    assert fill["side"] == "BUY"
    assert fill["qty"] == 100
    # 持倉 + cash 已更新（apply_fills 跑過）
    assert "2330.TW" in e.account.positions
    assert e.account.positions["2330.TW"].qty == 100
    assert e.account.cash < cash_before  # 買進現金減少


def test_e2e_post_submit_immediate_match_mkt_sell_position_reduced(tmp_dir):
    """live mode SELL MKT → match 把持倉扣減、realized_pnl 寫入。"""
    from sim.models import Position

    e = _make_engine(tmp_dir, watchlist=["2330.TW"])
    # 起始持倉：2330 200 股 @ avg_cost 900；cash 抽掉 200×900 = 180,000
    e.account.positions["2330.TW"] = Position(
        symbol="2330.TW", qty=200, avg_cost=900.0, realized_pnl=0.0,
    )
    e.account.cash = e.account.initial_cash - 200 * 900
    cash_before = e.account.cash

    feed = FakeFeed({"2330.TW": 1000.0, "0050.TW": 200.0})  # 賣出價 > 成本
    wl = _make_watchlist(tmp_dir)

    raw = {
        "decisions": [
            {"action": "sell", "symbol": "2330.TW", "qty": 100, "type": "MKT",
             "reason": "take profit"},
        ],
    }
    client = FakeClient(raw)

    result = run_advisor_once(
        e, feed, client, dry_run=False,
        watchlist_path=wl, fills_path=e.fills_log_path,
        audit_path=tmp_dir / "audit.jsonl",
    )

    assert result["status"] == "ok"
    assert result["submitted"] == 1
    assert result["matched_immediately"] == 1
    # 持倉減 100 後剩 100 股
    assert e.account.positions["2330.TW"].qty == 100
    # 賣出後 cash 增加（成交價 ≈ 1000 - 滑價 - 手續費 - 證交稅，仍 > 0）
    assert e.account.cash > cash_before
    # realized_pnl 應 > 0（成本 900 賣 ~999 ≈ 賺 ~99 × 100 - fee/tax）
    assert e.account.total_realized_pnl > 0
