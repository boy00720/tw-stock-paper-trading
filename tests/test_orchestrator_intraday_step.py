"""tests/test_orchestrator_intraday_step.py — intraday_screener 接進 afternoon mode 的驗證

避免實際呼叫 FinMind / yfinance（外部 API），整段用 monkeypatch 隔離 cmd_scan：
- 成功路徑：StepResult ok=True、key=intraday_screener
- 失敗路徑：StepResult ok=False、error 帶上例外訊息
- dry_run=True 才呼叫一次（避免雙推送）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator


def test_run_intraday_screener_success(monkeypatch):
    calls = []

    def fake_cmd_scan(dry_run: bool = False, **kw):
        calls.append({"dry_run": dry_run, **kw})

    fake_mod = type("M", (), {"cmd_scan": fake_cmd_scan})
    monkeypatch.setitem(sys.modules, "scripts.intraday_screener", fake_mod)

    results = {}
    orchestrator._run_intraday_screener(results)

    assert "intraday_screener" in results
    sr = results["intraday_screener"]
    assert sr.success is True
    assert sr.is_critical is False
    assert len(calls) == 1
    assert calls[0]["dry_run"] is True  # 避免雙推送


def test_run_intraday_screener_failure_does_not_raise(monkeypatch):
    def fake_cmd_scan(**kw):
        raise RuntimeError("FinMind 配額用盡")

    fake_mod = type("M", (), {"cmd_scan": fake_cmd_scan})
    monkeypatch.setitem(sys.modules, "scripts.intraday_screener", fake_mod)

    results = {}
    orchestrator._run_intraday_screener(results)  # 不應拋出

    sr = results["intraday_screener"]
    assert sr.success is False
    assert sr.is_critical is False
    assert "FinMind 配額用盡" in (sr.error or "")


def test_intraday_step_runs_before_watchlist_merger():
    """source 層級檢查：afternoon mode 內 _run_intraday_screener 必須在 _run_watchlist_merger 之前。"""
    src = Path(orchestrator.__file__).read_text(encoding="utf-8")
    # 取 run_afternoon 函式區段
    start = src.index("def run_afternoon(")
    end = src.index("def run_evening(", start)
    block = src[start:end]
    i_intraday = block.find("_run_intraday_screener(")
    i_merger = block.find("_run_watchlist_merger(")
    assert i_intraday != -1, "afternoon 沒掛 _run_intraday_screener"
    assert i_merger != -1, "afternoon 沒掛 _run_watchlist_merger"
    assert i_intraday < i_merger, "intraday 必須在 watchlist_merger 之前才能餵到 auxiliary_signals.intraday"
