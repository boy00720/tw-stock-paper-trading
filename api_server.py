#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Python API 伺服器 — 接收 wechat_bridge.mjs 的指令請求 (Port 3003)
支援所有 Agent 指令與通用 AI 聊天
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
import glob as glob_mod

# ── 清除代理，避免 API 流量被截斷 ──────────────────────────
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import yfinance as yf
from FinMind.data import DataLoader

from utils.claude_helper import ClaudeHelper

AUTOMATION_DIR = os.path.dirname(os.path.abspath(__file__))
if AUTOMATION_DIR not in sys.path:
    sys.path.append(AUTOMATION_DIR)
ENV_PATH = os.path.join(AUTOMATION_DIR, ".env")
WORKFLOW_AUDIT_LOG = os.path.join(AUTOMATION_DIR, "logs", "workflow_control_audit.jsonl")
API_SERVER_LOG = os.path.join(AUTOMATION_DIR, "logs", "api_server.log")
LARK_CONFIG_PATH = os.path.join(AUTOMATION_DIR, "config", "lark_config.yaml")

# 微信 context_token invalid 告警去重（reason → 上次發送 epoch ms）
_INVALID_ALERT_COOLDOWN: Dict[str, float] = {}
# 智囊團群（與 lark_config.yaml 一致），告警目的地
_INVALID_ALERT_CHAT_ID = "oc_your_feishu_chat_id"

WORKFLOW_USAGE = (
    "為避免誤觸，workflow 控制僅接受明確命令：\n"
    "• /tw-workflow vacation\n"
    "• /tw-workflow resume\n"
    "• /tw-workflow pause-morning\n"
    "• /tw-workflow pause-afternoon"
)
WORKFLOW_ACTION_ALIASES = {
    "vacation": "vacation",
    "休假模式": "vacation",
    "暫停今天": "vacation",
    "暂停今天": "vacation",
    "resume": "resume",
    "恢復排程": "resume",
    "恢复排程": "resume",
    "取消休假": "resume",
    "pause-morning": "pause-morning",
    "暫停晨報": "pause-morning",
    "暂停晨报": "pause-morning",
    "pause-afternoon": "pause-afternoon",
    "暫停盤後": "pause-afternoon",
    "暂停盘后": "pause-afternoon",
}
WORKFLOW_REPLY_MAP = {
    "vacation": "⏸️ 收到，已為您開啟休假模式。今日的晨報、盤後及晚間日誌皆已暫停執行。",
    "resume": "▶️ 收到，已取消休假模式，恢復今日所有自動排程。",
    "pause-morning": "⏸️ 收到，今日晨報流程已暫停。",
    "pause-afternoon": "⏸️ 收到，今日盤後流程已暫停。",
}
WORKFLOW_STATE_PATCH = {
    "vacation": {
        "skip_morning": True,
        "skip_afternoon": True,
        "skip_evening": True,
    },
    "resume": {
        "skip_morning": False,
        "skip_afternoon": False,
        "skip_evening": False,
    },
    "pause-morning": {"skip_morning": True},
    "pause-afternoon": {"skip_afternoon": True},
}

SERVICE_AUDIT_LOG = os.path.join(AUTOMATION_DIR, "logs", "service_control_audit.jsonl")
SERVICE_ALLOWED_SOURCES = {"wechat-bridge", "feishu-bot"}
SERVICE_USAGE = (
    "服務控制指令：\n"
    "• 查服務狀態\n"
    "• 重啟飛書（或：重啟 gateway / proxy / dsa / 微信bot）"
)
SERVICE_LABELS = {
    "com.w.feishu-bot": "飛書 Bot",
    "com.w.wechat-bridge-3001": "微信 Bridge",
    "com.w.wechat-api-3003": "微信 API",
    "com.w.weixin-claude-bot": "微信 Claude Bot",
    "com.w.claude-gemini-gateway": "Claude Gateway",
    "com.w.claude-nvidia-backup-proxy": "Nvidia Proxy",
    "com.w.dsa.server8000": "DSA Server",
}
SERVICE_STATUS_PATTERNS = [
    re.compile(r"^(?:查|看|查看|檢查|检查)?(?:一下)?(?:服務|服务|service)(?:狀態|状态|status)?$"),
    re.compile(r"^(?:服務|服务)(?:列表|清單|清单)?$"),
]
SERVICE_RESTART_PATTERN = re.compile(r"^(?:重啟|重启|restart)(.*)$")
SERVICE_RESTART_ALIASES = {
    "飛書": "com.w.feishu-bot",
    "飞书": "com.w.feishu-bot",
    "feishu": "com.w.feishu-bot",
    "gateway": "com.w.claude-gemini-gateway",
    "proxy": "com.w.claude-nvidia-backup-proxy",
    "dsa": "com.w.dsa.server8000",
    "微信bot": "com.w.weixin-claude-bot",
    "微信claudebot": "com.w.weixin-claude-bot",
    "微信": "com.w.wechat-bridge-3001",
    "微信bridge": "com.w.wechat-bridge-3001",
    "bridge": "com.w.wechat-bridge-3001",
    "wechat": "com.w.wechat-bridge-3001",
    "wechatbridge": "com.w.wechat-bridge-3001",
    "微信api": "com.w.wechat-api-3003",
    "api": "com.w.wechat-api-3003",
    "wechatapi": "com.w.wechat-api-3003",
}
SERVICE_RESTART_LABELS = set(SERVICE_RESTART_ALIASES.values())
SERVICE_SELF_BLOCK_BY_SOURCE = {
    "wechat-bridge": {"com.w.wechat-bridge-3001", "com.w.wechat-api-3003"},
    "feishu-bot": {"com.w.feishu-bot"},
}

CODE_TASKS_FILE = os.path.join(AUTOMATION_DIR, "logs", "code_control_tasks.json")
CODE_AUDIT_LOG = os.path.join(AUTOMATION_DIR, "logs", "code_control_audit.jsonl")
CODE_EXEC_TIMEOUT_SECONDS = int(os.getenv("CODE_EXEC_TIMEOUT_SECONDS", "900"))

CODE_TARGETS = {
    "feishu": {
        "label": "飛書 Bot",
        "path": "scripts/feishu_bot.py",
    },
    "wechat-bridge": {
        "label": "微信 Bridge",
        "path": "wechat_bridge.mjs",
    },
    "wechat-api": {
        "label": "微信 API",
        "path": "api_server.py",
    },
}

CODE_USAGE = (
    "受控編程指令：\n"
    "• /code create <target> <需求描述>（target：feishu / wechat-bridge / wechat-api）\n"
    "• /code approve <任務ID>\n"
    "• /code run <任務ID>\n"
    "• /code status [任務ID]\n"
    "• /code cancel <任務ID>"
)

CODE_ALLOWED_SOURCES = {"wechat-bridge", "feishu-bot"}
CODE_ALLOWED_TARGETS_BY_SOURCE = {
    "wechat-bridge": {"feishu"},
    "feishu-bot": {"wechat-bridge", "wechat-api"},
}
CODE_TASKS_LOCK = threading.Lock()
MAX_CODE_TASKS = 200

