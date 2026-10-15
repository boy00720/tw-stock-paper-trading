"""tests/test_watchlist_merger_freshness.py — load_long_term freshness 過期自動退池

驗證 utils.watchlist_merger._is_freshness_expired 與 load_long_term：
- freshness_until 早於 today → 自動跳過
- freshness_until 等於 today / 未來 → 保留
- 缺欄位或解析失敗 → 保留（fail-open）
- in_pool=False → 仍跳過
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from utils import watchlist_merger as wm


def test_freshness_expired_true_for_past_date():
    assert wm._is_freshness_expired({"freshness_until": "2020-01-01"}, today=date(2026, 5, 13))


def test_freshness_expired_false_for_today():
    assert not wm._is_freshness_expired({"freshness_until": "2026-05-13"}, today=date(2026, 5, 13))


def test_freshness_expired_false_for_future():
    assert not wm._is_freshness_expired({"freshness_until": "2026-07-27"}, today=date(2026, 5, 13))


def test_freshness_expired_false_for_missing_field():
    assert not wm._is_freshness_expired({}, today=date(2026, 5, 13))
    assert not wm._is_freshness_expired({"freshness_until": None}, today=date(2026, 5, 13))
    assert not wm._is_freshness_expired({"freshness_until": ""}, today=date(2026, 5, 13))


def test_freshness_expired_false_for_garbage_value():
    assert not wm._is_freshness_expired({"freshness_until": "not-a-date"}, today=date(2026, 5, 13))
    assert not wm._is_freshness_expired({"freshness_until": 12345}, today=date(2026, 5, 13))


def test_freshness_expired_accepts_iso_datetime():
    assert wm._is_freshness_expired({"freshness_until": "2020-01-01T08:00:00"}, today=date(2026, 5, 13))


def test_load_long_term_skips_expired(tmp_path, monkeypatch):
    today = date.today()
    state = {
        "symbols": {
            "2357.TW": {
                "name": "華碩",
                "in_pool": True,
                "freshness_until": (today - timedelta(days=1)).isoformat(),
            },
            "2377.TW": {
                "name": "微星",
                "in_pool": True,
                "freshness_until": (today + timedelta(days=30)).isoformat(),
            },
            "2891.TW": {
                "name": "中信金",
                "in_pool": True,
                # 缺 freshness_until → 保留
            },
            "0050.TW": {
                "name": "元大台灣 50",
                "in_pool": False,
                "freshness_until": (today + timedelta(days=30)).isoformat(),
            },
        },
        "updated_at": "2026-05-01T17:36:00",
    }
    state_file = tmp_path / "value_state.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(wm, "VALUE_STATE_FILE", state_file)

    items = wm.load_long_term()
    syms = [it["symbol"] for it in items]
    assert "2357.TW" not in syms  # 過期被擋
    assert "0050.TW" not in syms  # in_pool=False
    assert set(syms) == {"2377.TW", "2891.TW"}


def test_load_long_term_returns_empty_when_state_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(wm, "VALUE_STATE_FILE", tmp_path / "missing.json")
    assert wm.load_long_term() == []


def test_load_long_term_all_expired_returns_empty(tmp_path, monkeypatch):
    state = {
        "symbols": {
            "2357.TW": {"name": "X", "in_pool": True, "freshness_until": "2020-01-01"},
            "2377.TW": {"name": "Y", "in_pool": True, "freshness_until": "2020-02-01"},
        },
    }
    state_file = tmp_path / "value_state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(wm, "VALUE_STATE_FILE", state_file)
    assert wm.load_long_term() == []
