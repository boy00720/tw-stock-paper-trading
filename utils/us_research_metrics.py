"""美股 Research Pack 與五面分析共用的純資料正規化。"""

from __future__ import annotations

import math
from typing import Any, Optional

from utils.us_fundamentals import dividend_yield_ratio


def number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def period_return(closes: list[float], periods: int) -> Optional[float]:
    if len(closes) <= periods or not closes[-(periods + 1)]:
        return None
    return (closes[-1] / closes[-(periods + 1)] - 1) * 100.0


def technical_metrics(rows: list[dict[str, Any]], spy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid_rows = [(row, number(row.get("close"))) for row in rows]
    valid_rows = [(row, close) for row, close in valid_rows if close is not None]
    valid_spy_rows = [(row, number(row.get("close"))) for row in spy_rows]
    valid_spy_rows = [(row, close) for row, close in valid_spy_rows if close is not None]
    closes = [close for _, close in valid_rows]
    spy_closes = [close for _, close in valid_spy_rows]
    last_bar = valid_rows[-1][0].get("date") if valid_rows else None
    metrics: dict[str, Any] = {
        "available": len(closes) >= 50,
        "last_bar": str(last_bar) if last_bar is not None else "",
        "last_close": closes[-1] if closes else None,
        "ma20": None,
        "ma50": None,
        "ma_stacked": None,
        "ret20_pct": None,
        "spy_ret20_pct": period_return(spy_closes, 20),
        "rs_vs_spy_pct": None,
        "volume_ratio_20d": None,
        "warnings": [],
    }
    if len(closes) < 50:
        metrics["warnings"].append("technical indicators unavailable: fewer than 50 daily bars")
        return metrics

    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    ret20 = period_return(closes, 20)
    spy_ret20 = metrics["spy_ret20_pct"]
    aligned_volumes = [number(row.get("volume")) for row, _ in valid_rows[-20:]]
    vol_ratio = None
    if len(aligned_volumes) == 20 and all(value is not None for value in aligned_volumes):
        total_volume = sum(aligned_volumes)
        if total_volume:
            vol_ratio = aligned_volumes[-1] / (total_volume / 20)
    elif any(value is None for value in aligned_volumes):
        metrics["warnings"].append("volume ratio unavailable: latest 20 daily bars are not aligned")

    metrics.update({
        "ma20": ma20,
        "ma50": ma50,
        "ma_stacked": closes[-1] > ma20 > ma50,
        "ret20_pct": ret20,
        "rs_vs_spy_pct": ret20 - spy_ret20 if ret20 is not None and spy_ret20 is not None else None,
        "volume_ratio_20d": vol_ratio,
    })
    return metrics


def info_sections(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    div, div_source = dividend_yield_ratio(info)
    valuation = {
        "trailing_pe": number(info.get("trailingPE")),
        "forward_pe": number(info.get("forwardPE")),
        "price_to_book": number(info.get("priceToBook")),
        "peg_ratio": number(info.get("pegRatio")),
        "dividend_yield_ratio": div,
        "dividend_yield_source": div_source,
    }
    fundamental = {
        "market_cap": number(info.get("marketCap")),
        "enterprise_value": number(info.get("enterpriseValue")),
        "total_revenue": number(info.get("totalRevenue")),
        "revenue_growth": number(info.get("revenueGrowth")),
        "earnings_growth": number(info.get("earningsGrowth")),
        "profit_margins": number(info.get("profitMargins")),
        "return_on_equity": number(info.get("returnOnEquity")),
        "total_cash": number(info.get("totalCash")),
        "total_debt": number(info.get("totalDebt")),
        "free_cashflow": number(info.get("freeCashflow")),
    }
    capital = {
        "held_percent_institutions": number(info.get("heldPercentInstitutions")),
        "held_percent_insiders": number(info.get("heldPercentInsiders")),
        "shares_outstanding": number(info.get("sharesOutstanding")),
        "average_volume": number(info.get("averageVolume")),
    }
    return {
        "valuation": {
            "available": any(valuation[key] is not None for key in (
                "forward_pe", "price_to_book", "peg_ratio", "dividend_yield_ratio",
            )),
            "metrics": valuation,
        },
        "fundamental": {
            "available": any(fundamental[key] is not None for key in (
                "revenue_growth", "earnings_growth", "profit_margins", "return_on_equity",
            )),
            "metrics": fundamental,
        },
        "capital": {
            "available": capital["held_percent_institutions"] is not None,
            "metrics": capital,
        },
    }
