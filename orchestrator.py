#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator.py — W 的 AI 助理團隊總調度器

負責整合所有 Agent 的執行順序、資料傳遞與失敗處理。
所有排程統一由此腳本發動，Crontab 只留一個 entry。

使用方式（從 automation/ 目錄執行）：
    python3 orchestrator.py --mode morning
    python3 orchestrator.py --mode afternoon
    python3 orchestrator.py --mode evening
    python3 orchestrator.py --mode weekly_mon
    python3 orchestrator.py --mode weekly_fri
    python3 orchestrator.py --mode weekly_sun
"""

import os
from pathlib import Path
import sys
import json
import time
import logging
import threading
import traceback
import concurrent.futures
from datetime import datetime, date
from typing import Optional, Dict, Any

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# ── 清除 proxy / 污染的 Claude route env ─────────────────────────────────────
for _k in [
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
    "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_REASONING_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION", "CLAUDE_CODE_SUBAGENT_MODEL",
]:
    os.environ.pop(_k, None)

_BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault("PYTHON_BIN", sys.executable or "/usr/bin/python3")

from dotenv import load_dotenv
load_dotenv(_BASE_DIR / ".env", override=True)
from utils.master_agent_control import (
    MasterControlContext,
    activate_control,
    build_context,
    build_run_id,
    load_payload,
    record_interactive_exception,
    record_master_event,
    write_payload,
)
from utils.agent_metrics import get_metrics_collector, get_metrics_reporter
from utils.idempotency_manager import get_idempotency_manager, IdempotencyManager

# ── Logging ─────────────────────────────────────────────────────────────────
(_BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_BASE_DIR / "logs" / "orchestrator.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("orchestrator")

CHAT_ID = "oc_your_feishu_chat_id"


# ─────────────────────────────────────────────────────────────────────────────
# 全局狀態管理器 (Daily State Manager)
# ─────────────────────────────────────────────────────────────────────────────
class DailyStateManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.state_dir = _BASE_DIR / "logs" / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.today_str = datetime.now().strftime("%Y-%m-%d")
        self.state_file = self.state_dir / f"state_{self.today_str}.json"
        
        # 如果今天是第一次啟動，初始化狀態
        if not self.state_file.exists():
            self._write_state({
                "date": self.today_str,
                "workflow_status": {
                    "morning_run": False,
                    "afternoon_run": False,
                    "evening_run": False
                },
                "news_data": {
                    "success": False,
                    "file_path": None,
                    "economy_count": 0
                },
                "stock_data": {
                    "morning_success": False,
                    "afternoon_success": False,
                    "alerts_count": 0,
                    "has_alerts": False
                },
                "plan_data": {
                    "asked": False,
                    "is_replied": False
                },
                "errors": []
            })
            
    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, state: dict):
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def update(self, key: str, value: Any):
        """更新特定模塊的狀態（線程安全）"""
        with self._lock:
            state = self._read_state()
            if isinstance(value, dict) and key in state and isinstance(state[key], dict):
                state[key].update(value)
            else:
                state[key] = value
            self._write_state(state)

    def log_error(self, component: str, error_msg: str):
        """記錄執行錯誤（線程安全）"""
        with self._lock:
            state = self._read_state()
            if "errors" not in state:
                state["errors"] = []
            state["errors"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "component": component,
                "error": error_msg
            })
            self._write_state(state)

    def get_state(self) -> dict:
        return self._read_state()

global_state = DailyStateManager()


# ─────────────────────────────────────────────────────────────────────────────
# 工具：自動重試機制
# ─────────────────────────────────────────────────────────────────────────────
def with_retry(max_retries=3, delay=30):
    """
    自動重試裝飾器：出現異常時等待 delay 秒後重試，最多 max_retries 次。
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    # 如果回傳的是 StepResult，且 success 為 False，雖然沒噴 Error 但一樣當作失敗重試
                    if hasattr(result, "success") and not result.success:
                        raise ValueError(f"Agent 返回失敗狀態: {result.error}")
                    return result
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(f"⚠️ [{func.__name__}] 第 {attempt} 次嘗試失敗 ({e})，{delay} 秒後重試...")
                        time.sleep(delay)
                    else:
                        logger.error(f"❌ [{func.__name__}] 連續 {max_retries} 次嘗試皆失敗。最後錯誤: {e}")

            # 如果把次數用光了，就回傳一個失敗的 StepResult
            agent_name = func.__doc__.strip() if func.__doc__ else func.__name__
            return StepResult(agent_name, False, error=f"Max retries reached: {last_exception}")
        return wrapper
    return decorator

# ─────────────────────────────────────────────────────────────────────────────
# 執行結果封裝
# ─────────────────────────────────────────────────────────────────────────────

class StepResult:
    def __init__(self, name: str, success: bool, data: Any = None, error: str = "", is_critical: bool = True):
        self.name = name
        self.success = success
        self.data = data
        self.error = error
        self.is_critical = is_critical
        self.started_at = datetime.now()
        # 關鍵步驟失敗時立即推飛書告警
        if not success and is_critical and error:
            try:
                _emit_critical_error_alert(name, error)
            except Exception:
                pass

    def __str__(self):
        status = "✅" if self.success else ("❌" if self.is_critical else "⚠️ (非致命)")
        return f"{status} [{self.name}] {self.error or 'OK'}"

# ─────────────────────────────────────────────────────────────────────────────
# 各 Agent 執行器（動態 import，失敗時優雅降級）
# ─────────────────────────────────────────────────────────────────────────────

@with_retry(max_retries=2, delay=30)
def run_news_agent(control: Optional[MasterControlContext] = None, force: bool = False) -> StepResult:
    """新聞資訊 W"""
    agent_name = "新聞資訊 W"
    idempotency = get_idempotency_manager()

    # 冪等性檢查
    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於今日執行，跳過", is_critical=False)

    collector = get_metrics_collector()
    start_time = time.time()

    with collector.measure("news_crawler", "morning"):
        try:
            logger.info("→ 啟動 新聞資訊 W")
            from scripts.news_crawler import NewsCrawler
            crawler = NewsCrawler()
            with activate_control(control):
                result = crawler.run()
            success = result.get("success", False)
            duration = time.time() - start_time

            if success:
                logger.info(f"新聞 W 完成：{result.get('economy_count', 0)} 則財經新聞 → {result.get('news_file', '')}")
            global_state.update("news_data", {"success": success, "file_path": result.get('news_file'), "economy_count": result.get('economy_count', 0)})
            collector.set_output_metrics(items=result.get('economy_count', 0))
            # 追蹤 AI token usage（from ClaudeHelper.last_usage）
            ai_usage = result.get("ai_usage") or {}
            if ai_usage:
                collector.set_ai_usage(ai_usage.get("input_tokens", 0), ai_usage.get("output_tokens", 0))

            # 記錄執行結果
            idempotency.record_execution(
                agent_name=agent_name,
                success=success,
                output_path=result.get('news_file'),
                duration_sec=duration,
                metadata={"economy_count": result.get('economy_count', 0)}
            )

            return StepResult(agent_name, success, data=result, error=result.get("error", ""), is_critical=False)
        except Exception as e:
            logger.error(f"新聞資訊 W 執行拋出異常: {e}")
            collector.end_run(success=False, error=str(e))
            idempotency.record_execution(agent_name=agent_name, success=False, error=str(e))
            return StepResult(agent_name, False, error=str(e), is_critical=False)

@with_retry(max_retries=2, delay=30)
def run_stock_agent(mode: str = "morning", control: Optional[MasterControlContext] = None, force: bool = False) -> StepResult:
    """股市 W（晨報 / 盤後）"""
    agent_name = f"股市 W（{mode}）"
    idempotency = get_idempotency_manager()

    # 冪等性檢查
    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於今日執行，跳過", is_critical=False)

    collector = get_metrics_collector()
    collector.start_run("stock_monitor", mode)
    start_time = time.time()

    try:
        logger.info(f"→ 啟動 {agent_name}")
        old_argv = sys.argv
        sys.argv = ["stock_monitor.py", "--mode", mode]
        try:
            from scripts.stock_monitor import main as stock_main
            with activate_control(control):
                stock_main()
            _update_stock_mode_status(mode, True)
            collector.end_run(success=True)
            duration = time.time() - start_time
            idempotency.record_execution(agent_name=agent_name, success=True, duration_sec=duration)
            return StepResult(agent_name, True)
        finally:
            sys.argv = old_argv
    except SystemExit as e:
        # 讀取警報檔（無論成功失敗都試著讀）
        alerts = _read_stock_alerts()
        success = e.code == 0
        global_state.update("stock_data", {"alerts_count": len(alerts), "has_alerts": len(alerts) > 0})
        _update_stock_mode_status(mode, success)
        duration = time.time() - start_time
        if e.code == 0:
            logger.info(f"股市 W 完成，今日警報 {len(alerts)} 筆")
        collector.set_output_metrics(items=len(alerts))
        collector.end_run(success=success, error=f"exit {e.code}" if e.code != 0 else None)
        idempotency.record_execution(agent_name=agent_name, success=success, duration_sec=duration)
        return StepResult(agent_name, success, data={"alerts": alerts}, error=f"exit {e.code}" if e.code != 0 else "")
    except Exception as e:
        _update_stock_mode_status(mode, False)
        logger.error(f"股市 W 執行失敗: {e}\n{traceback.format_exc()}")
        collector.end_run(success=False, error=str(e))
        idempotency.record_execution(agent_name=agent_name, success=False)
        return StepResult(agent_name, False, error=str(e))



# ─────────────────────────────────────────────────────────────────────────────
# 輔助：環境感知 (環境判斷)
# ─────────────────────────────────────────────────────────────────────────────
def is_trading_day() -> bool:
    """
    判斷今日是否為交易日。
    使用 calendar_helper 排除週末 + 台股假日清單。
    """
    from utils.calendar_helper import is_tw_trading_day
    trading = is_tw_trading_day()

    if not trading:
        logger.info("📅 環境感知：今天為休市日（週末或國定假日）。")

    global_state.update("environment", {"is_trading_day": trading})
    return trading


def _read_stock_alerts() -> list:
    """讀取股市 W 產出的今日警報"""
    try:
        alerts_file = _BASE_DIR / "logs" / "stock_alerts_today.json"
        if alerts_file.exists():
            data = json.loads(alerts_file.read_text(encoding="utf-8"))
            return data.get("alerts", [])
    except Exception as e:
        logger.warning("讀取警報檔失敗: %s", e)
    return []


def _update_stock_mode_status(mode: str, success: bool):
    field = f"{mode}_success"
    if field in {"morning_success", "afternoon_success"}:
        global_state.update("stock_data", {field: success})


def _base_master_control(run_id: str, step_name: str) -> MasterControlContext:
    return build_context(
        run_id=run_id,
        step_name=step_name,
        source=os.getenv("WECHAT_PUSH_SOURCE", "orchestrator"),
        to_user=os.getenv("WECHAT_PUSH_TO_USER", ""),
        context_token=os.getenv("WECHAT_PUSH_CONTEXT_TOKEN", ""),
    )


def _build_step_control(run_id: str, step_name: str) -> MasterControlContext:
    base = _base_master_control(run_id, step_name)
    return build_context(
        run_id=base.run_id,
        step_name=step_name,
        source=base.source,
        to_user=base.to_user,
        context_token=base.context_token,
    )


