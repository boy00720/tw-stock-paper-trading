#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飛書雙向機器人 — 長連接模式（WebSocket）
無需公網域名或 ngrok，SDK 主動連飛書伺服器
"""

import os
import sys
import json
import logging
import re
import threading
import subprocess
import fcntl
import glob as glob_mod
import urllib.request
import urllib.error
from typing import Optional
from datetime import datetime

for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "socks_proxy", "SOCKS_PROXY"]:
    os.environ.pop(_k, None)

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets
_orig_websockets_connect = websockets.connect

def _direct_websockets_connect(*args, **kwargs):
    kwargs.setdefault("proxy", None)
    return _orig_websockets_connect(*args, **kwargs)

websockets.connect = _direct_websockets_connect

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from dotenv import load_dotenv
from utils.claude_helper import ClaudeHelper
from utils.master_agent_status import format_master_health_summary, format_master_audit_summary

_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_env_path)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/feishu_bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

APP_ID         = os.getenv("FEISHU_APP_ID")
APP_SECRET     = os.getenv("FEISHU_APP_SECRET")
AUTOMATION_DIR = "/Users/w/Cursor/automation"
FEISHU_CLIENT  = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

_processed: set     = set()
_processed_lock = threading.Lock()

# ── M3 sim 模擬盤接點 ────────────────────────────────────────────────────
# 規格：sim/REQUIREMENTS.md §8 結構化指令；M3 PLAN: ~/.claude/plans/sprightly-growing-kite.md
# 設計選擇（已與 W 確認）：
#   - 撮合觸發：B 案 — bot 啟動 background runner thread（30 秒一次）
#   - 狀態持久化：A 案 — 記憶體單例，bot 重啟資料歸零（M3 不做 jsonl replay）
#   - 用戶白名單：A 案 — 不限制但 audit
_SIM_LOCK = threading.Lock()
_SIM_ENGINE = None              # type: Optional["MatchEngine"]
_SIM_FEED = None                # type: Optional["QuoteFeed"]
_SIM_RUNNER_STARTED = False


def _on_sim_fill(fill_dict: dict, snapshot: dict) -> None:
    """M5：sim 即時成交 callback。推 W 個人 DM 不推群組。

    兩條路徑共用：
    1. runner.run_loop 撮合（LMT/STOP/TP 等候價位成交）
    2. commands.dispatch 內 _do_order/_do_close_all 立即撮合（MKT 即時回饋）

    理由（M5 PLAN 收件人決策）：sim 是個人模擬盤的私有交易記錄，跟群組
    其他自動化推送混在一起會洗版。推送目標固定 FEISHU_USER_OPEN_ID env；
    未設定則僅 log warning。
    """
    try:
        from sim import reports as sim_reports
        text = sim_reports.fmt_realtime_fill(fill_dict, snapshot)
        open_id = os.getenv("FEISHU_USER_OPEN_ID")
        if not open_id:
            logger.warning("[sim] FEISHU_USER_OPEN_ID 未設定，即時通知略過")
            return
        send_message(open_id, text, receive_id_type="open_id")
    except Exception:
        logger.exception("[sim] 即時成交通知推送失敗")


def _ensure_sim_initialised():
    """
    第一次有 sim 指令時 lazy init engine + feed + 啟動 background runner thread。
    回傳 (engine, feed)。
    """
    global _SIM_ENGINE, _SIM_FEED, _SIM_RUNNER_STARTED
    with _SIM_LOCK:
        if _SIM_ENGINE is None:
            from sim.engine import MatchEngine
            from sim.feed import QuoteFeed
            _SIM_ENGINE = MatchEngine()
            _SIM_FEED = QuoteFeed()
            logger.warning(
                "[sim] 初始化記憶體模式 MatchEngine — bot 重啟資料歸零（M3 不做 replay）"
            )
        if not _SIM_RUNNER_STARTED:
            from sim.runner import run_loop

            equity_curve_path = os.path.join(
                AUTOMATION_DIR, "logs", "sim", "equity_curve.jsonl"
            )

            t = threading.Thread(
                target=run_loop,
                args=(_SIM_ENGINE, _SIM_FEED),
                kwargs={
                    "interval_sec": _SIM_ENGINE.match_interval_sec(),
                    "on_fill": _on_sim_fill,
                    "equity_curve_path": equity_curve_path,
                },
                daemon=True,
                name="sim-runner",
            )
            t.start()
            _SIM_RUNNER_STARTED = True
            logger.info(
                "[sim] background runner thread 已啟動（interval=%ss, on_fill=on, equity_curve=%s）",
                _SIM_ENGINE.match_interval_sec(),
                equity_curve_path,
            )
    return _SIM_ENGINE, _SIM_FEED


def _audit_sim(raw: str, reply: str, sender: Optional[str], chat_id: str, parsed, kind: str = "cmd") -> None:
    """每筆 sim 訊息落 logs/sim/audit.jsonl。

    kind: "cmd"（M3 結構化指令）或 "nl"（M4 自然語言查詢）。
    parsed 可為 None（kind="nl" 時，沒有 ParsedCommand 物件）。
    """
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "source": "feishu",
        "kind": kind,
        "sender_open_id": sender or "unknown",
        "chat_id": chat_id,
        "command": parsed.cmd if parsed else ("nl" if kind == "nl" else "_unknown"),
        "args": parsed.args if parsed else {},
        "raw": raw[:500],
        "reply_preview": (reply or "")[:300],
    }
    audit_path = os.path.join(AUTOMATION_DIR, "logs", "sim", "audit.jsonl")
    try:
        os.makedirs(os.path.dirname(audit_path), exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            # args 可能含 Enum，dataclass-friendly default fallback
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.warning("[sim] audit 寫入失敗：%s", exc)

CLAUDE_MD_PATH = "/Users/w/Cursor/CLAUDE.md"

SCRIPT_NAMES = {
    "news_crawler":    "新聞資訊 W",
    "stock_monitor":  "股市 W",
    "daily_report":   "日報 W",
    "ecommerce":      "跨境電商 W",
    "reflection":      "反思 W",
}

CRON_MAP = {
    "news_crawler":    {"label": "新聞資訊 W",  "keyword": "news_crawler.py"},
    "stock_monitor":  {"label": "股市 W",      "keyword": "stock_monitor.py"},
    "daily_report":   {"label": "日報 W",      "keyword": "daily_report.py"},
    "ecommerce":      {"label": "跨境電商 W",  "keyword": "ecommerce.py"},
    "reflection":     {"label": "反思 W",      "keyword": "reflection.py"},
    "orchestrator_morning":   {"label": "晨間流程",   "keyword": "run_automation.sh --mode morning"},
    "orchestrator_afternoon": {"label": "盤後流程",   "keyword": "run_automation.sh --mode afternoon"},
    "orchestrator_evening":   {"label": "晚間流程",   "keyword": "--mode evening"},
}


def log_claude_route_plan(purpose: str, helper: ClaudeHelper) -> None:
    try:
        routes = helper.resolve_routes()
        if not routes:
            logger.warning("%s Claude route plan: no routes resolved", purpose)
            return
        summaries = [helper.route_debug_summary(route) for route in routes]
        logger.info("%s Claude route plan: %s", purpose, " | ".join(summaries))
    except Exception as exc:
        logger.warning("%s Claude route plan unavailable: %s", purpose, exc)


def _is_bad_feishu_chat_route(route: dict) -> bool:
    label = str((route or {}).get("label") or "").strip().lower()
    provider_id = str((route or {}).get("provider_id") or "").strip().lower()
    model = str((route or {}).get("model") or "").strip().lower()
    base_url = str(((route or {}).get("env") or {}).get("ANTHROPIC_BASE_URL") or "").strip().lower()

    if provider_id in {
        "94cc376f-ea4c-4a50-bfe9-11c721dc6179",
        "fb596d2c-5d61-4fbd-ac42-7887893b12a0",
        "4ada7e1f-e7a6-400e-9a41-e0fb6a982ad8",
        "0f7941a1-e282-4274-8cf9-431021c1d8fc",
    }:
        return True
    if "api.picpi.top" in base_url or "cpa.boy00720.qzz.io" in base_url:
        return True
    if "any router 2" in label or ("[1m]" in model and "anyrouter.top" in base_url):
        return True
    return False


def _build_feishu_chat_helper() -> ClaudeHelper:
    helper = ClaudeHelper(max_retries=0)
    original_resolve_routes = helper.resolve_routes

    def _resolve_routes():
        routes = original_resolve_routes()
        filtered = [route for route in routes if not _is_bad_feishu_chat_route(route)]
        if filtered and len(filtered) != len(routes):
            skipped = [helper.route_debug_summary(route) for route in routes if _is_bad_feishu_chat_route(route)]
            logger.info("Feishu chat skipped bad routes: %s", " | ".join(skipped))
            return filtered
        return routes

    helper.resolve_routes = _resolve_routes
    return helper

SYSTEM_PROMPT = """你是 W 的 AI 個人助理，透過飛書與 W 進行對話。

