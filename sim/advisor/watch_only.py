"""automation/sim/advisor/watch_only.py — watch-only 持倉半自動提醒規則。

watch-only 是「目前刻意缺 stop，但需要持續觀察」的持倉狀態。本模組只負責：
- 讀 config/sim_watch_only.yaml
- 對單檔 positions + quote 評估 action_rule
- 回傳是否需要 ACTION NEEDED

不直接掛單、不發通知，避免觀察規則變成自動交易。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_WATCH_ONLY_PATH = _BASE_DIR / "config" / "sim_watch_only.yaml"


def load_watch_only(path: Path | str = DEFAULT_WATCH_ONLY_PATH) -> dict[str, dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        watch_only = data.get("watch_only") or {}
        return watch_only if isinstance(watch_only, dict) else {}
    except Exception:
        logger.exception("[watch_only] 讀取失敗: %s", p)
        return {}



def _float_rule(rule: dict[str, Any], key: str, default: float) -> float:
    try:
        value = rule.get(key, default)
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _evaluate_leveraged_etf_watch(
    symbol: str,
    *,
    avg_cost: float,
    quote: float,
    rule: dict[str, Any],
    position_state: str = "holding",
    exit_price: float | None = None,
) -> dict[str, Any]:
    """槓反 ETF 實盤人工觀察：狀態燈 + 建議，不做自動交易。

    position_state=flat（已平倉空手）時不報跌破成本，改回報本輪結算 + 空手狀態；
    再進場機會的盤中精細訊號交由 scripts/00631l_live_risk_monitor.py 負責，此處不重算。
    """
    if position_state == "flat":
        settle_pct = (exit_price - avg_cost) / avg_cost if (exit_price and exit_price > 0 and avg_cost > 0) else None
        dev_pct = (quote - exit_price) / exit_price if (exit_price and exit_price > 0 and quote and quote > 0) else None
        if settle_pct is not None:
            settle_txt = f"本輪實盤結算 {avg_cost:.2f}→{exit_price:.2f} = {settle_pct * 100:+.2f}%"
        else:
            settle_txt = "已平倉空手（exit_price 未填，暫無法結算）"
        dev_txt = f"，現價較賣出價 {dev_pct * 100:+.2f}%" if dev_pct is not None else ""
        suggestion = f"{settle_txt}；目前空手，再進場機會由盤中雷達觀察{dev_txt}。"
        return {
            "action_needed": False,
            "message": suggestion,
            "suggestion": suggestion,
            "rule_type": "leveraged_etf_watch",
            "quote": quote,
            "computed_stop": None,
            "return_pct": settle_pct if settle_pct is not None else 0.0,
            "return_pct_display": settle_pct * 100 if settle_pct is not None else None,
            "status_level": "flat",
            "manual_only": True,
            "position_state": "flat",
            "exit_price": exit_price,
            "settle_pct": settle_pct,
        }

    ret_pct = (quote - avg_cost) / avg_cost if avg_cost > 0 else 0.0
    yellow_loss = _float_rule(rule, "yellow_loss_pct", 0.01)
    red_loss = _float_rule(rule, "red_loss_pct", 0.02)
    strong_red_loss = _float_rule(rule, "strong_red_loss_pct", 0.03)
    profit_protect = _float_rule(rule, "profit_protect_pct", 0.03)
    profit_trail = _float_rule(rule, "profit_trail_pct", 0.05)

    status_level = "green"
    suggestion = "續抱觀察；維持人工風控，不接自動下單。"
    action_needed = False
    if ret_pct <= -strong_red_loss:
        status_level = "strong_red"
        suggestion = f"{symbol} 跌破實盤成本約 {strong_red_loss:.0%}，建議優先人工處理（減碼/補風控）。"
        action_needed = True
    elif ret_pct <= -red_loss:
        status_level = "red"
        suggestion = f"{symbol} 跌破實盤成本約 {red_loss:.0%}，建議人工檢查是否減碼或補風控。"
        action_needed = True
    elif ret_pct <= -yellow_loss:
        status_level = "yellow"
        suggestion = f"{symbol} 接近/跌破實盤成本防線約 {yellow_loss:.0%}，提高觀察頻率。"
        action_needed = True
    elif ret_pct >= profit_trail:
        status_level = "green"
        suggestion = f"{symbol} 實盤浮盈超過 {profit_trail:.0%}，建議人工檢查是否上移保護線/鎖利。"
        action_needed = True
    elif ret_pct >= profit_protect:
        status_level = "green"
        suggestion = f"{symbol} 實盤浮盈超過 {profit_protect:.0%}，建議人工拉保本線。"
        action_needed = True

    return {
        "action_needed": action_needed,
        "message": rule.get("message") if action_needed and rule.get("message") else suggestion,
        "suggestion": suggestion,
        "rule_type": "leveraged_etf_watch",
        "quote": quote,
        "computed_stop": None,
        "return_pct": ret_pct,
        "return_pct_display": ret_pct * 100,
        "status_level": status_level,
        "manual_only": True,
    }


def evaluate_watch_only_action(
    symbol: str,
    *,
    avg_cost: float,
    quote: Optional[float],
    config: dict[str, Any],
) -> dict[str, Any]:
    """評估單檔 watch-only 是否命中轉補提醒。

    支援 action_rule.type：
    - below_avg_cost_pct：quote <= avg_cost * (1 - pct)
    - stop_distance_from_quote_pct：用 avg_cost * (1 - stop_pct_from_avg_cost) 推估 stop，
      若 (quote - stop) / quote >= min_distance_pct，代表 stop 已不再太貼近現價。
    - leveraged_etf_watch：槓反 ETF 實盤觀察卡；以成本、浮盈保護、跌破成本分級產生人工建議。
      position_state=flat（已平倉空手）時改回報本輪結算 + 空手狀態，不報跌破成本。
    """
    rule = config.get("action_rule") or {}
    rule_type = rule.get("type")
    result = {
        "action_needed": False,
        "message": None,
        "rule_type": rule_type,
        "quote": quote,
        "computed_stop": None,
    }

    position_state = str(config.get("position_state") or "holding").strip().lower()
    exit_price_raw = config.get("exit_price")
    try:
        exit_price = float(exit_price_raw) if exit_price_raw is not None else None
    except (TypeError, ValueError):
        exit_price = None

    # flat 槓反 ETF 結算只需 entry + exit，不依賴即時 quote，先於 quote guard 處理。
    if rule_type == "leveraged_etf_watch" and position_state == "flat":
        return _evaluate_leveraged_etf_watch(
            symbol, avg_cost=avg_cost, quote=quote or 0.0, rule=rule,
            position_state="flat", exit_price=exit_price,
        )

    if quote is None or quote <= 0 or avg_cost <= 0:
        result["message"] = "缺少有效 quote 或 avg_cost，暫無法評估轉補條件。"
        return result

    if rule_type == "leveraged_etf_watch":
        return _evaluate_leveraged_etf_watch(symbol, avg_cost=avg_cost, quote=quote, rule=rule)

    if rule_type == "below_avg_cost_pct":
        pct = float(rule.get("pct") or 0)
        threshold = avg_cost * (1 - pct)
        result["threshold"] = threshold
        if quote <= threshold:
            result["action_needed"] = True
            result["message"] = rule.get("message") or f"{symbol} 已跌破 avg_cost {pct:.1%}。"
        return result

    if rule_type == "stop_distance_from_quote_pct":
        stop_pct = float(rule.get("stop_pct_from_avg_cost") or 0)
        min_distance = float(rule.get("min_distance_pct") or 0)
        computed_stop = avg_cost * (1 - stop_pct)
        distance = (quote - computed_stop) / quote
        result["computed_stop"] = computed_stop
        result["distance_pct"] = distance
        if distance >= min_distance:
            result["action_needed"] = True
            result["message"] = rule.get("message") or f"{symbol} 推估 stop 與現價距離已達 {min_distance:.1%}。"
        return result

    result["message"] = "未設定可機器判斷的 action_rule。"
    return result