# ── W 的 Profile ─────────────────────────────────────────
PROFILE_SUMMARY = """
你是 W 的私人 AI 助理，暱稱 W，真實姓名未知。

【背景】台灣人，做跨境電商（Amazon / 速賣通 / 獨立站），股市投資者（穩健保守，不做短線）。
【關注】跨境電商、科技 AI 半導體、國際局勢、貴金屬石油、全球財經。
【語言】繁體中文。
【溝通風格】
1. 當 W 只是閒聊或打招呼（如「你好」、「早安」）時，請以同樣簡短自然的口吻回覆，切勿強行塞入長篇大論的工作進度或毒雞湯。
2. 當 W 詢問工作或進度時，才使用：簡短直接、重點優先、附關鍵數字；不喜歡冗長前言；並提供可執行的下一步及毒雞湯。
3. 問候語需符合當下時間（早上→早安、下午→午安、晚上→晚安）；若使用者問候與時間不符，請溫和地用當下時間回覆。
【體重】目前 86kg，目標 80kg，困擾是體重下不去（戒精緻碳水）。
"""

PYTHON = os.getenv("PYTHON_BIN", "/Users/w/.homebrew/bin/python3.13")


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH, override=True)
    except Exception:
        pass


def _safe_str(value) -> str:
    return str(value or "").strip()


def _helper() -> ClaudeHelper:
    _load_env()
    return ClaudeHelper(max_retries=0)


def _normalize_workflow_text(value: str) -> str:
    return _safe_str(value).replace("\u3000", " ").replace("\xa0", " ").strip()


def _extract_workflow_action(value: str) -> Optional[str]:
    normalized = _normalize_workflow_text(value)
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered in WORKFLOW_ACTION_ALIASES:
        return WORKFLOW_ACTION_ALIASES[lowered]
    return WORKFLOW_ACTION_ALIASES.get(normalized)


def _extract_prefixed_workflow_action(value: str) -> Optional[str]:
    normalized = _normalize_workflow_text(value)
    if not normalized:
        return None
    match = re.match(r"^/(?:tw-workflow|workflow)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if not match:
        return None
    return _extract_workflow_action(match.group(1))


def _snapshot_workflow_status(global_state) -> dict:
    try:
        state = global_state.get_state()
    except Exception:
        return {}
    workflow_status = state.get("workflow_status", {})
    return dict(workflow_status) if isinstance(workflow_status, dict) else {}


def _append_workflow_audit(record: dict):
    os.makedirs(os.path.dirname(WORKFLOW_AUDIT_LOG), exist_ok=True)
    with open(WORKFLOW_AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_api_server_log(record: dict):
    os.makedirs(os.path.dirname(API_SERVER_LOG), exist_ok=True)
    with open(API_SERVER_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _handle_wechat_invalid_alert(data: dict) -> Tuple[int, dict]:
    """
    處理 bridge 推來的 context_token invalid 告警。
    payload: {"reason": str, "target": str, "trace_id": str|None}
    回傳: (http_status, body)
    """
    reason = str(data.get("reason") or "unknown").strip() or "unknown"
    target = str(data.get("target") or "").strip()
    trace_id = str(data.get("trace_id") or "").strip() or None

    now_ms = datetime.now().timestamp() * 1000
    cooldown_ms = max(60_000, int(
        os.environ.get("WECHAT_INVALID_ALERT_COOLDOWN_MS", "1800000") or "1800000"))
    last_at = _INVALID_ALERT_COOLDOWN.get(reason, 0.0)
    if now_ms - last_at < cooldown_ms:
        return 200, {
            "alerted": False,
            "reason": "cooldown",
            "cooldown_remain_ms": int(cooldown_ms - (now_ms - last_at)),
        }

    try:
        from utils.lark_notifier import LarkNotifier  # 延遲 import
        notifier = LarkNotifier(LARK_CONFIG_PATH)
        title = "⚠️ 微信 context_token 失效"
        content = (
            f"原因: {reason}\n"
            f"時間: {datetime.now().isoformat(timespec='seconds')}\n"
            f"目標: {target or 'n/a'}\n"
            f"trace_id: {trace_id or 'n/a'}\n\n"
            "下一步: 請到微信對 bot 發任意訊息（例如 ping）刷新 token，"
            "之後 wechat_failed_queue 會自動 replay。"
        )
        ok = notifier.send_message(
            receive_id=_INVALID_ALERT_CHAT_ID,
            title=title,
            content=content,
            receive_id_type="chat_id",
        )
        if ok:
            _INVALID_ALERT_COOLDOWN[reason] = now_ms
            return 200, {"alerted": True, "reason": reason}
        _append_api_server_log({
            "time": datetime.now().isoformat(timespec="seconds"),
            "type": "wechat_invalid_alert",
            "event": "lark_send_failed",
            "reason": reason,
            "target": target,
            "trace_id": trace_id,
        })
        return 502, {"alerted": False, "reason": "lark_send_failed"}
    except Exception as e:
        _append_api_server_log({
            "time": datetime.now().isoformat(timespec="seconds"),
            "type": "wechat_invalid_alert",
            "event": "exception",
            "reason": reason,
            "target": target,
            "trace_id": trace_id,
            "error": str(e),
        })
        return 500, {"alerted": False, "reason": "exception", "error": str(e)}


def _parse_month_value(value: str) -> Optional[int]:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    mapping = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "十一": 11,
        "十二": 12,
    }
    return mapping.get(value)


def _handle_simple_date_diff(text: str) -> Optional[str]:
    normalized = _safe_str(text)
    match = re.search(r"(?:(\d{1,2}|一|二|三|四|五|六|七|八|九|十|十一|十二)\s*月\s*)?(\d{1,2})\s*(?:號|日).{0,12}(?:今天|現在).{0,8}(?:幾天|多少天)", normalized)
    if not match:
        return None
    month = _parse_month_value(match.group(1) or "") or datetime.now().month
    day = int(match.group(2))
    today = datetime.now().date()
    try:
        start = today.replace(month=month, day=day)
    except ValueError:
        return None
    if start > today:
        start = start.replace(year=start.year - 1)
    elapsed_exclusive = (today - start).days
    elapsed_inclusive = elapsed_exclusive + 1
    return f"從 {start.strftime('%Y/%m/%d')} 到今天 {today.strftime('%Y/%m/%d')}：不含起始日是 {elapsed_exclusive} 天；含起始日是 {elapsed_inclusive} 天。"


def _audit_chat_reply(command: str, source: str, text: str, reply: str):
    _append_api_server_log({
        "time": datetime.now().isoformat(timespec="seconds"),
        "type": "chat_handler",
        "event": "reply_ready",
        "command": _safe_str(command),
        "source": _safe_str(source) or "unknown",
        "input_preview": _safe_str(text)[:200],
        "reply_preview": _safe_str(reply)[:500],
        "reply_len": len(_safe_str(reply)),
    })


def _audit_llm_route_event(event: str, route: dict, error: Exception = None):
    route = route or {}
    env = route.get("env") or {}
    base_host = _safe_str(env.get("ANTHROPIC_BASE_URL")) or "https://api.anthropic.com"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_host)
        if parsed.netloc:
            base_host = parsed.netloc
    except Exception:
        pass

    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "type": "llm_route",
        "event": event,
        "label": _safe_str(route.get("label")) or None,
        "source": _safe_str(route.get("source")) or None,
        "app_type": _safe_str(route.get("app_type")) or None,
        "provider_id": _safe_str(route.get("provider_id")) or None,
        "model": _safe_str(route.get("model")) or None,
        "base_host": base_host,
        "retryable": _is_retryable_route_error(error) if error else None,
        "error": _safe_str(error)[:500] if error else None,
    }
    _append_api_server_log(record)


def _audit_chat_fallback_event(text: str, error: Exception, source: str):
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "type": "chat_fallback",
        "event": "llm_unavailable",
        "source": _safe_str(source) or "unknown",
        "input_preview": _safe_str(text)[:120],
        "error": _safe_str(error)[:500],
    }
    _append_api_server_log(record)


