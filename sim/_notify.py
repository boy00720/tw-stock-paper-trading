"""automation/sim/_notify.py — sim 子系統共用 DM 推送 helper

為什麼存在：sim 系列所有 CLI（reports / advisor runner / morning_check）都需要推
W 個人 DM（FEISHU_USER_OPEN_ID）。各自包一份 LarkNotifier 構造邏輯會重複，且
lark_notifier 簽名改動時要改多處。本模組統一封裝。

對外 API：
- send_dm(title, content) -> bool：推 W 個人 DM，未設 open_id 跳過 return False
- notify_fill(fill, account_snapshot) -> bool：包 fmt_realtime_fill 後 send_dm
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUTOMATION_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LARK_CONFIG = AUTOMATION_DIR / "config" / "lark_config.yaml"


def send_dm(title: str, content: str) -> bool:
    """推 W 個人 DM；FEISHU_USER_OPEN_ID 未設則 log warning return False。"""
    open_id = os.getenv("FEISHU_USER_OPEN_ID")
    if not open_id:
        logger.warning("[sim._notify] FEISHU_USER_OPEN_ID 未設定，跳過推送")
        return False
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier(str(DEFAULT_LARK_CONFIG))
        return bool(notifier.send_message(
            receive_id=open_id,
            title=title,
            content=content,
            receive_id_type="open_id",
        ))
    except Exception:
        logger.exception("[sim._notify] 推送 DM 失敗")
        return False


def notify_fill(fill: dict[str, Any], account_snapshot: dict[str, Any]) -> bool:
    """advisor 撮合 fill 後推即時 DM；title 用 emoji + symbol，content 走 fmt_realtime_fill。"""
    try:
        from sim.reports import DEFAULT_FILLS_PATH, _per_fill_exit_context, enrich_fill_with_order_context, fmt_realtime_fill
        enriched = enrich_fill_with_order_context(fill)
        if (enriched.get("side") or "").upper() == "SELL":
            fid = enriched.get("fill_id")
            if fid:
                ctx = _per_fill_exit_context(DEFAULT_FILLS_PATH).get(fid) or {}
                enriched.update({k: v for k, v in ctx.items() if v is not None})
        text = fmt_realtime_fill(enriched, account_snapshot)
        side = (fill.get("side") or "").upper()
        symbol = fill.get("symbol", "?")
        emoji = "🟢" if side == "BUY" else "🔴"
        title = f"{emoji} advisor 成交 {symbol} {side}"
        return send_dm(title, text)
    except Exception:
        logger.exception("[sim._notify] notify_fill 失敗")
        return False