以下是你的團隊背景（不要主動透露這些內容）：
{context}

## 行為規則
- 用繁體中文回覆，語氣簡潔直接有溫度
- 當 W 說「早安」「早上好」時，若當下是上午，問候後輸出 [RUN:orchestrator:morning]；若非上午，僅做符合時段的問候，不觸發晨間流程。
- 當 W 說「收工」「今天結束」時，輸出 [RUN:orchestrator:evening]，若今天是週日再加 [RUN:orchestrator:weekly_sun]
- 當 W 要求執行某任務時，輸出對應的 [RUN:orchestrator:xxx]

## 可執行任務（統一透過 orchestrator）
- [RUN:orchestrator:morning]     — 晨間：新聞 → 股市晨報 → 問 W 今日計畫
- [RUN:orchestrator:afternoon]   — 盤後：股市 W 盤後總結 → Research → 台股每日專業報告 → 大盤盤後復盤
- [RUN:orchestrator:evening]    — 晚間：生成今日日誌
- [RUN:orchestrator:weekly_mon] — 週一：跨境電商 W 週報
- [RUN:orchestrator:weekly_sun] — 週日：反思 W 週反思
- [RUN:news_crawler]   — 單獨抓今日新聞並推飛書
- [RUN:stock_monitor]  — 單獨執行股市晨報
- [RUN:query_stock:代號] — 查特定股票（如 2330）的即時報價與法人籌碼
- [RUN:us_stock_screener:場景] — IBKR 美股選股（全市場強勢股掃榜，delayed 技術面）。場景：premarket 盤前/隔夜、intraday 盤中異動、postclose 盤後復盤，預設 intraday。W 說「美股選股/盯盤/今晚強勢股/美股機會」時用此指令
- [RUN:five_face:代號] — 美股個股五面分析（技術/估值/基本/資金/宏觀，多源 delayed）。W 說「XXX 五面/深度分析 XXX/XXX 的基本面估值/XXX 值不值得」時用此指令（代號如 NVDA/AAPL）