def _summarize_route_error_reason(error_text: str) -> str:
    lowered = _safe_str(error_text).lower()
    if not lowered:
        return "未知錯誤"

    if any(token in lowered for token in ["401", "authentication", "invalid_api_key", "auth_token", "api key", "api_key", "authorization"]):
        return "認證失敗"
    if any(token in lowered for token in ["403", "forbidden", "blocked"]):
        return "權限受限"
    if any(token in lowered for token in ["429", "rate limit", "rate_limit", "quota_exhausted"]):
        return "請求過於頻繁"
    if any(token in lowered for token in ["503", "502", "504", "overloaded_error", "resource_exhausted", "no available accounts"]):
        return "服務繁忙"
    if any(token in lowered for token in ["timeout", "timed out"]):
        return "請求逾時"
    if any(token in lowered for token in ["connection", "network", "dns"]):
        return "連線異常"
    return "暫時不可用"


def _build_chat_route_error_reply(error: Exception, max_routes: int = 3) -> str:
    raw = _safe_str(error)
    route_lines = []
    for line in raw.splitlines():
        normalized = _safe_str(line)
        if normalized.startswith("route[") and "error=" in normalized:
            route_lines.append(normalized)

    if not route_lines:
        reason = _summarize_route_error_reason(raw)
        return f"⚠️ 目前模型路由忙碌或受限（{reason}），請稍後再試。"

    attempted = len(route_lines)
    lines = [f"⚠️ 路由暫時不可用（已嘗試 {attempted} 條）。"]

    for index, line in enumerate(route_lines[:max_routes], start=1):
        label_match = re.search(r"label=(.*?)\s+source=", line)
        model_match = re.search(r"model=(.*?)\s+base_host=", line)
        host_match = re.search(r"base_host=([^\s]+)", line)
        error_match = re.search(r"error=(.*)$", line)

        label = _safe_str(label_match.group(1) if label_match else "") or "unknown"
        model = _safe_str(model_match.group(1) if model_match else "") or "unknown"
        host = _safe_str(host_match.group(1) if host_match else "") or "unknown"
        reason = _summarize_route_error_reason(error_match.group(1) if error_match else "")

        lines.append(f"{index}) {label}/{model}@{host}：{reason}")

    if attempted > max_routes:
        lines.append(f"…其餘 {attempted - max_routes} 條略。")

    lines.append("請稍後再試。")
    return "\n".join(lines)


def _audit_workflow_event(handler: BaseHTTPRequestHandler, command: str, data: dict, text: str, action: Optional[str], applied: bool, result: str, before: Optional[dict] = None, after: Optional[dict] = None):
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "text": text,
        "action": action,
        "applied": applied,
        "result": result,
        "source": _safe_str(data.get("source")) or "unknown",
        "from_user": _safe_str(data.get("from_user")) or _safe_str(data.get("from_user_id")) or None,
        "from_name": _safe_str(data.get("from_name")) or None,
        "trace_id": _safe_str(data.get("trace_id")) or None,
        "bot_run_id": _safe_str(data.get("bot_run_id")) or None,
        "remote_addr": handler.client_address[0] if handler.client_address else None,
        "state_before": before,
        "state_after": after,
    }
    _append_workflow_audit(record)


def _handle_workflow_request(handler: BaseHTTPRequestHandler, command: str, data: dict, text: str) -> Optional[str]:
    action = None
    is_legacy_plain_text = False

    if command == "/workflow":
        action = _extract_workflow_action(data.get("action") or text)
    elif command == "/chat":
        action = _extract_prefixed_workflow_action(text)
        if action is None:
            action = _extract_workflow_action(text)
            is_legacy_plain_text = action is not None
    else:
        return None

    if action is None:
        return None

    from orchestrator import global_state

    if is_legacy_plain_text:
        _audit_workflow_event(
            handler,
            command,
            data,
            text,
            action,
            False,
            "rejected_legacy_plain_text",
            before=_snapshot_workflow_status(global_state),
            after=None,
        )
        return WORKFLOW_USAGE

    before = _snapshot_workflow_status(global_state)
    global_state.update("workflow_status", WORKFLOW_STATE_PATCH[action])
    after = _snapshot_workflow_status(global_state)
    _audit_workflow_event(
        handler,
        command,
        data,
        text,
        action,
        True,
        "applied",
        before=before,
        after=after,
    )
    return WORKFLOW_REPLY_MAP[action]


def _normalize_service_text(value: str) -> str:
    return re.sub(r"[\s,，。.!！?？…．~～]+", "", _safe_str(value)).lower()


def _resolve_service_target(value: str) -> Tuple[str, str]:
    raw = _safe_str(value)
    normalized = _normalize_service_text(raw)
    if not normalized:
        return "empty", ""

    if raw.startswith("com.w."):
        label = raw
        if label in SERVICE_RESTART_LABELS:
            return "allowed", label
        return "unknown", label

    allow_label = SERVICE_RESTART_ALIASES.get(normalized)
    if allow_label:
        return "allowed", allow_label

    return "unknown", normalized



def _parse_service_text(value: str) -> Tuple[str, str]:
    normalized = _normalize_service_text(value)
    if not normalized:
        return "", ""

    for pattern in SERVICE_STATUS_PATTERNS:
        if pattern.match(normalized):
            return "status", ""

    match = SERVICE_RESTART_PATTERN.match(normalized)
    if not match:
        return "", ""

    target_state, target_value = _resolve_service_target(match.group(1))
    if target_state == "empty":
        return "usage", ""
    if target_state == "unknown":
        return "unknown", target_value
    return "restart", target_value


def _resolve_service_control(command: str, data: dict, text: str) -> Tuple[str, str]:
    if command == "/chat":
        return _parse_service_text(text)

    if command == "/service/status":
        return "status", ""

    if command.startswith("/service/restart/"):
        alias = command[len("/service/restart/"):]
        state, value = _resolve_service_target(alias)
        if state == "allowed":
            return "restart", value
        if state == "empty":
            return "usage", ""
        return "unknown", value

    if command == "/service":
        explicit_action = _safe_str(data.get("action")).lower()
        if explicit_action == "status":
            return "status", ""
        if explicit_action == "restart":
            state, value = _resolve_service_target(data.get("target") or text)
            if state == "allowed":
                return "restart", value
            if state == "empty":
                return "usage", ""
            return "unknown", value
        return _parse_service_text(data.get("text") or text)

    return "", ""


def _append_service_audit(record: dict):
    os.makedirs(os.path.dirname(SERVICE_AUDIT_LOG), exist_ok=True)
    with open(SERVICE_AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _audit_service_event(handler: BaseHTTPRequestHandler, command: str, data: dict, text: str, action: str, allowed: bool, result: str, label: str = "", detail: str = ""):
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "text": text,
        "action": action,
        "allowed": allowed,
        "result": result,
        "label": label or None,
        "detail": detail or None,
        "source": _safe_str(data.get("source")) or "unknown",
        "from_user": _safe_str(data.get("from_user")) or _safe_str(data.get("from_user_id")) or None,
        "from_name": _safe_str(data.get("from_name")) or None,
        "trace_id": _safe_str(data.get("trace_id")) or None,
        "bot_run_id": _safe_str(data.get("bot_run_id")) or None,
        "remote_addr": handler.client_address[0] if handler.client_address else None,
    }
    _append_service_audit(record)


def _collect_services() -> Tuple[dict, str]:
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        return {}, str(e)

    stdout = result.stdout or ""
    if result.returncode != 0 and not stdout.strip():
        return {}, _safe_str(result.stderr) or f"launchctl exit={result.returncode}"

    service_info: Dict[str, Tuple[str, str]] = {}
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid_str, status_str, label = [segment.strip() for segment in parts]
        if label.startswith("com.w."):
            service_info[label] = (pid_str, status_str)

    return service_info, ""


