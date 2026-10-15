#!/usr/bin/env python3
"""離線日誌錯誤監控 + 飛書主動推送。

設計重點：
- launchd 以短命令定時呼叫本腳本（預設跑一輪退出）。
- 只讀每個 log 自上次 offset 後的新內容；首次啟用可從檔案尾端建立 baseline。
- 以 rule + normalized line 產生 fingerprint，配合 cooldown 去重，避免洗版。
- --no-send 只列出會推送的事件，便於驗證。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml


AUTOMATION_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = AUTOMATION_DIR / "config" / "log_error_monitor.yaml"
DEFAULT_STATE_PATH = AUTOMATION_DIR / "logs" / "error_monitor_state.json"
DEFAULT_AUDIT_PATH = AUTOMATION_DIR / "logs" / "error_monitor_audit.jsonl"
DEFAULT_LOG_PATH = AUTOMATION_DIR / "logs" / "log-error-monitor.log"

logger = logging.getLogger("log_error_monitor")


@dataclass
class Rule:
    id: str
    severity: str
    pattern: str
    tags: list[str] = field(default_factory=list)
    regex: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        self.regex = re.compile(self.pattern, re.IGNORECASE)


@dataclass
class Event:
    path: str
    rule_id: str
    severity: str
    line_no: int
    line: str
    context: list[str]
    fingerprint: str
    tags: list[str]
    detected_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "line_no": self.line_no,
            "line": self.line,
            "context": self.context,
            "fingerprint": self.fingerprint,
            "tags": self.tags,
            "detected_at": self.detected_at,
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"files": {}, "fingerprints": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("state 讀取失敗，改用空 state: %s", path)
        return {"files": {}, "fingerprints": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def expand_files(patterns: Iterable[str], base_dir: Path = AUTOMATION_DIR) -> list[Path]:
    paths: set[Path] = set()
    for pat in patterns:
        pattern = str(base_dir / pat) if not str(pat).startswith("/") else str(pat)
        for match in glob.glob(pattern):
            p = Path(match)
            if p.is_file():
                paths.add(p.resolve())
    return sorted(paths)


def file_identity(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def _normalize_for_fingerprint(line: str) -> str:
    text = line.strip()
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+", "<ts>", text)
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}(?:,\d+)?\b", "<time>", text)
    # 股票代號歸一：避免批量逐檔錯誤（如 yfinance 100 檔各報一行）因 symbol 不同
    # 爆出不同指紋、整批繞過 cooldown 洗版。台股 .TW/.TWO、港股 .HK 都歸成 <ticker>。
    text = re.sub(r"\b\d{3,6}\.(?:TW|TWO|HK)\b", "<ticker>", text, flags=re.IGNORECASE)
    text = re.sub(r"ord-[a-f0-9]{8,}\b", "ord-<id>", text, flags=re.IGNORECASE)
    text = re.sub(r"fill-[a-f0-9]{8,}\b", "fill-<id>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text[:500]


def make_fingerprint(path: Path, rule: Rule, line: str) -> str:
    payload = f"{path.name}|{rule.id}|{_normalize_for_fingerprint(line)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _decode_bytes(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def read_incremental(
    path: Path,
    state: dict[str, Any],
    *,
    baseline_on_first_run: bool,
    max_bytes_per_file: int,
) -> tuple[list[str], int, int]:
    """讀取 path 新增內容，回傳 (lines, start_offset, end_offset)。"""
    files_state = state.setdefault("files", {})
    key = str(path)
    ident = file_identity(path)
    prev = files_state.get(key)
    size = int(ident["size"])

    if prev is None:
        start = size if baseline_on_first_run else max(0, size - max_bytes_per_file)
    else:
        same_inode = prev.get("inode") == ident["inode"]
        prev_offset = int(prev.get("offset", 0) or 0)
        if (not same_inode) or size < prev_offset:
            # rotate / truncate：保守讀尾端，但避免全量掃爆。
            start = max(0, size - max_bytes_per_file)
        else:
            start = prev_offset

    if size - start > max_bytes_per_file:
        start = size - max_bytes_per_file

    if start >= size:
        files_state[key] = {**ident, "offset": size, "updated_at": _now_iso()}
        return [], start, size

    with path.open("rb") as f:
        f.seek(start)
        data = f.read(size - start)

    text = _decode_bytes(data)
    lines = text.splitlines()
    files_state[key] = {**ident, "offset": size, "updated_at": _now_iso()}
    return lines, start, size


def build_rules(config: dict[str, Any]) -> list[Rule]:
    return [
        Rule(
            id=str(item["id"]),
            severity=str(item.get("severity", "warning")),
            pattern=str(item["pattern"]),
            tags=list(item.get("tags") or []),
        )
        for item in config.get("rules", [])
    ]


def build_ignore_regexes(config: dict[str, Any]) -> list[re.Pattern[str]]:
    """config.ignore_patterns → 已編譯正則；命中的行視為已知可容忍噪音，直接跳過。"""
    out: list[re.Pattern[str]] = []
    for pat in config.get("ignore_patterns", []) or []:
        try:
            out.append(re.compile(str(pat), re.IGNORECASE))
        except re.error:
            logger.warning("ignore_pattern 編譯失敗，略過: %r", pat)
    return out


def match_events(
    path: Path,
    lines: list[str],
    rules: list[Rule],
    *,
    context_before: int,
    context_after: int,
    ignore_regexes: Optional[list[re.Pattern[str]]] = None,
) -> tuple[list[Event], int]:
    """掃描 lines → (events, ignored)。命中 ignore_regexes 的行不產生事件，只計數。"""
    ignore_regexes = ignore_regexes or []
    events: list[Event] = []
    ignored = 0
    detected_at = _now_iso()
    for idx, line in enumerate(lines):
        if any(rx.search(line) for rx in ignore_regexes):
            ignored += 1
            continue
        for rule in rules:
            if not rule.regex.search(line):
                continue
            lo = max(0, idx - context_before)
            hi = min(len(lines), idx + context_after + 1)
            context = lines[lo:hi]
            events.append(Event(
                path=str(path),
                rule_id=rule.id,
                severity=rule.severity,
                line_no=idx + 1,
                line=line.strip()[:1000],
                context=[c[:1000] for c in context],
                fingerprint=make_fingerprint(path, rule, line),
                tags=rule.tags,
                detected_at=detected_at,
            ))
            break
    return events, ignored


def filter_deduped(
    events: list[Event],
    state: dict[str, Any],
    *,
    cooldown_seconds: int,
    max_events_per_run: int,
) -> tuple[list[Event], int]:
    fingerprints = state.setdefault("fingerprints", {})
    now = _now_ts()
    out: list[Event] = []
    suppressed = 0
    seen_this_run: set[str] = set()

    for event in events:
        fp = event.fingerprint
        last = float(fingerprints.get(fp, {}).get("last_sent_ts", 0) or 0)
        if fp in seen_this_run or (last and now - last < cooldown_seconds):
            suppressed += 1
            continue
        out.append(event)
        seen_this_run.add(fp)
        if len(out) >= max_events_per_run:
            suppressed += max(0, len(events) - (len(out) + suppressed))
            break
    return out, suppressed


def mark_sent(events: list[Event], state: dict[str, Any], *, sent: bool) -> None:
    fingerprints = state.setdefault("fingerprints", {})
    now = _now_ts()
    for event in events:
        fingerprints[event.fingerprint] = {
            "last_sent_ts": now,
            "last_sent_at": _now_iso(),
            "sent": bool(sent),
            "rule_id": event.rule_id,
            "path": event.path,
            "line": event.line[:300],
        }


def format_event(event: Event) -> str:
    rel = str(Path(event.path))
    try:
        rel = str(Path(event.path).relative_to(AUTOMATION_DIR))
    except ValueError:
        pass
    tags = ",".join(event.tags) if event.tags else "—"
    context = "\n".join(event.context[-6:])
    return "\n".join([
        f"severity: {event.severity}",
        f"rule: {event.rule_id}",
        f"file: {rel}",
        f"fingerprint: {event.fingerprint}",
        f"tags: {tags}",
        "",
        "命中行：",
        event.line,
        "",
        "上下文：",
        context,
    ])


def resolve_lark_target(config: dict[str, Any]) -> tuple[str, str]:
    notify = config.get("notify") or {}
    open_id_env = notify.get("prefer_open_id_env") or "FEISHU_USER_OPEN_ID"
    open_id = os.getenv(open_id_env, "")
    if open_id:
        return open_id, "open_id"

    chat_env = notify.get("fallback_chat_id_env") or "FEISHU_DEFAULT_CHAT_ID"
    chat_id = os.getenv(chat_env, "")
    if chat_id:
        return chat_id, "chat_id"

    # 最後回退到 lark_config.yaml dimension。
    try:
        lark_cfg_path = AUTOMATION_DIR / str(notify.get("lark_config_path") or "config/lark_config.yaml")
        lark_cfg = load_yaml(lark_cfg_path)
        dim = notify.get("fallback_dimension") or "stock_monitor_agent"
        channel_id = (lark_cfg.get("dimensions") or {}).get(dim, {}).get("channel_id", "")
        if channel_id:
            return channel_id, "chat_id"
    except Exception:
        logger.exception("resolve_lark_target failed")
    return "", "chat_id"


def send_lark_event(event: Event, config: dict[str, Any]) -> bool:
    notify = config.get("notify") or {}
    if not notify.get("enabled", True):
        return False
    sys.path.insert(0, str(AUTOMATION_DIR))
    from utils.lark_notifier import LarkNotifier

    lark_config_path = AUTOMATION_DIR / str(notify.get("lark_config_path") or "config/lark_config.yaml")
    notifier = LarkNotifier(str(lark_config_path))
    receive_id, receive_id_type = resolve_lark_target(config)
    if not receive_id:
        logger.warning("飛書目標為空，跳過推送")
        return False
    title = f"🚨 automation 日誌異常：{event.rule_id}"
    return notifier.send_message_chunked(
        receive_id,
        title,
        format_event(event),
        receive_id_type=receive_id_type,
    )


def run_once(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    no_send: bool = False,
    baseline_on_first_run_override: Optional[bool] = None,
) -> dict[str, Any]:
    config = load_yaml(config_path)
    if not config.get("enabled", True):
        return {"status": "disabled", "events": 0, "sent": 0, "suppressed": 0}

    state = load_state(state_path)
    rules = build_rules(config)
    ignore_regexes = build_ignore_regexes(config)
    files = expand_files(config.get("files", []))
    baseline = bool(config.get("baseline_on_first_run", True))
    if baseline_on_first_run_override is not None:
        baseline = baseline_on_first_run_override
    max_bytes = int(config.get("max_bytes_per_file", 65536))
    context_before = int(config.get("context_before", 2))
    context_after = int(config.get("context_after", 2))

    all_events: list[Event] = []
    ignored_total = 0
    for path in files:
        try:
            lines, start, end = read_incremental(
                path,
                state,
                baseline_on_first_run=baseline,
                max_bytes_per_file=max_bytes,
            )
            if not lines:
                continue
            logger.info("scan %s bytes=%s-%s lines=%s", path, start, end, len(lines))
            evs, ignored = match_events(
                path,
                lines,
                rules,
                context_before=context_before,
                context_after=context_after,
                ignore_regexes=ignore_regexes,
            )
            all_events.extend(evs)
            ignored_total += ignored
        except Exception as exc:
            logger.exception("scan failed: %s", path)
            synthetic = Event(
                path=str(path),
                rule_id="monitor-scan-failed",
                severity="critical",
                line_no=0,
                line=f"monitor scan failed: {exc}",
                context=[repr(exc)],
                fingerprint=hashlib.sha256(f"scan-failed|{path}|{exc}".encode()).hexdigest()[:16],
                tags=["monitor", "self_error"],
                detected_at=_now_iso(),
            )
            all_events.append(synthetic)

    selected, suppressed = filter_deduped(
        all_events,
        state,
        cooldown_seconds=int(config.get("cooldown_seconds", 3600)),
        max_events_per_run=int(config.get("max_events_per_run", 5)),
    )

    sent_count = 0
    for event in selected:
        record = {"type": "log_error_event", **event.to_dict(), "no_send": no_send}
        append_jsonl(audit_path, record)
        if no_send:
            print("--- EVENT ---")
            print(format_event(event))
            sent_ok = False
        else:
            sent_ok = send_lark_event(event, config)
            if sent_ok:
                sent_count += 1
        # no-send 也標記，避免 dry-run 同一批歷史錯誤反覆洗終端；測試可用乾淨 state。
        mark_sent([event], state, sent=sent_ok and not no_send)

    save_state(state_path, state)
    result = {
        "status": "ok",
        "files": len(files),
        "matched": len(all_events),
        "ignored": ignored_total,
        "selected": len(selected),
        "sent": sent_count,
        "suppressed": suppressed,
        "no_send": no_send,
    }
    logger.info("result %s", result)
    return result


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(DEFAULT_LOG_PATH, encoding="utf-8"),
        ],
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="離線日誌錯誤監控 + 飛書主動推送")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--audit-path", default=str(DEFAULT_AUDIT_PATH))
    parser.add_argument("--once", action="store_true", help="跑一輪後退出（預設行為）")
    parser.add_argument("--no-send", action="store_true", help="不發飛書，只印出會推送的事件")
    parser.add_argument("--scan-existing", action="store_true", help="首次無 state 時也掃描既有尾端內容（測試用）")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    result = run_once(
        config_path=Path(args.config),
        state_path=Path(args.state_path),
        audit_path=Path(args.audit_path),
        no_send=args.no_send,
        baseline_on_first_run_override=False if args.scan_existing else None,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in {"ok", "disabled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
