"""automation/sim/calendar.py — 台股交易日判定（共用模組）

提供：
- is_trading_day(d=None, *, calendar_path=None) -> bool
  日期級檢查：非週末 + 不在 tw_holidays 清單 = True
- should_run_now(now=None, *, calendar_path=None) -> bool
  時段級檢查：is_trading_day + 盤中 09:00–13:30 = True

所有排程入口（morning_check / daily-report / metrics / brief / eod-cleanup /
weekly-report）共用 is_trading_day；advisor decide / dispatch 用 should_run_now。

假日清單來源：config/trading_calendar.yaml（tw_holidays 欄位，YYYY-MM-DD 字串）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TRADING_CALENDAR = _BASE_DIR / "config" / "trading_calendar.yaml"

# 台股盤中時段
_TRADING_OPEN = dtime(9, 0)
_TRADING_CLOSE = dtime(13, 30)


def _load_calendar(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return set(str(d) for d in (data.get("tw_holidays") or []))
    except Exception:
        logger.exception("[sim.calendar] 讀取 trading calendar 失敗: %s", path)
        return set()


def is_trading_day(
    d: Optional[date] = None,
    *,
    calendar_path: Optional[Path] = None,
) -> bool:
    """日期級判定：非週末 + 不在台股假日清單 = True。

    d 可以是 date 或 datetime；None 時用 today()。
    """
    if d is None:
        d = date.today()
    elif isinstance(d, datetime):
        d = d.date()
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    cal = _load_calendar(calendar_path or DEFAULT_TRADING_CALENDAR)
    return d.isoformat() not in cal


def should_run_now(
    now: Optional[datetime] = None,
    *,
    calendar_path: Optional[Path] = None,
) -> bool:
    """盤中 + 非假日 + 非週末才 True。

    等價於 is_trading_day(now) AND 09:00 <= now.time() <= 13:30。
    """
    n = now or datetime.now()
    if not is_trading_day(n, calendar_path=calendar_path):
        return False
    return _TRADING_OPEN <= n.time() <= _TRADING_CLOSE