def _render_service_status() -> Tuple[bool, str, str]:
    service_info, err = _collect_services()
    if err:
        return False, f"❌ 無法查詢服務狀態：{err}", err

    lines = ["📋 服務狀態一覽", ""]
    for label, display_name in SERVICE_LABELS.items():
        if label in service_info:
            pid_str, status_str = service_info[label]
            if pid_str and pid_str != "-":
                lines.append(f"✅ {display_name}（PID {pid_str}）")
            else:
                exit_code = status_str if status_str != "0" else "正常退出"
                lines.append(f"❌ {display_name}（未運行，exit {exit_code}）")
        else:
            lines.append(f"⚠️ {display_name}（未註冊）")

    extras = [label for label in service_info if label not in set(SERVICE_LABELS.keys())]
    if extras:
        lines.append("")
        lines.append("其他服務：")
        for label in extras:
            pid_str, _ = service_info[label]
            status = f"PID {pid_str}" if pid_str and pid_str != "-" else "未運行"
            lines.append(f"  · {label}（{status}）")

    return True, "\n".join(lines), ""


def _restart_service(label: str) -> Tuple[bool, str, str]:
    display_name = SERVICE_LABELS.get(label, label)
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        return False, f"❌ 重啟 {display_name} 例外：{e}", str(e)

    if result.returncode != 0:
        err = _safe_str(result.stderr) or _safe_str(result.stdout) or f"exit={result.returncode}"
        return False, f"❌ 重啟 {display_name} 失敗：{err}", err

    service_info, _ = _collect_services()
    pid_str, _ = service_info.get(label, ("", ""))
    if pid_str and pid_str != "-":
        return True, f"🔄 已重啟 {display_name}（PID {pid_str}）", pid_str
    return True, f"🔄 已送出重啟 {display_name} 指令", "kickstart_ok"


def _handle_service_control_request(handler: BaseHTTPRequestHandler, command: str, data: dict, text: str) -> Optional[str]:
    action, value = _resolve_service_control(command, data, text)
    if not action:
        return None

    source = _safe_str(data.get("source"))

    def reject(result: str, detail: str = "") -> str:
        _audit_service_event(handler, command, data, text, action=action, allowed=False, result=result, label=value, detail=detail)
        return result

    if source not in SERVICE_ALLOWED_SOURCES:
        return reject("⛔ 服務控制僅接受微信橋接或飛書來源請求", detail=f"source={source or 'missing'}")

    if action == "usage":
        _audit_service_event(handler, command, data, text, action=action, allowed=True, result="usage")
        return SERVICE_USAGE

    if action == "status":
        ok, message, detail = _render_service_status()
        _audit_service_event(
            handler,
            command,
            data,
            text,
            action="status",
            allowed=True,
            result="status_ok" if ok else "status_failed",
            detail=detail,
        )
        return message

    if action == "unknown":
        allowed_targets = "、".join(SERVICE_RESTART_ALIASES.keys())
        return reject(f"❌ 不支援的重啟目標：{value}\n可用目標：{allowed_targets}", detail=value)

    if action == "restart":
        blocked_labels = SERVICE_SELF_BLOCK_BY_SOURCE.get(source, set())
        if value in blocked_labels:
            return reject("⛔ 為避免雙端自殺，禁止重啟來源自身服務", detail=value)
        ok, message, detail = _restart_service(value)
        _audit_service_event(
            handler,
            command,
            data,
            text,
            action="restart",
            allowed=True,
            result="restart_ok" if ok else "restart_failed",
            label=value,
            detail=detail,
        )
        return message

    return SERVICE_USAGE


def _default_code_store() -> dict:
    return {
        "version": 1,
        "last_id": 0,
        "tasks": [],
    }


def _load_code_store() -> dict:
    if not os.path.exists(CODE_TASKS_FILE):
        return _default_code_store()
    try:
        with open(CODE_TASKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _default_code_store()
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            data["tasks"] = []
        data["last_id"] = int(data.get("last_id") or 0)
        data["version"] = int(data.get("version") or 1)
        return data
    except Exception:
        return _default_code_store()


def _save_code_store(store: dict):
    os.makedirs(os.path.dirname(CODE_TASKS_FILE), exist_ok=True)
    with open(CODE_TASKS_FILE, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)


def _append_code_audit(record: dict):
    os.makedirs(os.path.dirname(CODE_AUDIT_LOG), exist_ok=True)
    with open(CODE_AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _audit_code_event(handler: BaseHTTPRequestHandler, command: str, data: dict, text: str, action: str, allowed: bool, result: str, task_id: str = "", detail: str = ""):
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "text": text,
        "action": action,
        "allowed": allowed,
        "result": result,
        "task_id": task_id or None,
        "detail": detail or None,
        "source": _safe_str(data.get("source")) or "unknown",
        "from_user": _safe_str(data.get("from_user")) or _safe_str(data.get("from_user_id")) or None,
        "from_name": _safe_str(data.get("from_name")) or None,
        "trace_id": _safe_str(data.get("trace_id")) or None,
        "bot_run_id": _safe_str(data.get("bot_run_id")) or None,
        "remote_addr": handler.client_address[0] if handler.client_address else None,
    }
    _append_code_audit(record)


def _resolve_code_text(command: str, data: dict, text: str) -> str:
    if command == "/chat":
        normalized = _safe_str(text)
        return normalized if normalized.lower().startswith("/code") else ""

    if command == "/code":
        normalized = _safe_str(data.get("text") or text)
        if normalized:
            if normalized.lower().startswith("/code"):
                return normalized
            return f"/code {normalized}".strip()

        action = _safe_str(data.get("action")).lower()
        if not action:
            return ""
        if action == "create":
            target = _safe_str(data.get("target"))
            requirement = _safe_str(data.get("requirement"))
            return f"/code create {target} {requirement}".strip()
        if action in {"approve", "run", "cancel"}:
            task_id = _safe_str(data.get("task_id"))
            return f"/code {action} {task_id}".strip()
        if action == "status":
            task_id = _safe_str(data.get("task_id"))
            return f"/code status {task_id}".strip()

    return ""


def _find_code_task(store: dict, task_id: str) -> Optional[dict]:
    for task in store.get("tasks", []):
        if _safe_str(task.get("id")) == task_id:
            return task
    return None


def _render_code_task(task: dict) -> str:
    req = _safe_str(task.get("request"))
    req_short = req if len(req) <= 120 else req[:117] + "..."
    lines = [
        f"🧩 任務 {task.get('id')}",
        f"狀態：{task.get('status')}",
        f"目標：{task.get('target')}（{task.get('target_path')}）",
        f"需求：{req_short}",
        f"建立：{task.get('created_at')} by {task.get('created_by') or 'unknown'}",
    ]
    if task.get("approved_at"):
        lines.append(f"批准：{task.get('approved_at')} by {task.get('approved_by') or 'unknown'}")
    if task.get("started_at"):
        lines.append(f"執行：{task.get('started_at')} by {task.get('run_by') or 'unknown'}")
    if task.get("completed_at"):
        lines.append(f"完成：{task.get('completed_at')}")
    if task.get("result"):
        lines.append(f"結果：{task.get('result')}")
    output = _safe_str(task.get("output_tail"))
    if output:
        lines.append("輸出摘要：")
        lines.append(output)
    return "\n".join(lines)


def _render_code_status(store: dict, task_id: str = "") -> str:
    tasks = store.get("tasks", [])
    if task_id:
        task = _find_code_task(store, task_id)
        if not task:
            return f"❌ 找不到任務 {task_id}"
        return _render_code_task(task)

    if not tasks:
        return "📭 目前沒有受控編程任務"

    latest = list(reversed(tasks[-5:]))
    lines = ["🧾 近期受控編程任務（最新 5 筆）"]
    for task in latest:
        req = _safe_str(task.get("request"))
        req_short = req if len(req) <= 36 else req[:33] + "..."
        lines.append(
            f"• {task.get('id')} | {task.get('status')} | {task.get('target')} | {req_short}"
        )
    lines.append("\n可用：/code status <任務ID>")
    return "\n".join(lines)


def _extract_tail(stdout: str, stderr: str, limit: int = 1800) -> str:
    merged = "\n".join([part for part in [stdout or "", stderr or ""] if part]).strip()
    if not merged:
        return ""
    if len(merged) <= limit:
        return merged
    return merged[-limit:]


def _git_changed_paths() -> set:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=AUTOMATION_DIR,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(_extract_tail(result.stdout, result.stderr, limit=800) or "git status failed")

    paths = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            paths.add(path)
    return paths


def _enforce_code_task_scope(target_rel: str) -> Tuple[bool, str]:
    try:
        changed = _git_changed_paths()
    except Exception as e:
        return False, f"無法檢查 Git 修改範圍：{e}"

    unexpected = sorted(path for path in changed if path != target_rel)
    if unexpected:
        return False, "偵測到任務範圍外修改：" + "、".join(unexpected[:8])
    return True, ""


def _execute_code_task(task: dict) -> Tuple[bool, str, str]:
    target_rel = _safe_str(task.get("target_path"))
    abs_target = os.path.join(AUTOMATION_DIR, target_rel)
    if not os.path.exists(abs_target):
        return False, "目標檔案不存在", ""

    requirement = _safe_str(task.get("request"))
    prompt = (
        "你正在 automation 專案內執行受控編程任務。\n"
        f"只允許修改這一個檔案：{target_rel}\n"
        "不得新增或修改其他檔案。\n"
        "完成修改後請自行做必要檢查，最後回覆簡短摘要。\n\n"
        "任務需求如下：\n"
        f"{requirement}\n"
    )

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--permission-mode",
                "bypassPermissions",
                prompt,
            ],
            cwd=AUTOMATION_DIR,
            capture_output=True,
            text=True,
            timeout=CODE_EXEC_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return False, "系統找不到 claude CLI，請先確認執行環境", ""
    except subprocess.TimeoutExpired:
        return False, f"執行超時（>{CODE_EXEC_TIMEOUT_SECONDS} 秒）", ""
    except Exception as e:
        return False, f"執行例外：{e}", ""

    output_tail = _extract_tail(result.stdout, result.stderr)
    if result.returncode != 0:
        return False, f"Claude CLI 執行失敗（exit {result.returncode}）", output_tail

    scope_ok, scope_error = _enforce_code_task_scope(target_rel)
    if not scope_ok:
        return False, scope_error, output_tail

    compile_check = subprocess.run(
        [PYTHON, "-m", "py_compile", abs_target],
        cwd=AUTOMATION_DIR,
        capture_output=True,
        text=True,
    )
    if compile_check.returncode != 0:
        compile_tail = _extract_tail("", compile_check.stderr, limit=800)
        return False, "修改完成但語法檢查失敗", compile_tail or output_tail

    return True, "已完成修改並通過語法檢查", output_tail


