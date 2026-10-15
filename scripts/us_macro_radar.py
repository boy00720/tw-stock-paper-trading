#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股宏觀消息面雷達（給 W 個人飛書 DM）。

本線只做宏觀消息 / 事件日曆，不接 IBKR / 盈透、不做交易動作。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# 避免本機代理造成外部資料源 CONNECT 問題（沿用美股提醒腳本邏輯）
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

from dotenv import load_dotenv  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim._notify import send_dm  # noqa: E402
from utils.macro_calendar import MacroSnapshot, build_macro_snapshot, format_macro_snapshot  # noqa: E402

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

LOG_PATH = _BASE_DIR / "logs" / "us_macro_radar.log"
STATE_PATH = _BASE_DIR / "logs" / "us_macro_radar_state.json"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

FIXED_SUMMARY_MODES = {"midday", "afterhours", "weekend"}
EVENT_MODES = {"event_check"}
ALL_MODES = sorted(FIXED_SUMMARY_MODES | EVENT_MODES)


def _state_date(asof: str) -> str:
    try:
        return datetime.fromisoformat(asof).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def _snapshot_digest(snapshot: MacroSnapshot) -> str:
    basis = "\n".join(
        [snapshot.tavily_summary]
        + [item.title for item in snapshot.tavily_sources[:5]]
        + [item.title for item in snapshot.fed_items[:3]]
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _alert_key(snapshot: MacroSnapshot) -> str:
    date_key = _state_date(snapshot.asof)
    if snapshot.mode in FIXED_SUMMARY_MODES:
        return f"{snapshot.mode}:{date_key}"
    return f"{snapshot.mode}:{date_key}:{snapshot.severity}:{_snapshot_digest(snapshot)}"


def _already_sent(key: str, path: Path | None = None) -> bool:
    target = path or STATE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return False
    return key in set(data.get("sent_keys") or [])


def _mark_sent(key: str, path: Path | None = None) -> None:
    target = path or STATE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    keys = list(dict.fromkeys(list(data.get("sent_keys") or []) + [key]))[-100:]
    data["sent_keys"] = keys
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _title_for_snapshot(snapshot: MacroSnapshot) -> str:
    if snapshot.mode == "event_check":
        emoji = "🚨" if snapshot.severity == "high" else "🟡" if snapshot.severity == "medium" else "⚪"
        return f"{emoji} {snapshot.title}｜{snapshot.severity}"
    emoji = "🛰️" if snapshot.mode == "midday" else "🌙" if snapshot.mode == "afterhours" else "🗓️"
    return f"{emoji} {snapshot.title}｜{snapshot.severity}"


def build_message(snapshot: MacroSnapshot) -> tuple[str, str]:
    content = format_macro_snapshot(snapshot)
    boundary = "\n\n【邊界】\n- 本線只做美股宏觀消息面與事件日曆，不使用 IBKR / 盈透作消息源，不含任何自動下單。"
    return _title_for_snapshot(snapshot), (content + boundary).strip()


def should_send(snapshot: MacroSnapshot, *, send_low: bool = False) -> bool:
    if snapshot.mode in EVENT_MODES and not send_low:
        return snapshot.severity == "high"
    return True


def run(mode: str, *, dry_run: bool = False, force: bool = False, send_low: bool = False) -> int:
    if mode not in ALL_MODES:
        logger.error("未知 mode: %s", mode)
        return 2

    snapshot = build_macro_snapshot(mode)
    title, content = build_message(snapshot)
    key = _alert_key(snapshot)

    if not should_send(snapshot, send_low=send_low):
        logger.info("%s severity=%s 未達事件推送門檻，跳過", mode, snapshot.severity)
        if dry_run:
            print(f"# {title}\n\n[DRY-RUN: severity 未達事件推送門檻，正式運行會跳過]\n\n{content}")
        return 0

    if not dry_run and not force and _already_sent(key):
        logger.info("同一份宏觀雷達已推送過，跳過: %s", key)
        return 0

    if dry_run:
        print(f"# {title}\n\n[DRY-RUN: key={key}]\n\n{content}")
        return 0

    ok = send_dm(title, content)
    if ok:
        _mark_sent(key)
    logger.info("美股宏觀雷達推送結果: %s key=%s", ok, key)
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="美股宏觀消息面 / 事件雷達")
    parser.add_argument("--mode", choices=ALL_MODES, default="midday")
    parser.add_argument("--dry-run", action="store_true", help="只輸出，不推送飛書")
    parser.add_argument("--force", action="store_true", help="正式運行時略過去重檢查")
    parser.add_argument("--send-low", action="store_true", help="event_check 即使未達 high 也推送（除錯用）")
    args = parser.parse_args()
    raise SystemExit(run(args.mode, dry_run=args.dry_run, force=args.force, send_low=args.send_low))


if __name__ == "__main__":
    main()
