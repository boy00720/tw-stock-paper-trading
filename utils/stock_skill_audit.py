#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股市 skill 推送與錯誤審計工具。"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
DESKTOP_DIR = Path.home() / "Desktop"

WECHAT_BAD_STATUSES = {
    "error",
    "queued_for_repair",
    "queued_waiting_context",
    "replayed_error",
}
MASTER_ERROR_KEYS = (
    "feishu_failed",
    "wechat_failed",
    "feishu_pdf_failed",
    "feishu_pdf_upload_failed",
    "wechat_pdf_failed",
    "pdf_missing",
)
ORCHESTRATOR_ERROR_PATTERN = re.compile(
    r"ERROR|Traceback|Max retries reached|執行失敗|初始化失敗|回傳錯誤碼",
    re.IGNORECASE,
)
KNOWN_WARNING_PATTERN = re.compile(r"記憶管家初始化失敗|chromadb|sentence_transformers", re.IGNORECASE)
STOCK_KEYWORDS = (
    "stock",
    "screener",
    "sepa",
    "value",
    "market_review",
    "daily_stock_report",
    "股市",
    "選股",
    "台股",
    "大盤",
    "SEPA",
    "短線",
    "成長選股",
    "價值選股",
)


@dataclass
class AuditFinding:
    level: str
    source: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditResult:
    date: str
    generated_at: str
    since: datetime | None
    bridge_health: dict[str, Any] | None
    findings: list[AuditFinding]
    wechat_records_checked: int
    master_events_checked: int
    orchestrator_lines_checked: int

    @property
    def has_blocker(self) -> bool:
        return any(f.level == "error" for f in self.findings)

    @property
    def has_warning(self) -> bool:
        return any(f.level == "warning" for f in self.findings)

    @property
    def conclusion(self) -> str:
        if self.has_blocker:
            return "尚未閉環"
        if self.has_warning:
            return "有 warning，需人工確認"
        return "全部閉環"

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "conclusion": self.conclusion,
            "since": self.since.isoformat(timespec="seconds") if self.since else None,
            "bridge_health": self.bridge_health,
            "wechat_records_checked": self.wechat_records_checked,
            "master_events_checked": self.master_events_checked,
            "orchestrator_lines_checked": self.orchestrator_lines_checked,
            "findings": [
                {
                    "level": f.level,
                    "source": f.source,
                    "message": f.message,
                    "details": f.details,
                }
                for f in self.findings
            ],
        }


def _parse_date(value: str) -> str:
    if value in {"today", ""}:
        return date.today().isoformat()
    datetime.strptime(value, "%Y-%m-%d")
    return value


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_row_time(row: dict[str, Any]) -> datetime | None:
    raw = str(row.get("time") or row.get("timestamp") or row.get("created_at") or "")
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _row_after(row: dict[str, Any], since: datetime | None) -> bool:
    if since is None:
        return True
    parsed = _parse_row_time(row)
    return parsed is not None and parsed >= since


def _line_after(line: str, since: datetime | None) -> bool:
    if since is None:
        return True
    if len(line) < 19:
        return False
    try:
        parsed = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return parsed >= since


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _record_date(row: dict[str, Any]) -> str:
    raw = str(row.get("time") or row.get("timestamp") or row.get("created_at") or "")
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return ""


def _is_stock_related(row: dict[str, Any]) -> bool:
    text = json.dumps(row, ensure_ascii=False)
    return any(keyword in text for keyword in STOCK_KEYWORDS)


def _load_failed_queue() -> Any:
    path = LOG_DIR / "wechat_failed_queue.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_parse_error": True}


def _pending_queue_count(queue: Any) -> int:
    if queue is None:
        return 0
    if isinstance(queue, list):
        return len(queue)
    if isinstance(queue, dict):
        for key in ("items", "queue", "failed", "pending"):
            value = queue.get(key)
            if isinstance(value, list):
                return len(value)
        if queue.get("_parse_error"):
            return 1
    return 0


def _bridge_health() -> tuple[dict[str, Any] | None, AuditFinding | None]:
    for port in (3001, 5123):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                data["_port"] = port
                finding = None
                if data.get("requires_user_refresh") or data.get("context_invalid"):
                    finding = AuditFinding(
                        "warning",
                        "wechat_health",
                        "微信 bridge context 需要刷新，可能需在微信回覆 Ping",
                        data,
                    )
                elif data.get("ready_for_push") is False and data.get("summary_status") not in {"ready", "ok", None}:
                    finding = AuditFinding("warning", "wechat_health", "微信 bridge 目前不是 ready 狀態", data)
                return data, finding
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    return None, AuditFinding("warning", "wechat_health", "無法連線到微信 bridge health，僅依本地 delivery log 審計", {})