def _handle_code_control_request(handler: BaseHTTPRequestHandler, command: str, data: dict, text: str) -> Optional[str]:
    code_text = _resolve_code_text(command, data, text)
    if not code_text:
        return None

    normalized = _safe_str(code_text)
    m_create = re.match(r"^/code\s+create\s+(\S+)\s+(.+)$", normalized, flags=re.IGNORECASE)
    m_approve = re.match(r"^/code\s+approve\s+(\S+)$", normalized, flags=re.IGNORECASE)
    m_run = re.match(r"^/code\s+run\s+(\S+)$", normalized, flags=re.IGNORECASE)
    m_status = re.match(r"^/code\s+status(?:\s+(\S+))?$", normalized, flags=re.IGNORECASE)
    m_cancel = re.match(r"^/code\s+cancel\s+(\S+)$", normalized, flags=re.IGNORECASE)

    source = _safe_str(data.get("source"))
    actor = _safe_str(data.get("from_user")) or _safe_str(data.get("from_user_id")) or "unknown"

    def reject(action: str, result: str, detail: str = "") -> str:
        _audit_code_event(handler, command, data, text, action=action, allowed=False, result=result, detail=detail)
        return result

    if m_create:
        if source not in CODE_ALLOWED_SOURCES:
            return reject("create", "⛔ 受控編程僅接受微信橋接或飛書來源請求", detail=f"source={source or 'missing'}")

        target = _safe_str(m_create.group(1)).lower()
        requirement = _safe_str(m_create.group(2))
        if target not in CODE_TARGETS:
            return reject("create", f"❌ 目前僅支援目標：{', '.join(CODE_TARGETS.keys())}")

        allowed_targets = CODE_ALLOWED_TARGETS_BY_SOURCE.get(source, set())
        if target not in allowed_targets:
            allowed_text = ", ".join(sorted(allowed_targets)) if allowed_targets else "無"
            return reject("create", f"⛔ 來源 {source} 不可建立目標 {target} 任務，可用目標：{allowed_text}", detail=f"source={source},target={target}")

        if len(requirement) < 4:
            return reject("create", "❌ 需求描述太短，請至少提供 4 個字")

        with CODE_TASKS_LOCK:
            store = _load_code_store()
            store["last_id"] = int(store.get("last_id") or 0) + 1
            task_id = f"C{store['last_id']:04d}"
            task = {
                "id": task_id,
                "target": target,
                "target_path": CODE_TARGETS[target]["path"],
                "request": requirement,
                "status": "created",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "created_by": actor,
                "created_source": source,
                "approved_at": "",
                "approved_by": "",
                "started_at": "",
                "completed_at": "",
                "run_by": "",
                "result": "",
                "output_tail": "",
            }
            tasks = store.get("tasks", [])
            tasks.append(task)
            store["tasks"] = tasks[-MAX_CODE_TASKS:]
            _save_code_store(store)

        _audit_code_event(handler, command, data, text, action="create", allowed=True, result="created", task_id=task_id)
        return (
            f"✅ 已建立受控編程任務 {task_id}\n"
            f"目標：{target}（{CODE_TARGETS[target]['label']}）\n"
            f"需求：{requirement}\n\n"
            f"下一步請下：/code approve {task_id}"
        )

    if m_approve:
        if source not in CODE_ALLOWED_SOURCES:
            return reject("approve", "⛔ 受控編程僅接受微信橋接或飛書來源請求", detail=f"source={source or 'missing'}")

        task_id = _safe_str(m_approve.group(1)).upper()
        with CODE_TASKS_LOCK:
            store = _load_code_store()
            task = _find_code_task(store, task_id)
            if not task:
                return reject("approve", f"❌ 找不到任務 {task_id}")
            if task.get("status") != "created":
                return reject("approve", f"⚠️ 任務 {task_id} 目前狀態為 {task.get('status')}，不可批准")
            task["status"] = "approved"
            task["approved_at"] = datetime.now().isoformat(timespec="seconds")
            task["approved_by"] = actor
            _save_code_store(store)

        _audit_code_event(handler, command, data, text, action="approve", allowed=True, result="approved", task_id=task_id)
        return f"✅ 任務 {task_id} 已批准\n下一步請下：/code run {task_id}"

    if m_run:
        if source not in CODE_ALLOWED_SOURCES:
            return reject("run", "⛔ 受控編程僅接受微信橋接或飛書來源請求", detail=f"source={source or 'missing'}")

        task_id = _safe_str(m_run.group(1)).upper()
        with CODE_TASKS_LOCK:
            store = _load_code_store()
            task = _find_code_task(store, task_id)
            if not task:
                return reject("run", f"❌ 找不到任務 {task_id}")
            if task.get("status") != "approved":
                return reject("run", f"⚠️ 任務 {task_id} 目前狀態為 {task.get('status')}，不可執行")
            task["status"] = "running"
            task["started_at"] = datetime.now().isoformat(timespec="seconds")
            task["run_by"] = actor
            _save_code_store(store)
            run_task = dict(task)

        ok, result_text, output_tail = _execute_code_task(run_task)

        with CODE_TASKS_LOCK:
            store = _load_code_store()
            task = _find_code_task(store, task_id)
            if not task:
                return reject("run", f"❌ 任務 {task_id} 在執行後遺失，請檢查 {CODE_TASKS_FILE}")
            task["status"] = "succeeded" if ok else "failed"
            task["completed_at"] = datetime.now().isoformat(timespec="seconds")
            task["result"] = result_text
            task["output_tail"] = output_tail
            _save_code_store(store)

        _audit_code_event(
            handler,
            command,
            data,
            text,
            action="run",
            allowed=True,
            result="succeeded" if ok else "failed",
            task_id=task_id,
            detail=result_text,
        )
        status_icon = "✅" if ok else "❌"
        message = [
            f"{status_icon} 任務 {task_id} 執行{'成功' if ok else '失敗'}",
            f"結果：{result_text}",
        ]
        if output_tail:
            message.append("輸出摘要：")
            message.append(output_tail)
        return "\n".join(message)

    if m_status:
        task_id = _safe_str(m_status.group(1)).upper() if m_status.group(1) else ""
        with CODE_TASKS_LOCK:
            store = _load_code_store()
        _audit_code_event(handler, command, data, text, action="status", allowed=True, result="ok", task_id=task_id)
        return _render_code_status(store, task_id)

    if m_cancel:
        if source not in CODE_ALLOWED_SOURCES:
            return reject("cancel", "⛔ 受控編程僅接受微信橋接或飛書來源請求", detail=f"source={source or 'missing'}")

        task_id = _safe_str(m_cancel.group(1)).upper()
        with CODE_TASKS_LOCK:
            store = _load_code_store()
            task = _find_code_task(store, task_id)
            if not task:
                return reject("cancel", f"❌ 找不到任務 {task_id}")
            if task.get("status") not in {"created", "approved"}:
                return reject("cancel", f"⚠️ 任務 {task_id} 目前狀態為 {task.get('status')}，不可取消")
            task["status"] = "canceled"
            task["completed_at"] = datetime.now().isoformat(timespec="seconds")
            task["result"] = f"已由 {actor} 取消"
            _save_code_store(store)

        _audit_code_event(handler, command, data, text, action="cancel", allowed=True, result="canceled", task_id=task_id)
        return f"✅ 任務 {task_id} 已取消"

    return CODE_USAGE


