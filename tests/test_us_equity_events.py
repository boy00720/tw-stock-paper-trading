from types import SimpleNamespace

from utils.us_equity_events import fetch_sec_filings, fetch_yfinance_events


class FakeIloc:
    def __init__(self, row):
        self.row = row

    def __getitem__(self, _key):
        return SimpleNamespace(to_dict=lambda: self.row)


class FakeIndex:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, _key):
        return self.value


class FakeFrame:
    empty = False

    def __init__(self, row, timestamp="2026-07-01T12:00:00+00:00"):
        self.iloc = FakeIloc(row)
        self.index = FakeIndex(timestamp)


class FakeTicker:
    info = {"shortName": "ABC Company"}
    news = [{
        "title": "ABC Company reports quarterly results",
        "publisher": "TestWire",
        "link": "https://example.test/news",
        "relatedTickers": ["ABC"],
    }]
    earnings_dates = FakeFrame({"EPS Estimate": "1.0"})
    recommendations = FakeFrame({"To Grade": "Buy"})


def test_yfinance_events_are_normalized():
    events, warnings = fetch_yfinance_events("ABC", ticker_factory=lambda _symbol: FakeTicker())
    assert not warnings
    assert events[0]["symbol"] == "ABC"
    assert events[0]["event_type"] == "news"
    assert {item["event_type"] for item in events} == {"news", "earnings", "analyst_rating"}


def test_nested_provider_is_normalized_and_unrelated_news_filtered():
    class MixedTicker:
        info = {"longName": "Apple Inc."}
        news = [
            {"content": {
                "title": "Apple unveils a new product",
                "provider": {"displayName": "Reuters"},
                "relatedTickers": ["AAPL"],
            }},
            {"content": {
                "title": "Tesla expands factory",
                "provider": {"displayName": "OtherWire"},
                "relatedTickers": ["TSLA"],
            }},
        ]
        earnings_dates = None
        recommendations = None

    events, warnings = fetch_yfinance_events("AAPL", ticker_factory=lambda _symbol: MixedTicker())
    assert [item["title"] for item in events] == ["Apple unveils a new product"]
    assert events[0]["source"] == "Reuters"
    assert events[0]["data"]["relevance_basis"] == "relatedTickers"
    assert "1 unrelated item" in warnings[0]


def test_old_schema_without_related_tickers_uses_company_text():
    class LegacyTicker:
        info = {"shortName": "NVIDIA Corporation"}
        news = [{"title": "NVIDIA launches a new chip", "publisher": "LegacyWire"}]
        earnings_dates = None
        recommendations = None

    events, warnings = fetch_yfinance_events("NVDA", ticker_factory=lambda _symbol: LegacyTicker())
    assert not warnings
    assert events[0]["data"]["relevance_basis"] == "company_text"


def test_short_ticker_requires_metadata_or_company_name():
    class ShortTicker:
        info = {"shortName": "C3.ai, Inc."}
        news = [
            {"title": "AI software company cuts guidance"},
            {"title": "C3.ai cuts guidance"},
        ]
        earnings_dates = None
        recommendations = None

    events, warnings = fetch_yfinance_events("AI", ticker_factory=lambda _symbol: ShortTicker())
    assert [event["title"] for event in events] == ["C3.ai cuts guidance"]
    assert "1 unrelated item" in warnings[0]


def test_epoch_and_frame_index_are_normalized_to_iso():
    class TimestampTicker:
        info = {"shortName": "ABC Company"}
        news = [{
            "title": "ABC Company update",
            "publisher": "Wire",
            "providerPublishTime": 1798761600,
            "relatedTickers": ["ABC"],
        }]
        earnings_dates = FakeFrame({"EPS Estimate": "1.0"}, "2026-08-01T12:00:00+00:00")
        recommendations = FakeFrame({"To Grade": "Buy"}, "2026-07-01T12:00:00+00:00")

    events, _ = fetch_yfinance_events("ABC", ticker_factory=lambda _symbol: TimestampTicker())
    by_type = {event["event_type"]: event for event in events}
    assert by_type["news"]["published_at"].endswith("+00:00")
    assert by_type["earnings"]["published_at"] == ""
    assert by_type["earnings"]["scheduled_at"] == "2026-08-01T12:00:00+00:00"
    assert by_type["analyst_rating"]["published_at"] == "2026-07-01T12:00:00+00:00"
    assert all(event["retrieved_at"] for event in events)


def test_missing_news_timestamp_stays_unknown():
    class LegacyTicker:
        info = {"shortName": "ABC Company"}
        news = [{"title": "ABC Company update", "publisher": "Wire"}]
        earnings_dates = None
        recommendations = None

    events, _ = fetch_yfinance_events("ABC", ticker_factory=lambda _symbol: LegacyTicker())
    assert events[0]["published_at"] == ""
    assert events[0]["retrieved_at"]


def test_company_alias_does_not_match_inside_another_word():
    class AppleTicker:
        info = {"longName": "Apple Inc."}
        news = [{"title": "Pineapple fraud investigation triggers recall"}]
        earnings_dates = None
        recommendations = None

    events, warnings = fetch_yfinance_events("AAPL", ticker_factory=lambda _symbol: AppleTicker())
    assert events == []
    assert "1 unrelated item" in warnings[0]


def test_sec_requires_identifiable_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    events, warnings = fetch_sec_filings("ABC", cik="123")
    assert events == []
    assert "SEC_USER_AGENT" in warnings[0]