## 修改排程時間
當 W 說「把 XXX 改到 HH 點」時，輸出 [CRON:script_name:HH:MM]。
名稱對應：晨間流程→orchestrator_morning, 股市 W→stock_monitor, 日報 W→daily_report, 反思 W→reflection, 電商 W→ecommerce, 新聞→news_crawler。
例：「把晨間流程改到9點」→「把晨間流程改到9點」→ [CRON:orchestrator_morning:09:00]
例：「股市改成9點半」→「把股市改成9點半」→ [CRON:stock_monitor:09:30]

## 注意
- [RUN:xxx] 和 [CRON:xxx] 是內部指令，回答 W 時不要原文顯示
- [RUN:xxx] 改用「正在為你啟動 XXX」
- [CRON:xxx] 不需要額外說明，系統自動回報結果
- 回答問題直接給答案，不用鋪陳開場白
"""

MASTER_HEALTH_PATTERNS = [
    re.compile(r"^(?:請|请|麻煩|麻烦|幫我|帮我)?(?:查看|看|查)?(?:一下)?(?:主控|通知主控|通知路由)(?:健康|狀態|状态|健康狀態|健康状态).*$"),
    re.compile(r"^(?:主控現在怎樣|主控现在怎样|主控現在如何|主控现在如何|主控有沒有掛|主控有没有挂)$"),
]

MASTER_AUDIT_PATTERNS = [
    re.compile(r"^(?:請|请|麻煩|麻烦|幫我|帮我)?(?:查看|看|查)?(?:一下)?(?:主控|通知)(?:稽核|審計|审计).*$"),
    re.compile(r"^(?:今天通知有沒有失敗|今天通知有没有失败|今天通知失敗了嗎|今天通知失败了吗|今天哪些通知失敗|今天哪些通知失败|看今天通知失敗|看今天通知失败|看今天推送失敗|看今天推送失败|主控今天有沒有失敗|主控今天有没有失败)$"),
]

# ── Workflow 控制 ──────────────────────────────────────────────────────────
WORKFLOW_PATTERNS = {
    "vacation": re.compile(r"^(?:今天)?(?:休假|暫停今天|暂停今天|全部暫停|全部暂停|休假模式)$"),
    "resume": re.compile(r"^(?:恢復排程|恢复排程|取消休假|恢復|恢复|全部恢復|全部恢复|恢復排程)$"),
    "pause-morning": re.compile(r"^(?:暫停晨報|暂停晨报|暫停晨間|暂停晨间|跳過晨報|跳过晨报)$"),
    "pause-afternoon": re.compile(r"^(?:暫停盤後|暂停盘后|暫停下午|暂停下午|跳過盤後|跳过盘后)$"),
}

WORKFLOW_STATE_PATCH = {
    "vacation": {"skip_morning": True, "skip_afternoon": True, "skip_evening": True},
    "resume": {"skip_morning": False, "skip_afternoon": False, "skip_evening": False},
    "pause-morning": {"skip_morning": True},
    "pause-afternoon": {"skip_afternoon": True},
}

WORKFLOW_REPLY_MAP = {
    "vacation": "⏸️ 收到，已開啟休假模式。今日晨報、盤後、晚間日誌皆暫停。",
    "resume": "▶️ 收到，已取消休假，恢復今日所有自動排程。",
    "pause-morning": "⏸️ 收到，今日晨報流程已暫停。",
    "pause-afternoon": "⏸️ 收到，今日盤後流程已暫停。",
}

# ── 服務管理 ──────────────────────────────────────────────────────────────
SERVICE_LABELS = {
    "com.w.feishu-bot": "飛書 Bot",
    "com.w.wechat-bridge-3001": "微信 Bridge",
    "com.w.wechat-api-3003": "微信 API",
    "com.w.weixin-claude-bot": "微信 Claude Bot",
    "com.w.claude-gemini-gateway": "Claude Gateway",
    "com.w.claude-nvidia-backup-proxy": "Nvidia Proxy",
    "com.w.dsa.server8000": "DSA Server",
}

# 可重啟白名單（不含飛書自己，避免自殺）
RESTARTABLE_SERVICES = {
    "微信": "com.w.wechat-bridge-3001",
    "微信bridge": "com.w.wechat-bridge-3001",
    "微信api": "com.w.wechat-api-3003",
    "api": "com.w.wechat-api-3003",
    "gateway": "com.w.claude-gemini-gateway",
    "proxy": "com.w.claude-nvidia-backup-proxy",
    "dsa": "com.w.dsa.server8000",
    "微信bot": "com.w.weixin-claude-bot",
}

SERVICE_STATUS_PATTERNS = [
    re.compile(r"^(?:查|看|查看|檢查|检查)?(?:一下)?(?:服務|服务|service)(?:狀態|状态|status)?$"),
    re.compile(r"^(?:服務|服务)(?:列表|清單|清单)?$"),
    re.compile(r"^(?:所有)?(?:服務|服务)(?:怎[麼么]樣|如何|還好嗎|还好吗)$"),
]

SERVICE_RESTART_PATTERN = re.compile(r"^(?:重啟|重启|restart)\s*(.+)$")

# ── 排查 ──────────────────────────────────────────────────────────────────
DIAGNOSE_PATTERNS = [
    re.compile(r"^(?:排查|排查一下|診斷|诊断|查錯|查错|checkerrors?)$"),
    re.compile(r"^(?:最近有什麼錯誤|最近有什么错误|有沒有報錯|有没有报错)$"),
]

DIAGNOSE_LOG_FILES = [
    ("飛書 Bot", "logs/feishu_bot.log"),
    ("股市 W", "logs/stock_monitor.log"),
    ("新聞 W", "logs/news_crawler.log"),
    ("日報 W", "logs/daily_report.log"),
    ("電商 W", "logs/ecommerce.log"),
    ("反思 W", "logs/reflection.log"),
    ("台股報告", "scripts/logs/daily_stock_report.log"),
]


def load_context() -> str:
    parts = []
    for path in ["/Users/w/Cursor/CLAUDE.md", "/Users/w/Cursor/profile.md"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                parts.append(f.read())
        except Exception:
            pass
    return "\n\n---\n\n".join(parts)


def send_message(receive_id: str, text: str, receive_id_type: str = "chat_id"):
    import time
    for attempt in range(3):
        try:
            req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                ).build()
            resp = FEISHU_CLIENT.im.v1.message.create(req)
            if not resp.success():
                logger.error(f"飛書發送失敗: {resp.code} {resp.msg}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return False
            logger.info(
                "飛書發送成功: receive_id_type=%s receive_id=%s message_id=%s",
                receive_id_type,
                receive_id,
                getattr(getattr(resp, "data", None), "message_id", ""),
            )
            return True
        except Exception as e:
            if attempt < 2:
                logger.warning(f"發送訊息失敗（第 {attempt+1} 次），{2**attempt}s 後重試: {e}")
                time.sleep(2 ** attempt)
            else:
                logger.error(f"發送訊息例外（已重試 3 次）: {e}")
                return False
    return False


def _strip_feishu_mentions(text: str) -> str:
    cleaned = re.sub(r"@_user_\d+\s*", "", text or "")
    cleaned = re.sub(r"<at[^>]*>.*?</at>\s*", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def resolve_master_query(text: str) -> Optional[str]:
    normalized = re.sub(r"[\s,，。.!！?？…．~～]+", "", _strip_feishu_mentions(text))
    if not normalized:
        return None

    for pattern in MASTER_HEALTH_PATTERNS:
        if pattern.match(normalized):
            return "health"

    for pattern in MASTER_AUDIT_PATTERNS:
        if pattern.match(normalized):
            return "audit"

    return None


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=True)
    except Exception:
        pass

def call_claude(user_message: str) -> str:
    _load_env()
    try:
        helper = _build_feishu_chat_helper()
        log_claude_route_plan("Feishu chat", helper)
        response = helper.chat(
            prompt=user_message,
            system=SYSTEM_PROMPT.format(context=load_context()),
            max_tokens=2000,
        )
        logger.info("Feishu chat Claude call completed")
        return response
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return f"抱歉，發生錯誤：{e}"


# ── Workflow 控制 ─────────────────────────────────────────────────────────

def resolve_workflow_action(text: str) -> Optional[str]:
    """自然語言匹配 workflow 動作"""
    normalized = re.sub(r"[\s,，。.!！?？…．~～]+", "", text)
    if not normalized:
        return None
    for action, pattern in WORKFLOW_PATTERNS.items():
        if pattern.match(normalized):
            return action
    return None


def _read_daily_state() -> dict:
    path = os.path.join(AUTOMATION_DIR, "logs", "state", f"state_{datetime.now().strftime('%Y-%m-%d')}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_daily_state(state: dict):
    state_dir = os.path.join(AUTOMATION_DIR, "logs", "state")
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, f"state_{datetime.now().strftime('%Y-%m-%d')}.json")
    with open(path, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f, ensure_ascii=False, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)


def _audit_workflow(action: str, before: dict, after: dict):
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "command": "feishu_workflow",
        "action": action,
        "applied": True,
        "source": "feishu",
        "state_before": before,
        "state_after": after,
    }
    audit_path = os.path.join(AUTOMATION_DIR, "logs", "workflow_control_audit.jsonl")
    os.makedirs(os.path.dirname(audit_path), exist_ok=True)
    try:
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Workflow audit write failed: {e}")


def apply_workflow_action(action: str) -> str:
    """套用 workflow 動作到 orchestrator 的 state JSON"""
    try:
        state = _read_daily_state()
        before = dict(state.get("workflow_status", {}))
        ws = state.setdefault("workflow_status", {})
        ws.update(WORKFLOW_STATE_PATCH[action])
        after = dict(ws)
        _write_daily_state(state)
        _audit_workflow(action, before, after)
        logger.info(f"Workflow action applied: {action} | before={before} after={after}")
        return WORKFLOW_REPLY_MAP[action]
    except Exception as e:
        logger.error(f"Workflow action failed: {e}")
        return f"❌ Workflow 操作失敗：{e}"


# ── 服務管理 ──────────────────────────────────────────────────────────────

def resolve_service_query(text: str) -> Optional[str]:
    """匹配服務查詢或重啟指令"""
    normalized = re.sub(r"[\s,，。.!！?？…．~～]+", "", text)
    if not normalized:
        return None

    for pattern in SERVICE_STATUS_PATTERNS:
        if pattern.match(normalized):
            return "status"

    m = SERVICE_RESTART_PATTERN.match(normalized)
    if m:
        target = m.group(1).strip().lower()
        if target in ("飛書", "飞书", "feishu"):
            return "restart_self"
        label = RESTARTABLE_SERVICES.get(target)
        if label:
            return f"restart:{label}"
        return f"restart_unknown:{target}"

    return None


def check_all_services() -> str:
    """查詢所有 launchd 服務狀態"""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.splitlines()
    except Exception as e:
        return f"❌ 無法查詢服務狀態：{e}"

    # 解析 launchctl list 輸出：PID\tStatus\tLabel
    service_info = {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 3:
            pid_str, status_str, label = parts
            if label.startswith("com.w."):
                service_info[label] = (pid_str.strip(), status_str.strip())

    report_lines = ["📋 服務狀態一覽", ""]
    for label, display_name in SERVICE_LABELS.items():
        if label in service_info:
            pid_str, status_str = service_info[label]
            if pid_str and pid_str != "-":
                report_lines.append(f"✅ {display_name}（PID {pid_str}）")
            else:
                exit_code = status_str if status_str != "0" else "正常退出"
                report_lines.append(f"❌ {display_name}（未運行，exit {exit_code}）")
        else:
            report_lines.append(f"⚠️ {display_name}（未註冊）")

    # 檢查是否有未列在白名單的 com.w. 服務
    known = set(SERVICE_LABELS.keys())
    extras = [l for l in service_info if l not in known]
    if extras:
        report_lines.append("")
        report_lines.append("其他服務：")
        for label in extras:
            pid_str, _ = service_info[label]
            status = f"PID {pid_str}" if pid_str and pid_str != "-" else "未運行"
            report_lines.append(f"  · {label}（{status}）")

    return "\n".join(report_lines)


def restart_service(label: str) -> str:
    """重啟指定 launchd 服務"""
    display_name = SERVICE_LABELS.get(label, label)
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            logger.info(f"Service restarted: {label}")
            return f"🔄 已重啟 {display_name}"
        else:
            err = result.stderr.strip() or result.stdout.strip()
            logger.warning(f"Service restart failed: {label} — {err}")
            return f"❌ 重啟 {display_name} 失敗：{err}"
    except Exception as e:
        logger.error(f"Service restart exception: {label} — {e}")
        return f"❌ 重啟 {display_name} 例外：{e}"


# ── 快速排查 ──────────────────────────────────────────────────────────────

def resolve_diagnose_query(text: str) -> bool:
    normalized = re.sub(r"[\s,，。.!！?？…．~～]+", "", text)
    return any(p.match(normalized) for p in DIAGNOSE_PATTERNS)


def diagnose_recent_errors() -> str:
    """掃描各 agent log 最近的 ERROR/FAILED 行"""
    error_pattern = re.compile(r"(ERROR|FAILED|Exception|Traceback|❌)", re.IGNORECASE)
    report_lines = ["🔍 快速排查報告", ""]
    found_any = False

    for agent_name, log_rel in DIAGNOSE_LOG_FILES:
        log_path = os.path.join(AUTOMATION_DIR, log_rel)
        if not os.path.exists(log_path):
            report_lines.append(f"⚠️ {agent_name}：log 不存在")
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                # 讀最後 200 行
                all_lines = f.readlines()
                tail = all_lines[-200:] if len(all_lines) > 200 else all_lines
            errors = [line.strip() for line in tail if error_pattern.search(line)]
            if errors:
                found_any = True
                recent = errors[-3:]  # 最近 3 條
                report_lines.append(f"❌ {agent_name}（{len(errors)} 個錯誤）：")
                for err in recent:
                    report_lines.append(f"  · {err[:120]}")
                report_lines.append("")
            else:
                report_lines.append(f"✅ {agent_name}：近期無錯誤")
        except Exception as e:
            report_lines.append(f"⚠️ {agent_name}：讀取失敗 {e}")

    # 額外掃描 orchestrator log（取最新的）
    orch_pattern = os.path.join(AUTOMATION_DIR, "logs", "orchestrator_full_*.log")
    orch_logs = sorted(glob_mod.glob(orch_pattern))
    if orch_logs:
        latest_orch = orch_logs[-1]
        try:
            with open(latest_orch, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                tail = all_lines[-200:] if len(all_lines) > 200 else all_lines
            errors = [line.strip() for line in tail if error_pattern.search(line)]
            if errors:
                found_any = True
                recent = errors[-3:]
                report_lines.append(f"❌ Orchestrator（{len(errors)} 個錯誤）：")
                for err in recent:
                    report_lines.append(f"  · {err[:120]}")
            else:
                report_lines.append(f"✅ Orchestrator：近期無錯誤")
        except Exception as e:
            report_lines.append(f"⚠️ Orchestrator：讀取失敗 {e}")

    if not found_any:
        report_lines.append("")
        report_lines.append("🎉 所有 Agent 近期運行正常！")

    return "\n".join(report_lines)


def run_script(cmd: str, chat_id: str):
    """cmd 可以是 'script_name' 或 'orchestrator --mode xxx'"""
    def _run():
        timeout_seconds = 180
        python_bin = sys.executable or "/usr/bin/python3"
        if cmd.startswith("orchestrator"):
            parts = cmd.split()
            mode = next((parts[i+1] for i, p in enumerate(parts) if p == "--mode" and i+1 < len(parts)), "")
            name_map = {
                "morning": "晨間流程（新聞→股市晨報→問計畫）",
                "afternoon": "盤後流程（股市 W → Research → 專業報告 → 大盤復盤）",
                "evening": "晚間流程（生成今日日誌）",
                "weekly_mon": "週一流程（跨境電商 W 週報）",
                "weekly_sun": "週日流程（反思 W 週反思）",
            }
            timeout_map = {
                "morning": 420,
                "afternoon": 900,
                "evening": 600,
                "weekly_mon": 900,
                "weekly_sun": 900,
            }
            name = name_map.get(mode, f"Orchestrator（{mode}）")
            full_cmd = [python_bin, os.path.join(AUTOMATION_DIR, "orchestrator.py")] + parts[1:]
            timeout_seconds = timeout_map.get(mode, 420)
        else:
            name = SCRIPT_NAMES.get(cmd, cmd)
            full_cmd = [python_bin, os.path.join(AUTOMATION_DIR, "scripts", f"{cmd}.py")]
        try:
            result = subprocess.run(full_cmd, cwd=AUTOMATION_DIR, capture_output=True, text=True, timeout=timeout_seconds)
            if result.returncode != 0:
                send_message(chat_id, f"⚠️ {name} 執行出錯：\n{result.stderr[:400]}")
            else:
                send_message(chat_id, f"✅ {name} 完成")
        except subprocess.TimeoutExpired:
            minutes = max(1, timeout_seconds // 60)
            send_message(chat_id, f"⏱️ {name} 執行超時（{minutes}分鐘）")
        except Exception as e:
            send_message(chat_id, f"❌ {name} 失敗：{e}")
    threading.Thread(target=_run, daemon=True).start()


def run_us_screener(scene: str, chat_id: str):
    """飛書按需觸發 IBKR 美股選股：異步跑 CLI、stdout 當清單回飛書。

    read-only / delayed；約 40s，先回「正在跑」再回清單。
    """
    scene = scene if scene in {"premarket", "intraday", "postclose"} else "intraday"

    def _run():
        python_bin = sys.executable or "/usr/bin/python3"
        env = os.environ.copy()
        env["US_STOCK_SCREENER_ENABLED"] = "1"
        full_cmd = [python_bin, os.path.join(AUTOMATION_DIR, "scripts", "us_stock_screener.py"),
                    "--scene", scene, "--dry-run"]
        try:
            result = subprocess.run(full_cmd, cwd=AUTOMATION_DIR, capture_output=True,
                                    text=True, timeout=180, env=env)
            out = (result.stdout or "").strip()
            # 過濾 ib_insync 日誌雜訊，只留清單行
            lines = [l for l in out.splitlines()
                     if l and not l.startswith("2026-") and "ib_insync" not in l]
            clean = "\n".join(lines).strip() or "（本輪無候選）"
            send_message(chat_id, f"📊 IBKR 美股選股（{scene}）\n\n{clean}")
        except subprocess.TimeoutExpired:
            send_message(chat_id, "⏱️ 美股選股執行超時（3分鐘）")
        except Exception as e:
            send_message(chat_id, f"❌ 美股選股失敗：{e}")

    send_message(chat_id, f"⏳ 正在跑 IBKR 美股選股（{scene}），約 40 秒回清單…")
    threading.Thread(target=_run, daemon=True).start()


def run_five_face(symbol: str, chat_id: str):
    """飛書按需觸發美股五面分析：異步跑 CLI、stdout 當報告回飛書。

    多源（IBKR 技術 + yfinance 估值/基本/資金 + 巨表宏觀）；read-only / delayed。
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        send_message(chat_id, "請提供股票代號，例如「NVDA 五面分析」")
        return

    def _run():
        python_bin = sys.executable or "/usr/bin/python3"
        env = os.environ.copy()
        env["US_FIVE_FACE_ENABLED"] = "1"
        full_cmd = [python_bin, os.path.join(AUTOMATION_DIR, "scripts", "us_five_face_analyzer.py"),
                    "--symbol", symbol, "--dry-run"]
        try:
            result = subprocess.run(full_cmd, cwd=AUTOMATION_DIR, capture_output=True,
                                    text=True, timeout=180, env=env)
            out = (result.stdout or "").strip()
            lines = [l for l in out.splitlines()
                     if l and not l.startswith("2026-") and "ib_insync" not in l
                     and "pkg_resources" not in l and "UserWarning" not in l]
            clean = "\n".join(lines).strip() or "（無分析結果）"
            send_message(chat_id, clean)
        except subprocess.TimeoutExpired:
            send_message(chat_id, f"⏱️ {symbol} 五面分析執行超時（3分鐘）")
        except Exception as e:
            send_message(chat_id, f"❌ {symbol} 五面分析失敗：{e}")

    send_message(chat_id, f"⏳ 正在跑 {symbol} 五面分析，約 30 秒回報…")
    threading.Thread(target=_run, daemon=True).start()