def _normalize_text(raw: str) -> str:
    return re.sub(r"[\s,，。.!！?？…．~～]+", "", (raw or "").lower())


def _current_greeting() -> str:
    hour = datetime.now().hour
    if hour < 11:
        return "早安"
    if hour < 17:
        return "午安"
    return "晚安"


def _is_simple_greeting(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    greeting_tokens = [
        "早安", "早上好", "午安", "下午好", "晚安", "晚上好",
        "你好", "哈囉", "嗨", "hi", "hello",
    ]
    return len(normalized) <= 12 and any(token in normalized for token in greeting_tokens)


def _is_retryable_route_error(error: Exception) -> bool:
    text = str(error)
    if hasattr(error, "body"):
        text += str(error.body)
    lowered = text.lower()
    retry_markers = [
        "503",
        "overloaded_error",
        "no available accounts",
        "rate limit",
        "429",
        "401",
        "403",
        "forbidden",
        "blocked",
        "invalid_api_key",
        "authentication",
        "auth_token",
        "api key",
        "api_key",
        "authorization",
        "could not resolve authentication method",
    ]
    return any(marker in lowered for marker in retry_markers)


def _extract_single_user_prompt(messages) -> str:
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Claude chat expects exactly one user message")

    message = messages[0] or {}
    if not isinstance(message, dict) or _safe_str(message.get("role")) != "user":
        raise ValueError("Claude chat expects a single user-role message")

    content = message.get("content")
    if isinstance(content, str):
        prompt = content.strip()
        if prompt:
            return prompt
        raise ValueError("Claude chat user message content is empty")

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if not isinstance(block, dict) or _safe_str(block.get("type")) != "text":
                raise ValueError("Claude chat only supports text content blocks")
            text = _safe_str(block.get("text"))
            if text:
                text_parts.append(text)
        prompt = "\n".join(text_parts).strip()
        if prompt:
            return prompt
        raise ValueError("Claude chat user message content is empty")

    raise ValueError("Claude chat received unsupported message content format")


# ── 股票查詢 ─────────────────────────────────────────────
def _call_claude_primary(system_prompt, messages, max_tokens):
    helper = _helper()
    prompt = _extract_single_user_prompt(messages)

    routes = helper.resolve_routes()
    if routes:
        _audit_llm_route_event("primary_selected", routes[0])

    try:
        reply = helper.chat(
            prompt=prompt,
            system=system_prompt or "",
            max_tokens=max_tokens,
        )
        if routes:
            _audit_llm_route_event("chat_completed", routes[0])
        return reply
    except Exception as e:
        if routes:
            _audit_llm_route_event("primary_failed", routes[0], e)
        raise e


_STOCK_NAME_MAP = {
    # 台股
    "台積電": "2330", "台积电": "2330", "台積電 adr": "TSM",
    "聯發科": "2454", "联发科": "2454",
    "鴻海": "2317", "鸿海": "2317",
    "台達電": "2308", "台达电": "2308",
    "台塑": "1301",
    "南亞": "1303", "南亚": "1303",
    "中鋼": "2002", "中钢": "2002",
    "中華電": "2412", "中华电": "2412",
    "富邦金": "2881",
    "國泰金": "2882", "国泰金": "2882",
    "中信金": "2891",
    "兆豐金": "2886", "兆丰金": "2886",
    "大立光": "3008",
    "統一": "1216", "统一": "1216",
    "長榮": "2603", "长荣": "2603",
    "陽明": "2609", "阳明": "2609",
    "萬海": "2615", "万海": "2615",
    "聯詠": "3034", "联咏": "3034",
    "日月光": "3711",
    "廣達": "2382", "广达": "2382",
    "緯創": "3231", "纬创": "3231",
    "英業達": "2356", "英业达": "2356",
    "華碩": "2357", "华硕": "2357",
    "友達": "2409", "友达": "2409",
    "群創": "3481", "群创": "3481",
    "台泥": "1101",
    "亞泥": "1102", "亚泥": "1102",
    # 美股
    "蘋果": "AAPL", "苹果": "AAPL",
    "微軟": "MSFT", "微软": "MSFT",
    "谷歌": "GOOGL",
    "亞馬遜": "AMZN", "亚马逊": "AMZN",
    "特斯拉": "TSLA",
    "輝達": "NVDA", "英伟达": "NVDA", "Nvidia": "NVDA",
    "Meta": "META",
    "阿里": "BABA", "阿里巴巴": "BABA",
    "拼多多": "PDD",
    "京東": "JD", "京东": "JD",
    "ASML": "ASML",
    "兆赫": "6226",
    "臻鼎": "4958",
}


def _resolve_stock_symbol(query: str) -> Tuple[str, str]:
    """從自然語言查詢中解析股票代號，回傳 (symbol, suffix_hint)。"""
    q = query.strip()
    # 直接數字代號（4位台股，允許前後接中文或標點）
    num_match = re.search(r"(?<!\d)(\d{4})(?!\d)", q)
    if num_match:
        return num_match.group(1), ".TW"
    # 名稱映射
    for name, sym in sorted(_STOCK_NAME_MAP.items(), key=lambda x: -len(x[0])):
        if name in q:
            return sym, ".TW" if sym.isdigit() else ""
    # 英文代號
    en_match = re.search(r"\b([A-Z]{1,5})\b", q.upper())
    if en_match:
        return en_match.group(1), ""
    return "", ""


def _read_daily_news_text() -> str:
    try:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        filepath = os.path.join(AUTOMATION_DIR, "logs", "daily_news", f"{today}.md")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()[:4000]
    except Exception:
        pass
    return ""


def generate_stock_reply(stock_id: str) -> str:
    try:
        ticker = yf.Ticker(f"{stock_id}.TW")
        hist = ticker.history(period="2d")
        if len(hist) >= 2:
            prev = hist["Close"].iloc[-2]
            curr = hist["Close"].iloc[-1]
            pct = (curr - prev) / prev * 100
            price_info = f"收盤價: {curr:.1f} ({'+' if pct>0 else ''}{pct:.2f}%)"
        elif len(hist) == 1:
            curr = hist["Close"].iloc[-1]
            price_info = f"收盤價: {curr:.1f}"
        else:
            return f"❌ 無法取得 {stock_id} 的股價資料"

        dl = DataLoader()
        first_day = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        df = dl.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=first_day)

        inst_info = ""
        if df is not None and not df.empty:
            recent_date = df["date"].max()
            stats = {
                "外資": {"today": 0, "month": 0},
                "投信": {"today": 0, "month": 0},
                "自營商": {"today": 0, "month": 0},
            }
            for _, row in df.iterrows():
                net = (row["buy"] - row["sell"]) / 1000.0
                n = row["name"]
                k = "外資" if "Foreign" in n else "投信" if "Investment" in n else "自營商" if "Dealer" in n else None
                if not k:
                    continue
                stats[k]["month"] += net
                if row["date"] == recent_date:
                    stats[k]["today"] += net

            def f(v): return f"+{v:,.0f}" if v > 0 else f"{v:,.0f}"
            inst_info = (
                f"[今日(張)] 外: {f(stats['外資']['today'])} / "
                f"投: {f(stats['投信']['today'])} / "
                f"自: {f(stats['自營商']['today'])}\n"
                f"[本月(張)] 外: {f(stats['外資']['month'])} / "
                f"投: {f(stats['投信']['month'])} / "
                f"自: {f(stats['自營商']['month'])}"
            )
        else:
            inst_info = "近期無法人進出數據（可能為美股或非台股代號）"

        raw = f"股票代號: {stock_id}\n{price_info}\n{inst_info}"

        try:
            analysis = _call_claude_primary("你是專業台股分析師。給出80字以內的極簡精闢短評，Markdown格式，不寒暄。", [{"role": "user", "content": raw}], 300)
        except Exception:
            analysis = "（AI 暫時無法提供短評）"

        return f"📊 **{stock_id} 即時查詢**\n\n{price_info}\n{inst_info}\n\n💡 {analysis}"
    except Exception as e:
        return f"❌ 查詢失敗: {e}"