def _send_payload_notification(payload: Dict[str, Any], wechat_gate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from utils.lark_notifier import LarkNotifier
    from utils.wechat_notifier import WechatNotifier

    wechat_gate = dict(wechat_gate or {})
    notification = payload.get("notification") or {}
    control = payload.get("control") or {}
    payload_meta = payload.get("meta") or {}
    result = {
        "run_id": payload.get("run_id") or "",
        "step_name": payload.get("step_name") or "",
        "agent": payload.get("agent") or "",
        "feishu_sent": False,
        "wechat_sent": False,
        "success": True,
        "errors": [],
        "channels": {
            "feishu": {"attempted": False, "sent": False, "pdf_sent": False},
            "wechat": {"attempted": False, "sent": False, "pdf_sent": False},
        },
    }

    pdf_file = (
        payload.get("meta", {}).get("pdf_file")
        or payload.get("artifacts", {}).get("pdf_file")
    )
    pdf_exists = bool(pdf_file and Path(pdf_file).exists())

    feishu_payload = notification.get("feishu") or {}
    if feishu_payload:
        attach_pdf = bool(feishu_payload.get("attach_pdf"))
        result["channels"]["feishu"] = {
            "attempted": True,
            "sent": False,
            "title": str(feishu_payload.get("title") or "").strip() or "通知",
            "content_length": len(str(feishu_payload.get("content") or "").strip()),
            "attach_pdf": attach_pdf,
        }
        notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        ok = notifier.send_message_chunked(
            CHAT_ID,
            str(feishu_payload.get("title") or "").strip() or "通知",
            str(feishu_payload.get("content") or "").strip(),
        )
        result["feishu_sent"] = ok
        result["channels"]["feishu"]["sent"] = ok
        if not ok:
            result["errors"].append("feishu_failed")

        extra_sent = 0
        extra_failed = 0
        for extra in feishu_payload.get("extra_messages") or []:
            extra_ok = notifier.send_message(
                CHAT_ID,
                str(extra.get("title") or "").strip() or "通知",
                str(extra.get("content") or "").strip(),
            )
            if extra_ok:
                extra_sent += 1
            else:
                extra_failed += 1
                result["errors"].append("feishu_extra_failed")
        result["channels"]["feishu"]["extra_sent"] = extra_sent
        result["channels"]["feishu"]["extra_failed"] = extra_failed

        if attach_pdf:
            if pdf_exists:
                file_key = notifier.upload_file(pdf_file)
                if file_key:
                    file_ok = notifier.send_file(CHAT_ID, file_key)
                    if not file_ok:
                        result["errors"].append("feishu_pdf_failed")
                    result["channels"]["feishu"]["pdf_sent"] = file_ok
                else:
                    result["errors"].append("feishu_pdf_upload_failed")
                    result["channels"]["feishu"]["pdf_sent"] = False
            else:
                result["errors"].append("feishu_pdf_missing")
                result["channels"]["feishu"]["pdf_sent"] = False

    # ── 微信推送已停用（2026-05-25）──────────────────────────────────
    # 微信 bot 轉型為查詢 agent，不再接收推送通知。
    # 保留 wechat channel 結構以維持下游 metrics / audit 相容性。
    wechat_payload = notification.get("wechat") or {}
    if wechat_payload:
        result["channels"]["wechat"] = {
            "attempted": False, "sent": False, "skipped": True,
            "skip_reason": "wechat_push_disabled",
        }
    if False and wechat_payload:  # disabled
        attach_pdf = bool(wechat_payload.get("attach_pdf"))
        expected_sequence_index = payload_meta.get("sequence_index")
        expected_sequence_total = payload_meta.get("sequence_total")
        expected_range_start = payload_meta.get("range_start")
        expected_range_end = payload_meta.get("range_end")
        expected_category = payload_meta.get("category")
        result["channels"]["wechat"] = {
            "attempted": True,
            "sent": False,
            "text_length": len(str(wechat_payload.get("text") or "")),
            "typing_seconds": int(wechat_payload.get("typing_seconds") or 0),
            "pdf_path": str(pdf_file or ""),
            "pdf_error": "",
            "attach_pdf": attach_pdf,
            "accepted": False,
            "delivered": False,
            "queued": False,
            "delivery_state": "skipped",
            "pending_repair_count": 0,
            "trace_id": "",
            "queue_id": "",
            "error": "",
            "gated": False,
            "gate_reason": "",
            "health_summary": "",
            "logical_id": "",
            "chunk_count": None,
            "ack_mode": "",
            "details": [],
            "meta": {},
            "partial_suspected": False,
            "expected_sequence_index": expected_sequence_index,
            "expected_sequence_total": expected_sequence_total,
            "expected_range_start": expected_range_start,
            "expected_range_end": expected_range_end,
            "expected_category": expected_category,
        }
        gate_enabled = bool(wechat_gate.get("enabled"))
        if gate_enabled:
            gate_reason = str(wechat_gate.get("reason") or "wechat_gate_closed")
            gate_summary = str(wechat_gate.get("health_summary") or "")
            wx = WechatNotifier()
            metadata = {
                "category": expected_category,
                "sequence_index": expected_sequence_index,
                "sequence_total": expected_sequence_total,
                "range_start": expected_range_start,
                "range_end": expected_range_end,
                "step_name": payload.get("step_name") or "",
                "agent": payload.get("agent") or "",
            }
            queued_ok = False
            if str(wechat_payload.get("text") or "").strip():
                queued_ok = wx.send_message(
                    str(wechat_payload.get("text") or ""),
                    to_user=str(control.get("to_user") or "").strip() or None,
                    typing_seconds=int(wechat_payload.get("typing_seconds") or 0),
                    context_token=str(control.get("context_token") or "").strip() or None,
                    allow_unready_queue=True,
                    metadata={k: v for k, v in metadata.items() if v not in (None, "")},
                )
            wx_result = dict(getattr(wx, "last_result", {}) or {})
            queue_result = {
                "queued": bool(wx_result.get("queued")),
                "pending_count": wx_result.get("pending_repair_count", wechat_gate.get("pending_repair_count")),
                "queue_id": str(wx_result.get("queue_id") or ""),
                "logical_id": str(wx_result.get("logical_id") or ""),
                "meta": dict(wx_result.get("meta") or {}),
                "trace_id": str(wx_result.get("trace_id") or ""),
                "accepted": bool(wx_result.get("accepted")),
                "delivery_state": str(wx_result.get("delivery_state") or ("queued_for_repair" if queued_ok else "gated")),
                "error": str(wx.last_error or wx_result.get("reason") or gate_summary or gate_reason),
            }
            result["channels"]["wechat"].update({
                "attempted": False,
                "gated": True,
                "gate_reason": gate_reason,
                "health_summary": gate_summary,
                "delivery_state": queue_result.get("delivery_state") or ("queued_for_repair" if queue_result.get("queued") else "gated"),
                "pending_repair_count": queue_result.get("pending_count", wechat_gate.get("pending_repair_count")),
                "queue_id": queue_result.get("queue_id", ""),
                "queued": bool(queue_result.get("queued")),
                "accepted": bool(queue_result.get("accepted")),
                "trace_id": queue_result.get("trace_id", ""),
                "logical_id": queue_result.get("logical_id", ""),
                "meta": queue_result.get("meta") or {},
                "error": queue_result.get("error", ""),
            })
        else:
            wx = WechatNotifier()
            metadata = {
                "category": expected_category,
                "sequence_index": expected_sequence_index,
                "sequence_total": expected_sequence_total,
                "range_start": expected_range_start,
                "range_end": expected_range_end,
                "news_count": payload_meta.get("news_count"),
                "step_name": payload.get("step_name") or "",
                "agent": payload.get("agent") or "",
            }
            ok = wx.send_message(
                str(wechat_payload.get("text") or ""),
                to_user=str(control.get("to_user") or "").strip() or None,
                typing_seconds=int(wechat_payload.get("typing_seconds") or 0),
                context_token=str(control.get("context_token") or "").strip() or None,
                allow_unready_queue=True,
                metadata={k: v for k, v in metadata.items() if v not in (None, "")},
            )
            wx_result = dict(getattr(wx, "last_result", {}) or {})
            delivered = bool(wx_result.get("delivered"))
            queued = bool(wx_result.get("queued"))
            wx_meta = dict(wx_result.get("meta") or {})
            chunk_details = list(wx_result.get("details") or [])
            logical_id = str(wx_result.get("logical_id") or "")
            partial_suspected = False
            partial_reasons = []
            if delivered:
                if not logical_id:
                    partial_reasons.append("missing_logical_id")
                if wx_result.get("chunk_count") in (None, ""):
                    partial_reasons.append("missing_chunk_count")
                if not chunk_details:
                    partial_reasons.append("missing_chunk_details")
                if expected_sequence_total and wx_meta.get("sequence_total") != expected_sequence_total:
                    partial_reasons.append("sequence_total_mismatch")
                if expected_sequence_index and wx_meta.get("sequence_index") != expected_sequence_index:
                    partial_reasons.append("sequence_index_mismatch")
                if expected_range_start and wx_meta.get("range_start") != expected_range_start:
                    partial_reasons.append("range_start_mismatch")
                if expected_range_end and wx_meta.get("range_end") != expected_range_end:
                    partial_reasons.append("range_end_mismatch")
                if expected_category and wx_meta.get("category") != expected_category:
                    partial_reasons.append("category_mismatch")
                partial_suspected = bool(partial_reasons)

            delivery_state = str(wx_result.get("delivery_state") or ("sent" if delivered else "error"))
            if partial_suspected:
                delivery_state = "partial_suspected"

            result["wechat_sent"] = delivered and not partial_suspected
            result["channels"]["wechat"].update({
                "sent": delivered and not partial_suspected,
                "accepted": bool(wx_result.get("accepted")),
                "delivered": delivered,
                "queued": queued,
                "delivery_state": delivery_state,
                "pending_repair_count": wx_result.get("pending_repair_count"),
                "trace_id": str(wx_result.get("trace_id") or ""),
                "queue_id": str(wx_result.get("queue_id") or ""),
                "error": str(wx.last_error or wx_result.get("reason") or ""),
                "logical_id": logical_id,
                "chunk_count": wx_result.get("chunk_count"),
                "ack_mode": str(wx_result.get("ack_mode") or ""),
                "details": chunk_details,
                "meta": wx_meta,
                "partial_suspected": partial_suspected,
                "partial_reasons": partial_reasons,
            })
            if partial_suspected:
                result["errors"].append("wechat_partial_suspected")
            elif not ok and not queued:
                result["errors"].append(wx.last_error or "wechat_failed")

            if attach_pdf:
                if pdf_exists:
                    file_ok = wx.send_file(
                        str(pdf_file),
                        to_user=str(control.get("to_user") or "").strip() or None,
                        context_token=str(control.get("context_token") or "").strip() or None,
                    )
                    result["channels"]["wechat"]["pdf_sent"] = file_ok
                    result["channels"]["wechat"]["pdf_error"] = "" if file_ok else (wx.last_error or "wechat_pdf_failed")
                    if not file_ok:
                        result["errors"].append(wx.last_error or "wechat_pdf_failed")
                else:
                    result["errors"].append("wechat_pdf_missing")
                    result["channels"]["wechat"]["pdf_sent"] = False
                    result["channels"]["wechat"]["pdf_error"] = "wechat_pdf_missing"

    result["success"] = not result["errors"]
    record_master_event(
        "payload_dispatch",
        run_id=str(payload.get("run_id") or ""),
        step_name=str(payload.get("step_name") or ""),
        source=str(control.get("source") or payload.get("source") or ""),
        to_user=str(control.get("to_user") or payload.get("to_user") or ""),
        data={
            "agent": str(payload.get("agent") or ""),
            "success": result["success"],
            "errors": result["errors"],
            "channels": result["channels"],
            "artifacts": payload.get("artifacts") or {},
        },
    )
    return result


# step_name → idempotency agent_name 映射。
# 用於 _dispatch_master_payloads 推送完成後回寫送達狀態，
# 讓「上次成功但有 IM 通道未送達」的 step 在下一輪能自動補發。
_STEP_TO_IDEMPOTENCY_AGENT = {
    "stock_morning": "股市 W（morning）",
    "stock_afternoon": "股市 W（afternoon）",
    "daily_stock_report": "台股每日專業報告 W",
    "market_review": "大盤盤後復盤 W",
    "daily_research": "Research W 每日市場深度研究",
    "stock_screener": "短線選股 W",
    "daily_report": "日報 W",
    "ecommerce": "跨境電商 W",
    "reflection": "反思 W",
    "news": "新聞資訊 W",
}


def _classify_channel_status(channel_info: dict) -> str:
    """把 _send_payload_notification 回傳的 channel info 收斂成 idempotency 用的狀態字串。

    返回值之一：
      - "sent"   完整送達
      - "queued" 微信已入 repair / deferred queue（等 token 修復）
      - "gated"  微信因 health 阻擋而未嘗試
      - "failed" 嘗試送達但失敗（含 partial_suspected）
      - "skipped" 該 channel 本就沒有 payload
    """
    if not channel_info or not isinstance(channel_info, dict):
        return "skipped"

    # 本輪沒有該 channel 的 payload → 不算未送達，視為 skipped
    if channel_info.get("attempted") is False and not (
        channel_info.get("queued") or channel_info.get("gated") or channel_info.get("delivered")
    ):
        return "skipped"

    # wechat 路徑（有 delivered/queued/gated 欄位）
    has_wechat_shape = any(k in channel_info for k in ("delivered", "queued", "gated", "delivery_state"))
    if has_wechat_shape:
        if channel_info.get("partial_suspected"):
            return "failed"
        if channel_info.get("sent") or channel_info.get("delivered"):
            return "sent"
        if channel_info.get("queued"):
            return "queued"
        if channel_info.get("gated"):
            return "gated"
        return "failed"

    # feishu 路徑：看 sent
    return "sent" if channel_info.get("sent") else "failed"


def _dispatch_master_payloads(run_id: str, step_names: list[str]) -> StepResult:
    from utils.wechat_notifier import WechatNotifier
    from utils.idempotency_manager import get_idempotency_manager

    idempotency = get_idempotency_manager()

    sent = 0
    failed = 0
    errors: list[str] = []
    seen_steps: set[str] = set()
    wechat_queued = 0
    wechat_gated = 0
    # 未送達清單：{step: <step_name>, agent: <agent_name>, feishu: <status>, wechat: <status>}
    undelivered_steps: list[dict] = []
    wechat_gate_reason = ""
    gate_state = {
        "enabled": False,
        "reason": "",
        "health_summary": "",
        "pending_repair_count": 0,
    }

    for step_name in step_names:
        if not step_name or step_name in seen_steps:
            continue
        seen_steps.add(step_name)
        payload = load_payload(run_id, step_name)
        if not payload:
            continue

        # wechat health gate 已停用（2026-05-25）— 微信推送已關閉

        dispatch_result = _send_payload_notification(payload, wechat_gate=gate_state)
        if dispatch_result["feishu_sent"] or dispatch_result["wechat_sent"]:
            sent += 1

        wechat_channel = dispatch_result.get("channels", {}).get("wechat", {})
        feishu_channel = dispatch_result.get("channels", {}).get("feishu", {})

        # 回寫送達狀態到 idempotency，讓未送達的 step 下一輪自動補發
        agent_name = _STEP_TO_IDEMPOTENCY_AGENT.get(step_name)
        channel_status = {}
        if feishu_channel:
            channel_status["feishu"] = _classify_channel_status(feishu_channel)
        if wechat_channel:
            channel_status["wechat"] = _classify_channel_status(wechat_channel)
        if agent_name and channel_status:
            try:
                idempotency.mark_delivery_status(agent_name, channel_status)
            except Exception as exc:
                logger.warning(f"回寫 idempotency delivery_status 失敗 [{agent_name}]: {exc}")

        # 將任一通道未完成（非 sent/skipped）的 step 收進 undelivered_steps
        if channel_status:
            undelivered_channels = {
                ch: st for ch, st in channel_status.items()
                if st not in {"sent", "skipped"}
            }
            if undelivered_channels:
                undelivered_steps.append({
                    "step": step_name,
                    "agent": agent_name or step_name,
                    "channels": channel_status,
                    "undelivered": undelivered_channels,
                })

        if wechat_channel.get("queued"):
            wechat_queued += 1
            gate_state = {
                "enabled": True,
                "reason": str(wechat_channel.get("delivery_state") or "wechat_queued"),
                "health_summary": str(wechat_channel.get("error") or wechat_channel.get("delivery_state") or ""),
                "pending_repair_count": wechat_channel.get("pending_repair_count") or 0,
            }
        elif wechat_channel.get("gated"):
            wechat_gated += 1

        if gate_state["enabled"] and not wechat_gate_reason:
            wechat_gate_reason = str(gate_state.get("reason") or "wechat_gate_closed")

        if dispatch_result["errors"]:
            failed += 1
            errors.append(f"{step_name}: {' | '.join(dispatch_result['errors'])}")
        time.sleep(0.4)

    # 回寫通知結果到 agent_metrics
    if sent > 0:
        try:
            notif_collector = get_metrics_collector()
            notif_collector.start_run("notification_dispatch", run_id)
            sent_channels = ["feishu"] if sent > 0 else []
            notif_collector.set_notification_status(sent=True, channels=sent_channels)
            notif_collector.set_output_metrics(items=sent)
            notif_collector.end_run(success=True)
        except Exception as exc:
            logger.debug(f"[metrics] 通知 metrics 寫入失敗: {exc}")

    data = {
        "sent": sent,
        "failed": failed,
        "wechat_queued": wechat_queued,
        "wechat_gated": wechat_gated,
        "wechat_gate_reason": wechat_gate_reason,
        "undelivered_steps": undelivered_steps,
    }
    if failed:
        return StepResult(
            "主控通知路由",
            False,
            data=data,
            error="；".join(errors),
            is_critical=False,
        )
    return StepResult(
        "主控通知路由",
        True,
        data=data,
        is_critical=False,
    )


def _precheck_wechat_health(mode: str) -> dict:
    """已停用（2026-05-25）— 微信推送已關閉，不再需要預檢。"""
    return {"status": "disabled", "summary_text": "wechat_push_disabled"}


def _emit_delivery_failure_alert(mode: str, dispatch_results: list[StepResult]) -> None:
    """彙整本輪 dispatch 的未送達步驟，飛書群發告警。

    本函式設計成「絕不拋例外」：告警本身失敗時改記 critical log，
    避免遮蔽 orchestrator 主要結果。
    """
    try:
        all_undelivered: list[dict] = []
        for dr in dispatch_results:
            if not dr:
                continue
            data = getattr(dr, "data", None) or {}
            for item in data.get("undelivered_steps") or []:
                if item and item.get("step"):
                    all_undelivered.append(item)

        # 微信推送已關閉，過濾掉 wechat 通道的 undelivered（都是 intentional skip）
        for item in all_undelivered:
            ud = item.get("undelivered") or {}
            ud.pop("wechat", None)
        all_undelivered = [item for item in all_undelivered if item.get("undelivered")]

        if not all_undelivered:
            return

        # 整合摘要
        lines = [
            f"**⚠️ {mode} 推送未完成摘要**",
            f"共有 {len(all_undelivered)} 個步驟有通道未送達，已標記為待補發（下輪自動重試）：",
            "",
        ]
        for item in all_undelivered:
            channel_desc = ", ".join(f"{ch}={st}" for ch, st in (item.get("undelivered") or {}).items())
            lines.append(f"- {item.get('agent') or item.get('step')}：{channel_desc}")
        lines += [
            "",
            "🔁 補發策略：下一輪 orchestrator 啟動時，should_execute 會偵測未送達通道並重新觸發。",
        ]
        content = "\n".join(lines)

        try:
            from utils.lark_notifier import LarkNotifier
            notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
            ok = notifier.send_message(CHAT_ID, "⚠️ 推送未完成摘要", content)
            if not ok:
                logger.critical(
                    "[delivery_alert] 飛書告警送出失敗，未送達摘要：%s", all_undelivered
                )
        except Exception as exc:
            logger.critical(
                "[delivery_alert] 飛書告警拋出例外：%s；未送達摘要：%s",
                exc, all_undelivered,
            )
    except Exception as exc:
        # 任何意料外錯誤都不該影響主流程
        logger.error(f"[delivery_alert] 彙整告警時發生例外：{exc}")



def _emit_critical_error_alert(step_name: str, error_msg: str, mode: str = "") -> None:
    """Agent 嚴重失敗時立即推送飛書告警（不等日報）"""
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        now_str = datetime.now().strftime("%H:%M:%S")
        content = "\n".join([
            f"🚨 **關鍵步驟失敗**",
            f"",
            f"步驟：{step_name}",
            f"工作流：{mode or '未知'}",
            f"時間：{now_str}",
            f"錯誤：{error_msg[:500]}",
            f"",
            f"💡 建議：檢查日誌或手動重跑此步驟。",
        ])
        notifier.send_message(CHAT_ID, f"🚨 {step_name} 失敗", content)
        logger.info(f"[critical-alert] 已推送即時錯誤告警: {step_name}")
    except Exception as exc:
        logger.error(f"[critical-alert] 即時告警推送失敗: {exc}")


def _truncate_context(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n……（截斷，完整內容見原始 payload/log）"


def _format_alerts_for_research(alerts: list, limit: int = 12) -> str:
    if not alerts:
        return "（今日 stock_alerts_today.json 無警報）"
    lines = []
    for a in alerts[:limit]:
        if not isinstance(a, dict):
            continue
        symbol = a.get("symbol") or "?"
        name = a.get("name") or symbol
        change = a.get("change_pct")
        alert = a.get("alert") or a.get("level") or "?"
        price = a.get("price") or a.get("close") or "?"
        try:
            change_text = f"{float(change):+.2f}%"
        except (TypeError, ValueError):
            change_text = str(change or "?")
        lines.append(f"- {name}（{symbol}）：{price} / {change_text} / alert={alert}")
    if len(alerts) > limit:
        lines.append(f"- ……另有 {len(alerts) - limit} 筆警報未列出")
    return "\n".join(lines) if lines else "（警報格式不可讀）"


def _build_daily_research_context(master_run_id: str, alerts: list, results: Optional[Dict[str, StepResult]] = None) -> str:
    stock_payload = load_payload(master_run_id, "stock_afternoon") or {}
    stock_brief = ((stock_payload.get("notification") or {}).get("feishu") or {}).get("content", "") or ""
    market_payload = load_payload(master_run_id, "market_review") or {}
    market_review = ((market_payload.get("notification") or {}).get("feishu") or {}).get("content", "") or ""
    stock_report_payload = load_payload(master_run_id, "daily_stock_report") or {}
    stock_report = ((stock_report_payload.get("notification") or {}).get("feishu") or {}).get("content", "") or ""

    risk_radar_text = ""
    try:
        risk_path = _BASE_DIR / "logs" / "risk_radar.json"
        if risk_path.exists():
            risk_radar_text = risk_path.read_text(encoding="utf-8")
    except Exception as exc:
        risk_radar_text = f"（risk_radar 讀取失敗：{exc}）"

    result_lines = []
    for key, value in (results or {}).items():
        if isinstance(value, StepResult):
            result_lines.append(f"- {key}: {'success' if value.success else 'failed'} {value.error or ''}".strip())

    return "\n\n".join([
        "【盤後流程狀態】\n" + ("\n".join(result_lines) if result_lines else "（尚無彙整結果）"),
        "【今日股市警報】\n" + _format_alerts_for_research(alerts),
        "【股市 W 盤後摘要】\n" + (_truncate_context(stock_brief, 5000) or "（未取得 stock_afternoon payload）"),
        "【大盤盤後復盤摘要】\n" + (_truncate_context(market_review, 2200) or "（尚未取得 market_review payload；如為並行任務，請以股市 W 與外部搜尋為主）"),
        "【台股每日專業報告摘要】\n" + (_truncate_context(stock_report, 2200) or "（尚未取得 daily_stock_report payload；如為並行任務，請以股市 W 與外部搜尋為主）"),
        "【新聞風險雷達 risk_radar.json】\n" + (_truncate_context(risk_radar_text, 1800) or "（今日無 risk_radar 或尚未生成）"),
    ])


def run_daily_research_agent(
    master_run_id: str,
    alerts: list,
    results: Optional[Dict[str, StepResult]] = None,
    force: bool = False,
) -> StepResult:
    """Research W 每日市場深度研究"""
    agent_name = "Research W 每日市場深度研究"
    idempotency = get_idempotency_manager()
    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於今日執行，跳過", is_critical=False)

    start_time = time.time()
    try:
        logger.info("→ 啟動 Research W 每日市場深度研究")
        from scripts.researcher import ResearchAgent

        agent = ResearchAgent()
        topic = f"{datetime.now().strftime('%Y-%m-%d')} 台股盤後、新聞面、國際局勢與美股連動每日研究"
        context = _build_daily_research_context(master_run_id, alerts, results=results)
        control = _build_step_control(master_run_id, "daily_research")
        with activate_control(control):
            run_result = agent.run(topic=topic, context=context, report_kind="daily_market")
        success = bool(run_result.get("success")) if isinstance(run_result, dict) else bool(run_result)
        duration = time.time() - start_time
        output_path = run_result.get("output_path") if isinstance(run_result, dict) else None
        idempotency.record_execution(
            agent_name=agent_name,
            success=success,
            output_path=output_path,
            duration_sec=duration,
            metadata={"step_name": "daily_research", "report_kind": "daily_market"},
        )
        if success:
            global_state.update("research_data", {"daily_market": True})
            return StepResult(agent_name, True, data=run_result, is_critical=False)
        return StepResult(agent_name, False, data=run_result, error=(run_result or {}).get("error", "Research W 回傳失敗"), is_critical=False)
    except Exception as e:
        logger.error("Research W 每日市場深度研究失敗: %s\n%s", e, traceback.format_exc())
        idempotency.record_execution(agent_name=agent_name, success=False)
        return StepResult(agent_name, False, error=str(e), is_critical=False)


def trigger_research_if_needed(
    alerts: list,
    quotes: Optional[list] = None,
    run_id: Optional[str] = None,
) -> list[str]:
    """
    判斷今日股市是否有重要異常，若有則觸發 Research W

    觸發條件：
    1. 傳統：重點權值股 + critical alert 或漲跌幅 > 4.5%
    2. 擴充：成交量異常、法人連續買賣、技術指標突破

    抑振機制：同一標的 7 天內不重複觸發，除非價格變動 > 5% 或嚴重程度升級
    """
    if not alerts:
        return []

    emitted_steps: list[str] = []

    # === 傳統觸發條件 ===
    critical_symbols = ["2330.TW", "2454.TW", "2317.TW", "2382.TW", "^TWII"]
    traditional_targets = []
    for a in alerts:
        if a["symbol"] in critical_symbols and (a["alert"] == "critical" or abs(a["change_pct"]) >= 4.5):
            traditional_targets.append(a)

    if traditional_targets:
        logger.info(f"🚨 傳統條件偵測到異常：{[t['name'] for t in traditional_targets]}")

    # === 擴充觸發條件（需要 quotes 數據）===
    extended_targets = []
    if quotes:
        try:
            import pandas as pd
            from utils.research_trigger_detector import get_trigger_detector

            detector = get_trigger_detector()

            # 構建 price_data（簡化版，從 quotes 中提取）
            price_data = {}
            for q in quotes:
                symbol = q.get("symbol")
                if symbol:
                    # 創建簡易 DataFrame
                    price_data[symbol] = pd.DataFrame({
                        "close": [q.get("price", 0)],
                        "volume": [q.get("volume", 0)],
                    })

            # 構建 inst_data（空字典，因為沒有直接傳入）
            inst_data = {}

            triggers = detector.scan_all_triggers(quotes, inst_data, price_data)

            # 只取 critical 和 warning
            for t in triggers:
                if t.severity in ("critical", "warning"):
                    extended_targets.append({
                        "symbol": t.symbol,
                        "name": t.name,
                        "trigger_type": t.trigger_type,
                        "description": t.description,
                        "suggested_topic": t.suggested_topic,
                        "metrics": t.metrics,
                    })

            if extended_targets:
                logger.info(f"🚨 擴充條件偵測到異常：{[t['name'] for t in extended_targets]}")
        except Exception as e:
            logger.debug(f"擴充觸發條件檢查失敗: {e}")

    # 合併目標（去重）
    all_targets = {t["symbol"]: t for t in traditional_targets}
    for t in extended_targets:
        if t["symbol"] not in all_targets:
            all_targets[t["symbol"]] = t

    if not all_targets:
        return []

    logger.info(f"🚨 總計偵測到 {len(all_targets)} 個研究目標")

    # === 抑振機制：檢查是否需要重新觸發 ===
    try:
        from utils.research_trigger_suppressor import get_trigger_suppressor

        suppressor = get_trigger_suppressor()
        filtered_targets = {}

        for symbol, target in all_targets.items():
            current_price = target.get("price", 0) or target.get("change_pct", 0)
            trigger_type = target.get("trigger_type", "traditional")

            should_trigger, reason = suppressor.should_trigger(
                symbol=symbol,
                current_price=current_price,
                trigger_type=trigger_type,
            )

            if should_trigger:
                filtered_targets[symbol] = target
                logger.info(f"✅ [{symbol}] 允許觸發: {reason}")
            else:
                logger.info(f"⏭️ [{symbol}] 被抑制: {reason}")

        all_targets = filtered_targets

    except Exception as e:
        logger.debug(f"抑振檢查失敗: {e}")

    if not all_targets:
        logger.info("所有研究目標均被抑制，本輪不觸發 Research W")
        return []

    logger.info(f"🚨 抑振後剩餘 {len(all_targets)} 個研究目標，呼叫 Research W...")

    try:
        from scripts.researcher import ResearchAgent
        agent = ResearchAgent()

        for symbol, target in all_targets.items():
            # 判斷是傳統還是擴充
            if target.get("trigger_type") and target.get("suggested_topic"):
                # 擴充觸發
                topic = target["suggested_topic"]
                context = f"標的：{target['name']} | {target['description']} | 指標: {target.get('metrics', {})}"
                trigger_type = target.get("trigger_type", "extended")
                severity = "warning"  # 擴充觸發預設 warning
            else:
                # 傳統觸發
                topic = f"{target['name']} 單日異常 {'大漲' if target['change_pct'] > 0 else '大跌'} {target['change_pct']}%"
                context = f"標的：{target['name']} | 今日收盤跌破/突破設定區間，或漲跌幅達 {target['change_pct']}%，觸發系統 {target['alert']} 警報。"
                trigger_type = "traditional"
                severity = target.get("alert", "warning")

            step_name = f"research_{symbol.lower().replace('.', '_').replace('^', 'idx_')}"
            control = _build_step_control(run_id, step_name) if run_id else None

            with activate_control(control):
                result = agent.run(topic=topic, context=context, report_kind="anomaly")

            # 記錄觸發歷史（抑振機制）
            try:
                from utils.research_trigger_suppressor import get_trigger_suppressor

                suppressor = get_trigger_suppressor()
                suppressor.record_trigger(
                    symbol=symbol,
                    trigger_types=[trigger_type],
                    price=target.get("price", 0) or 0,
                    severity=severity,
                    research_path=result.get("output_path") if isinstance(result, dict) else None,
                )
            except Exception as e:
                logger.debug(f"記錄觸發歷史失敗 {symbol}: {e}")

            global_state.update("research_data", {symbol: True})
            emitted_steps.append(step_name)

        return emitted_steps
    except Exception as e:
        logger.error(f"Research W 自動觸發失敗: {e}")
        return []


def run_daily_report_agent(
    plan_content: str = "",
    alerts: list = None,
    control: Optional[MasterControlContext] = None,
    force: bool = False,
) -> StepResult:
    """日報 W"""
    agent_name = "日報 W"
    idempotency = get_idempotency_manager()

    # 冪等性檢查
    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於今日執行，跳過", is_critical=False)

    collector = get_metrics_collector()
    start_time = time.time()

    with collector.measure("daily_report", "evening"):
        try:
            logger.info(f"→ 啟動 {agent_name}")
            from scripts.daily_report import DailyReporter
            reporter = DailyReporter()
            with activate_control(control):
                ok = reporter.run(plan_content=plan_content, alerts=alerts or [])
            duration = time.time() - start_time
            idempotency.record_execution(agent_name=agent_name, success=ok, duration_sec=duration)
            return StepResult(agent_name, ok)
        except Exception as e:
            logger.error(f"日報 W 執行失敗: {e}\n{traceback.format_exc()}")
            collector.end_run(success=False, error=str(e))
            idempotency.record_execution(agent_name=agent_name, success=False)
            return StepResult(agent_name, False, error=str(e))

def run_stock_report_agent(control: Optional[MasterControlContext] = None, force: bool = False) -> StepResult:
    """台股每日專業報告 W"""
    agent_name = "台股每日專業報告 W"
    idempotency = get_idempotency_manager()

    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於今日執行，跳過", is_critical=False)

    start_time = time.time()
    try:
        logger.info("→ 啟動 台股每日專業報告 W")
        from scripts.daily_stock_report import main as stock_report_main
        with activate_control(control):
            code = stock_report_main()
        duration = time.time() - start_time
        success = code == 0
        idempotency.record_execution(agent_name=agent_name, success=success, duration_sec=duration)
        return StepResult(agent_name, success)
    except SystemExit as e:
        duration = time.time() - start_time
        success = e.code == 0
        idempotency.record_execution(agent_name=agent_name, success=success, duration_sec=duration)
        return StepResult(agent_name, success, error=f"exit {e.code}")
    except Exception as e:
        logger.error(f"台股每日專業報告 W 執行失敗: {e}\n{traceback.format_exc()}")
        idempotency.record_execution(agent_name=agent_name, success=False)
        return StepResult(agent_name, False, error=str(e))

def run_ecommerce_agent(control: Optional[MasterControlContext] = None, force: bool = False) -> StepResult:
    """跨境電商 W"""
    agent_name = "跨境電商 W"
    idempotency = get_idempotency_manager()

    # 冪等性檢查
    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於本週執行，跳過", is_critical=False)

    start_time = time.time()

    try:
        logger.info(f"→ 啟動 {agent_name}")
        from scripts.ecommerce import main as eco_main
        with activate_control(control):
            eco_main()
        duration = time.time() - start_time
        idempotency.record_execution(agent_name=agent_name, success=True, duration_sec=duration)
        return StepResult(agent_name, True)
    except SystemExit as e:
        duration = time.time() - start_time
        success = e.code == 0
        idempotency.record_execution(agent_name=agent_name, success=success, duration_sec=duration)
        return StepResult(agent_name, success, error=f"exit {e.code}")
    except Exception as e:
        logger.error(f"跨境電商 W 執行失敗: {e}\n{traceback.format_exc()}")
        idempotency.record_execution(agent_name=agent_name, success=False)
        return StepResult(agent_name, False, error=str(e))

def _dsa_python_bin(dsa_dir: str) -> str:
    venv_python = Path(dsa_dir) / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


@with_retry(max_retries=2, delay=30)
def run_market_review_agent(control: Optional[MasterControlContext] = None, force: bool = False) -> StepResult:
    """大盤盤後復盤 W"""
    import subprocess
    agent_name = "大盤盤後復盤 W"
    idempotency = get_idempotency_manager()

    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於今日執行，跳過", is_critical=False)

    start_time = time.time()
    try:
        logger.info("→ 啟動 大盤盤後復盤 W")
        # 由於 main.py 在 dsa 目錄
        dsa_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsa")

        # 繼承目前的環境變數 (含 .env 的 overrides)
        env = os.environ.copy()
        if control is not None:
            env.update(control.to_env())

        cmd = [_dsa_python_bin(dsa_dir), "main.py", "--market-review"]
        if control is not None:
            cmd.append("--no-notify")
        result = subprocess.run(
            cmd,
            cwd=dsa_dir,
            env=env,
            capture_output=True,
            text=True
        )
        duration = time.time() - start_time
        if result.returncode == 0:
            if control is not None:
                report_path = os.path.join(
                    dsa_dir,
                    "reports",
                    f"market_review_{datetime.now().strftime('%Y%m%d')}.md",
                )
                if os.path.exists(report_path):
                    report_text = Path(report_path).read_text(encoding="utf-8")
                    write_payload(
                        {
                            "agent": "market_review",
                            "artifacts": {
                                "report_file": report_path,
                            },
                            "notification": {
                                "feishu": {
                                    "title": "🎯 大盤盤後復盤",
                                    "content": report_text,
                                },
                                "wechat": {
                                    "text": report_text,
                                    "typing_seconds": 10,
                                },
                            },
                        },
                        control=control,
                    )
            logger.info("大盤盤後復盤 W 執行成功")
            idempotency.record_execution(agent_name=agent_name, success=True, duration_sec=duration)
            return StepResult(agent_name, True)
        else:
            logger.error(f"大盤盤後復盤 W 回傳錯誤碼 {result.returncode}\n{result.stderr}")
            idempotency.record_execution(agent_name=agent_name, success=False, duration_sec=duration)
            return StepResult(agent_name, False, error=f"exit {result.returncode}")
    except Exception as e:
        logger.error(f"大盤盤後復盤 W 執行失敗: {e}\n{traceback.format_exc()}")
        idempotency.record_execution(agent_name=agent_name, success=False)
        return StepResult(agent_name, False, error=str(e))


def run_stock_screener_agent(master_run_id: str, force: bool = False) -> tuple[StepResult, Optional[StepResult]]:
    """短線選股 W：跑 stock_screener 並透過 master_payload 推送。

    回傳 (screener_result, notification_dispatch_result)；
    被 idempotency 跳過時 notification_dispatch_result 為 None。
    """
    agent_name = "短線選股 W"
    idempotency = get_idempotency_manager()

    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return (
            StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於今日執行，跳過", is_critical=False),
            None,
        )

    start_time = time.time()
    try:
        logger.info("📊 啟動短線選股子代理...")
        from scripts.stock_screener import run_screener
        screener_control = _build_step_control(master_run_id, "stock_screener")
        with activate_control(screener_control):
            screener_state = run_screener(dry_run=False)
        duration = time.time() - start_time
        if screener_state:
            logger.info(
                "📊 短線選股完成：動能 %d 檔，反彈 %d 檔",
                len(screener_state.momentum_picks),
                len(screener_state.rebound_picks),
            )
            idempotency.record_execution(agent_name=agent_name, success=True, duration_sec=duration)
            screener_result = StepResult(
                agent_name,
                True,
                data={
                    "momentum": len(screener_state.momentum_picks),
                    "rebound": len(screener_state.rebound_picks),
                },
                is_critical=False,
            )
            dispatch_result = _dispatch_master_payloads(master_run_id, ["stock_screener"])
            return screener_result, dispatch_result
        idempotency.record_execution(agent_name=agent_name, success=False, duration_sec=duration)
        return StepResult(agent_name, False, error="無回傳", is_critical=False), None
    except Exception as e:
        logger.error("短線選股執行失敗: %s", e)
        idempotency.record_execution(agent_name=agent_name, success=False)
        return StepResult(agent_name, False, error=str(e), is_critical=False), None


def run_reflection_agent(control: Optional[MasterControlContext] = None, force: bool = False) -> StepResult:
    """反思 W"""
    agent_name = "反思 W"
    idempotency = get_idempotency_manager()

    # 冪等性檢查
    should_run, existing = idempotency.should_execute(agent_name, force=force)
    if not should_run:
        return StepResult(agent_name, True, data=existing.to_dict() if existing else {}, error="已於本週執行，跳過", is_critical=False)

    start_time = time.time()

    try:
        logger.info(f"→ 啟動 {agent_name}")
        from scripts.reflection import main as refl_main
        with activate_control(control):
            refl_main()
        duration = time.time() - start_time
        idempotency.record_execution(agent_name=agent_name, success=True, duration_sec=duration)
        return StepResult(agent_name, True)
    except SystemExit as e:
        duration = time.time() - start_time
        success = e.code == 0
        idempotency.record_execution(agent_name=agent_name, success=success, duration_sec=duration)
        return StepResult(agent_name, success, error=f"exit {e.code}")
    except Exception as e:
        logger.error(f"反思 W 執行失敗: {e}\n{traceback.format_exc()}")
        idempotency.record_execution(agent_name=agent_name, success=False)
        return StepResult(agent_name, False, error=str(e))

def ask_w_plan() -> StepResult:
    """互動例外：直接發訊息問 W 今日計畫，並存一個提示檔供晚間流程讀取"""
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        now = datetime.now()
        hour = now.hour
        if hour < 11:
            emoji = "🌅"
            greeting = "早安"
            scope = "今天"
            title = f"{emoji} W，今天的計畫？"
        elif hour < 17:
            emoji = "☀️"
            greeting = "午安"
            scope = "今天"
            title = f"{emoji} W，今天接下來的計畫？"
        else:
            emoji = "🌙"
            greeting = "晚安"
            scope = "今晚到明天"
            title = f"{emoji} W，今晚/明天的計畫？"

        prompt = (
            f"{emoji} {greeting}！請回覆你{scope}的幾件事：\n"
            f"1. {scope}有什麼重要行程或場合？\n"
            f"2. {scope}最重要的一件事是什麼？\n"
            "3. 有沒有特別想關注或處理的事情？\n\n"
            "（直接回覆在這條訊息底下，晚間日誌會自動整合）"
        )
        # 飛書通道
        sent_lark = notifier.send_message(CHAT_ID, title, prompt)
        record_interactive_exception(
            "ask_w_plan",
            channel="feishu",
            title=title,
            success=bool(sent_lark),
            details="" if sent_lark else "send_failed",
            metadata={"scope": scope, "greeting": greeting},
        )
        if sent_lark:
            logger.info("已發送計畫詢問到飛書（%s）", greeting)

        # 微信通道（與飛書並行通知，要求對齊；微信端任何異常不影響飛書側）
        sent_wechat = False
        try:
            from utils.wechat_notifier import WechatNotifier
            wx = WechatNotifier()
            wx_text = f"{title}\n\n{prompt}"
            sent_wechat = wx.send_message(
                wx_text,
                allow_unready_queue=True,
                metadata={"step_name": "ask_w_plan", "agent": "ask_w_plan"},
            )
            record_interactive_exception(
                "ask_w_plan",
                channel="wechat",
                title=title,
                success=bool(sent_wechat),
                details="" if sent_wechat else "send_failed",
                metadata={"scope": scope, "greeting": greeting},
            )
            if sent_wechat:
                logger.info("已發送計畫詢問到微信（%s）", greeting)
        except Exception as wx_exc:
            logger.error("微信發送計畫詢問失敗: %s", wx_exc)
            record_interactive_exception(
                "ask_w_plan",
                channel="wechat",
                title=title,
                success=False,
                details=str(wx_exc)[:200],
                metadata={"scope": scope, "greeting": greeting, "phase": "exception"},
            )

        sent = bool(sent_lark or sent_wechat)
        if sent:
            # 寫一個提示檔，晚間流程讀取（內容為空的，只是標記今天有問過）
            Path(_BASE_DIR / "logs" / "plan_asked_today.md").write_text(
                f"# 計畫詢問｜{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"# W 請在飛書/微信回覆，晚間日誌流程會再次詢問\n",
                encoding="utf-8"
            )
        return StepResult(
            "問 W 計畫",
            sent,
            error="" if sent else "all_channels_send_failed",
            is_critical=False,
        )
    except Exception as e:
        record_interactive_exception(
            "ask_w_plan",
            channel="feishu",
            title="W 計畫詢問",
            success=False,
            details=str(e),
            metadata={"phase": "exception"},
        )
        logger.warning(f"詢問 W 晨間計畫失敗: {e}")
        return StepResult("問 W 計畫", False, error=str(e), is_critical=False)

def read_w_plan() -> str:
    """讀取 W 回覆的今日計畫（從日誌目錄讀取）"""
    try:
        plan_file = _BASE_DIR / "logs" / "plan_today.md"
        if plan_file.exists():
            return plan_file.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("讀取 W 晨間計畫失敗: %s", e)
    return ""

# ─────────────────────────────────────────────────────────────────────────────
# 失敗通知
# ─────────────────────────────────────────────────────────────────────────────

def send_error_alert(step: StepResult, mode: str):
    """某個 Agent 失敗時，發送飛書 alert"""
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        msg = (
            f"⚠️  Orchestrator [{mode}] 執行异常\n"
            f"失敗環節：{step.name}\n"
            f"錯誤原因：{step.error}\n"
            f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        notifier.send_message(CHAT_ID, "⚠️ Orchestrator 執行异常", msg)
    except Exception as e:
        logger.error("送出失敗通知失敗: %s", e)

# ─────────────────────────────────────────────────────────────────────────────
# 各排程模式的執行流程
# ─────────────────────────────────────────────────────────────────────────────

def run_morning(force: bool = False) -> Dict[str, StepResult]:
    """晨間工作流（平行執行）：新聞資訊 W + 股市晨報 W + 問 W 計畫"""
    state_data = global_state.get_state()
    if state_data.get("workflow_status", {}).get("skip_morning"):
        logger.info("⏸️ 檢測到微信狀態覆寫：休假 / 暫停，跳過晨間工作流。")
        return {"morning_skipped": StepResult("晨間流程(跳過)", True, is_critical=False)}

    results = {}
    is_trading = is_trading_day()
    master_run_id = build_run_id("morning")
    notification_steps: list[str] = []

    _precheck_wechat_health("morning")

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        # 非同步觸發 3 個獨立（或容許延遲）的任務
        future_news = executor.submit(run_news_agent, _build_step_control(master_run_id, "news"), force)
        future_plan = executor.submit(ask_w_plan)

        if is_trading:
            # 股市 W 會自行在執行中段 (讀取新聞前) 由於資料抓取需要時間，通常能完美銜接新聞產出
            future_stock = executor.submit(
                run_stock_agent,
                "morning",
                _build_step_control(master_run_id, "stock_morning"),
                force,
            )
        else:
            future_stock = None
            logger.info("☕ 今日休市，跳過股市 W 晨報。")
            results["stock_morning"] = StepResult("股市 W(休市繞道)", True, is_critical=False)

        # 收集結果
        try:
            r_news = future_news.result(timeout=300)
        except concurrent.futures.TimeoutError:
            r_news = StepResult("新聞資訊 W", False, error="timeout 300s", is_critical=False)
        results["news"] = r_news
        print(str(r_news))

        if not r_news.success:
            logger.warning("新聞 W 失敗，股市 W 將無今日新聞上下文，繼續執行")
        else:
            news_data = r_news.data or {}
            logger.info(f"新聞 W 產出 {news_data.get('economy_count', 0)} 則財經新聞。")
            notification_steps.extend(news_data.get("payload_steps") or [])

        if future_stock:
            try:
                r_stock = future_stock.result(timeout=300)
            except concurrent.futures.TimeoutError:
                r_stock = StepResult(
                    "股市 W（morning）",
                    False,
                    error="timeout 300s; payload fallback pending",
                    is_critical=False,
                )
            results["stock_morning"] = r_stock
            print(str(r_stock))
            if r_stock.success:
                notification_steps.append("stock_morning")
            else:
                stock_payload = load_payload(master_run_id, "stock_morning")
                stock_meta = (stock_payload or {}).get("meta") or {}
                stock_payload_ready = bool(stock_payload and stock_meta.get("dispatch_ready"))
                if stock_payload_ready and "stock_morning" not in notification_steps:
                    notification_steps.append("stock_morning")
                    logger.info("股市 W（morning）雖逾時，但已找到可 dispatch 的初版 payload，補入主控通知步驟")

        # 等待計畫發問完成
        try:
            future_plan.result(timeout=60)
        except concurrent.futures.TimeoutError:
            logger.warning("問 W 計畫超時")

    if notification_steps:
        results["notification_dispatch"] = _dispatch_master_payloads(master_run_id, notification_steps)
        print(str(results["notification_dispatch"]))

    # 晨間記事提醒：讀取微信 bot 的 memos.json，有未完成記事就飛書提醒
    try:
        memo_file = Path.home() / ".weixin-claude-bot" / "memos.json"
        if memo_file.exists():
            memos = json.loads(memo_file.read_text(encoding="utf-8"))
            pending = [m for m in memos if not m.get("done")]
            if pending:
                memo_lines = [f"- {m['text']}　({m.get('created', '?')[:10]})" for m in pending[:10]]
                memo_content = f"📋 你有 {len(pending)} 則待辦記事：\n" + "\n".join(memo_lines)
                from utils.lark_notifier import LarkNotifier
                notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
                notifier.send_message(CHAT_ID, "📋 晨間記事提醒", memo_content)
                logger.info(f"[memo] 已推送 {len(pending)} 則記事提醒到飛書")
    except Exception as exc:
        logger.debug(f"[memo] 記事提醒失敗（非致命）: {exc}")

    # 晨間自動搜尋摘要：用 Tavily 搜「台股盤前」+「AI 產業新聞」推飛書
    try:
        _morning_auto_search_digest()
    except Exception as exc:
        logger.debug(f"[auto-search] 晨間搜尋摘要失敗（非致命）: {exc}")

    _emit_delivery_failure_alert("morning", list(results.values()))
    global_state.update("workflow_status", {"morning_run": True})
    return results

def _ai_summarize_digest(raw_sections: str) -> str:
    """用 grok-4.3-high 對搜尋結果做二次摘要整理"""
    import requests as _req

    base_url = os.getenv("DEEP_QUERY_BASE_URL", "https://ioll.pp.ua")
    api_key = os.getenv("DEEP_QUERY_API_KEY")
    model = os.getenv("DEEP_QUERY_MODEL", "grok-4.3-high")

    try:
        resp = _req.post(
            f"{base_url}/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": (
                        "你是私人秘書 MOSS01。將以下搜尋結果整理為一份精簡晨間簡報。"
                        "要求：繁體中文、每個主題 2-3 句重點、標出關鍵數字和趨勢、"
                        "最後一段寫「今日關注」列出 3 個值得留意的事。不要重複段落。"
                    )},
                    {"role": "user", "content": raw_sections},
                ],
                "max_tokens": 4000,
            },
            timeout=120,
        )
        resp.raise_for_status()
        body = resp.content.decode("utf-8")

        # 嘗試解析 JSON
        try:
            data = json.loads(body)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content
        except json.JSONDecodeError:
            pass

        # SSE streaming fallback
        text = ""
        for line in body.split("\n"):
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[6:])
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    text += delta
            except (json.JSONDecodeError, IndexError):
                pass
        return text or ""
    except Exception as e:
        logger.debug(f"[auto-search] AI 摘要失敗: {e}")
        return ""