def _audit_wechat(target_date: str, since: datetime | None = None) -> tuple[list[AuditFinding], int]:
    rows = [
        row for row in _read_json_lines(LOG_DIR / "wechat_delivery.jsonl")
        if _record_date(row) == target_date and _row_after(row, since) and _is_stock_related(row)
    ]
    findings: list[AuditFinding] = []
    replayed_success = {
        str(row.get("queue_id") or "")
        for row in rows
        if row.get("status") == "replayed_success" and row.get("queue_id")
    }

    for row in rows:
        status = str(row.get("status") or "")
        queue_id = str(row.get("queue_id") or row.get("repair_queue_id") or "")
        if status in WECHAT_BAD_STATUSES:
            if queue_id and queue_id in replayed_success:
                findings.append(
                    AuditFinding(
                        "warning",
                        "wechat_delivery",
                        f"微信曾出現 {status}，但 repair 已 replayed_success",
                        {
                            "trace_id": row.get("trace_id") or row.get("original_trace_id"),
                            "queue_id": queue_id,
                            "status": status,
                        },
                    )
                )
            else:
                findings.append(
                    AuditFinding(
                        "error",
                        "wechat_delivery",
                        f"微信推送異常且尚未確認補發成功：{status}",
                        {
                            "trace_id": row.get("trace_id") or row.get("original_trace_id"),
                            "queue_id": queue_id,
                            "status": status,
                            "msg": row.get("msg") or row.get("errmsg"),
                        },
                    )
                )

    queue = _load_failed_queue()
    queue_count = _pending_queue_count(queue)
    if queue_count:
        findings.append(
            AuditFinding(
                "error",
                "wechat_failed_queue",
                f"微信 repair queue 仍有 {queue_count} 筆待處理",
                {"queue_file": str(LOG_DIR / "wechat_failed_queue.json")},
            )
        )
    return findings, len(rows)


def _audit_master_events(target_date: str, since: datetime | None = None) -> tuple[list[AuditFinding], int]:
    path = LOG_DIR / "master_agent_audit" / f"events_{target_date}.jsonl"
    rows = [
        row for row in _read_json_lines(path)
        if (row.get("event_type") or row.get("event")) == "payload_dispatch" and _row_after(row, since) and _is_stock_related(row)
    ]
    findings: list[AuditFinding] = []
    for row in rows:
        data = row.get("data") or {}
        errors = data.get("errors") or []
        if data.get("success") is False or errors:
            level = "error" if any(any(key in str(err) for key in MASTER_ERROR_KEYS) for err in errors) else "warning"
            findings.append(
                AuditFinding(
                    level,
                    "master_payload_dispatch",
                    "master payload 通知分發異常",
                    {
                        "step_name": row.get("step_name"),
                        "agent": data.get("agent"),
                        "errors": errors,
                        "channels": data.get("channels"),
                    },
                )
            )
    return findings, len(rows)


def _audit_orchestrator_log(target_date: str, since: datetime | None = None, max_lines: int = 1200) -> tuple[list[AuditFinding], int]:
    path = LOG_DIR / "orchestrator.log"
    if not path.exists():
        return [AuditFinding("warning", "orchestrator_log", "找不到 orchestrator.log", {})], 0
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    target_lines = [line for line in lines if line.startswith(target_date) and _line_after(line, since)]
    findings: list[AuditFinding] = []
    for line in target_lines:
        if not ORCHESTRATOR_ERROR_PATTERN.search(line):
            continue
        if KNOWN_WARNING_PATTERN.search(line):
            findings.append(AuditFinding("warning", "orchestrator_log", "已知可降級 warning", {"line": line[:600]}))
        elif any(keyword in line for keyword in STOCK_KEYWORDS):
            findings.append(AuditFinding("error", "orchestrator_log", "股市流程出現錯誤 log", {"line": line[:600]}))
    return findings, len(target_lines)


