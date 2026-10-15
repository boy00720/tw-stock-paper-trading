"""automation/sim/morning_check.py — 盤前 08:55 健康檢查 + 飛書 DM

檢查項：
- launchctl plist 五件套（advisor-decide/dispatch/metrics/brief、feishu-bot）
- 今日 advisor_audit 是否乾淨起跑（decision_round 應為 0、dispatch_round 自然累積不檢查）
- orders.jsonl 非保護性 PENDING 殘留（STOP/TP/[auto-stop] 保護單不列入清理異常）
- fills.jsonl 末筆（時間/標的）

推送目標：FEISHU_USER_OPEN_ID env（個人 DM，不推群），對齊 sim/reports.py 的 _send_dm 模式。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LARK_CONFIG = AUTOMATION_DIR / "config" / "lark_config.yaml"
ADVISOR_AUDIT = AUTOMATION_DIR / "logs" / "sim" / "advisor_audit.jsonl"
ORDERS = AUTOMATION_DIR / "logs" / "sim" / "orders.jsonl"
FILLS = AUTOMATION_DIR / "logs" / "sim" / "fills.jsonl"

DAEMON_PLISTS = {
    "com.w.feishu-bot",
}
SCHEDULED_PLISTS = {
    "com.w.sim-advisor-decide",
    "com.w.sim-advisor-dispatch",
    "com.w.sim-advisor-metrics",
    "com.w.sim-advisor-brief",
}
EXPECTED_PLISTS = sorted(DAEMON_PLISTS | SCHEDULED_PLISTS)


def _check_plists() -> dict[str, str]:
    """daemon plist 看 PID（必須是數字）；排程式 plist 看 loaded（PID `-` 也正常）。"""
    try:
        out = subprocess.check_output(["launchctl", "list"], text=True, timeout=5)
    except Exception as e:
        return {label: f"err:{e}" for label in EXPECTED_PLISTS}
    lines = out.splitlines()
    result = {}
    for label in EXPECTED_PLISTS:
        match = next((l for l in lines if l.endswith(label) or label in l.split()), None)
        if not match:
            result[label] = "MISSING"
            continue
        pid = match.split()[0]
        if label in DAEMON_PLISTS:
            result[label] = pid if pid != "-" else "NOT_RUNNING"
        else:
            result[label] = f"LOADED(pid={pid})"
    return result


def _count_today_audit_by_type(today: str) -> dict[str, int]:
    """回傳今日 advisor_audit.jsonl 各 type 筆數（decision_round / dispatch_round / 其他）。"""
    counts: dict[str, int] = {}
    if not ADVISOR_AUDIT.exists():
        return counts
    with open(ADVISOR_AUDIT) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("time", "")
            if not t.startswith(today):
                continue
            kind = rec.get("type", "unknown")
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _is_protective_pending_order(rec: dict) -> bool:
    order_type = str(rec.get("type") or "").upper()
    notes = str(rec.get("notes") or "")
    return order_type in {"STOP", "TP"} or notes.startswith("[auto-stop]")


def _count_pending_orders() -> int:
    """以 order_id 取最後狀態統計非保護性 PENDING。"""
    if not ORDERS.exists():
        return 0
    last_state: dict[str, dict] = {}
    with open(ORDERS) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            oid = rec.get("order_id")
            status = rec.get("status")
            if oid and status:
                last_state[oid] = rec
    return sum(
        1 for rec in last_state.values()
        if rec.get("status") == "PENDING" and not _is_protective_pending_order(rec)
    )


def _last_fill() -> str:
    if not FILLS.exists():
        return "(無 fills 檔)"
    last = None
    with open(FILLS) as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return "(空)"
    try:
        d = json.loads(last)
        return f"{d.get('filled_at', '?')} {d.get('side', '?')} {d.get('symbol', '?')} x{d.get('qty', '?')} @{d.get('price', '?')}"
    except json.JSONDecodeError:
        return last[:80]


def build_report() -> tuple[str, str, bool]:
    today = datetime.now().strftime("%Y-%m-%d")
    plists = _check_plists()
    audit = _count_today_audit_by_type(today)
    pending = _count_pending_orders()
    last_fill = _last_fill()

    decision_today = audit.get("decision_round", 0)
    plists_ok = all(
        v.isdigit() if label in DAEMON_PLISTS else v.startswith("LOADED")
        for label, v in plists.items()
    )
    overall_ok = plists_ok and pending == 0

    emoji = "✅" if overall_ok else "⚠️"
    title = f"{emoji} sim 盤前檢查 {today} 08:55"

    lines: list[str] = []
    lines.append("【launchd plist】")
    for label, status in plists.items():
        if label in DAEMON_PLISTS:
            mark = "✅" if status.isdigit() else "❌"
        else:
            mark = "✅" if status.startswith("LOADED") else "❌"
        lines.append(f"{mark} {label}  {status}")
    lines.append("")
    lines.append("【今日 advisor 起跑狀態】")
    pending_mark = "✅" if pending == 0 else "⚠️"
    lines.append(f"{pending_mark} orders.jsonl 非保護性 PENDING 殘留: {pending}（STOP/TP/[auto-stop] 保護單不列入清理異常）")
    lines.append(f"ℹ️ advisor decision_round 今日: {decision_today}（08:55 跑時應為 0；若 > 0 代表 launchd 觸發晚於 09:00）")
    if audit:
        other = ", ".join(f"{k}={v}" for k, v in audit.items() if k != "decision_round")
        if other:
            lines.append(f"ℹ️ 其他 audit type 今日: {other}")
    lines.append(f"ℹ️ fills 末筆: {last_fill}")
    lines.append("")
    lines.append(f"整體判定: {'OK 開盤就緒' if overall_ok else '有異常請檢查 logs/sim/'}")

    return title, "\n".join(lines), overall_ok


def _send_dm(title: str, content: str) -> bool:
    from sim._notify import send_dm
    return send_dm(title, content)


def main() -> int:
    parser = argparse.ArgumentParser(prog="sim.morning_check")
    parser.add_argument("--no-send", action="store_true", help="只印不推送")
    parser.add_argument("--force", action="store_true", help="繞過休市日檢查（假日仍跑）")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(AUTOMATION_DIR / ".env")
    except ImportError:
        pass

    if not args.force and not args.no_send:
        from sim.calendar import is_trading_day
        if not is_trading_day():
            print("[morning_check] 今日非交易日（週末或台股假日）— skip（用 --force 繞過）")
            return 0

    title, content, _ok = build_report()

    if args.no_send:
        print(title)
        print()
        print(content)
        return 0

    if _send_dm(title, content):
        print(f"✅ 盤前檢查已推送 ({title})")
        return 0
    print(f"❌ 推送失敗")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
