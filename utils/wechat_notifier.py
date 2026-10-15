#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信推播工具 (透過本地 Node.js wechat_bridge.mjs)
支援長訊息自動分段，以及 typing_seconds 提示
"""
import requests
import logging
import os
import json
from pathlib import Path
from datetime import datetime
from uuid import uuid4

logger = logging.getLogger(__name__)

# 訊息分段長度（微信限制單條 2048 字元）
MAX_CHUNK = 2000
AUDIT_LOG = Path(__file__).parent.parent / "logs" / "wechat_delivery.jsonl"


class WechatNotifier:
    def __init__(self, endpoint="http://127.0.0.1:3001/send"):
        self.endpoint = endpoint
        self.file_endpoint = (
            endpoint.rsplit("/", 1)[0] + "/send-file"
            if endpoint.endswith("/send")
            else endpoint.rstrip("/") + "/send-file"
        )
        self.health_endpoint = (
            endpoint.rsplit("/", 1)[0] + "/health"
            if endpoint.endswith("/send")
            else endpoint.rstrip("/") + "/health"
        )

    def _audit_log(self, to_user: str, text: str, success: bool, error: str = ""):
        """記錄送達審計"""
        try:
            AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "channel": "wechat",
                    "to_user": to_user or "default",
                    "text_length": len(text),
                    "status": "success" if success else "error",
                    "error": error
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"審計日誌寫入失敗: {e}")

    @staticmethod
    def _resolve_default_target(to_user: str = None) -> str:
        return to_user or os.getenv("WECHAT_PUSH_TO_USER") or None

    @staticmethod
    def _resolve_default_context_token(context_token: str = None) -> str:
        return context_token or os.getenv("WECHAT_PUSH_CONTEXT_TOKEN") or None

    def _set_error(self, message: str) -> bool:
        self.last_error = message
        self.last_result = {
            "accepted": False,
            "delivered": False,
            "queued": False,
            "pending_repair_count": None,
            "delivery_state": "error",
            "reason": message,
            "logical_id": None,
            "chunk_count": None,
            "details": [],
        }
        logger.error("[WechatNotifier] %s", message)
        return False

    def _fetch_bridge_health(self, to_user: str = None):
        params = {"to_user": to_user} if to_user else None
        resp = requests.get(self.health_endpoint, params=params, timeout=5)
        if resp.status_code != 200:
            raise RuntimeError(f"Bridge 健康檢查失敗 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"Bridge 健康檢查回應非 JSON: {resp.text[:200]}")
        if data.get("status") != "ok":
            raise RuntimeError(f"Bridge 健康檢查失敗: {data}")
        self._last_health_data = data
        return data

    def get_bridge_health(self, to_user: str = None):
        try:
            return self._fetch_bridge_health(to_user)
        except requests.exceptions.ConnectionError:
            return {
                "status": "error",
                "ready_for_push": False,
                "requires_user_refresh": False,
                "context_invalid": False,
                "pending_failed_count": None,
                "summary_status": "bridge_unreachable",
                "summary_text": "微信 Bridge 未啟動 (Connection Refused)。",
                "target_user": to_user or self._resolve_default_target(),
            }
        except RuntimeError as e:
            return {
                "status": "error",
                "ready_for_push": False,
                "requires_user_refresh": False,
                "context_invalid": False,
                "pending_failed_count": None,
                "summary_status": "bridge_health_error",
                "summary_text": str(e),
                "target_user": to_user or self._resolve_default_target(),
            }
        except Exception as e:
            return {
                "status": "error",
                "ready_for_push": False,
                "requires_user_refresh": False,
                "context_invalid": False,
                "pending_failed_count": None,
                "summary_status": "bridge_health_exception",
                "summary_text": f"Bridge 健康檢查例外: {e}",
                "target_user": to_user or self._resolve_default_target(),
            }

    def _check_bridge_ready(self, to_user: str = None, context_token: str = None) -> bool:
        try:
            effective_target = self._resolve_default_target(to_user)
            effective_context = self._resolve_default_context_token(context_token)
            data = self._fetch_bridge_health(effective_target)

            if effective_context:
                return True

            if data.get("ready_for_push"):
                return True

            reason = data.get("context_reason") or "unknown"
            updated_at = data.get("context_updated_at") or "-"
            age_seconds = data.get("context_age_seconds")
            max_age_seconds = data.get("context_max_age_seconds")
            target = data.get("target_user") or effective_target or "-"
            detail = (
                f"微信 Bridge 尚未準備好推播 target={target} "
                f"reason={reason} updated_at={updated_at} "
                f"age_seconds={age_seconds if age_seconds is not None else '-'} "
                f"max_age_seconds={max_age_seconds if max_age_seconds is not None else '-'}。"
                "請先在微信對機器人發送一則訊息（例如：ping）刷新上下文。"
            )
            return self._set_error(detail)
        except requests.exceptions.ConnectionError:
            return self._set_error(
                "微信 Bridge 未啟動 (Connection Refused)。請確保 Node.js wechat_bridge.mjs 正在執行。"
            )
        except RuntimeError as e:
            return self._set_error(str(e))
        except Exception as e:
            return self._set_error(f"Bridge 健康檢查例外: {e}")

    @staticmethod
    def _build_logical_id() -> str:
        return f"wxmsg-{uuid4().hex[:12]}"

    def _build_message_meta(self, metadata: dict | None, logical_id: str) -> dict:
        meta = dict(metadata or {})
        meta.setdefault("logical_id", logical_id)
        return meta

    @staticmethod
    def _normalize_send_result(data: dict, logical_id: str) -> dict:
        details = data.get("details") or []
        chunk_count = data.get("chunk_count")
        if chunk_count in (None, "") and details:
            chunk_count = len(details)
        return {
            "accepted": data.get("status") in {"success", "queued"} or bool(data.get("queued_for_repair")),
            "delivered": data.get("status") == "success",
            "queued": data.get("status") == "queued" or bool(data.get("queued_for_repair")),
            "pending_repair_count": data.get("pending_repair_count", data.get("pending_after_replay")),
            "delivery_state": data.get("delivery_state") or ("queued_for_repair" if data.get("queued_for_repair") else data.get("status") or "error"),
            "trace_id": data.get("trace_id"),
            "queue_id": data.get("repair_queue_id"),
            "ack_mode": data.get("ack_mode"),
            "chunk_count": chunk_count,
            "details": details,
            "logical_id": data.get("logical_id") or logical_id,
            "reason": data.get("msg") or data.get("hint") or "",
            "meta": data.get("meta") or {},
        }

    def send_message(
        self,
        text: str,
        to_user: str = None,
        typing_seconds: int = 0,
        context_token: str = None,
        allow_unready_queue: bool = False,
        metadata: dict | None = None,
    ) -> bool:
        if text is None:
            logger.warning("[WechatNotifier] text 為 None，跳過發送")
            return False
        if not isinstance(text, str):
            text = str(text)
        if not text.strip():
            logger.warning("[WechatNotifier] text 為空白，跳過發送")
            return False

        self.last_error = ""
        logical_id = self._build_logical_id()
        message_meta = self._build_message_meta(metadata, logical_id)
        self.last_result = {
            "accepted": False,
            "delivered": False,
            "queued": False,
            "pending_repair_count": 0,
            "delivery_state": "init",
            "logical_id": logical_id,
            "chunk_count": None,
            "details": [],
            "meta": message_meta,
        }
        effective_target = self._resolve_default_target(to_user)
        effective_context = self._resolve_default_context_token(context_token)
        bridge_ready = self._check_bridge_ready(effective_target, effective_context)
        if not bridge_ready:
            if not allow_unready_queue:
                return False
            logger.warning(
                "[WechatNotifier] Bridge 未就緒，改以 deferred queue 模式嘗試投遞 target=%s logical_id=%s",
                effective_target,
                logical_id,
            )

        logger.info(
            "[WechatNotifier] 準備發送 logical message（共 %s 字）給 %s via %s logical_id=%s",
            len(text),
            effective_target,
            "explicit_context" if effective_context else ("bridge_cache" if bridge_ready else "deferred_queue"),
            logical_id,
        )

        try:
            resp = requests.post(
                self.endpoint,
                json={
                    "to_user": effective_target,
                    "text": text,
                    "typing_seconds": typing_seconds,
                    "context_token": effective_context,
                    "logical_id": logical_id,
                    "meta": message_meta,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error(f"[WechatNotifier] HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            try:
                data = resp.json()
            except ValueError:
                return self._set_error(f"無法解析 JSON 回應: {resp.text[:200]}")

            normalized = self._normalize_send_result(data, logical_id)
            normalized["meta"] = message_meta | dict(normalized.get("meta") or {})
            self.last_result = normalized
            self.last_error = str(normalized.get("reason") or "")

            if normalized.get("queued"):
                logger.warning(
                    "[WechatNotifier] 訊息已排隊 logical_id=%s trace_id=%s queue_id=%s state=%s target=%s pending=%s",
                    normalized.get("logical_id"),
                    normalized.get("trace_id"),
                    normalized.get("queue_id"),
                    normalized.get("delivery_state"),
                    effective_target,
                    normalized.get("pending_repair_count"),
                )
            elif normalized.get("delivered"):
                logger.info(
                    "[WechatNotifier] 發送成功 logical_id=%s trace_id=%s target=%s chunk_count=%s ack_mode=%s",
                    normalized.get("logical_id"),
                    normalized.get("trace_id"),
                    data.get("target"),
                    normalized.get("chunk_count"),
                    normalized.get("ack_mode"),
                )
            else:
                hint = data.get("hint") or ""
                message = f"API 失敗: {data}"
                if hint:
                    message += f" | hint={hint}"
                return self._set_error(message)
        except requests.exceptions.ConnectionError:
            self._audit_log(effective_target, text, False, "connection_error")
            return self._set_error(
                "微信 Bridge 未啟動 (Connection Refused)。請確保 Node.js wechat_bridge.mjs 正在執行。"
            )
        except Exception as e:
            self._audit_log(effective_target, text, False, str(e))
            return self._set_error(f"發送例外: {e}")

        final_result = self.last_result or {}
        if final_result.get("delivered"):
            self._audit_log(effective_target, text, True)
        elif not final_result.get("accepted") and not final_result.get("queued"):
            self._audit_log(effective_target, text, False, str(final_result.get("reason") or "send_failed"))
        return bool(final_result.get("accepted") or final_result.get("delivered"))

    def send_file(
        self,
        file_path: str,
        to_user: str = None,
        context_token: str = None,
    ) -> bool:
        if file_path is None:
            logger.warning("[WechatNotifier] file_path 為 None，跳過發送")
            return False
        if not isinstance(file_path, str):
            file_path = str(file_path)
        if not file_path.strip():
            logger.warning("[WechatNotifier] file_path 為空白，跳過發送")
            return False
        if not os.path.isfile(file_path):
            return self._set_error(f"檔案不存在: {file_path}")

        self.last_error = ""
        self.last_result = {
            "accepted": False,
            "delivered": False,
            "queued": False,
            "pending_repair_count": 0,
            "delivery_state": "init",
            "logical_id": None,
            "chunk_count": None,
            "details": [],
        }
        effective_target = self._resolve_default_target(to_user)
        effective_context = self._resolve_default_context_token(context_token)
        bridge_ready = self._check_bridge_ready(effective_target, effective_context)
        if not bridge_ready:
            return False

        try:
            resp = requests.post(
                self.file_endpoint,
                json={
                    "to_user": effective_target,
                    "file_path": file_path,
                    "context_token": effective_context,
                },
                timeout=60,
            )
            if resp.status_code != 200:
                logger.error(f"[WechatNotifier] HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            try:
                data = resp.json()
            except ValueError:
                return self._set_error(f"無法解析 JSON 回應: {resp.text[:200]}")

            if data.get("status") != "success":
                return self._set_error(f"微信檔案發送失敗: {data}")

            trace_id = data.get("trace_id")
            self.last_result = {
                "accepted": True,
                "delivered": True,
                "queued": False,
                "pending_repair_count": 0,
                "delivery_state": data.get("delivery_state") or "sent",
                "trace_id": trace_id,
                "file_name": data.get("file_name"),
                "file_size": data.get("file_size"),
                "logical_id": None,
                "chunk_count": None,
                "details": [],
            }
            if trace_id:
                logger.info(
                    "[WechatNotifier] 檔案發送成功 trace_id=%s target=%s file=%s",
                    trace_id,
                    data.get("target"),
                    data.get("file_name"),
                )
            return True
        except requests.exceptions.ConnectionError:
            return self._set_error(
                "微信 Bridge 未啟動 (Connection Refused)。請確保 Node.js wechat_bridge.mjs 正在執行。"
            )
        except Exception as e:
            return self._set_error(f"檔案發送例外: {e}")