def query_stock_inst(stock_id: str, chat_id: str):
    try:
        import yfinance as yf
        from FinMind.data import DataLoader
        from datetime import datetime

        ticker = yf.Ticker(f"{stock_id}.TW")
        hist   = ticker.history(period="2d")
        if len(hist) >= 2:
            prev = hist['Close'].iloc[-2]
            curr = hist['Close'].iloc[-1]
            pct  = (curr - prev) / prev * 100
            price_info = f"收盤價: {curr:.1f} TWD ({'+' if pct > 0 else ''}{pct:.2f}%)"
        elif len(hist) == 1:
            curr = hist['Close'].iloc[-1]
            price_info = f"收盤價: {curr:.1f} TWD"
        else:
            price_info = "無最新股價"

        dl = DataLoader()
        first = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        df = dl.taiwan_stock_institutional_investors(stock_id=stock_id, start_date=first)

        def fmt(v): return f"+{v:,.0f}" if v > 0 else f"{v:,.0f}"

        if df is not None and not df.empty:
            stats = {"外資": {"today": 0, "month": 0}, "投信": {"today": 0, "month": 0}, "自營商": {"today": 0, "month": 0}}
            latest = df['date'].max()
            for _, row in df.iterrows():
                k = "外資" if "Foreign" in row['name'] else "投信" if "Investment" in row['name'] else "自營商" if "Dealer" in row['name'] else None
                if k and k in stats:
                    v = (row['buy'] - row['sell']) / 1000.0
                    stats[k]["month"] += v
                    if row['date'] == latest:
                        stats[k]["today"] += v
            inst_info = (
                f"[今日] 外: {fmt(stats['外資']['today'])} / 投: {fmt(stats['投信']['today'])} / 自: {fmt(stats['自營商']['today'])} 張\n"
                f"[本月] 外: {fmt(stats['外資']['month'])} / 投: {fmt(stats['投信']['month'])} / 自: {fmt(stats['自營商']['month'])} 張"
            )
        else:
            inst_info = "近期無三大法人數據（可能為美股或非台股代碼）"

        try:
            helper = ClaudeHelper(max_retries=0)
            log_claude_route_plan("Feishu stock analysis", helper)
            analysis = helper.chat(
                prompt=f"{stock_id}: {price_info}\n{inst_info}",
                system="你是專業股市分析師。根據資料給出 80 字以內的精闢短評，Markdown 格式，不寒暄。",
                max_tokens=200,
            )
            logger.info("Feishu stock analysis Claude call completed stock_id=%s", stock_id)
        except Exception as exc:
            logger.warning("Feishu stock analysis Claude call failed stock_id=%s error=%s", stock_id, exc)
            analysis = "（AI 分析暫時無法使用）"

        send_message(chat_id, f"📊 **{stock_id} 即時查詢**\n{price_info}\n{inst_info}\n\n💡 {analysis}")
    except Exception as e:
        logger.error(f"查詢個股失敗: {e}")
        send_message(chat_id, f"❌ 查詢失敗: {e}")