def run_audit(target_date: str, repair: bool = False, since: datetime | None = None) -> AuditResult:
    target_date = _parse_date(target_date)
    findings: list[AuditFinding] = []

    health, health_finding = _bridge_health()
    if health_finding:
        findings.append(health_finding)

    wechat_findings, wechat_checked = _audit_wechat(target_date, since)
    findings.extend(wechat_findings)

    master_findings, master_checked = _audit_master_events(target_date, since)
    findings.extend(master_findings)

    log_findings, log_checked = _audit_orchestrator_log(target_date, since)
    findings.extend(log_findings)

    if repair:
        pending = [f for f in findings if f.source in {"wechat_delivery", "wechat_failed_queue"} and f.level == "error"]
        if pending and (not health or health.get("requires_user_refresh") or health.get("context_invalid")):
            findings.append(
                AuditFinding(
                    "warning",
                    "repair",
                    "偵測到微信 pending，但 context 不可用；請先在微信回覆 Ping 後再審計",
                    {},
                )
            )
        elif pending:
            findings.append(
                AuditFinding(
                    "warning",
                    "repair",
                    "偵測到微信 pending；目前只做審計，不重造通知，請使用既有 bridge repair 機制補發",
                    {},
                )
            )

    return AuditResult(
        date=target_date,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        since=since,
        bridge_health=health,
        findings=findings,
        wechat_records_checked=wechat_checked,
        master_events_checked=master_checked,
        orchestrator_lines_checked=log_checked,
    )


def render_markdown(result: AuditResult, command_results: list[dict[str, Any]] | None = None) -> str:
    command_results = command_results or []
    lines = [
        f"# 股市 skill 執行摘要｜{result.generated_at}",
        "",
        f"## 最終結論：{result.conclusion}",
        "",
        "## 執行命令",
        "",
    ]
    if command_results:
        lines.extend(["| mode | exit code | 狀態 |", "|---|---:|---|"])
        for item in command_results:
            status = "成功" if item.get("exit_code") == 0 else "失敗"
            lines.append(f"| `{item.get('mode')}` | {item.get('exit_code')} | {status} |")
        lines.append("")
    else:
        lines.append("本次只執行審計，未執行 orchestrator。")
        lines.append("")

    lines.extend([
        "## 審計統計",
        "",
        f"- 日期：`{result.date}`",
        f"- 審計起點：`{result.since.isoformat(timespec='seconds') if result.since else '當日全部紀錄'}`",
        f"- 微信 delivery 檢查筆數：`{result.wechat_records_checked}`",
        f"- master payload dispatch 檢查筆數：`{result.master_events_checked}`",
        f"- orchestrator log 今日檢查行數：`{result.orchestrator_lines_checked}`",
        "",
        "## 微信 Bridge Health",
        "",
    ])
    if result.bridge_health:
        health = result.bridge_health
        lines.extend([
            f"- port：`{health.get('_port')}`",
            f"- ready_for_push：`{health.get('ready_for_push')}`",
            f"- requires_user_refresh：`{health.get('requires_user_refresh')}`",
            f"- context_invalid：`{health.get('context_invalid')}`",
            f"- pending_failed_count：`{health.get('pending_failed_count')}`",
            f"- summary：`{health.get('summary_text') or health.get('summary_status')}`",
        ])
    else:
        lines.append("- 無法取得 bridge health；已改用本地 log 審計。")

    lines.extend(["", "## 異常與 warning", ""])
    if not result.findings:
        lines.append("未偵測到漏推、repair pending 或股市流程錯誤。")
    else:
        lines.extend(["| 等級 | 來源 | 訊息 | 細節 |", "|---|---|---|---|"])
        for finding in result.findings:
            details = json.dumps(finding.details, ensure_ascii=False)
            if len(details) > 240:
                details = details[:237] + "..."
            lines.append(f"| {finding.level} | `{finding.source}` | {finding.message} | `{details}` |")

    if result.has_blocker:
        lines.extend([
            "",
            "## 下一步",
            "",
            "- 若錯誤與微信 context 有關，先在微信回覆 `Ping`，再重新執行審計。",
            "- 若是 master payload 或飛書 PDF 錯誤，查看 `logs/master_agent_audit/` 與 `logs/orchestrator.log`。",
        ])
    return "\n".join(lines).rstrip() + "\n"


def write_desktop_summary(result: AuditResult, command_results: list[dict[str, Any]] | None = None) -> Path:
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = DESKTOP_DIR / f"股市skill執行摘要_{stamp}.md"
    path.write_text(render_markdown(result, command_results), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="股市 skill 推送與錯誤審計")
    parser.add_argument("--date", default="today", help="審計日期，格式 YYYY-MM-DD 或 today")
    parser.add_argument("--since", default="", help="只審計此 ISO 時間之後的新紀錄")
    parser.add_argument("--repair", action="store_true", help="允許 repair 檢查提示")
    parser.add_argument("--no-repair", action="store_true", help="只審計，不做 repair 相關動作")
    parser.add_argument("--write-summary", action="store_true", help="將審計摘要寫到桌面")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args()

    result = run_audit(args.date, repair=args.repair and not args.no_repair, since=parse_since(args.since))
    if args.write_summary:
        path = write_desktop_summary(result)
        print(path)
    elif args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
    return 1 if result.has_blocker else 0


if __name__ == "__main__":
    raise SystemExit(main())
