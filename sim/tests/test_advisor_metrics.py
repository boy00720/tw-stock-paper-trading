"""sim/tests/test_advisor_metrics.py — M6 階段二 區塊 F 測試"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.metrics import (
    compute_baseline_return,
    compute_four_pillars,
    compute_max_drawdown_pct,
    compute_profit_loss_ratio,
    load_excluded_dates,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ─────────────────────────────────────────────────────
# compute_max_drawdown_pct
# ─────────────────────────────────────────────────────


def test_mdd_missing_file_returns_zero(tmp_dir):
    assert compute_max_drawdown_pct(tmp_dir / "missing.jsonl") == 0.0


def test_mdd_single_point_returns_zero(tmp_dir):
    p = tmp_dir / "eq.jsonl"
    _write_jsonl(p, [{"time": "2026-05-06T09:00:00", "equity": 1000}])
    assert compute_max_drawdown_pct(p) == 0.0


def test_mdd_monotonic_up_returns_zero(tmp_dir):
    p = tmp_dir / "eq.jsonl"
    _write_jsonl(p, [
        {"time": f"2026-05-06T09:0{i}:00", "equity": 1000 + i * 10}
        for i in range(5)
    ])
    assert compute_max_drawdown_pct(p) == 0.0


def test_mdd_simple_drawdown(tmp_dir):
    p = tmp_dir / "eq.jsonl"
    _write_jsonl(p, [
        {"time": "2026-05-06T09:00:00", "equity": 1000},
        {"time": "2026-05-06T09:01:00", "equity": 1100},
        {"time": "2026-05-06T09:02:00", "equity": 900},
    ])
    # peak 1100 → trough 900 → dd = 200/1100 = 0.1818...
    assert compute_max_drawdown_pct(p) == pytest.approx(200 / 1100, rel=1e-6)


def test_mdd_takes_max_across_multiple_drawdowns(tmp_dir):
    p = tmp_dir / "eq.jsonl"
    _write_jsonl(p, [
        {"time": "2026-05-06T09:00:00", "equity": 1000},
        {"time": "2026-05-06T09:01:00", "equity": 950},   # dd 5%
        {"time": "2026-05-06T09:02:00", "equity": 1200},  # 新高
        {"time": "2026-05-06T09:03:00", "equity": 800},   # dd (1200-800)/1200 = 33.3%
        {"time": "2026-05-06T09:04:00", "equity": 1100},
    ])
    assert compute_max_drawdown_pct(p) == pytest.approx(400 / 1200, rel=1e-6)


def test_mdd_since_filter(tmp_dir):
    p = tmp_dir / "eq.jsonl"
    _write_jsonl(p, [
        {"time": "2026-04-01T09:00:00", "equity": 1000},  # 應被過濾
        {"time": "2026-04-01T09:01:00", "equity": 500},   # 應被過濾（這個會造大回撤但被過濾）
        {"time": "2026-05-06T09:00:00", "equity": 900},
        {"time": "2026-05-06T09:01:00", "equity": 990},
    ])
    # since=2026-05-01 → 只看後兩筆，無回撤
    assert compute_max_drawdown_pct(p, since=date(2026, 5, 1)) == 0.0


# ─────────────────────────────────────────────────────
# compute_profit_loss_ratio
# ─────────────────────────────────────────────────────


def test_pl_ratio_missing_fills_returns_zero(tmp_dir):
    result = compute_profit_loss_ratio(tmp_dir / "missing.jsonl")
    assert result == {"wins": 0, "losses": 0, "avg_win": 0.0, "avg_loss": 0.0, "ratio": 0.0}


def test_pl_ratio_only_wins_returns_inf(tmp_dir):
    p = tmp_dir / "fills.jsonl"
    _write_jsonl(p, [
        {"order_id": "o1", "symbol": "X.TW", "side": "BUY", "qty": 100, "price": 100.0,
         "fee": 0, "tax": 0, "fill_id": "f1", "filled_at": "2026-05-06T09:00:00"},
        {"order_id": "o2", "symbol": "X.TW", "side": "SELL", "qty": 100, "price": 110.0,
         "fee": 0, "tax": 0, "fill_id": "f2", "filled_at": "2026-05-06T10:00:00"},
    ])
    r = compute_profit_loss_ratio(p)
    assert r["wins"] == 1
    assert r["losses"] == 0
    assert r["ratio"] == float("inf")


def test_pl_ratio_only_losses(tmp_dir):
    p = tmp_dir / "fills.jsonl"
    _write_jsonl(p, [
        {"order_id": "o1", "symbol": "X.TW", "side": "BUY", "qty": 100, "price": 100.0,
         "fee": 0, "tax": 0, "fill_id": "f1", "filled_at": "2026-05-06T09:00:00"},
        {"order_id": "o2", "symbol": "X.TW", "side": "SELL", "qty": 100, "price": 90.0,
         "fee": 0, "tax": 0, "fill_id": "f2", "filled_at": "2026-05-06T10:00:00"},
    ])
    r = compute_profit_loss_ratio(p)
    assert r["wins"] == 0
    assert r["losses"] == 1
    assert r["ratio"] == 0.0
    assert r["avg_loss"] > 0  # avg_loss 是絕對值


def test_pl_ratio_mixed(tmp_dir):
    p = tmp_dir / "fills.jsonl"
    _write_jsonl(p, [
        # 倉位 A：賺 1000
        {"order_id": "o1", "symbol": "A.TW", "side": "BUY", "qty": 100, "price": 100.0,
         "fee": 0, "tax": 0, "fill_id": "f1", "filled_at": "2026-05-06T09:00:00"},
        {"order_id": "o2", "symbol": "A.TW", "side": "SELL", "qty": 100, "price": 110.0,
         "fee": 0, "tax": 0, "fill_id": "f2", "filled_at": "2026-05-06T10:00:00"},
        # 倉位 B：虧 500
        {"order_id": "o3", "symbol": "B.TW", "side": "BUY", "qty": 100, "price": 50.0,
         "fee": 0, "tax": 0, "fill_id": "f3", "filled_at": "2026-05-06T11:00:00"},
        {"order_id": "o4", "symbol": "B.TW", "side": "SELL", "qty": 100, "price": 45.0,
         "fee": 0, "tax": 0, "fill_id": "f4", "filled_at": "2026-05-06T12:00:00"},
    ])
    r = compute_profit_loss_ratio(p)
    assert r["wins"] == 1
    assert r["losses"] == 1
    # ratio = 1000 / 500 = 2.0
    assert r["ratio"] == pytest.approx(2.0, rel=1e-3)


# ─────────────────────────────────────────────────────
# compute_baseline_return（注入 yfinance 失敗）
# ─────────────────────────────────────────────────────


def test_baseline_yfinance_missing_returns_none(monkeypatch):
    """yfinance import 失敗 → 回 None。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert compute_baseline_return("0050.TW") is None