def update_crontab(script_name: str, hour: int, minute: int) -> bool:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        keyword = CRON_MAP.get(script_name, {}).get("keyword", f"{script_name}.py")
        lines = result.stdout.splitlines()
        new_lines, updated = [], False
        for line in lines:
            if keyword in line:
                parts = line.strip().split(maxsplit=5)
                if len(parts) == 6:
                    _, _, dom, mon, dow, cmd = parts
                    new_lines.append(f"{minute:02d} {hour:02d} {dom} {mon} {dow} {cmd}")
                    updated = True
                    logger.info(f"Crontab updated: {script_name} → {hour:02d}:{minute:02d}")
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if not updated:
            logger.warning(f"Crontab 未找到匹配行: {keyword}")
            return False

        proc = subprocess.run(["crontab", "-"], input="\n".join(new_lines) + "\n", text=True, capture_output=True)
        if proc.returncode != 0:
            logger.error(f"Crontab 寫入失敗: {proc.stderr}")
            return False

        # 同步更新 CLAUDE.md 表格
        label = CRON_MAP.get(script_name, {}).get("label", script_name)
        try:
            with open(CLAUDE_MD_PATH, "r", encoding="utf-8") as f:
                md = f.read()
            md = re.sub(
                r"(\|\s*" + re.escape(label) + r"\s*\|[^|]*\|\s*)(\d{2}:\d{2})(\s*\|)",
                lambda x: x.group(1) + f"{hour:02d}:{minute:02d}" + x.group(3),
                md,
            )
            with open(CLAUDE_MD_PATH, "w", encoding="utf-8") as f:
                f.write(md)
        except Exception as e:
            logger.warning(f"CLAUDE.md 同步失敗: {e}")
        return True
    except Exception as e:
        logger.error(f"update_crontab 失敗: {e}")
        return False




