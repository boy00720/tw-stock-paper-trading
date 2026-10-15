"""美股宏觀消息面雷達測試（mock 外部資料源 / 不發網路）。"""

from __future__ import annotations

from datetime import datetime

from scripts import us_macro_radar as radar
from utils.macro_calendar import MacroNewsItem, MacroSnapshot, build_macro_snapshot


class _FakeResponse:
    def __init__(self, payload=None, content: bytes = b""):
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _rss_response(title: str = "Federal Reserve issues FOMC statement"):
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss><channel><title>Fed</title><item>
<title>{title}</title>
<link>https://www.federalreserve.gov/test.htm</link>
<description>Fed official update</description>
<pubDate>Wed, 17 Jun 2026 14:00:00 -0400</pubDate>
</item></channel></rss>
"""
    return _FakeResponse(content=xml.encode("utf-8"))


def _config():
    return {
        "modes": {
            "event_check": {"title": "事件檢查", "query": "event {date_taipei} {date_et}"},
            "weekend": {"title": "週末前瞻", "query": "week ahead"},
        },
        "event_watchlist": {
            "high": [
                {"key": "cpi", "label": "CPI", "category": "inflation", "usual_time_et": "08:30", "keywords": ["CPI"]}
            ]
        },
        "severity_keywords": {
            "high": ["CPI", "FOMC", "rate decision", "hotter than expected"],
            "medium": ["Fed", "inflation"],
        },
        "rss_feeds": {
            "fed": [{"name": "Fed Monetary", "url": "https://fed.example/rss", "category": "fed"}]
        },
    }


def test_event_check_no_release_is_low_even_when_fed_rss_mentions_fomc():
    def tavily(*args, **kwargs):
        return _FakeResponse({
            "answer": "No major US macroeconomic data release or FOMC decision today.",
            "results": [{"title": "Fed recap", "url": "https://www.cnbc.com/fed-recap"}],
        })

    snapshot = build_macro_snapshot(
        "event_check",
        config=_config(),
        now=datetime(2026, 6, 21, 12, 0),
        tavily_requester=tavily,
        rss_requester=lambda *a, **k: _rss_response(),
        tavily_api_key="test",
    )

    assert snapshot.severity == "low"
    assert radar.should_send(snapshot) is False


def test_event_check_actual_release_is_high():
    def tavily(*args, **kwargs):
        return _FakeResponse({
            "answer": "CPI was released today. Actual inflation was hotter than expected versus forecast.",
            "results": [{"title": "CPI hotter than expected", "url": "https://www.cnbc.com/cpi"}],
        })

    snapshot = build_macro_snapshot(
        "event_check",
        config=_config(),
        now=datetime(2026, 6, 21, 12, 0),
        tavily_requester=tavily,
        rss_requester=lambda *a, **k: _rss_response("Fed routine update"),
        tavily_api_key="test",
    )

    assert snapshot.severity == "high"
    assert radar.should_send(snapshot) is True


def test_fixed_summary_sends_even_low_severity():
    snapshot = MacroSnapshot(
        mode="weekend",
        title="週末前瞻",
        asof="2026-06-21T10:00+08:00",
        severity="low",
    )

    assert radar.should_send(snapshot) is True


def test_state_dedup_prevents_second_fixed_push(tmp_path, monkeypatch):
    state_path = tmp_path / "macro_state.json"
    snapshot = MacroSnapshot(
        mode="midday",
        title="午間雷達",
        asof="2026-06-21T12:30+08:00",
        tavily_summary="Market macro summary",
        tavily_sources=[MacroNewsItem(title="Fed watch", source="CNBC")],
        severity="medium",
    )
    sent = []

    monkeypatch.setattr(radar, "STATE_PATH", state_path)
    monkeypatch.setattr(radar, "build_macro_snapshot", lambda mode: snapshot)
    monkeypatch.setattr(radar, "send_dm", lambda title, content: sent.append((title, content)) or True)

    assert radar.run("midday", dry_run=False) == 0
    assert radar.run("midday", dry_run=False) == 0
    assert len(sent) == 1
    assert radar._already_sent(radar._alert_key(snapshot), path=state_path) is True


def test_blocked_social_sources_are_filtered():
    def tavily(*args, **kwargs):
        return _FakeResponse({
            "answer": "Inflation and Fed are in focus.",
            "results": [
                {"title": "Social noise", "url": "https://www.facebook.com/noise"},
                {"title": "Market source", "url": "https://www.cnbc.com/markets"},
            ],
        })

    snapshot = build_macro_snapshot(
        "weekend",
        config=_config(),
        now=datetime(2026, 6, 21, 12, 0),
        tavily_requester=tavily,
        rss_requester=lambda *a, **k: _rss_response("Fed routine update"),
        tavily_api_key="test",
    )

    assert [item.source for item in snapshot.tavily_sources] == ["cnbc.com"]