def _morning_auto_search_digest():
    """用 Tavily API 搜尋預設主題，彙整成晨間新聞簡報推飛書，再用 AI 做摘要"""
    import requests as _req

    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        logger.debug("[auto-search] 無 TAVILY_API_KEY，跳過")
        return

    topics = [
        ("台股盤前", "台股 盤前 今日 重點 指數"),
        ("美股隔夜", "美股 隔夜 收盤 S&P500 那斯達克 重點"),
        ("AI 產業", "AI 人工智慧 產業 最新動態 突破"),
        ("台灣經濟", "台灣 經濟 政策 央行 匯率 最新"),
    ]

    sections: list[str] = []
    for label, query in topics:
        try:
            resp = _req.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_answer": True,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            answer = data.get("answer", "")
            results = data.get("results", [])
            lines = [f"**{label}**"]
            if answer:
                lines.append(answer[:500])
            for r in results[:3]:
                title = r.get("title", "")
                url = r.get("url", "")
                lines.append(f"  • {title}\n    {url}")
            sections.append("\n".join(lines))
        except Exception as e:
            logger.debug(f"[auto-search] {label} 搜尋失敗: {e}")
            sections.append(f"**{label}**\n  搜尋失敗")

    if not sections:
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    raw_content = "\n\n".join(sections)

    # AI 摘要整理
    ai_summary = _ai_summarize_digest(raw_content)

    if ai_summary:
        content = ai_summary + "\n\n---\n📎 原始搜尋結果：\n\n" + raw_content
    else:
        content = raw_content

    title = f"🔍 {today_str} 晨間搜尋簡報"

    from utils.lark_notifier import LarkNotifier
    notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
    notifier.send_message(CHAT_ID, title, content)
    logger.info("[auto-search] 晨間搜尋簡報已推送到飛書")


