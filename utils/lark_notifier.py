#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飛書通知工具（App 認證版）
"""

import os
import json
import yaml
import requests
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

AUDIT_LOG = Path(__file__).parent.parent / "logs" / "lark_delivery.jsonl"

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class LarkNotifier:
    """飛書通知器（使用 App ID + App Secret）"""

    MAX_CHUNK = 2000

    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.app_id = os.getenv("FEISHU_APP_ID")
        self.app_secret = os.getenv("FEISHU_APP_SECRET")
        self._token = None
        self._token_expire_at = 0.0

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("飛書配置讀取失敗 (%s): %s", config_path, e)
            return {}

    def _get_token(self, force_refresh: bool = False) -> str:
        """取得 tenant_access_token"""
        if not force_refresh and self._token and time.time() < self._token_expire_at:
            return self._token

        resp = requests.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"飛書認證失敗: {data.get('msg')}")

        self._token = data["tenant_access_token"]
        expire = int(data.get("expire", 7200))
        self._token_expire_at = time.time() + max(expire - 60, 60)
        logger.info("飛書 token 取得成功")
        return self._token

    @staticmethod
    def _is_auth_error(result: Dict[str, Any]) -> bool:
        code = result.get("code")
        msg = str(result.get("msg", "")).lower()
        return code in {99991661, 99991663, 99991668} or "access token" in msg

    def _resolve_channel_id(self, agent_key: str) -> str:
        """從 YAML 取 channel_id，空值則 fallback 到 env 的 FEISHU_DEFAULT_CHAT_ID"""
        value = self.config.get("dimensions", {}).get(agent_key, {}).get("channel_id", "")
        if not value:
            value = os.getenv("FEISHU_DEFAULT_CHAT_ID", "")
        return value

    def _audit_log(self, receive_id: str, title: str, success: bool, error: str = ""):
        """記錄送達審計"""
        try:
            AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": datetime.now().isoformat(),
                    "channel": "lark",
                    "receive_id": receive_id,
                    "title": title,
                    "status": "success" if success else "error",
                    "error": error
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"審計日誌寫入失敗: {e}")

    def send_message(self, receive_id: str, title: str, content: str,
                     receive_id_type: str = "chat_id") -> bool:
        """發送消息到指定群組或用戶（含重試）"""
        if not self.app_id or not self.app_secret:
            logger.warning("飛書 App ID / Secret 未設置，跳過發送")
            self._audit_log(receive_id, title, False, "missing_credentials")
            return False
        if not receive_id:
            logger.warning("receive_id 為空，跳過發送")
            self._audit_log(receive_id, title, False, "empty_receive_id")
            return False

        def _post(token: str) -> Dict[str, Any]:
            resp = requests.post(
                f"{FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": f"{title}\n\n{content}"})
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()

        for attempt in range(3):
            try:
                token = self._get_token()
                result = _post(token)
                if result.get("code") != 0 and self._is_auth_error(result):
                    logger.warning("飛書 token 疑似過期，嘗試刷新後重送")
                    token = self._get_token(force_refresh=True)
                    result = _post(token)

                if result.get("code") != 0:
                    error_msg = f"code={result.get('code')} msg={result.get('msg')}"
                    logger.error(f"消息發送失敗: {error_msg}")
                    self._audit_log(receive_id, title, False, error_msg)
                    return False

                logger.info(f"消息發送成功: {title}")
                self._audit_log(receive_id, title, True)
                return True

            except Exception as e:
                if attempt < 2:
                    delay = 2 ** attempt
                    logger.warning(f"發送失敗（第 {attempt+1} 次），{delay}s 後重試: {e}")
                    time.sleep(delay)
                else:
                    logger.error(f"消息發送異常（已重試 3 次）: {e}")
                    self._audit_log(receive_id, title, False, str(e))
                    return False
        return False

    @staticmethod
    def _chunk_text(content: str, max_chunk: int = None) -> list[str]:
        if content is None:
            return []
        text = str(content)
        if not text:
            return [""]
        limit = max_chunk or LarkNotifier.MAX_CHUNK
        return [text[i:i + limit] for i in range(0, len(text), limit)]

    def send_message_chunked(
        self,
        receive_id: str,
        title: str,
        content: str,
        receive_id_type: str = "chat_id",
    ) -> bool:
        chunks = self._chunk_text(content)
        total = len(chunks)
        logger.info("飛書準備分段發送 %s 段（共 %s 字）: %s", total, len(content), title)
        ok = True
        for i, chunk in enumerate(chunks):
            chunk_title = f"{title}（{i + 1}/{total}）" if total > 1 else title
            if not self.send_message(receive_id, chunk_title, chunk, receive_id_type=receive_id_type):
                ok = False
        return ok

    def send_news_notification(self, category: str, message: str, news_count: int) -> bool:
        receive_id = self._resolve_channel_id("news_agent")
        if not receive_id:
            logger.warning("新聞通知的 chat_id 未設置")
            return False
        return self.send_message(receive_id, f"📰 {category} 新聞（{news_count} 則）", message)

    def send_daily_report(self, report_content: str) -> bool:
        receive_id = self._resolve_channel_id("daily_report_agent")
        if not receive_id:
            logger.warning("日報的 channel_id 未設置")
            return False
        return self.send_message(receive_id, "📋 每日日報", report_content)

    def _format_name_with_symbol(self, name: str, symbol: str) -> str:
        suffix = f"（{symbol}）"
        return name if name.endswith(suffix) else f"{name}{suffix}"

    def send_stock_alert(self, alert_type: str, message: str, stock_info: Dict = None) -> bool:
        receive_id = self._resolve_channel_id("stock_monitor_agent")
        if not receive_id:
            logger.warning("股市警報的 chat_id 未設置")
            return False
        icon = {"critical": "🚨", "warning": "⚠️"}.get(alert_type, "ℹ️")
        title = f"{icon} 股市警報"
        if stock_info:
            title += f"：{self._format_name_with_symbol(stock_info.get('name', ''), stock_info.get('symbol', ''))}"
        return self.send_message(receive_id, title, message)

    def send_error_notification(self, title: str, message: str) -> bool:
        logger.error(f"[錯誤通知] {title}: {message}")
        receive_id = (
            self._resolve_channel_id("stock_monitor_agent")
            or self._resolve_channel_id("news_agent")
        )
        if not receive_id:
            logger.warning("錯誤通知 channel_id 未設置，無法推送")
            return False
        return self.send_message(receive_id, f"❌ {title}", message)

    # ── 檔案上傳與發送 ───────────────────────────────────────────────

    def upload_file(self, file_path: str, file_type: str = "stream") -> Optional[str]:
        """
        上傳檔案到飛書，回傳 file_key

        Args:
            file_path: 本地檔案絕對路徑
            file_type: 上傳類型，預設 stream（適用於 PDF）

        Returns:
            file_key 或 None（失敗）
        """
        if not self.app_id or not self.app_secret:
            logger.warning("飛書 App ID / Secret 未設置，跳過上傳")
            return None

        path = Path(file_path)
        if not path.exists():
            logger.error("檔案不存在：%s", file_path)
            return None

        try:
            token = self._get_token()
            url = f"{FEISHU_BASE}/im/v1/files"

            with open(file_path, "rb") as f:
                files = {
                    "file": (path.name, f, "application/pdf"),
                }
                data = {
                    "file_type": file_type,
                    "file_name": path.name,
                }
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data=data,
                    files=files,
                    timeout=60,
                )

            result = resp.json()
            if result.get("code") != 0 and self._is_auth_error(result):
                logger.warning("飛書 token 疑似過期，嘗試刷新後重傳")
                token = self._get_token(force_refresh=True)
                with open(file_path, "rb") as f:
                    files = {"file": (path.name, f, "application/pdf")}
                    data = {"file_type": file_type, "file_name": path.name}
                    resp = requests.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        data=data,
                        files=files,
                        timeout=60,
                    )
                result = resp.json()

            if result.get("code") != 0:
                logger.error("檔案上傳失敗: code=%s msg=%s", result.get("code"), result.get("msg"))
                return None

            file_key = result.get("data", {}).get("file_key")
            logger.info("檔案上傳成功: %s -> file_key=%s", path.name, file_key)
            return file_key

        except Exception as e:
            logger.error("檔案上傳異常: %s", e, exc_info=True)
            return None

    def send_file(self, receive_id: str, file_key: str,
                  receive_id_type: str = "chat_id") -> bool:
        """
        發送檔案訊息到飛書群組

        Args:
            receive_id: 群組 chat_id
            file_key: 上傳後取得的 file_key
            receive_id_type: "chat_id" 或 "open_id"

        Returns:
            發送成功與否
        """
        if not self.app_id or not self.app_secret:
            logger.warning("飛書 App ID / Secret 未設置，跳過發送")
            return False
        if not receive_id:
            logger.warning("receive_id 為空，跳過發送")
            return False
        if not file_key:
            logger.warning("file_key 為空，跳過發送")
            return False

        try:
            def _post(token: str) -> Dict[str, Any]:
                resp = requests.post(
                    f"{FEISHU_BASE}/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": receive_id,
                        "msg_type": "file",
                        "content": json.dumps({"file_key": file_key}),
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json()

            token = self._get_token()
            result = _post(token)
            if result.get("code") != 0 and self._is_auth_error(result):
                logger.warning("飛書 token 疑似過期，嘗試刷新後重送")
                token = self._get_token(force_refresh=True)
                result = _post(token)

            if result.get("code") != 0:
                logger.error("檔案訊息發送失敗: code=%s msg=%s", result.get("code"), result.get("msg"))
                return False

            logger.info("檔案訊息發送成功")
            return True

        except Exception as e:
            logger.error("檔案訊息發送異常: %s", e, exc_info=True)
            return False

    def send_pdf_report(self, receive_id: str, pdf_path: str,
                        title: str = "📊 台股每日報告",
                        receive_id_type: str = "chat_id") -> Dict[str, bool]:
        """
        一鍵發送 PDF 報告：先發文字摘要，再發 PDF 附件

        Args:
            receive_id: 群組 chat_id
            pdf_path: PDF 檔案路徑
            title: 文字訊息標題
            receive_id_type: "chat_id" 或 "open_id"

        Returns:
            {"text_ok": bool, "file_ok": bool, "file_key": str|None}
        """
        result = {"text_ok": False, "file_ok": False, "file_key": None}

        # 先發文字提示
        text = f"{title}\n\n📎 完整 PDF 報告已生成，正在上傳附件..."
        result["text_ok"] = self.send_message(receive_id, title, text, receive_id_type)

        # 上傳並發送 PDF
        file_key = self.upload_file(pdf_path)
        result["file_key"] = file_key
        if file_key:
            result["file_ok"] = self.send_file(receive_id, file_key, receive_id_type)

        return result
