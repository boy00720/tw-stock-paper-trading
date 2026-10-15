from __future__ import annotations

import json
from pathlib import Path

from utils.watchlist_schema import (
    cross_signals,
    cross_signals_count,
    default_watchlist,
    load_watchlist,
    pool_items,
    watchlist_summary,
)


def test_default_watchlist_has_expected_shape() -> None:
    payload = default_watchlist()
    assert payload["schema_version"] == 1
    assert set(payload["formal_pools"].keys()) == {"short_term", "medium_term", "long_term"}
    assert set(payload["auxiliary_signals"].keys()) == {"intraday"}
    summary = payload["summary"]
    assert summary["short_term_count"] == 0
    assert summary["cross_signals_count"] == 0


def test_load_watchlist_returns_default_for_missing_file(tmp_path: Path) -> None:
    payload = load_watchlist(tmp_path / "missing.json")
    assert payload["formal_pools"]["short_term"] == []
    assert payload["auxiliary_signals"]["intraday"] == []
    assert payload["cross_signals"] == {}


def test_pool_items_prefers_new_schema(tmp_path: Path) -> None:
    payload = {
        "summary": {
            "short_term_count": 1,
            "medium_term_count": 0,
            "long_term_count": 0,
            "intraday_count": 1,
            "intraday_strict_count": 1,
            "cross_signals_count": 0,
        },
        "formal_pools": {
            "short_term": [{"symbol": "AAA", "name": "A"}],
            "medium_term": [],
            "long_term": [],
        },
        "auxiliary_signals": {
            "intraday": [{"symbol": "BBB", "name": "B"}],
        },
        "short_term": [{"symbol": "LEGACY", "name": "Legacy"}],
        "intraday": [{"symbol": "LEGACY2", "name": "Legacy2"}],
        "cross_signals": {},
    }
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    watchlist = load_watchlist(path)
    assert [item["symbol"] for item in pool_items(watchlist, "short_term")] == ["AAA"]
    assert [item["symbol"] for item in pool_items(watchlist, "intraday")] == ["BBB"]


def test_pool_items_falls_back_to_legacy_shape(tmp_path: Path) -> None:
    payload = {
        "summary": {
            "short_term_count": 1,
            "medium_term_count": 0,
            "long_term_count": 0,
            "intraday_count": 0,
            "intraday_strict_count": 0,
            "cross_signals_count": 1,
        },
        "short_term": [{"symbol": "AAA", "name": "A"}],
        "cross_signals": {"AAA": {"name": "A", "pools": ["short_term"], "level": "⭐", "notes": {}, "summary": "短線｜⭐"}},
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    watchlist = load_watchlist(path)
    assert [item["symbol"] for item in pool_items(watchlist, "short_term")] == ["AAA"]
    assert cross_signals_count(watchlist) == 1
    assert list(cross_signals(watchlist).keys()) == ["AAA"]


def test_watchlist_summary_fills_missing_keys() -> None:
    summary = watchlist_summary({"summary": {"short_term_count": 3}})
    assert summary["short_term_count"] == 3
    assert summary["medium_term_count"] == 0
    assert summary["intraday_count"] == 0
    assert summary["cross_signals_count"] == 0