def test_baseline_history_failure_returns_none(monkeypatch):
    """yfinance.Ticker.history 拋例外 → 回 None。"""
    import sys as _sys
    import types
    fake_yf = types.SimpleNamespace()

    class FakeTicker:
        def __init__(self, *a, **kw):
            pass

        def history(self, **kwargs):
            raise RuntimeError("simulated network failure")

    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(_sys.modules, "yfinance", fake_yf)
    assert compute_baseline_return("0050.TW") is None


# ─────────────────────────────────────────────────────
# compute_four_pillars 整合
# ─────────────────────────────────────────────────────


def test_four_pillars_aggregation(tmp_dir, monkeypatch):
    """正常路徑：fills 含一勝一負、equity_curve 有回撤，四件套全部算對。"""
    fills = tmp_dir / "fills.jsonl"
    equity = tmp_dir / "eq.jsonl"

    _write_jsonl(fills, [
        # A 賺 1000
        {"order_id": "o1", "symbol": "A.TW", "side": "BUY", "qty": 100, "price": 100.0,
         "fee": 0, "tax": 0, "fill_id": "f1", "filled_at": "2026-05-06T09:00:00"},
        {"order_id": "o2", "symbol": "A.TW", "side": "SELL", "qty": 100, "price": 110.0,
         "fee": 0, "tax": 0, "fill_id": "f2", "filled_at": "2026-05-06T10:00:00"},
        # B 虧 500
        {"order_id": "o3", "symbol": "B.TW", "side": "BUY", "qty": 100, "price": 50.0,
         "fee": 0, "tax": 0, "fill_id": "f3", "filled_at": "2026-05-06T11:00:00"},
        {"order_id": "o4", "symbol": "B.TW", "side": "SELL", "qty": 100, "price": 45.0,
         "fee": 0, "tax": 0, "fill_id": "f4", "filled_at": "2026-05-06T12:00:00"},
    ])
    _write_jsonl(equity, [
        {"time": "2026-05-06T09:00:00", "equity": 100000},
        {"time": "2026-05-06T10:00:00", "equity": 110000},
        {"time": "2026-05-06T12:00:00", "equity": 95000},
    ])

    # baseline 失敗回 None，不影響其他欄位
    import sys as _sys
    import types
    fake_yf = types.SimpleNamespace()

    class FakeTicker:
        def __init__(self, *a, **kw):
            pass

        def history(self, **kwargs):
            raise RuntimeError("offline")

    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(_sys.modules, "yfinance", fake_yf)

    four = compute_four_pillars(fills, equity)

    # win_rate 應該真的有 1/2
    assert four["win_rate"]["total"] == 2
    assert four["win_rate"]["win"] == 1
    assert four["win_rate"]["rate"] == pytest.approx(0.5, rel=1e-3)

    # cum_realized_pnl ~= 1000 - 500 = 500
    assert four["cum_realized_pnl"] == pytest.approx(500, rel=1e-3)

    # MDD ~= (110000-95000)/110000
    assert four["max_drawdown_pct"] == pytest.approx(15000 / 110000, rel=1e-3)

    # 盈虧比 ratio ~= 1000/500 = 2.0
    assert four["profit_loss"]["ratio"] == pytest.approx(2.0, rel=1e-3)

    # baseline 應為 None（yfinance 失敗）
    assert four["baseline"]["return_pct"] is None
    assert four["baseline"]["symbol"] == "0050.TW"