def forward_code_control(text: str, chat_id: str) -> str:
    payload = {
        "command": "/chat",
        "text": text,
        "source": "feishu-bot",
        "from_user": chat_id,
        "from_name": "feishu",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:3003",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return str(data.get("reply") or "⚠️ API 未回傳 reply")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        logger.error("Feishu /code forward HTTPError: %s", detail)
        return f"❌ /code 轉發失敗：HTTP {e.code}\n{detail[:300]}"
    except Exception as e:
        logger.error("Feishu /code forward exception: %s", e)
        return f"❌ /code 轉發失敗：{e}"


def process_message(text: str, chat_id: str, sender_open_id: Optional[str] = None):
    logger.info(f"收到訊息: {text[:80]}")
    text = _strip_feishu_mentions(text)
    if not text:
        return

    normalized = re.sub(r"[\s,，。.!！?？…．~～]+", "", text.lower())
    greeting_tokens = ["早安", "早上好", "午安", "下午好", "晚安", "晚上好", "你好", "哈囉", "嗨", "hi", "hello"]
    if normalized and len(normalized) <= 12 and any(token in normalized for token in greeting_tokens):
        hour = datetime.now().hour
        if hour < 11:
            greet = "早安"
        elif hour < 17:
            greet = "午安"
        else:
            greet = "晚安"
        send_message(chat_id, f"{greet}，W。")
        if greet == "早安":
            send_message(chat_id, "⏳ 正在為你啟動晨間流程（新聞→股市晨報→問計畫），請稍候...")
            run_script("orchestrator --mode morning", chat_id)
        return

    master_query = resolve_master_query(text)
    if master_query == "health":
        send_message(chat_id, format_master_health_summary())
        return
    if master_query == "audit":
        send_message(chat_id, format_master_audit_summary())
        return

    workflow_action = resolve_workflow_action(text)
    if workflow_action:
        send_message(chat_id, apply_workflow_action(workflow_action))
        return

    service_query = resolve_service_query(text)
    if service_query == "status":
        send_message(chat_id, check_all_services())
        return
    if service_query == "restart_self":
        send_message(chat_id, "⚠️ 不支援從飛書重啟飛書 Bot 自身，請用終端操作。")
        return
    if service_query and service_query.startswith("restart:"):
        label = service_query.split(":", 1)[1]
        send_message(chat_id, restart_service(label))
        return
    if service_query and service_query.startswith("restart_unknown:"):
        target = service_query.split(":", 1)[1]
        available = "、".join(RESTARTABLE_SERVICES.keys())
        send_message(chat_id, f"❌ 不認識的服務「{target}」\n可重啟的服務：{available}")
        return

    if resolve_diagnose_query(text):
        send_message(chat_id, diagnose_recent_errors())
        return

    if text.startswith("查籌碼"):
        stock_id = text.replace("查籌碼", "").strip()
        if stock_id:
            send_message(chat_id, f"🔍 正在查詢 {stock_id}，請稍候...")
            threading.Thread(target=query_stock_inst, args=(stock_id, chat_id), daemon=True).start()
        else:
            send_message(chat_id, "請提供股票代號，例如「查籌碼 2330」")
        return

    if text.lower().startswith("/code"):
        send_message(chat_id, forward_code_control(text, chat_id))
        return

    # M3+M4：sim 模擬盤路由（在 /code 之後、LLM 之前接）
    # - M3 結構化指令（is_sim_prefix）→ commands.dispatch
    # - M4 自然語言查詢（is_sim_nl_query）→ nl_query.answer_nl_query
    try:
        from sim import commands as sim_commands
        from sim import nl_query as sim_nl

        if sim_commands.is_sim_prefix(text):
            engine, feed = _ensure_sim_initialised()
            parsed = sim_commands.parse_command(text)
            # M5：傳入 _on_sim_fill 讓 MKT 立即撮合的成交也推 W 個人 DM
            # （dispatch 內 _do_order/_do_close_all 自己 match，不經 runner）
            reply = sim_commands.dispatch(parsed, engine, feed, on_fill=_on_sim_fill)
            _audit_sim(text, reply, sender_open_id, chat_id, parsed, kind="cmd")
            send_message(chat_id, reply)
            return

        if sim_nl.is_sim_nl_query(text):
            engine, feed = _ensure_sim_initialised()
            snapshot = sim_nl.build_snapshot(engine, feed)
            reply = sim_nl.answer_nl_query(text, snapshot)
            _audit_sim(text, reply, sender_open_id, chat_id, None, kind="nl")
            send_message(chat_id, reply)
            return
    except Exception as exc:
        logger.exception("[sim] 訊息處理例外: %s", exc)
        send_message(chat_id, f"❌ sim 訊息處理失敗：{exc}")
        return

    raw = call_claude(text)

    orchestrator_tasks = re.findall(r"\[RUN:orchestrator:(\w+)\]", raw)
    script_tasks = re.findall(r"\[RUN:(?!orchestrator\b|query_stock\b|us_stock_screener\b|five_face\b)(\w+)\]", raw)
    query_tasks = re.findall(r"\[RUN:query_stock:(\w+)\]", raw)
    screener_tasks = re.findall(r"\[RUN:us_stock_screener:(\w+)\]", raw)
    five_face_tasks = re.findall(r"\[RUN:five_face:(\w+)\]", raw)
    cron_cmds = re.findall(r"\[CRON:(\w+):(\d+):(\d+)\]", raw)

    clean = re.sub(r"\[RUN:\w+(?::\w+)?\]", "", raw)
    clean = re.sub(r"\[CRON:\w+:\d+:\d+\]", "", clean)
    clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", clean, flags=re.DOTALL).strip()

    if clean:
        send_message(chat_id, clean)

    for mode in orchestrator_tasks:
        name_map = {
            "morning": "晨間流程（新聞→股市晨報→問計畫）",
            "afternoon": "盤後流程（股市 W → Research → 專業報告 → 大盤復盤）",
            "evening": "晚間流程（生成今日日誌）",
            "weekly_mon": "週一流程（跨境電商 W 週報）",
            "weekly_sun": "週日流程（反思 W 週反思）",
        }
        name = name_map.get(mode, f"Orchestrator（{mode}）")
        send_message(chat_id, f"⏳ 正在為你啟動{name}，請稍候...")
        run_script(f"orchestrator --mode {mode}", chat_id)

    for task in script_tasks:
        if task in SCRIPT_NAMES:
            name = SCRIPT_NAMES[task]
            send_message(chat_id, f"⏳ 正在為你啟動{name}，請稍候...")
            run_script(task, chat_id)

    for stock_id in query_tasks:
        send_message(chat_id, f"🔍 正在查詢 {stock_id}，請稍候...")
        threading.Thread(target=query_stock_inst, args=(stock_id, chat_id), daemon=True).start()

    for scene in screener_tasks:
        run_us_screener(scene, chat_id)

    for symbol in five_face_tasks:
        run_five_face(symbol, chat_id)

    for script, h, m in cron_cmds:
        if script not in CRON_MAP:
            send_message(chat_id, f"❌ 不認識的腳本：{script}")
            continue
        success = update_crontab(script, int(h), int(m))
        label = CRON_MAP[script]["label"]
        if success:
            send_message(chat_id, f"✅ 已將{label}改為 {int(h):02d}:{int(m):02d}")
        else:
            send_message(chat_id, f"❌ 修改{label}排程失敗")

def on_message_receive(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    msg_id  = message.message_id

    with _processed_lock:
        if msg_id in _processed:
            return
        _processed.add(msg_id)

    if message.message_type != "text":
        return

    try:
        content = json.loads(message.content)
        text    = content.get("text", "").strip()
        chat_id = message.chat_id
        # M3：取 sender open_id 給 sim audit 用；取不到就標 None（_audit_sim 會填 unknown）
        sender_open_id: Optional[str] = None
        try:
            sender_open_id = getattr(getattr(getattr(data.event, "sender", None), "sender_id", None), "open_id", None)
        except Exception:
            sender_open_id = None
        if text and chat_id:
            threading.Thread(
                target=process_message,
                args=(text, chat_id, sender_open_id),
                daemon=True,
            ).start()
    except Exception as e:
        logger.error(f"訊息解析失敗: {e}")


if __name__ == "__main__":
    logger.info("🤖 飛書機器人啟動（長連接模式）")
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )
    ws_client = lark.ws.Client(
        APP_ID, APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    ws_client.start()
