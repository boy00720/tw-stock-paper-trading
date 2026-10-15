"""美股基本面共用正規化工具。"""

from __future__ import annotations

import math
from typing import Any, Optional


def positive_number(value: Any, *, allow_zero: bool = False) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        return None
    return number


def dividend_yield_ratio(info: dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
    """回傳小數比率，優先使用年度股息除以現價。"""
    rate = None
    rate_key = None
    for key in ("dividendRate", "trailingAnnualDividendRate"):
        rate = positive_number(info.get(key), allow_zero=True)
        if rate is not None:
            rate_key = key
            break

    price = None
    price_key = None
    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        price = positive_number(info.get(key))
        if price is not None:
            price_key = key
            break

    if rate is not None and price is not None:
        ratio = rate / price
        if ratio <= 1:
            return ratio, f"{rate_key}/{price_key}"

    # 單獨的 dividendYield 在不同 provider／快取版本可能是百分點或小數比率，
    # 無法可靠判別時寧可標為缺資料，不以數值門檻猜單位。
    return None, None
