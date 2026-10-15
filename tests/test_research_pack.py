from __future__ import annotations

from types import SimpleNamespace

from utils.research_pack import _cli_summary, build_us_research_pack, format_research_pack_for_prompt


class _FakeProvider:
    last_init_kwargs = None

    def __init__(self, **kwargs):
        self.readonly = True
        type(self).last_init_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def fetch_us_stock_quote(self, symbol, **_kwargs):
        return SimpleNamespace(to_dict=lambda: {
            "symbol": symbol,
            "provider": "IBKR",
            "asof": "2026-07-13T10:00:00+08:00",
            "market_data_type": "delayed",
            "session": "delayed",
            "is_realtime": False,
            "permission_status": "ok",
            "bid": None,
            "ask": None,
            "last": 120.0,
            "close": 119.0,
            "market_price": 120.0,
            "warnings": [],
        })

    def fetch_historical_bars(self, symbol, **_kwargs):
        start = 100 if symbol != "SPY" else 200
        rows = [{"date": f"d{i}", "close": start + i, "volume": 1000 + i} for i in range(70)]
        return SimpleNamespace(rows=rows, warnings=[], asof="2026-07-13T10:00:00+08:00", bar_size="1 day")


class _FakeTicker:
    info = {
        "shortName": "ABC Company",
        "currentPrice": 100,
        "dividendRate": 2,
        "forwardPE": 20,
        "revenueGrowth": 0.1,
        "earningsGrowth": 0.2,
        "profitMargins": 0.15,
        "returnOnEquity": 0.18,
        "heldPercentInstitutions": 0.7,
    }
    news = [{"title": "ABC Company earnings", "publisher": "TestWire", "relatedTickers": ["ABC"]}]
    earnings_dates = None
    recommendations = None