def test_four_pillars_empty_data(tmp_dir, monkeypatch):
    """空 fills + 空 equity → 不 panic、所有欄位 0/None。"""
    fills = tmp_dir / "fills.jsonl"
    equity = tmp_dir / "eq.jsonl"
    fills.touch()
    equity.touch()

    import sys as _sys
    import types
    fake_yf = types.SimpleNamespace()

    class FakeTicker:
        def __init__(self, *a, **kw):
            pass

        def history(self, **kwargs):
            raise RuntimeError("offline")

    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(_sys.modules, "yfinance", fake_yf)

    four = compute_four_pillars(fills, equity)
    assert four["win_rate"]["total"] == 0
    assert four["cum_realized_pnl"] == 0.0
    assert four["max_drawdown_pct"] == 0.0
    assert four["profit_loss"]["ratio"] == 0.0
    assert four["baseline"]["return_pct"] is None


# ─── excluded_dates：把指定日期整日從 win_rate / pnl / MDD 排除 ───

def test_load_excluded_dates_missing_file_returns_empty(tmp_dir):
    assert load_excluded_dates(tmp_dir / "no.yaml") == set()


def test_load_excluded_dates_parses_valid_entries(tmp_dir):
    p = tmp_dir / "excl.yaml"
    p.write_text(
        "excluded_dates:\n"
        "  - date: '2026-05-08'\n"
        "    reason: 'brick'\n"
        "  - date: '2026-05-10'\n"
        "    reason: 'brick'\n",
        encoding="utf-8",
    )
    out = load_excluded_dates(p)
    assert out == {date(2026, 5, 8), date(2026, 5, 10)}


def test_load_excluded_dates_skips_malformed(tmp_dir):
    p = tmp_dir / "excl.yaml"
    p.write_text(
        "excluded_dates:\n"
        "  - date: '2026-05-08'\n"
        "    reason: 'brick'\n"
        "  - date: 'not-a-date'\n"
        "  - 'plain string'\n",
        encoding="utf-8",
    )
    out = load_excluded_dates(p)
    assert out == {date(2026, 5, 8)}


def test_mdd_excludes_specified_date(tmp_dir):
    """equity 在某天突然崩落，排除該日後 MDD 應顯著縮小。"""
    p = tmp_dir / "eq.jsonl"
    _write_jsonl(p, [
        {"time": "2026-05-07T13:30:00", "equity": 1_000_000.0},
        {"time": "2026-05-08T13:30:00", "equity": 500_000.0},   # 污染日（brick）
        {"time": "2026-05-11T13:30:00", "equity": 990_000.0},
    ])
    mdd_with_pollution = compute_max_drawdown_pct(p)
    mdd_cleaned = compute_max_drawdown_pct(p, excluded_dates={date(2026, 5, 8)})
    assert mdd_with_pollution > 0.49        # 1M → 500k：50% 回撤
    assert mdd_cleaned < 0.02               # 1M → 990k：≈1% 回撤
    assert mdd_cleaned < mdd_with_pollution


