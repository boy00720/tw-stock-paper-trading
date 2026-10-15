# -*- coding: utf-8 -*-

import json
from datetime import datetime
from pathlib import Path

from scripts.screener_review import _resolve_week_start, run_review
from utils import screener_tracker as screener_tracker_module
from utils.screener_tracker import ScreenerTracker


class TestScreenerReviewRegression:
    def test_weekly_report_counts_completed_trade_by_exit_date(self, tmp_path, monkeypatch):
        base_dir = Path(tmp_path)
        history_dir = base_dir / "logs" / "screener_tracker" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)

        record = {
            "timestamp": "2026-04-27T16:16:59.990376",
            "symbol": "6202.TW",
            "name": "盛群",
            "engine": "momentum",
            "pick_date": "2026-04-25",
            "entry_price": 57.8,
            "stop_loss": 54.91,
            "take_profit": 63.58,
            "time_stop_date": "2026-05-02",
            "score": 72,
            "inst_signal": "強買",
            "price": 57.8,
            "breakdown": {"ret_5d": 14},
            "entry_zone": [57.51, 58.38],
            "outcome": "loss",
            "exit_price": 54.3,
            "exit_date": "2026-04-27",
            "exit_reason": "stop_loss",
            "pnl_pct": -6.055363401727952,
            "holding_days": 2,
        }
        (history_dir / "2026-04.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        tracker = ScreenerTracker(base_dir)
        perf = tracker.generate_weekly_report(datetime(2026, 4, 27))

        assert perf.total_picks == 1
        assert perf.completed_trades == 1
        assert perf.losses == 1
        assert perf.wins == 0

    def test_run_review_does_not_mark_data_insufficient_when_week_has_completed_trade(self, tmp_path, monkeypatch):
        base_dir = Path(tmp_path)
        history_dir = base_dir / "logs" / "screener_tracker" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)

        record = {
            "timestamp": "2026-04-27T16:16:59.990376",
            "symbol": "6202.TW",
            "name": "盛群",
            "engine": "momentum",
            "pick_date": "2026-04-25",
            "entry_price": 57.8,
            "stop_loss": 54.91,
            "take_profit": 63.58,
            "time_stop_date": "2026-05-02",
            "score": 72,
            "inst_signal": "強買",
            "price": 57.8,
            "breakdown": {"ret_5d": 14},
            "entry_zone": [57.51, 58.38],
            "outcome": "loss",
            "exit_price": 54.3,
            "exit_date": "2026-04-27",
            "exit_reason": "stop_loss",
            "pnl_pct": -6.055363401727952,
            "holding_days": 2,
        }
        (history_dir / "2026-04.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        tracker = ScreenerTracker(base_dir)
        monkeypatch.setattr(screener_tracker_module, "_screener_tracker", tracker)

        result = run_review(trade_date=datetime(2026, 4, 27), dry_run=True)

        assert result["ok"] is True
        assert result["reason"] == ""
        assert result["metrics"]["completed_trades"] == 1
        assert "**完成交易**: 1 筆" in result["report_text"]

    def test_resolve_week_start_resets_time_component(self):
        week_start = _resolve_week_start(datetime(2026, 4, 27, 18, 36, 45))

        assert week_start == datetime(2026, 4, 27, 0, 0, 0)
