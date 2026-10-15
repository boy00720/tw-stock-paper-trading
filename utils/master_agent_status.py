#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Master agent status summaries.

提供主控通知路由的健康摘要與稽核摘要，供微信 bot / 飛書 bot 共用。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def audit_root() -> Path:
    return Path(__file__).resolve().parent.parent / "logs" / "master_agent_audit"


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def load_metrics(date_str: str = "") -> Dict[str, Any]:
    final_date = _safe_str(date_str) or _today_str()
    return _read_json(audit_root() / f"metrics_{final_date}.json")


def load_events(date_str: str = "") -> List[Dict[str, Any]]:
    final_date = _safe_str(date_str) or _today_str()
    return _read_jsonl(audit_root() / f"events_{final_date}.jsonl")


def _format_channel_line(name: str, payload: Dict[str, Any]) -> str:
    attempts = int(payload.get("attempts", 0) or 0)
    sent = int(payload.get("sent", 0) or 0)
    failed = int(payload.get("failed", 0) or 0)
    return f"- {name}: 嘗試 {attempts} / 成功 {sent} / 失敗 {failed}"


def _collect_failed_dispatches(metrics: Dict[str, Any]) -> List[str]:
    step_states = metrics.get("step_last_status") or {}
    lines: List[str] = []
    for step_name, payload in step_states.items():
        if not isinstance(payload, dict):
            continue
        if payload.get("event_type") != "payload_dispatch":
            continue
        if payload.get("success") is True:
            continue
        timestamp = _safe_str(payload.get("timestamp"))
        agent = _safe_str(payload.get("agent")) or "unknown"
        errors = payload.get("errors") or []
        error_text = " | ".join(str(item) for item in errors) if errors else "dispatch_failed"
        lines.append(f"- {step_name} ({agent}) @ {timestamp}: {error_text}")
    return lines


def format_master_health_summary(date_str: str = "") -> str:
    final_date = _safe_str(date_str) or _today_str()
    metrics = load_metrics(final_date)
    if not metrics:
        return f"主控狀態\n日期: {final_date}\n尚未找到主控 audit / metrics。"

    dispatch = metrics.get("dispatch") or {}
    channels = dispatch.get("channels") or {}
    interactive = metrics.get("interactive_exceptions") or {}
    event_counts = metrics.get("event_counts") or {}

    lines = [
        "主控狀態",
        f"日期: {final_date}",
        f"最近更新: {_safe_str(metrics.get('last_updated_at')) or '未知'}",
        "",
        "通知主流程",
        f"- payload emitted: {int(event_counts.get('payload_emitted', 0) or 0)}",
        f"- dispatch steps: {int(dispatch.get('attempted_steps', 0) or 0)}",
        f"- dispatch 成功: {int(dispatch.get('successful_steps', 0) or 0)}",
        f"- dispatch 失敗: {int(dispatch.get('failed_steps', 0) or 0)}",
    ]

    if channels:
        lines.append("")
        lines.append("渠道統計")
        for name in ("feishu", "wechat"):
            payload = channels.get(name)
            if isinstance(payload, dict):
                lines.append(_format_channel_line(name, payload))

    lines.extend(
        [
            "",
            "互動例外",
            f"- 總次數: {int(interactive.get('total', 0) or 0)}",
            f"- 成功: {int(interactive.get('success', 0) or 0)}",
            f"- 失敗: {int(interactive.get('failed', 0) or 0)}",
        ]
    )

    by_name = interactive.get("by_name") or {}
    if by_name:
        for name, payload in sorted(by_name.items()):
            if not isinstance(payload, dict):
                continue
            lines.append(
                f"- {name}: {int(payload.get('total', 0) or 0)} 次 "
                f"(成功 {int(payload.get('success', 0) or 0)} / 失敗 {int(payload.get('failed', 0) or 0)})"
            )

    failed_dispatches = _collect_failed_dispatches(metrics)
    lines.append("")
    lines.append("最近失敗")
    if failed_dispatches:
        lines.extend(failed_dispatches[:5])
    else:
        lines.append("- 今天尚未記錄通知失敗")

    return "\n".join(lines)


def _format_event_line(entry: Dict[str, Any]) -> str:
    timestamp = _safe_str(entry.get("timestamp"))
    time_only = timestamp[11:19] if len(timestamp) >= 19 else timestamp or "未知時間"
    event_type = _safe_str(entry.get("event_type")) or "unknown"
    step_name = _safe_str(entry.get("step_name")) or "-"
    data = entry.get("data") or {}

    if event_type == "payload_emitted":
        agent = _safe_str(data.get("agent")) or "unknown"
        channels = ",".join(data.get("channels") or []) or "-"
        return f"- {time_only} emitted {step_name} ({agent}) channels={channels}"

    if event_type == "payload_dispatch":
        agent = _safe_str(data.get("agent")) or "unknown"
        success = "成功" if data.get("success") else "失敗"
        errors = " | ".join(str(item) for item in (data.get("errors") or [])) or "-"
        return f"- {time_only} dispatch {step_name} ({agent}) {success} errors={errors}"

    if event_type == "interactive_exception":
        name = _safe_str(data.get("name")) or step_name or "interactive"
        channel = _safe_str(data.get("channel")) or "-"
        success = "成功" if data.get("success") else "失敗"
        return f"- {time_only} interactive {name} via {channel} {success}"

    return f"- {time_only} {event_type} {step_name}"


def format_master_audit_summary(date_str: str = "", limit: int = 8) -> str:
    final_date = _safe_str(date_str) or _today_str()
    metrics = load_metrics(final_date)
    events = load_events(final_date)
    if not metrics and not events:
        return f"主控稽核\n日期: {final_date}\n今天尚未找到主控 audit 記錄。"

    failed_dispatches = _collect_failed_dispatches(metrics)
    interactive = (metrics.get("interactive_exceptions") or {}) if metrics else {}

    lines = [
        "主控稽核",
        f"日期: {final_date}",
        f"最近更新: {_safe_str(metrics.get('last_updated_at')) if metrics else '未知'}",
        "",
        "今日失敗摘要",
    ]
    if failed_dispatches:
        lines.extend(failed_dispatches[:5])
    else:
        lines.append("- 今天尚未記錄通知 dispatch 失敗")

    lines.append(
        f"- 互動例外失敗: {int(interactive.get('failed', 0) or 0)} / {int(interactive.get('total', 0) or 0)}"
    )

    lines.extend(["", f"最近 {max(limit, 1)} 筆事件"])
    if events:
        for entry in events[-max(limit, 1):]:
            lines.append(_format_event_line(entry))
    else:
        lines.append("- 今天還沒有事件")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Master agent status summary")
    parser.add_argument("--kind", choices=["health", "audit"], default="health")
    parser.add_argument("--date", default="")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    if args.kind == "audit":
        print(format_master_audit_summary(args.date, args.limit))
        return
    print(format_master_health_summary(args.date))


if __name__ == "__main__":
    main()
