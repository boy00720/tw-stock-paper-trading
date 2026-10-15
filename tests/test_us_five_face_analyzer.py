from __future__ import annotations

from scripts import us_five_face_analyzer as ff


def _rising_bars(n=70, start=100.0):
    return [{"close": start + i, "high": start + i + 1, "low": start + i - 1,
             "volume": 2_000_000} for i in range(n)]


def _falling_bars(n=70, start=200.0):
    return [{"close": start - i, "high": start - i + 1, "low": start - i - 1,
             "volume": 1_000_000} for i in range(n)]


class _FakeBars:
    def __init__(self, rows):
        self.rows = rows


class _FakeProvider:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetch_historical_bars(self, symbol, **kwargs):
        return _FakeBars(self._rows)


def test_disabled_returns_none(monkeypatch):
    monkeypatch.delenv(ff.US_FIVE_FACE_ENABLED_ENV, raising=False)
    assert ff.analyze("NVDA") is None


def test_technical_strong_vs_weak():
    strong = ff._face_technical(_FakeProvider(_rising_bars()), "X", spy_ret20=2.0)
    weak = ff._face_technical(_FakeProvider(_falling_bars()), "Y", spy_ret20=2.0)
    assert strong.available and weak.available
    assert strong.score > 60
    assert weak.score < 40
    assert strong.score > weak.score


def test_technical_missing_latest_volume_matches_shared_metrics():
    rows = _rising_bars()
    rows[-1]["volume"] = None
    result = ff._face_technical(_FakeProvider(rows), "X", spy_ret20=2.0)
    assert result.available is True
    assert result.metrics["vol_ratio"] is None


def test_technical_insufficient_bars_unavailable():
    r = ff._face_technical(_FakeProvider([{"close": 1, "high": 1, "low": 1, "volume": 1}] * 30), "Z", None)
    assert r.available is False


def test_valuation_cheap_beats_expensive():
    cheap = ff._face_valuation({"pegRatio": 0.6, "forwardPE": 14})
    pricey = ff._face_valuation({"pegRatio": 4.0, "forwardPE": 60})
    assert cheap.available and cheap.score > 70
    assert pricey.score < cheap.score


def test_valuation_no_data_unavailable():
    assert ff._face_valuation({}).available is False


def test_dividend_yield_prefers_rate_over_price():
    ratio, source = ff._dividend_yield_ratio({
        "dividendRate": 1.08,
        "currentPrice": 315.32,
        "dividendYield": 99.0,
    })
    assert round(ratio, 6) == 0.003425
    assert source == "dividendRate/currentPrice"


def test_ambiguous_dividend_yield_without_rate_and_price_is_unavailable():
    assert ff._dividend_yield_ratio({"dividendYield": 0.34}) == (None, None)
    assert ff._dividend_yield_ratio({"dividendYield": 0.0306}) == (None, None)


def test_dividend_yield_zero_does_not_add_score():
    base = ff._face_valuation({"forwardPE": 20, "dividendRate": 0, "currentPrice": 100})
    missing = ff._face_valuation({"forwardPE": 20})
    assert base.score == missing.score
    assert base.metrics["dividendYieldRatio"] == 0


def test_dividend_yield_rejects_non_finite_values():
    assert ff._dividend_yield_ratio({"dividendRate": 1, "currentPrice": "Infinity"}) == (None, None)
    assert ff._dividend_yield_ratio({"dividendYield": "NaN"}) == (None, None)


def test_fundamental_strong_beats_weak():
    strong = ff._face_fundamental({"earningsGrowth": 0.5, "revenueGrowth": 0.3,
                                   "profitMargins": 0.4, "returnOnEquity": 0.3})
    weak = ff._face_fundamental({"earningsGrowth": -0.2, "revenueGrowth": -0.1,
                                 "profitMargins": -0.05, "returnOnEquity": -0.1})
    assert strong.score > 80
    assert weak.score < 30


def test_capital_uses_institutions_and_volume():
    tech = ff.FaceResult("技術面", True, 70, "x", metrics={"vol_ratio": 1.8})
    r = ff._face_capital({"heldPercentInstitutions": 0.8}, tech)
    assert r.available and r.score > 70


def test_analyze_composite_independent_degradation(monkeypatch):
    # 注入：provider 給強勢日K、info 給強體質、宏觀面固定 → 綜合可算，且某面 unavailable 不阻斷
    monkeypatch.setattr(ff, "_make_provider", lambda: _FakeProvider(_rising_bars()))
    monkeypatch.setattr(ff, "_load_info", lambda s: {
        "pegRatio": 0.6, "forwardPE": 14, "earningsGrowth": 0.5, "revenueGrowth": 0.3,
        "profitMargins": 0.4, "returnOnEquity": 0.3, "heldPercentInstitutions": 0.8})
    monkeypatch.setattr(ff, "_face_macro",
                        lambda: ff.FaceResult("宏觀面", False, 0.0, "x", note="測試略過"))

    res = ff.analyze("NVDA", enabled_override=True)
    assert res is not None
    names = {f.name: f for f in res.faces}
    assert names["技術面"].available and names["估值面"].available
    assert names["宏觀面"].available is False        # 某面 unavailable
    assert res.composite is not None                 # 其餘面照算綜合
    lines = ff.format_report(res)
    assert any("五面分析" in l for l in lines)
    assert any("宏觀面" in l and "暫無資料" in l for l in lines)
