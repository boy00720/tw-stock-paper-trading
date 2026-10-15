from utils.us_alert_dispatcher import dispatch_events, format_alert


def test_alert_dispatch_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("US_EQUITY_ALERT_PUSH_ENABLED", raising=False)
    calls = []
    result = dispatch_events(
        [{"symbol": "ABC", "title": "fraud investigation", "published_at": "2026-01-01"}],
        config={"enabled": True, "min_severity": "high", "cooldown_seconds": 10},
        state_path=tmp_path / "state.json",
        sender=lambda *_args: calls.append(True) or True,
        now=100,
    )
    assert result["candidate_count"] == 1
    assert result["sent_count"] == 0
    assert calls == []


def test_alert_dispatch_sends_once_then_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("US_EQUITY_ALERT_PUSH_ENABLED", "1")
    calls = []
    event = {"symbol": "ABC", "title": "fraud investigation", "published_at": "2026-01-01"}
    config = {"enabled": True, "min_severity": "high", "cooldown_seconds": 10}
    sender = lambda title, content: calls.append((title, content)) or True
    first = dispatch_events([event], config=config, state_path=tmp_path / "state.json", sender=sender, now=100)
    second = dispatch_events([event], config=config, state_path=tmp_path / "state.json", sender=sender, now=105)
    assert first["sent_count"] == 1
    assert second["cooldown_skipped_count"] == 1
    assert len(calls) == 1


def test_format_alert_is_explicitly_non_trading():
    _title, content = format_alert({"symbol": "ABC", "title": "Guidance cut", "severity": "high"})
    assert "非交易指令" in content


def test_theme_alias_matches_underlying_symbol(tmp_path, monkeypatch):
    monkeypatch.delenv("US_EQUITY_ALERT_PUSH_ENABLED", raising=False)
    result = dispatch_events(
        [{"symbol": "WDC", "title": "fraud investigation", "published_at": "2026-01-01"}],
        config={"enabled": False, "watch_symbols": ["DRAM"], "watch_aliases": {"DRAM": ["MU", "WDC"]}},
        state_path=tmp_path / "state.json",
        now=100,
    )
    assert result["candidate_count"] == 1