def test_research_pack_integrates_sections_without_persistence(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    pack = build_us_research_pack(
        "abc",
        provider_factory=_FakeProvider,
        ticker_factory=lambda _symbol: _FakeTicker(),
        include_macro=False,
    )

    assert pack.symbol == "ABC"
    assert pack.quote["is_realtime"] is False
    assert pack.historical["row_count"] == 70
    assert pack.technical["available"] is True
    assert pack.valuation["metrics"]["dividend_yield_ratio"] == 0.02
    assert pack.fundamentals["available"] is True
    assert pack.news[0]["source"] == "TestWire"
    assert pack.provider_map["quote"] == pack.quote["provider"]
    assert pack.provider_map["news"] == ["yfinance"]
    assert pack.historical["asof"] == "d69"
    assert pack.historical["retrieved_at"] == "2026-07-13T10:00:00+08:00"
    assert pack.provider_map.get("alert_dispatch") is None
    assert pack.provider_map.get("alert_queue") is None
    assert pack.freshness["technical"]


def test_research_pack_prompt_exposes_boundaries(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    pack = build_us_research_pack(
        "ABC",
        provider_factory=_FakeProvider,
        ticker_factory=lambda _symbol: _FakeTicker(),
        include_macro=False,
    )
    text = format_research_pack_for_prompt(pack)

    assert "market_data_type: delayed" in text
    assert "is_realtime: False" in text
    assert "technical" in text and "available: True" in text
    assert "fundamentals_available: True" in text
    assert "event_1:" in text and "ABC Company earnings" in text
    assert "provider=yfinance" in text
    assert "data={'relevance_basis': 'relatedTickers'}" in text
    assert "各 section 的 asof/freshness 不同" in text
    assert "不是自動交易或下單指令" in text

    summary = _cli_summary(pack)
    assert summary["sections"]["technical"]["metrics"]["ma20"]
    assert summary["sections"]["fundamentals"]["metrics"]["revenue_growth"] == 0.1
    assert summary["events"][0]["title"] == "ABC Company earnings"


def test_research_pack_factory_failure_does_not_fallback_to_live_ticker(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    calls = []

    def failing_factory(symbol):
        calls.append(symbol)
        raise RuntimeError("offline")

    pack = build_us_research_pack(
        "ABC",
        provider_factory=_FakeProvider,
        ticker_factory=failing_factory,
        include_macro=False,
    )

    assert calls == ["ABC"]
    assert pack.fundamentals["available"] is False
    assert any("yfinance events unavailable" in warning for warning in pack.warnings)


def test_event_freshness_uses_retrieved_time_not_future_schedule(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    class ScheduledTicker(_FakeTicker):
        news = []
        earnings_dates = SimpleNamespace(
            empty=False,
            iloc=SimpleNamespace(__getitem__=lambda _self, _index: None),
        )

    class Row:
        def to_dict(self):
            return {"EPS Estimate": "1.0"}

    class Iloc:
        def __getitem__(self, _index):
            return Row()

    ScheduledTicker.earnings_dates = SimpleNamespace(
        empty=False,
        iloc=Iloc(),
        index=["2026-08-01T12:00:00+00:00"],
    )
    pack = build_us_research_pack(
        "ABC",
        provider_factory=_FakeProvider,
        ticker_factory=lambda _symbol: ScheduledTicker(),
        include_macro=False,
    )

    earnings = next(event for event in pack.events if event["event_type"] == "earnings")
    assert earnings["scheduled_at"] == "2026-08-01T12:00:00+00:00"
    assert earnings["published_at"] == ""
    assert pack.freshness["events"] == earnings["retrieved_at"]


def test_technical_volume_ratio_keeps_bar_alignment():
    from utils.research_pack import _technical_snapshot

    rows = [{"close": 100 + i, "volume": 1000 + i} for i in range(70)]
    rows[-1]["volume"] = None
    bars = SimpleNamespace(rows=rows, warnings=[], asof="now")
    snapshot = _technical_snapshot(bars, bars)

    assert snapshot["metrics"]["volume_ratio_20d"] is None
    assert any("not aligned" in warning for warning in snapshot["warnings"])
    assert any("not aligned" in warning for warning in _cli_summary(SimpleNamespace(
        to_dict=lambda: {
            "market": "US", "symbol": "ABC", "asof": "now", "quote": {},
            "historical": {}, "technical": snapshot, "valuation": {},
            "fundamentals": {}, "capital": {}, "macro": {}, "news": [],
            "events": [], "freshness": {}, "warnings": [],
        }
    ))["warnings"])


def test_macro_watchlist_alone_is_not_current_data(monkeypatch):
    import utils.macro_calendar as macro_calendar
    from utils.research_pack import _macro_snapshot

    monkeypatch.setattr(macro_calendar, "fetch_fed_rss_items", lambda _config: [])
    monkeypatch.setattr(macro_calendar, "flatten_watch_events", lambda _config: [SimpleNamespace(to_dict=lambda: {"label": "CPI"})])
    snapshot = _macro_snapshot()

    assert snapshot["available"] is False
    assert snapshot["watch_events"] == [{"label": "CPI"}]
    assert "reference-only" in snapshot["warnings"][0]


def test_spy_reuses_symbol_bars_for_benchmark(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    class CountingProvider(_FakeProvider):
        calls = []

        def fetch_historical_bars(self, symbol, **kwargs):
            type(self).calls.append(symbol)
            return super().fetch_historical_bars(symbol, **kwargs)

    CountingProvider.calls = []
    build_us_research_pack(
        "SPY",
        provider_factory=CountingProvider,
        ticker_factory=lambda _symbol: _FakeTicker(),
        include_equity_events=False,
        include_macro=False,
    )
    assert CountingProvider.calls == ["SPY"]


def test_account_snapshot_uses_non_light_provider(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    class AccountProvider(_FakeProvider):
        def account_snapshot(self):
            return SimpleNamespace(to_dict=lambda: {"provider": "IBKR"})

    build_us_research_pack(
        "ABC",
        provider_factory=AccountProvider,
        ticker_factory=lambda _symbol: _FakeTicker(),
        include_account_snapshot=True,
        include_equity_events=False,
        include_macro=False,
    )

    assert AccountProvider.last_init_kwargs["light"] is False
