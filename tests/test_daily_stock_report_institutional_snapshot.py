# -*- coding: utf-8 -*-

import json
from datetime import datetime
from pathlib import Path

from scripts import daily_stock_report


def test_load_institutional_snapshot_prefers_complete_same_day_snapshot(tmp_path, monkeypatch):
    snapshot_dir = tmp_path / "logs" / "institutional_snapshots"
    snapshot_dir.mkdir(parents=True)

    morning_payload = {
        "date": "2026-04-28",
        "mode": "morning",
        "generated_at": "2026-04-28T08:00:00",
        "source": "stock_monitor.finmind",
        "status": "ok",
        "market_total_text": "morning market",
        "stocks": {},
        "leaders": {"foreign": {"buy_top": [], "sell_top": []}},
    }
    afternoon_payload = {
        "date": "2026-04-28",
        "mode": "afternoon",
        "generated_at": "2026-04-28T19:13:21",
        "source": "stock_monitor.finmind",
        "status": "ok",
        "market_total_text": "afternoon market",
        "stocks": {
            "2330.TW": {
                "外資": {"today": -22113.589, "month": 12174.374},
                "投信": {"today": 785.054, "month": 3414.025},
                "自營商": {"today": 500.225, "month": 5194.93},
            }
        },
        "leaders": {"foreign": {"buy_top": [], "sell_top": []}},
    }

    (snapshot_dir / "2026-04-28_morning.json").write_text(
        json.dumps(morning_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (snapshot_dir / "2026-04-28_afternoon.json").write_text(
        json.dumps(afternoon_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 4, 28, 20, 0, 0)

    monkeypatch.setattr(daily_stock_report, "proj_root", lambda: tmp_path)
    monkeypatch.setattr(daily_stock_report, "datetime", FixedDatetime)

    snapshot = daily_stock_report.load_institutional_snapshot()

    assert snapshot["snapshot_meta"]["path"].endswith("2026-04-28_afternoon.json")
    assert snapshot["stock_institutional"]["2330.TW"]["外資"]["today"] == -22113.589


def test_build_watchlist_digest_uses_loaded_stock_institutional_data():
    watchlist = {
        "summary": {"short_term_count": 1},
        "formal_pools": {
            "short_term": [{"symbol": "2330.TW", "name": "台積電", "score": 73, "engine": "momentum"}],
            "medium_term": [],
            "long_term": [],
        },
        "auxiliary_signals": {"intraday": []},
        "cross_signals": {},
    }
    stock_institutional = {
        "2330.TW": {
            "外資": {"today": -22113.589, "month": 12174.374},
            "投信": {"today": 785.054, "month": 3414.025},
            "自營商": {"today": 500.225, "month": 5194.93},
        }
    }

    digest = daily_stock_report.build_watchlist_digest(watchlist, stock_institutional, [])

    note = digest["top_short"][0]["institutional_note"]
    assert "法人資料暫缺" not in note
    assert "外資今-22,114張" in note
    assert "投信今+785張" in note
