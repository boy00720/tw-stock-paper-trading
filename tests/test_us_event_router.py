from utils.us_event_router import high_priority_events, route_events_by_symbol, save_alert_queue, score_event


def test_negative_event_is_high_priority():
    event = score_event({"symbol": "ABC", "event_type": "news", "title": "Company cuts guidance"})
    assert event["severity"] == "high"
    assert event["direction"] == "negative"


def test_events_are_routed_and_sorted_by_symbol():
    events = [
        {"symbol": "ABC", "title": "older", "published_at": "2026-01-01"},
        {"symbol": "ABC", "title": "newer", "published_at": "2026-01-02"},
        {"symbol": "XYZ", "title": "other", "published_at": "2026-01-03"},
    ]
    routed = route_events_by_symbol(events)
    assert list(routed) == ["ABC", "XYZ"]
    assert routed["ABC"][0]["title"] == "newer"


def test_8k_and_negative_news_are_alert_candidates():
    events = [
        {"symbol": "ABC", "event_type": "sec_filing", "title": "8-K", "data": {"form": "8-K"}},
        {"symbol": "XYZ", "event_type": "news", "title": "routine update"},
    ]
    assert [item["symbol"] for item in high_priority_events(events)] == ["ABC"]


def test_keyword_substrings_do_not_raise_severity():
    cases = [
        "Operating margin remains stable",
        "Regulator disapproved the proposal",
        "Company expands its product offering",
        "Marketplace seller tools launched",
    ]
    for title in cases:
        event = score_event({"symbol": "ABC", "event_type": "news", "title": title})
        assert event["severity"] == "low", title
        assert event["direction"] == "unknown", title


def test_financial_offering_and_rating_phrases_still_match():
    offering = score_event({"symbol": "ABC", "event_type": "news", "title": "Company announces public offering"})
    rating = score_event({"symbol": "ABC", "event_type": "news", "title": "Analyst rating downgrade"})
    assert (offering["severity"], offering["direction"]) == ("high", "negative")
    assert (rating["severity"], rating["direction"]) == ("high", "negative")


def test_alert_queue_is_deduplicated(tmp_path):
    events = [{"symbol": "ABC", "event_type": "news", "title": "fraud investigation", "published_at": "2026-01-01"}]
    first = save_alert_queue(events, root=tmp_path, asof="2026-01-02")
    second = save_alert_queue(events, root=tmp_path, asof="2026-01-02")
    assert first == second
    assert len((tmp_path / "2026-01-02.json").read_text().splitlines()) > 0