def generate_deep_query_reply(query: str) -> str:
    """
    深度查詢：綜合股價、法人籌碼、今日新聞與技術信號，輸出精華重點。
    不透過 Claude Code SDK，直接呼叫 API，因此不受 8-turns 限制。
    """
    try:
        stock_id, suffix = _resolve_stock_symbol(query)
        if not stock_id:
            return "❌ 未能從查詢中辨識股票代號或名稱。請嘗試直接輸入代號，例如「查籌碼 2330」。"

        # --- 1. 股價 ---
        symbol = f"{stock_id}{suffix}" if suffix and not stock_id.endswith(suffix) else (f"{stock_id}.TW" if stock_id.isdigit() else stock_id)
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d")
        if hist is None or hist.empty:
            return f"❌ 暫時無法取得 {stock_id} 的股價資料"

        close_series = hist["Close"].dropna()
        if len(close_series) < 2:
            return f"❌ {stock_id} 資料不足，無法計算漲跌"

        prev = float(close_series.iloc[-2])
        curr = float(close_series.iloc[-1])
        pct = (curr - prev) / prev * 100
        price_text = f"收盤價: {curr:.2f} ({'+' if pct > 0 else ''}{pct:.2f}%)"

        # --- 2. 法人籌碼 (台股) ---
        inst_text = ""
        if stock_id.isdigit():
            try:
                dl = DataLoader()
                first_day = datetime.now().replace(day=1).strftime("%Y-%m-%d")
                df = dl.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=first_day)
                if df is not None and not df.empty:
                    recent_date = df["date"].max()
                    stats = {"外資": 0, "投信": 0, "自營商": 0}
                    for _, row in df.iterrows():
                        if row["date"] != recent_date:
                            continue
                        net = (row["buy"] - row["sell"]) / 1000.0
                        n = row["name"]
                        if "Foreign" in n:
                            stats["外資"] += net
                        elif "Investment" in n:
                            stats["投信"] += net
                        elif "Dealer" in n:
                            stats["自營商"] += net
                    def fmt(v): return f"+{v:,.0f}" if v > 0 else f"{v:,.0f}"
                    inst_text = f"法人動向 ({recent_date}): 外資 {fmt(stats['外資'])} 張 / 投信 {fmt(stats['投信'])} 張 / 自營商 {fmt(stats['自營商'])} 張"
                else:
                    inst_text = "法人數據: 暫無資料"
            except Exception as e:
                inst_text = f"法人數據取得失敗: {e}"
        else:
            inst_text = "法人數據: 僅適用台股"

        # --- 3. 今日新聞 ---
        news_text = _read_daily_news_text()
        if not news_text:
            news_text = "今日新聞: 暫無已抓取的新聞摘要。"
        else:
            news_text = f"今日新聞摘要:\n{news_text}"

        # --- 4. Claude 精華總結 ---
        prompt = (
            f"用戶查詢: {query}\n"
            f"標的: {stock_id}\n"
            f"{price_text}\n"
            f"{inst_text}\n\n"
            f"{news_text}\n\n"
            "請根據以上股價、法人籌碼與今日新聞，給出 300 字以內的精華重點分析。"
            "要求：1) 先講最關鍵的結論；2) 再列 2-3 個重點；3) 最後給一行操作建議（觀望/注意/留意）。"
            "語氣簡潔專業，不寒暄，使用繁體中文與 Markdown 格式。"
        )
        try:
            summary = _call_claude_primary(
                "你是專業投資分析師，只輸出精華重點，不說廢話。",
                [{"role": "user", "content": prompt}],
                600,
            )
        except Exception:
            news_fallback = news_text[:500] if news_text else "今日新聞: 暫無已抓取的新聞摘要。"
            summary = (
                "⚠️ AI 精華總結暫時不可用，先提供原始重點：\n\n"
                f"- {price_text}\n"
                f"- {inst_text}\n\n"
                f"{news_fallback}"
            )

        return f"🔍 **{stock_id} 深度分析**\n\n{summary}"
    except Exception as e:
        return f"❌ 深度查詢失敗: {e}"