def _build_afternoon_summary_card(master_run_id: str, results: Dict[str, StepResult]) -> str:
    """彙整盤後全流程素材，產出「📊 盤後總結」卡片正文（不含標題、不含導引尾）。

    素材：stock_afternoon brief（大盤/名單/籌碼/專家觀點）、market_review 報告、
    短線選股檔數、警報數。優先 LLM 濃縮，失敗則規則化 fallback，保證一定有卡可發。
    """
    date_str = datetime.now().strftime("%Y-%m-%d %A")

    # 1) 讀素材（任何缺失都容錯跳過）
    af_payload = load_payload(master_run_id, "stock_afternoon") or {}
    brief = ((af_payload.get("notification") or {}).get("feishu") or {}).get("content", "") or ""

    mr_payload = load_payload(master_run_id, "market_review") or {}
    review_text = ((mr_payload.get("notification") or {}).get("feishu") or {}).get("content", "") or ""

    screener_res = results.get("stock_screener")
    sc_data = getattr(screener_res, "data", None) or {}
    if "momentum" in sc_data or "rebound" in sc_data:
        screener_line = f"動能 {sc_data.get('momentum', 0)} 檔 / 反彈 {sc_data.get('rebound', 0)} 檔"
    else:
        screener_line = "（無短線選股結果）"

    alert_count = (global_state.get_state().get("stock_data") or {}).get("alerts_count", 0)

    # 2) LLM 濃縮（max_tokens 小、有 fallback 保底）
    try:
        card = _llm_afternoon_summary(date_str, brief, review_text, screener_line, alert_count)
        if card and card.strip():
            return card.strip()
        logger.warning("[盤後總結] LLM 回傳空內容，改用規則 fallback")
    except Exception as exc:
        logger.warning(f"[盤後總結] LLM 濃縮失敗，改用規則 fallback: {exc}")

    return _fallback_afternoon_summary(brief, screener_line, alert_count)


