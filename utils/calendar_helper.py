#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calendar_helper.py — 台股交易日判斷

讀取 config/trading_calendar.yaml 的休市日清單，配合週末判斷。
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_CALENDAR_PATH = _BASE_DIR / "config" / "trading_calendar.yaml"

_holidays_cache: Optional[Set[date]] = None


def _load_holidays() -> Set[date]:
    """載入並快取台股休市日清單。"""
    global _holidays_cache
    if _holidays_cache is not None:
        return _holidays_cache

    holidays: Set[date] = set()
    try:
        import yaml
        if _CALENDAR_PATH.exists():
            data = yaml.safe_load(_CALENDAR_PATH.read_text(encoding="utf-8")) or {}
            for d in data.get("tw_holidays", []):
                if isinstance(d, str):
                    holidays.add(datetime.strptime(d, "%Y-%m-%d").date())
                elif isinstance(d, date):
                    holidays.add(d)
            logger.debug("載入 %d 個台股休市日", len(holidays))
        else:
            logger.warning("台股休市日清單不存在: %s，僅排除週末", _CALENDAR_PATH)
    except Exception as e:
        logger.error("載入台股休市日清單失敗: %s", e)

    _holidays_cache = holidays
    return holidays


def is_tw_trading_day(dt: Optional[datetime] = None) -> bool:
    """
    判斷指定日期是否為台股交易日。
    排除：週末（週六、週日）+ 已知假日清單。
    """
    target = (dt or datetime.now()).date() if isinstance(dt, datetime) else (dt or datetime.now().date())

    # 週末
    if target.weekday() in (5, 6):
        return False

    # 假日清單
    holidays = _load_holidays()
    if target in holidays:
        return False

    return True


def clear_cache() -> None:
    """清除快取（方便測試或 yaml 更新後重載）。"""
    global _holidays_cache
    _holidays_cache = None