# ── 執行 Agent 腳本 ─────────────────────────────────────
def run_agent(command: str, request_data: Optional[dict] = None) -> str:
    """
    根據 command 執行對應腳本，等待完成後回傳摘要。
    command 格式如 '/run/morning'、'/query_stock' 等。
    """
    _load_env()

    # 對應表
    SCRIPT_MAP = {
        "/run/morning":    ["scripts/stock_monitor.py",    "--mode", "morning"],
        "/run/afternoon":  ["orchestrator.py",            "--mode", "afternoon"],
        "/run/news":       ["scripts/news_crawler.py",       None],
        "/run/evening":    ["scripts/daily_report.py",      None],
        "/run/ecommerce":  ["scripts/ecommerce.py",        None],
        "/run/reflect":    ["scripts/reflection.py",        None],
    }

    if command not in SCRIPT_MAP:
        return f"❌ 不認識的指令：{command}"

    script_parts = SCRIPT_MAP[command]
    script_rel = script_parts[0]
    script_path = os.path.join(AUTOMATION_DIR, script_rel)
    cmd = [PYTHON, script_path] + [p for p in script_parts[1:] if p]

    name_map = {
        "/run/morning":    "台股晨報",
        "/run/afternoon":  "盤後流程",
        "/run/news":       "今日新聞摘要",
        "/run/evening":    "晚間日誌",
        "/run/ecommerce":  "跨境電商週報",
        "/run/reflect":    "本週反思",
    }
    name = name_map.get(command, script_rel)
    timeout_map = {
        "/run/afternoon": 900,
    }
    timeout_seconds = timeout_map.get(command, 300)
    env = os.environ.copy()
    request_data = request_data or {}
    request_context_token = _safe_str(request_data.get("context_token"))
    request_to_user = (
        _safe_str(request_data.get("to_user"))
        or _safe_str(request_data.get("from_user"))
        or _safe_str(request_data.get("from_user_id"))
    )
    if request_context_token:
        env["WECHAT_PUSH_CONTEXT_TOKEN"] = request_context_token
    if request_to_user:
        env["WECHAT_PUSH_TO_USER"] = request_to_user
    if request_context_token or request_to_user:
        env["WECHAT_PUSH_SOURCE"] = _safe_str(request_data.get("source")) or "automation_api"

    try:
        result = subprocess.run(
            cmd,
            cwd=AUTOMATION_DIR,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        today = datetime.now().strftime("%Y-%m-%d")

        if result.returncode != 0:
            return f"❌ {name} 執行失敗\n\n錯誤摘要：\n{result.stderr[-500:]}"
        else:
            summary = result.stdout[-1000:] or result.stderr[-1000:] or "（無文字輸出）"
            return f"✅ {name} 已完成\n\n📋 執行摘要：\n{summary}\n\n完整報告已推播到你的飛書和微信。"
    except subprocess.TimeoutExpired:
        minutes = max(1, timeout_seconds // 60)
        return f"⏱️ {name} 執行超時（{minutes}分鐘）"
    except Exception as e:
        return f"❌ {name} 執行例外：{e}"


# ── HTTP Handler ─────────────────────────────────────────
class BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(raw)
        except Exception as e:
            self._json(400, {"error": f"請求解析失敗: {e}"})
            return

        # 告警類路由：bridge fire-and-forget 通知
        if self.path == "/alert/wechat-context-invalid":
            status, body = _handle_wechat_invalid_alert(data)
            self._json(status, body)
            return

        command = data.get("command", "")
        stock_id = data.get("stock_id", "")
        text = data.get("text", "").strip()

        reply = ""
        try:
            code_reply = _handle_code_control_request(self, command, data, text)
            if code_reply is not None:
                reply = code_reply

            elif command == "/query_stock":
                if not stock_id:
                    reply = "請提供股票代號，例如「查籌碼 2330」"
                else:
                    reply = generate_stock_reply(stock_id)

            elif command == "/deep-query":
                query = _safe_str(data.get("query") or text)
                if not query:
                    reply = "請提供想查詢的標的，例如「研究台積電」或「分析 2330 走勢」。"
                else:
                    reply = generate_deep_query_reply(query)

            elif command.startswith("/run/"):
                reply = run_agent(command, data)

            elif command == "/chat" and text:
                service_reply = _handle_service_control_request(self, command, data, text)
                if service_reply is not None:
                    reply = service_reply
                else:
                    workflow_reply = _handle_workflow_request(self, command, data, text)
                    if workflow_reply is not None:
                        reply = workflow_reply
                    elif _is_simple_greeting(text):
                        # 問候語用規則回覆，避免模型忽略當下時段
                        reply = f"{_current_greeting()}，W。"
                    else:
                        date_diff_reply = _handle_simple_date_diff(text)
                        if date_diff_reply:
                            reply = date_diff_reply
                        else:
                            try:
                                global _MEM_AGENT
                                if "_MEM_AGENT" not in globals():
                                    from scripts.memory_agent import MemoryAgent
                                    _MEM_AGENT = MemoryAgent()
                                memory_agent = _MEM_AGENT
                                retrieved = memory_agent.retrieve(text, n_results=3)
                                memory_context = ""
                                if retrieved:
                                    docs = [f"文件 [{idx+1}]: {r['content'][:300]}" for idx, r in enumerate(retrieved)]
                                    memory_context = "\n\n【從長期記憶庫提取的相關歷史參考】\n" + "\n---\n".join(docs) + "\n\n(請注意：若上述歷史記憶與 W 剛說的話題無關，請直接忽略這些參考資料，單純回覆 W 的對話即可。)"
                            except Exception as e:
                                memory_context = f"\n(記憶庫提取失敗: {e})"

                            system_prompt = PROFILE_SUMMARY + memory_context

                            try:
                                reply = _call_claude_primary(system_prompt, [{"role": "user", "content": text}], 800)
                            except Exception as e:
                                _audit_chat_fallback_event(text=text, error=e, source=data.get("source"))
                                reply = _build_chat_route_error_reply(e)

            elif command == "/workflow":
                workflow_reply = _handle_workflow_request(self, command, data, text)
                reply = workflow_reply

            elif command == "/service" or command.startswith("/service/"):
                service_reply = _handle_service_control_request(self, command, data, text)
                reply = service_reply or SERVICE_USAGE

            else:
                reply = "⚠️ 無法識別的指令。請使用：\n• 查籌碼 2330\n• 晨報 / 盤後 / 今日新聞\n• 日誌 / 電商週報 / 本週反思\n• /code create <target> <需求描述>（target：feishu / wechat-bridge / wechat-api）\n• 或直接發訊息與我聊天"

        except Exception as e:
            _append_api_server_log({
                "time": datetime.now().isoformat(timespec="seconds"),
                "type": "chat_handler",
                "event": "unhandled_exception",
                "command": _safe_str(command),
                "source": _safe_str(data.get("source")) or "unknown",
                "input_preview": _safe_str(text)[:120],
                "error": _safe_str(e)[:500],
            })
            reply = "⚠️ 目前服務忙碌或受限，請稍後再試。"

        _audit_chat_reply(command=command, source=data.get("source"), text=text, reply=reply)
        self._json(200, {"reply": reply})

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "time": datetime.now().isoformat()})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # 安靜，不污染 stdout


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    _load_env()
    PORT = 3003
    server = ReusableHTTPServer(("127.0.0.1", PORT), BridgeHandler)
    print(f"🚀 API Server 啟動 http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