def _llm_afternoon_summary(
    date_str: str, brief: str, review_text: str, screener_line: str, alert_count: int
) -> str:
    """用 ClaudeHelper 把盤後素材濃縮成 6-10 行純文字卡片正文。"""
    from utils.claude_helper import ClaudeHelper

    system_prompt = (
        "你是一位穩健保守的台股盤後策略助理。請把以下盤後素材濃縮成一張「盤後總結」卡，"
        "只輸出純文字、繁體中文、約 6-10 行，每行一個重點、精煉好讀。"
        "不要 Markdown 標題、不要日期抬頭、不要開場白或結語。"
        "依序包含這幾行（資料不足的行可略過）：\n"
        "大盤：今日台股加權與國際盤定調（帶漲跌幅 + 一句定性）\n"
        "名單：追蹤名單最關鍵的 1-2 個異動或籌碼方向\n"
        "選股：直接引用我給的短線選股檔數\n"
        "明日：明日穩健保守操作建議（一句）\n"
        "一句話：把今日盤後收斂成一句結論\n"
        "原則：穩健、不薦股、不誇大、用詞口語但專業。"
    )
    prompt = (
        f"【日期】{date_str}\n\n"
        f"【盤後股市總結與專家觀點（含大盤/追蹤名單/籌碼/明日策略）】\n{brief[:6000]}\n\n"
        f"【大盤盤後復盤要點】\n{review_text[:2000] or '（今日無大盤復盤報告）'}\n\n"
        f"【短線選股結果】{screener_line}\n"
        f"【今日警報檔數】{alert_count} 則\n"
    )
    helper = ClaudeHelper()
    return helper.chat(system=system_prompt, prompt=prompt, max_tokens=600)


