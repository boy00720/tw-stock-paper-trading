#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一鍵刷新 intraday / watchlist frozen baseline。"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LEGACY_INTRADAY_SNAPSHOT = BASE_DIR / "docs" / "snapshots" / "2026-04-22_intraday_frozen_snapshot.json"
LEGACY_WATCHLIST_SNAPSHOT = BASE_DIR / "docs" / "snapshots" / "2026-04-22_watchlist_frozen_snapshot.json"
M1_INTRADAY_SNAPSHOT = BASE_DIR / "docs" / "snapshots" / "2026-04-22_intraday_m1_frozen_snapshot.json"
M1_WATCHLIST_SNAPSHOT = BASE_DIR / "docs" / "snapshots" / "2026-04-22_watchlist_m1_frozen_snapshot.json"
M1_ENGINE_VERSION = "SE-009B-m1-snapshot"


def _resolve_profile_paths(profile: str) -> tuple[Path, Path]:
    if profile == "m1":
        return M1_INTRADAY_SNAPSHOT, M1_WATCHLIST_SNAPSHOT
    return LEGACY_INTRADAY_SNAPSHOT, LEGACY_WATCHLIST_SNAPSHOT


def main() -> None:
    parser = argparse.ArgumentParser(description="刷新 intraday/watchlist frozen baseline")
    parser.add_argument("--profile", choices=["legacy", "m1"], default="legacy")
    args = parser.parse_args()

    intraday_snapshot, watchlist_snapshot = _resolve_profile_paths(args.profile)
    recomputed = subprocess.check_output(
        [
            "python3",
            str(BASE_DIR / "scripts" / "intraday_screener.py"),
            "--recompute-snapshot",
            str(intraday_snapshot),
        ],
        text=True,
    )
    intraday = json.loads(recomputed)

    engine_version = intraday.get("engine_version")
    if args.profile == "m1":
        if engine_version != M1_ENGINE_VERSION:
            raise RuntimeError("M1 frozen baseline engine_version 不正確，拒絕覆寫 snapshot")
        has_m1_snapshot = all(
            isinstance((result.get("values") or {}).get("m1_snapshot"), dict)
            for result in intraday.get("results", [])
        )
        if not has_m1_snapshot:
            raise RuntimeError("M1 frozen baseline 缺少 values.m1_snapshot，拒絕覆寫 snapshot")

    intraday_snapshot.write_text(
        json.dumps(intraday, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    subprocess.run(
        [
            "python3",
            str(BASE_DIR / "utils" / "watchlist_merger.py"),
            "--refresh-frozen",
            "--frozen-profile",
            args.profile,
        ],
        check=True,
    )

    watchlist = json.loads(watchlist_snapshot.read_text(encoding="utf-8"))
    print(json.dumps({
        "profile": args.profile,
        "intraday_snapshot": {
            "path": str(intraday_snapshot),
            "engine_version": engine_version,
            "passed_count": intraday.get("passed_count"),
            "strict_passed_count": intraday.get("strict_passed_count"),
        },
        "watchlist_snapshot": {
            "path": str(watchlist_snapshot),
            "summary": watchlist.get("summary", {}),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
