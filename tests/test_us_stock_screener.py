from __future__ import annotations

from scripts import us_stock_screener as uss


def _rising_bars(n: int = 70, start: float = 100.0):
    """穩定上升的日K（多頭排列 + 正動量）。"""
    return [{"close": start + i, "high": start + i + 1, "low": start + i - 1,
             "volume": 1_000_000} for i in range(n)]


def _falling_bars(n: int = 70, start: float = 200.0):
    """下降趨勢的日K（弱結構）。"""
    return [{"close": start - i, "high": start - i + 1, "low": start - i - 1,
             "volume": 1_000_000} for i in range(n)]


def test_screener_disabled_returns_none(monkeypatch):
    monkeypatch.delenv(uss.US_STOCK_SCREENER_ENABLED_ENV, raising=False)
    assert uss.run_screener("intraday") is None


def test_compute_insufficient_bars_returns_none():
    rows = [{"close": 100, "high": 101, "low": 99, "volume": 1000} for _ in range(30)]
    assert uss._compute("TEST", rows, None, []) is None


def test_compute_strong_stock_scores_high():
    cand = uss._compute("STRONG", _rising_bars(), spy_ret_20d=2.0, sources=["MOST_ACTIVE"])
    assert cand is not None
    assert cand.ma_stacked is True
    assert cand.change_20d_pct > 0
    assert cand.rs_vs_spy > 0          # 漲幅強於 SPY
    assert cand.score > 50


def test_compute_weak_stock_scores_low():
    cand = uss._compute("WEAK", _falling_bars(), spy_ret_20d=2.0, sources=["TOP_PERC_GAIN"])
    assert cand is not None
    assert cand.ma_stacked is False
    assert cand.score < 20


def test_run_screener_excludes_benchmark_etf_and_ranks(monkeypatch):
    monkeypatch.setenv(uss.US_STOCK_SCREENER_ENABLED_ENV, "1")

    class _Row:
        def __init__(self, sym):
            self.symbol = sym
            self.rank = 0
            self.con_id = 1
            self.exchange = "NASDAQ"

    class _Bars:
        def __init__(self, rows):
            self.rows = rows

    strong, weak = _rising_bars(), _falling_bars()

    class _FakeProvider:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch_scanner(self, code, **kwargs):
            return [_Row("SPY"), _Row("STRONG"), _Row("WEAK")]

        def fetch_historical_bars(self, symbol, **kwargs):
            return _Bars(weak if symbol == "WEAK" else strong)

    monkeypatch.setattr(uss, "_make_provider", lambda: _FakeProvider())
    monkeypatch.setattr(uss.time, "sleep", lambda *a, **k: None)

    picks = uss.run_screener("intraday", dry_run=False)
    syms = [c.symbol for c in picks]

    assert "SPY" not in syms            # 基準 ETF 被排除
    assert "STRONG" in syms
    assert picks[0].symbol == "STRONG"  # 強勢排第一


def test_run_screener_empty_scan_returns_empty(monkeypatch):
    monkeypatch.setenv(uss.US_STOCK_SCREENER_ENABLED_ENV, "1")

    class _FakeProvider:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch_scanner(self, code, **kwargs):
            return []

        def fetch_historical_bars(self, symbol, **kwargs):
            raise AssertionError("無候選時不應取歷史 bar")

    monkeypatch.setattr(uss, "_make_provider", lambda: _FakeProvider())
    assert uss.run_screener("intraday", dry_run=False) == []