def _fallback_afternoon_summary(brief: str, screener_line: str, alert_count: int) -> str:
    """LLM 不可用時的規則化卡片正文：至少給大盤、選股、警報硬數據。"""
    import re

    lines: list[str] = ["（Claude 濃縮暫不可用，以下為數據摘要）"]
    m = re.search(r"台股加權[^\n]*?[▲▼][^\n]*", brief) or re.search(r"加權[^\n]*?[▲▼][^\n]*", brief)
    if m:
        lines.append(f"大盤：{m.group(0).strip()}")
    lines.append(f"選股：{screener_line}")
    lines.append(f"警報：今日 {alert_count} 則異常警示")
    return "\n".join(lines)


def _send_afternoon_summary_card(card_text: str) -> bool:
    """獨立、韌性推送盤後總結卡到飛書群（排在各詳細則之前）。回傳是否送達。"""
    if not card_text or not card_text.strip():
        logger.warning("[盤後總結] 卡片內容為空，略過推送")
        return False
    date_str = datetime.now().strftime("%Y-%m-%d %A")
    title = f"📊 盤後總結 · {date_str}"
    content = card_text.strip() + "\n\n（完整數據與籌碼見下方各則）"
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        ok = notifier.send_message(CHAT_ID, title, content)  # 成功時 LarkNotifier 內部已記 log
        if not ok:
            logger.warning("[盤後總結] 飛書發送未成功")
        return bool(ok)
    except Exception as exc:
        logger.error(f"[盤後總結] 飛書推送失敗（非致命）: {exc}")
        return False


