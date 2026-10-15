#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_core_watchlist.py — 核心清單策略體檢

對 config/stocks.yaml 的核心監控股票，跑 SEPA / 價值 / 短線雙引擎三套紀律檢查，
提供「保留 / 觀察 / 建議剔除」建議。支援 W 做清單精簡。

判定邏輯：
    - ⭐⭐⭐ 多池通過（3 池中 ≥ 2 池亮燈）→ 強烈保留，可能是跨池訊號
    - ⭐⭐   任一池通過 → 保留
    - ⭐     SEPA 接近通過（7/8）或 Value 4/5 → 觀察，暫不剔除
    - ❌    所有池都 ≤ 4/8 或 ≤ 3/5 → **建議剔除**（提供理由）

用法：
    python3 scripts/validate_core_watchlist.py
    python3 scripts/validate_core_watchlist.py --output logs/core_health_YYYY-MM-DD.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import yaml

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

from utils.finmind_helper import FinMindHelper

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("validate_core")


def format_name_with_code(name: str, symbol: str) -> str:
    suffix = f"（{symbol.replace('.TW', '').replace('.TWO', '').lstrip('^')}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


@dataclass
class CoreValidation:
    symbol: str
    name: str
    category: str
    sepa_passed: int = 0        # SEPA 通過項數 (0-8)
    sepa_total: int = 0
    value_passed: int = 0       # Value 通過項數 (0-5)
    value_total: int = 0
    in_momentum: bool = False   # 是否在短線 active_picks
    in_rebound: bool = False
    recommendation: str = ""     # 保留 / 觀察 / 剔除
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


def load_stocks_yaml() -> List[Dict]:
    path = _BASE_DIR / "config" / "stocks.yaml"
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("watch_list", [])


def load_active_picks() -> Dict[str, str]:
    """取短線選股 active picks，返回 {symbol: engine}"""
    path = _BASE_DIR / "logs" / "screener_tracker" / "active_picks.json"
    result = {}
    if not path.exists():
        return result
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for p in data.get("picks", []):
        if p.get("outcome") == "pending":
            result[p["symbol"]] = p.get("engine", "momentum")
    return result


def check_single_stock(symbol: str, name: str, category: str, fm: FinMindHelper,
                       active_picks: Dict[str, str]) -> CoreValidation:
    """對單檔股票跑三套紀律檢查"""
    v = CoreValidation(symbol=symbol, name=name, category=category)

    # 呼叫既有 SEPA 與 Value 檢查邏輯
    try:
        from scripts.sepa_screener import check_sepa, fetch_price_history, fetch_fundamentals, fetch_historical_earnings, fetch_benchmark_return
        benchmark = fetch_benchmark_return(20)
        price_df = fetch_price_history(symbol, days=200)
        fundamentals = fetch_fundamentals(symbol, fm=fm)
        earnings_df = fetch_historical_earnings(symbol)
        sepa_result = check_sepa(symbol, name, category, price_df, fundamentals, earnings_df, benchmark)
        v.sepa_passed = sum(1 for x in sepa_result.checks.values() if x is True)
        v.sepa_total = sum(1 for x in sepa_result.checks.values() if x is not None)
        v.details["sepa_missing"] = sepa_result.missing
        v.details["sepa_failed_items"] = [k for k, val in sepa_result.checks.items() if val is False]
    except Exception as e:
        logger.debug("SEPA 檢查 %s 失敗: %s", symbol, e)

    try:
        from scripts.value_screener import check_value, fetch_value_data, fetch_dividend_history, fetch_income_statement
        data = fetch_value_data(symbol, fm=fm)
        div_years = fetch_dividend_history(symbol, years=6, fm=fm)
        income_stmt = fetch_income_statement(symbol)
        value_result = check_value(symbol, name, category, data, div_years, income_stmt)
        v.value_passed = sum(1 for x in value_result.checks.values() if x is True)
        v.value_total = sum(1 for x in value_result.checks.values() if x is not None)
        v.details["value_missing"] = value_result.missing
        v.details["value_failed_items"] = [k for k, val in value_result.checks.items() if val is False]
    except Exception as e:
        logger.debug("Value 檢查 %s 失敗: %s", symbol, e)

    # 短線（從 active_picks）
    if symbol in active_picks:
        engine = active_picks[symbol]
        if engine == "momentum":
            v.in_momentum = True
        elif engine == "rebound":
            v.in_rebound = True

    # 綜合判定
    sepa_pass = v.sepa_total > 0 and v.sepa_passed == v.sepa_total
    value_pass = v.value_total > 0 and v.value_passed == v.value_total
    short_pass = v.in_momentum or v.in_rebound

    pool_count = sum([sepa_pass, value_pass, short_pass])

    if pool_count >= 2:
        v.recommendation = "⭐⭐⭐ 強烈保留"
        v.reason = f"{pool_count} 池同時亮燈（跨池訊號）"
    elif pool_count == 1:
        which = []
        if sepa_pass: which.append("SEPA")
        if value_pass: which.append("Value")
        if short_pass: which.append("短線")
        v.recommendation = "⭐⭐ 保留"
        v.reason = f"通過 {'/'.join(which)} 池"
    elif (v.sepa_total >= 6 and v.sepa_passed >= 6) or (v.value_total >= 4 and v.value_passed >= 4):
        v.recommendation = "⭐ 觀察"
        v.reason = f"接近通過（SEPA {v.sepa_passed}/{v.sepa_total}、Value {v.value_passed}/{v.value_total}）"
    else:
        v.recommendation = "❌ 建議剔除"
        reasons = []
        if v.sepa_total > 0:
            reasons.append(f"SEPA {v.sepa_passed}/{v.sepa_total}")
        if v.value_total > 0:
            reasons.append(f"Value {v.value_passed}/{v.value_total}")
        if not short_pass:
            reasons.append("無短線雙引擎訊號")
        v.reason = "三池皆未達標（" + "；".join(reasons) + "）"

    return v