def test_pl_ratio_excludes_specified_date(tmp_dir):
    """brick 日的假 fill 報損益會嚴重歪曲 avg；排除後應回正常。"""
    p = tmp_dir / "fills.jsonl"
    _write_jsonl(p, [
        {"fill_id": "f1", "order_id": "o1", "symbol": "2330.TW", "side": "BUY",
         "qty": 1000, "price": 900.0, "fee": 0.0, "tax": 0.0,
         "filled_at": "2026-05-07T10:00:00"},
        {"fill_id": "f2", "order_id": "o2", "symbol": "2330.TW", "side": "SELL",
         "qty": 1000, "price": 950.0, "fee": 0.0, "tax": 0.0,
         "filled_at": "2026-05-07T13:00:00"},
        # 5/8 brick：產生 1 筆極端虧損（假設 advisor 撞到沒清空的舊資料）
        {"fill_id": "f3", "order_id": "o3", "symbol": "2330.TW", "side": "BUY",
         "qty": 1000, "price": 900.0, "fee": 0.0, "tax": 0.0,
         "filled_at": "2026-05-08T10:00:00"},
        {"fill_id": "f4", "order_id": "o4", "symbol": "2330.TW", "side": "SELL",
         "qty": 1000, "price": 100.0, "fee": 0.0, "tax": 0.0,
         "filled_at": "2026-05-08T13:00:00"},
    ])
    full = compute_profit_loss_ratio(p)
    cleaned = compute_profit_loss_ratio(p, excluded_dates={date(2026, 5, 8)})
    assert full["losses"] == 1
    assert cleaned["losses"] == 0
    assert cleaned["wins"] == 1


def test_four_pillars_auto_loads_excluded_dates(tmp_dir, monkeypatch):
    """compute_four_pillars 不指定 excluded_dates 應自動讀 config；payload 回 excluded_dates。"""
    excl_file = tmp_dir / "excl.yaml"
    excl_file.write_text(
        "excluded_dates:\n  - date: '2026-05-08'\n    reason: 'brick'\n",
        encoding="utf-8",
    )
    # 改 default path 指向 tmp
    import sim.advisor.metrics as _m
    monkeypatch.setattr(_m, "DEFAULT_EXCLUDED_DATES_PATH", excl_file)

    fills = tmp_dir / "fills.jsonl"
    equity = tmp_dir / "eq.jsonl"
    fills.touch()
    equity.touch()

    import sys as _sys
    import types
    fake_yf = types.SimpleNamespace()

    class FakeTicker:
        def __init__(self, *a, **kw):
            pass

        def history(self, **kwargs):
            raise RuntimeError("offline")
    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(_sys.modules, "yfinance", fake_yf)

    four = compute_four_pillars(fills, equity)
    assert four["excluded_dates"] == ["2026-05-08"]


def test_four_pillars_explicit_empty_excluded_overrides_config(tmp_dir, monkeypatch):
    """傳 excluded_dates=set() 應視為「不排除」，不讀 config。"""
    excl_file = tmp_dir / "excl.yaml"
    excl_file.write_text(
        "excluded_dates:\n  - date: '2026-05-08'\n    reason: 'brick'\n",
        encoding="utf-8",
    )
    import sim.advisor.metrics as _m
    monkeypatch.setattr(_m, "DEFAULT_EXCLUDED_DATES_PATH", excl_file)

    fills = tmp_dir / "fills.jsonl"
    equity = tmp_dir / "eq.jsonl"
    fills.touch()
    equity.touch()

    import sys as _sys
    import types
    fake_yf = types.SimpleNamespace()

    class FakeTicker:
        def __init__(self, *a, **kw):
            pass

        def history(self, **kwargs):
            raise RuntimeError("offline")
    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(_sys.modules, "yfinance", fake_yf)

    four = compute_four_pillars(fills, equity, excluded_dates=set())
    assert four["excluded_dates"] == []