def run_afternoon(force: bool = False) -> Dict[str, StepResult]:
    """盤後工作流（非同步優化）"""
    state_data = global_state.get_state()
    if state_data.get("workflow_status", {}).get("skip_afternoon"):
        logger.info("⏸️ 檢測到微信狀態覆寫：休假 / 暫停，跳過盤後工作流。")
        return {"afternoon_skipped": StepResult("盤後流程(跳過)", True, is_critical=False)}

    results = {}
    master_run_id = build_run_id("afternoon")

    if not is_trading_day():
        logger.info("☕ 今日休市，跳過盤後總結與專業報告。")
        results["stock_afternoon"] = StepResult("股市 W(休市繞道)", True, is_critical=False)
        global_state.update("stock_data", {"afternoon_success": True})
        global_state.update("workflow_status", {"afternoon_run": True})
        return results

    # 盤後資料具備強烈相依性：先產生盤後總結 -> 接續研究與深度報告
    _precheck_wechat_health("afternoon")
    r = run_stock_agent("afternoon", control=_build_step_control(master_run_id, "stock_afternoon"), force=force)
    results["stock_afternoon"] = r
    print(str(r))

    if r.success:
        notification_steps = ["stock_afternoon"]

        screener_result, screener_dispatch = run_stock_screener_agent(master_run_id, force=force)
        results["stock_screener"] = screener_result
        if screener_dispatch is not None:
            results["stock_screener_notification"] = screener_dispatch
        _run_intraday_screener(results)
        _run_watchlist_merger(results)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            alerts = _read_stock_alerts()
            # 異常 Research W、正式報告、大盤復盤可先並行；每日 Research W 等正式報告/復盤完成後再整合，避免少素材。
            future_research = executor.submit(trigger_research_if_needed, alerts, run_id=master_run_id)
            future_r2 = executor.submit(run_stock_report_agent, _build_step_control(master_run_id, "daily_stock_report"))
            future_r3 = executor.submit(run_market_review_agent, _build_step_control(master_run_id, "market_review"))

            try:
                r2 = future_r2.result(timeout=300)
            except concurrent.futures.TimeoutError:
                r2 = StepResult("台股每日專業報告 W", False, error="timeout 300s")
            results["stock_report"] = r2
            print(str(r2))

            try:
                r3 = future_r3.result(timeout=300)
            except concurrent.futures.TimeoutError:
                r3 = StepResult("大盤盤後復盤 W", False, error="timeout 300s")
            results["market_review"] = r3
            print(str(r3))

            r_daily_research = run_daily_research_agent(master_run_id, alerts, results, force=force)
            results["daily_research"] = r_daily_research
            print(str(r_daily_research))
            if r_daily_research.success:
                notification_steps.append("daily_research")

            # Research W 每日健康摘要：彙整今日各類 Research W 成功率，只 log + 存 state（不洗版）
            try:
                from scripts.researcher import format_research_health_line, summarize_research_health
                health_line = format_research_health_line(summarize_research_health())
                logger.info(health_line)
                global_state.update("research_data", {"health": health_line})
            except Exception as exc:
                logger.warning(f"[Research W 健康摘要] 彙整失敗（非致命）: {exc}")

            try:
                notification_steps.extend(future_research.result(timeout=300) or [])
            except concurrent.futures.TimeoutError:
                logger.warning("Research W 超時")
            notification_steps.extend(["daily_stock_report", "market_review"])

        # ── 盤後總結卡：彙整全流程，搶在各詳細則之前優先送達（獨立韌性，不被後段 dispatch 連累）──
        try:
            _send_afternoon_summary_card(_build_afternoon_summary_card(master_run_id, results))
        except Exception as exc:
            logger.error(f"[盤後總結] 組裝/推送失敗（非致命）: {exc}")

        results["notification_dispatch"] = _dispatch_master_payloads(master_run_id, notification_steps)
        print(str(results["notification_dispatch"]))

    _emit_delivery_failure_alert("afternoon", list(results.values()))
    global_state.update("workflow_status", {"afternoon_run": True})
    return results

def run_evening(force: bool = False) -> Dict[str, StepResult]:
    """晚間工作流（每日）：讀 W 計畫 → 讀今日警報 → 日報 W"""
    state_data = global_state.get_state()
    if state_data.get("workflow_status", {}).get("skip_evening"):
        logger.info("⏸️ 檢測到微信狀態覆寫：休假 / 暫停，跳過晚間工作流。")
        return {"evening_skipped": StepResult("晚間流程(跳過)", True, is_critical=False)}

    results = {}
    master_run_id = build_run_id("evening")

    _precheck_wechat_health("evening")

    plan = read_w_plan()
    alerts = _read_stock_alerts()
    global_state.update("stock_data", {"alerts_count": len(alerts), "has_alerts": len(alerts) > 0})
    if alerts:
        logger.info(f"今日股市警報 {len(alerts)} 筆，將整合進日誌")

    r = run_daily_report_agent(
        plan_content=plan,
        alerts=alerts,
        control=_build_step_control(master_run_id, "daily_report"),
        force=force,
    )
    results["daily_report"] = r
    print(str(r))

    if r.success:
        results["notification_dispatch"] = _dispatch_master_payloads(master_run_id, ["daily_report"])
        print(str(results["notification_dispatch"]))
    
    # 盤後日誌寫完後，觸發 MemoryAgent 進行向量化存檔
    if r.success:
        try:
            logger.info("🧠 啟動記憶管家，將今日日誌/警報吸收至 ChromaDB...")
            from scripts.memory_agent import MemoryAgent
            agent = MemoryAgent()
            counts = agent.ingest_daily_files()
            logger.info("🧠 記憶吸收完成：%s", counts)
        except RuntimeError as e:
            logger.error("記憶管家初始化失敗: %s", e)
            global_state.log_error("memory_agent", str(e))
        except Exception as e:
            logger.error("記憶管家執行失敗: %s", e)

    # ── 日終秘書報告：彙整當天所有 agent 結果為一條飛書訊息 ──
    try:
        _send_secretary_daily_digest()
    except Exception as exc:
        logger.debug(f"[digest] 秘書日報推送失敗（非致命）: {exc}")

    _emit_delivery_failure_alert("evening", list(results.values()))
    global_state.update("workflow_status", {"evening_run": True})
    return results


