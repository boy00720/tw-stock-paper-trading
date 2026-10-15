#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股市 skill 正式 runner。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils.stock_skill_audit import run_audit, write_desktop_summary

DEFAULT_MODES = ["morning", "afternoon", "evening", "weekly_mon", "weekly_fri", "weekly_sun"]


def _tail_text(text: str, limit: int = 1600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def run_mode(mode: str, force: bool) -> dict[str, Any]:
    cmd = [sys.executable, "orchestrator.py", "--mode", mode]
    if force:
        cmd.append("--force")
    started_at = datetime.now()
    proc = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    return {
        "mode": mode,
        "command": " ".join(cmd),
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "exit_code": proc.returncode,
        "stdout_tail": _tail_text(proc.stdout),
        "stderr_tail": _tail_text(proc.stderr),
    }


def build_modes(include_weekly_sun: bool, modes: list[str] | None = None) -> list[str]:
    selected = list(modes or DEFAULT_MODES)
    if include_weekly_sun and "weekly_sun" not in selected:
        selected.append("weekly_sun")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="執行完整股市 skill 流程並審計推送/錯誤")
    parser.add_argument("--force", action="store_true", help="傳遞 --force 給 orchestrator modes")
    parser.add_argument("--include-weekly-sun", action="store_true", help="額外執行 weekly_sun 反思流程")
    parser.add_argument("--no-repair", action="store_true", help="只審計，不做 repair 相關動作")
    parser.add_argument(
        "--mode",
        action="append",
        choices=["morning", "afternoon", "evening", "weekly_mon", "weekly_fri", "weekly_sun", "all"],
        help="指定要執行的 orchestrator mode；可重複指定。未指定時跑股市預設組合。",
    )
    args = parser.parse_args()

    run_started_at = datetime.now()
    command_results: list[dict[str, Any]] = []
    for mode in build_modes(args.include_weekly_sun, args.mode):
        print(f"========== 股市 skill 啟動 mode={mode} ==========")
        result = run_mode(mode, force=args.force)
        command_results.append(result)
        print(f"mode={mode} exit={result['exit_code']}")

    audit = run_audit("today", repair=not args.no_repair, since=run_started_at)
    summary_path = write_desktop_summary(audit, command_results)
    print(f"股市 skill 摘要已寫入：{summary_path}")
    print(f"最終結論：{audit.conclusion}")

    failed_modes = [item for item in command_results if item.get("exit_code") != 0]
    if failed_modes or audit.has_blocker:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