def generate_report(results: List[CoreValidation]) -> str:
    today = date.today().isoformat()
    total = len(results)
    keep_strong = [r for r in results if r.recommendation.startswith("⭐⭐⭐")]
    keep = [r for r in results if r.recommendation.startswith("⭐⭐ ")]
    watch = [r for r in results if r.recommendation.startswith("⭐ ")]
    remove = [r for r in results if r.recommendation.startswith("❌")]

    lines = [
        f"# 🔬 核心清單策略體檢 ({today})",
        "",
        f"**檢查範圍**: `stocks.yaml` {total} 檔核心監控股",
        f"**建議保留（強）**: {len(keep_strong)} 檔",
        f"**建議保留**: {len(keep)} 檔",
        f"**建議觀察**: {len(watch)} 檔",
        f"**建議剔除**: {len(remove)} 檔",
        "",
    ]

    if keep_strong:
        lines.extend(["## ⭐⭐⭐ 強烈保留（跨池訊號）", ""])
        for r in keep_strong:
            lines.append(f"### {format_name_with_code(r.name, r.symbol)} [{r.category}]")
            lines.append(f"- SEPA: {r.sepa_passed}/{r.sepa_total}、Value: {r.value_passed}/{r.value_total}、短線: {'量價突破' if r.in_momentum else '均值回歸' if r.in_rebound else '-'}")
            lines.append(f"- 結論：{r.reason}")
            lines.append("")

    if keep:
        lines.extend(["## ⭐⭐ 保留", ""])
        for r in keep:
            lines.append(f"- **{format_name_with_code(r.name, r.symbol)}** [{r.category}] — {r.reason}")
            lines.append(f"  SEPA {r.sepa_passed}/{r.sepa_total}、Value {r.value_passed}/{r.value_total}")
        lines.append("")

    if watch:
        lines.extend(["## ⭐ 觀察（接近通過）", ""])
        for r in watch:
            lines.append(f"- **{format_name_with_code(r.name, r.symbol)}** — {r.reason}")
            if r.details.get("sepa_failed_items"):
                lines.append(f"  SEPA 未達: {', '.join(r.details['sepa_failed_items'][:3])}")
            if r.details.get("value_failed_items"):
                lines.append(f"  Value 未達: {', '.join(r.details['value_failed_items'][:2])}")
        lines.append("")

    if remove:
        lines.extend(["## ❌ 建議剔除", ""])
        for r in remove:
            lines.append(f"### {format_name_with_code(r.name, r.symbol)} [{r.category}]")
            lines.append(f"- **結論**: {r.reason}")
            if r.details.get("sepa_failed_items"):
                lines.append(f"- SEPA 未達項: {', '.join(r.details['sepa_failed_items'][:5])}")
            if r.details.get("value_failed_items"):
                lines.append(f"- Value 未達項: {', '.join(r.details['value_failed_items'][:3])}")
            lines.append("")

    lines.extend([
        "---",
        "",
        "## 📌 紀律提醒",
        "- 「建議剔除」不代表股票不好，只是當前**三池紀律皆未達**。",
        "- 長期核心持有依然合理，但若以**數據驅動紀律**為準則，該檔暫無進場訊號。",
        "- 剔除後 stocks.yaml 減少 → 股市W 晨報更專注在「有訊號的股」。",
        "- 個人判斷仍然優先，本工具僅提供**紀律視角的第二意見**。",
        "",
        "## 🔧 如何執行剔除建議",
        "1. 編輯 `config/stocks.yaml`",
        "2. 移除「❌ 建議剔除」的 symbol 區塊",
        "3. 下一次 `stock_monitor.py` 跑時自動生效",
        "",
        '> "Discipline is the bridge between watchlist and results." — 改寫自 Jim Rohn',
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="核心清單策略體檢")
    parser.add_argument("--output", help="輸出報告路徑（預設 logs/core_health_YYYY-MM-DD.md）")
    parser.add_argument("--push", action="store_true", help="推送飛書+微信")
    parser.add_argument("--dry-run", action="store_true", help="不推送")
    args = parser.parse_args()

    today = date.today().isoformat()
    logger.info("=== 核心清單策略體檢 %s ===", today)

    fm = FinMindHelper()
    logger.info("✓ FinMind 已啟用")

    stocks = load_stocks_yaml()
    active_picks = load_active_picks()
    logger.info("核心清單: %d 檔，短線 active: %d 檔", len(stocks), len(active_picks))

    results: List[CoreValidation] = []
    for i, stock in enumerate(stocks, 1):
        logger.info("[%d/%d] 檢查 %s (%s)", i, len(stocks), stock["symbol"], stock["name"])
        result = check_single_stock(
            stock["symbol"],
            stock["name"],
            stock.get("category", "未分類"),
            fm,
            active_picks,
        )
        results.append(result)

    report = generate_report(results)

    output = Path(args.output) if args.output else (_BASE_DIR / "logs" / f"core_health_{today}.md")
    output.write_text(report, encoding="utf-8")
    logger.info("✓ 報告已輸出: %s", output)

    if not args.dry_run and args.push:
        from utils.lark_notifier import LarkNotifier
        from utils.wechat_notifier import WechatNotifier

        notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        notifier.send_message(
            "oc_your_feishu_chat_id",
            f"🔬 核心清單策略體檢 · {today}",
            report[:4000],
        )
        WechatNotifier().send_message(report[:1500], typing_seconds=4, allow_unready_queue=True)
        logger.info("✓ 已推送飛書與微信")

    print(report)


def push_notifications(report: str) -> None:
    """飛書 + 微信雙推"""
    from utils.lark_notifier import LarkNotifier
    try:
        from utils.wechat_notifier import WechatNotifier
        has_wechat = True
    except ImportError:
        has_wechat = False

    try:
        lark = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        chat_id = os.getenv("FEISHU_DEFAULT_CHAT_ID")
        title = "🔬 核心清單策略體檢"
        if chat_id:
            lark.send_message(chat_id, title, report, receive_id_type="chat_id")
            logger.info("✓ 飛書已推送（群組 chat_id）")
        else:
            logger.warning("飛書群組目標未設定（FEISHU_DEFAULT_CHAT_ID）")
    except Exception as e:
        logger.error("飛書推送失敗: %s", e)

    if has_wechat:
        try:
            wechat = WechatNotifier()
            target = os.getenv("WECHAT_PUSH_TO_USER")
            ok = wechat.send_message(report, to_user=target, typing_seconds=3)
            if ok:
                logger.info("✓ 微信已推送")
        except Exception as e:
            logger.error("微信推送失敗: %s", e)


def cmd_validate(push: bool = False) -> None:
    """供 orchestrator 呼叫的入口"""
    today = date.today().isoformat()
    fm = FinMindHelper()
    stocks = load_stocks_yaml()
    active_picks = load_active_picks()

    results: List[CoreValidation] = []
    for stock in stocks:
        result = check_single_stock(
            stock["symbol"],
            stock["name"],
            stock.get("category", "未分類"),
            fm,
            active_picks,
        )
        results.append(result)

    report = generate_report(results)
    out_path = _BASE_DIR / "logs" / f"core_health_{today}.md"
    out_path.write_text(report, encoding="utf-8")
    logger.info("✓ 體檢報告存檔: %s", out_path)
    if push:
        push_notifications(report)


if __name__ == "__main__":
    main()