def _fetch_weather_guangzhou() -> str:
    """用 Open-Meteo API 取廣州天氣預報（免費、無需 API key）"""
    import requests as _req

    try:
        # 廣州座標: 23.13, 113.26
        resp = _req.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 23.13,
                "longitude": 113.26,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
                "timezone": "Asia/Shanghai",
                "forecast_days": 2,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})

        weather_codes = {
            0: "☀️ 晴", 1: "🌤 大致晴", 2: "⛅ 多雲", 3: "☁️ 陰",
            45: "🌫 霧", 48: "🌫 霧凇", 51: "🌦 小雨", 53: "🌧 中雨",
            55: "🌧 大雨", 61: "🌦 小雨", 63: "🌧 中雨", 65: "🌧 大雨",
            71: "🌨 小雪", 73: "🌨 中雪", 75: "❄️ 大雪",
            80: "🌦 陣雨", 81: "🌧 陣雨", 82: "⛈ 暴雨",
            95: "⛈ 雷雨", 96: "⛈ 雷暴冰雹",
        }

        lines = ["🌤 廣州天氣"]
        dates = daily.get("time", [])
        t_max = daily.get("temperature_2m_max", [])
        t_min = daily.get("temperature_2m_min", [])
        precip = daily.get("precipitation_sum", [])
        codes = daily.get("weathercode", [])

        for i, d in enumerate(dates[:2]):
            label = "今日" if i == 0 else "明日"
            code_text = weather_codes.get(codes[i] if i < len(codes) else 0, "未知")
            hi = t_max[i] if i < len(t_max) else "?"
            lo = t_min[i] if i < len(t_min) else "?"
            rain = precip[i] if i < len(precip) else 0
            rain_text = f"，降水 {rain}mm" if rain and rain > 0 else ""
            lines.append(f"  {label}：{code_text} {lo}~{hi}°C{rain_text}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"[weather] 天氣取得失敗: {e}")
        return ""


def _fetch_tomorrow_calendar() -> str:
    """讀取明日行事曆提醒（從 memos.json 中帶日期的項目）"""
    try:
        from datetime import timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        memo_file = Path.home() / ".weixin-claude-bot" / "memos.json"
        if not memo_file.exists():
            return ""
        memos = json.loads(memo_file.read_text(encoding="utf-8"))
        tomorrow_items = [
            m for m in memos
            if not m.get("done") and m.get("due_date", "").startswith(tomorrow)
        ]
        if not tomorrow_items:
            return ""
        lines = [f"📅 明日行程（{tomorrow}）"]
        for m in tomorrow_items[:5]:
            lines.append(f"  - {m['text']}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"[calendar] 行事曆讀取失敗: {e}")
        return ""


def _send_secretary_daily_digest():
    """彙整當天所有 agent 結果 + 記事 + 天氣 + metrics，推送一條飛書秘書日報"""
    state = global_state.get_state()
    today_str = datetime.now().strftime("%Y-%m-%d")
    sections: list[str] = []

    # 1. 天氣預報
    weather = _fetch_weather_guangzhou()
    if weather:
        sections.append(weather)

    # 2. 工作流狀態
    wf = state.get("workflow_status", {})
    status_icons = {True: "✅", False: "❌"}
    wf_lines = [
        f"  晨間：{status_icons.get(wf.get('morning_run'), '⏭️')}",
        f"  盤後：{status_icons.get(wf.get('afternoon_run'), '⏭️')}",
        f"  晚間：{status_icons.get(wf.get('evening_run'), '⏭️')}",
    ]
    sections.append("📊 工作流\n" + "\n".join(wf_lines))

    # 3. 新聞
    news = state.get("news_data", {})
    if news.get("success"):
        sections.append(f"📰 新聞：{news.get('economy_count', 0)} 則財經新聞")
    else:
        sections.append("📰 新聞：未產出")

    # 4. 股市
    stock = state.get("stock_data", {})
    alert_count = stock.get("alerts_count", 0)
    sections.append(f"📈 股市：{alert_count} 則警報")

    # 5. 記事提醒
    try:
        memo_file = Path.home() / ".weixin-claude-bot" / "memos.json"
        if memo_file.exists():
            memos = json.loads(memo_file.read_text(encoding="utf-8"))
            pending = [m for m in memos if not m.get("done")]
            if pending:
                memo_lines = [f"  - {m['text']}" for m in pending[:5]]
                sections.append(f"📋 待辦記事（{len(pending)} 則）\n" + "\n".join(memo_lines))
    except Exception:
        pass

    # 6. 明日行程
    calendar = _fetch_tomorrow_calendar()
    if calendar:
        sections.append(calendar)

    # 7. Agent metrics 摘要
    try:
        reporter = get_metrics_reporter()
        summary = reporter.generate_summary(days=1)
        if "error" not in summary:
            total = summary.get("total_runs", 0)
            rate = summary["overall"].get("success_rate", 0)
            cost = summary["overall"].get("total_cost_usd", 0)
            sections.append(f"🤖 Agent 績效：{total} 次執行，成功率 {rate}%，API 費用 ${cost}")
    except Exception:
        pass

    # 8. 錯誤
    errors = state.get("errors", [])
    if errors:
        sections.append(f"⚠️ 錯誤（{len(errors)} 則）\n  " + "\n  ".join(str(e) for e in errors[:3]))

    content = "\n\n".join(sections)
    title = f"🗓️ {today_str} 秘書日報"

    from utils.lark_notifier import LarkNotifier
    notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
    notifier.send_message(CHAT_ID, title, content)
    logger.info("[digest] 秘書日報已推送到飛書")

def run_weekly_mon(force: bool = False) -> Dict[str, StepResult]:
    """週一工作流：電商 W 週報 + 成長選股 W (SEPA 中線)"""
    results = {}
    master_run_id = build_run_id("weekly-mon")

    _precheck_wechat_health("weekly_mon")

    # Step 1: 電商週報
    r = run_ecommerce_agent(_build_step_control(master_run_id, "ecommerce"), force=force)
    results["ecommerce"] = r
    print(str(r))

    # Step 2: SEPA 中線選股（成長選股 W）
    try:
        logger.info("📈 週一 SEPA 中線選股掃描...")
        from scripts.sepa_screener import cmd_scan as sepa_scan
        sepa_scan(dry_run=False)
        results["sepa_screener"] = StepResult("成長選股 W", True, is_critical=False)
    except Exception as e:
        logger.error("SEPA 選股失敗: %s", e)
        results["sepa_screener"] = StepResult("成長選股 W", False, error=str(e), is_critical=False)

    # Step 3: 觀察池合併
    _run_watchlist_merger(results)

    if r.success:
        results["notification_dispatch"] = _dispatch_master_payloads(master_run_id, ["ecommerce"])
        print(str(results["notification_dispatch"]))
    _emit_delivery_failure_alert("weekly_mon", list(results.values()))
    return results


def _is_first_friday_of_month(today: Optional[date] = None) -> bool:
    """判斷今天是否為本月第一個週五"""
    today = today or date.today()
    if today.weekday() != 4:  # 4 = Friday
        return False
    return today.day <= 7


def _run_intraday_screener(results: Dict[str, StepResult]) -> None:
    """跑日內選股 W：盤後 M1 資料齊全時掃描，輸出進 logs/intraday/，供 watchlist_merger 撈為 auxiliary_signals.intraday。

    dry_run=True：不另推飛書 / 微信（避免與股市 W 盤後重複），只落地 json/md 給 watchlist_merger 與 advisor 用。
    """
    try:
        from scripts.intraday_screener import cmd_scan as intraday_scan
        logger.info("📈 執行日內選股 W 盤後掃描...")
        intraday_scan(dry_run=True)
        results["intraday_screener"] = StepResult("日內選股 W", True, is_critical=False)
    except Exception as e:
        logger.error("日內選股執行失敗: %s", e)
        results["intraday_screener"] = StepResult("日內選股 W", False, error=str(e), is_critical=False)


def _run_watchlist_merger(results: Dict[str, StepResult]) -> None:
    """跑觀察池合併（統一寫入 logs/watchlist/current.json）"""
    try:
        from utils.watchlist_merger import build_watchlist, save_watchlist
        wl = build_watchlist()
        save_watchlist(wl)
        s = wl.get("summary", {})
        logger.info(
            "✓ 觀察池合併: 短線 %d / 中線 %d / 長線 %d / 日內 %d / 跨池 %d",
            s.get("short_term_count", 0), s.get("medium_term_count", 0),
            s.get("long_term_count", 0), s.get("intraday_count", 0), s.get("cross_signals_count", 0),
        )
        results["watchlist_merger"] = StepResult("觀察池合併", True, is_critical=False)
    except Exception as e:
        logger.error("觀察池合併失敗: %s", e)
        results["watchlist_merger"] = StepResult("觀察池合併", False, error=str(e), is_critical=False)

def run_weekly_sun(force: bool = False) -> Dict[str, StepResult]:
    """週日工作流：反思 W"""
    results = {}
    master_run_id = build_run_id("weekly-sun")
    _precheck_wechat_health("weekly_sun")
    r = run_reflection_agent(_build_step_control(master_run_id, "reflection"), force=force)
    results["reflection"] = r
    print(str(r))
    if r.success:
        results["notification_dispatch"] = _dispatch_master_payloads(master_run_id, ["reflection"])
        print(str(results["notification_dispatch"]))
    _emit_delivery_failure_alert("weekly_sun", list(results.values()))
    return results


def run_weekly_fri(force: bool = False) -> Dict[str, StepResult]:
    """週五工作流：短線選股 + 復盤報告"""
    results = {}
    master_run_id = build_run_id("weekly-fri")

    _precheck_wechat_health("weekly_fri")

    # Step 1: 執行短線選股
    logger.info("📊 週五選股掃描...")
    screener_result, screener_dispatch = run_stock_screener_agent(master_run_id, force=False)
    results["stock_screener"] = screener_result
    if screener_dispatch is not None:
        results["stock_screener_notification"] = screener_dispatch

    # Step 2: 執行週五復盤（含保底通知）
    try:
        from scripts.screener_review import run_review
        logger.info("📈 週五選股復盤...")
        review_control = _build_step_control(master_run_id, "screener_review")
        with activate_control(review_control):
            review_result = run_review()

        if review_result and review_result.get("ok"):
            results["screener_review"] = StepResult("選股復盤 W", True, data=review_result, is_critical=False)
            logger.info("📈 週五復盤完成")
            # 立即分發週回測 payload，避免被後續長時間任務阻塞
            results["screener_review_notification"] = _dispatch_master_payloads(master_run_id, ["screener_review"])
        else:
            reason = (review_result or {}).get("reason", "UNKNOWN")
            results["screener_review"] = StepResult("選股復盤 W", False, data=review_result, error=reason, is_critical=False)
            logger.warning("週五復盤未完整生成：%s", reason)
    except Exception as e:
        logger.error("選股復盤執行失敗: %s", e)
        results["screener_review"] = StepResult("選股復盤 W", False, error=str(e), is_critical=False)

    # Step 3: 價值選股 W（force 時強制執行，否則只在每月第一個週五）
    if force or _is_first_friday_of_month():
        try:
            logger.info("💎 執行價值選股掃描...")
            from scripts.value_screener import cmd_scan as value_scan
            value_scan(dry_run=False)
            results["value_screener"] = StepResult("價值選股 W", True, is_critical=False)
        except Exception as e:
            logger.error("價值選股失敗: %s", e)
            results["value_screener"] = StepResult("價值選股 W", False, error=str(e), is_critical=False)

        # Step 3b: 核心清單體檢（與價值選股一起跑）
        try:
            logger.info("🔬 執行核心清單體檢...")
            from scripts.validate_core_watchlist import cmd_validate
            cmd_validate(push=True)
            results["core_health"] = StepResult("核心體檢 W", True, is_critical=False)
        except Exception as e:
            logger.error("核心體檢失敗: %s", e)
            results["core_health"] = StepResult("核心體檢 W", False, error=str(e), is_critical=False)
    else:
        logger.info("本週五非月初且未強制執行，跳過價值選股與核心體檢")

    # Step 4: 觀察池合併
    _run_watchlist_merger(results)

    # Step 5: 分發週回測 payload（若有）
    results["notification_dispatch"] = _dispatch_master_payloads(master_run_id, ["screener_review"])

    _emit_delivery_failure_alert("weekly_fri", list(results.values()))
    return results

def run_all_modes() -> Dict[str, StepResult]:
    """全架構工作流：依序執行今天該跑的模式並彙總結果"""
    combined: Dict[str, StepResult] = {}
    weekday = datetime.now().weekday()

    ordered_modes = [
        ("morning", run_morning),
        ("afternoon", run_afternoon),
        ("evening", run_evening),
    ]
    # 週一加跨境電商
    if weekday == 0:
        ordered_modes.append(("weekly_mon", run_weekly_mon))
    # 週五加選股復盤
    if weekday == 4:
        ordered_modes.append(("weekly_fri", run_weekly_fri))
    # 週日加反思
    if weekday == 6:
        ordered_modes.append(("weekly_sun", run_weekly_sun))

    for mode_name, runner in ordered_modes:
        logger.info("========== 全架構巡檢：啟動 [%s] ==========", mode_name)
        print(f"\n{'-'*42}\n  全架構模式：{mode_name}\n{'-'*42}\n")
        try:
            mode_results = runner()
        except Exception as e:
            logger.error("模式 %s 執行拋出異常: %s\n%s", mode_name, e, traceback.format_exc())
            combined[f"{mode_name}/orchestrator"] = StepResult(
                f"{mode_name} orchestrator", False, error=str(e), is_critical=True
            )
            continue

        if not mode_results:
            combined[f"{mode_name}/empty"] = StepResult(
                f"{mode_name} empty result", True, is_critical=False
            )
            continue

        for step_name, step_result in mode_results.items():
            combined[f"{mode_name}/{step_name}"] = step_result

    return combined

# ─────────────────────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="W 的 AI 助理團隊總調度器")
    parser.add_argument(
        "--mode",
        choices=["morning", "afternoon", "evening", "weekly_mon", "weekly_fri", "weekly_sun", "all"],
        default="morning",
        help="""
        morning     — 晨間（新聞 + 股市晨報 + 問 W 今日計畫）
        afternoon   — 盤後（股市 W 盤後總結 + Research + 台股每日專業報告 + 大盤盤後復盤 + 短線選股）
        evening     — 晚間（日報 W 生成今日日誌）
        weekly_mon  — 週一（跨境電商 W 週報）
        weekly_fri  — 週五（短線選股 + 週復盤）
        weekly_sun  — 週日（反思 W 週反思）
        all         — 全架構（依序跑 morning/afternoon/evening/weekly_*）
        """,
    )
    parser.add_argument("--notify", action="store_true", help="完成後發送摘要到飛書")
    parser.add_argument("--force", action="store_true", help="強制重跑，忽略冪等性檢查（即使今日已執行過也重新執行）")
    args = parser.parse_args()

    started_at = datetime.now()
    logger.info(f"========== Orchestrator 啟動 [{args.mode}] ==========")
    print(f"\n{'='*50}")
    print(f"  Orchestrator 啟動｜模式：{args.mode}")
    print(f"{'='*50}\n")

    mode_map = {
        "morning":     run_morning,
        "afternoon":   run_afternoon,
        "evening":     run_evening,
        "weekly_mon":  run_weekly_mon,
        "weekly_fri":  run_weekly_fri,
        "weekly_sun":  run_weekly_sun,
        "all":         run_all_modes,
    }

    # 執行選擇的模式，傳遞 force 參數
    if args.mode == "all":
        results = mode_map[args.mode]()
    else:
        results = mode_map[args.mode](force=args.force)

    elapsed = (datetime.now() - started_at).total_seconds()
    logger.info(f"========== Orchestrator 完成 [{args.mode}]｜耗時 {elapsed:.1f}s ==========")

    # 發送完成摘要（可選）
    if args.notify:
        try:
            from utils.lark_notifier import LarkNotifier
            notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
            lines = [f"**Orchestrator 完成報告｜{args.mode}**"]
            for name, r in results.items():
                lines.append(str(r))
            lines.append(f"\n_耗時 {elapsed:.1f}s_")
            notifier.send_message(CHAT_ID, "✅ Orchestrator 完成報告", "\n".join(lines))
        except Exception:
            pass

    # 任意一個步驟失敗就 exit 1
    failed = [r for r in results.values() if not r.success and r.is_critical]
    if failed:
        first_fail = failed[0]
        global_state.log_error(first_fail.name, first_fail.error)
        send_error_alert(first_fail, args.mode)
        print(f"\n⚠️  有 {len(failed)} 個關鍵步驟失敗，已通知系統異常而退出")
        sys.exit(1)

    # 週日生成 Agent 績效週報
    if datetime.now().weekday() == 6:  # 週日
        try:
            reporter = get_metrics_reporter()
            report = reporter.generate_weekly_report()
            report_file = _BASE_DIR / "logs" / "agent_metrics_weekly.md"
            report_file.write_text(report, encoding="utf-8")
            logger.info(f"[Metrics] 週績效報告已生成: {report_file}")
        except Exception as e:
            logger.debug(f"[Metrics] 週報告生成失敗: {e}")

    sys.exit(0)

if __name__ == "__main__":
    main()
