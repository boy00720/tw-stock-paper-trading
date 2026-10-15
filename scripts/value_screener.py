#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
value_screener.py — 價值選股W (長線價值池篩選器) MVP

責任:
    1. 每月首週五掃描候選池（MVP 用 stock_universe.yaml，後擴全市場）
    2. 對每檔執行價值五項紀律檢查
    3. 清單治理: 連續 2 個月通過才進池，進池保鮮期 90 天（1 季度）
    4. 產生報告並推送飛書 + 微信

價值五項 (詳見 /Users/w/Cursor/股市選股紀律.md):
    ① 估值吸引力: P/E 低於產業均值 20% 或歷史均值 1 SD 以下
    ② 資本效率: 近四季 ROE > 10%
    ③ 現金回饋: 近五年年年發放股息，股息率 > 3%
    ④ 財務健康: 近四季無虧損，營業利益率為正
    ⑤ 排除過熱: 非景氣循環股在高點區間

用法:
    python3 scripts/value_screener.py                    # 每月掃描+推送
    python3 scripts/value_screener.py --dry-run          # 不推播
    python3 scripts/value_screener.py --symbol 2412.TW   # 單檔檢查
    python3 scripts/value_screener.py --status           # 觀察池現況
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, List, Optional

for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import numpy as np
import pandas as pd
import yaml
import yfinance as yf

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

from utils.lark_notifier import LarkNotifier
from utils.us_fundamentals import dividend_yield_ratio

try:
    from utils.wechat_notifier import WechatNotifier
    HAS_WECHAT = True
except ImportError:
    HAS_WECHAT = False

try:
    from utils.finmind_helper import FinMindHelper
    HAS_FINMIND = True
except ImportError:
    HAS_FINMIND = False


# ─────────────────────────────────────────────────────
# 常量 (與紀律檔一致)
# ─────────────────────────────────────────────────────

LOG_DIR = _BASE_DIR / "logs" / "value_screener"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRACKER_DIR = _BASE_DIR / "logs" / "value_tracker"
TRACKER_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = TRACKER_DIR / "watchlist_state.json"

# 清單治理
CONFIRM_MONTHS = 2          # 連續 2 個月通過才進池
FRESHNESS_DAYS = 90         # 進池保鮮期 90 天 (1 季度)
MIN_POOL_SIZE = 5
MAX_POOL_SIZE = 10

# 價值五項閾值
THRESHOLDS = {
    "roe": 0.10,            # ② ROE > 10%
    "dividend_yield": 0.03, # ③ 股息率 > 3%
    "dividend_years": 5,    # ③ 近五年年年發放
    "pe_max_absolute": 15,  # ① MVP: P/E < 15 (簡化版，產業均值對比暫 TODO)
    "operating_margin_min": 0.0,  # ④ 營業利益率為正
}

# 景氣循環股清單 (⑤ 排除)
CYCLICAL_STOCKS = {
    "2610.TW",  # 長榮海運
    "2615.TW",  # 萬海
    "2603.TW",  # 長榮 (航運)
    "2002.TW",  # 中鋼
    "1722.TW",  # 台肥
    "2301.TW",  # 光寶科 (景氣循環)
    # TODO: 完整清單應從產業分類動態判定
}


# ─────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("value_screener")


# ─────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────

@dataclass
class ValueResult:
    """單檔股價值五項檢查結果"""
    symbol: str
    name: str
    sector: str
    passed: bool = False
    checks: Dict[str, Optional[bool]] = field(default_factory=dict)
    values: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ─────────────────────────────────────────────────────
# 候選池
# ─────────────────────────────────────────────────────

def load_universe(source: str = "yaml", limit: Optional[int] = None, fm: Optional["FinMindHelper"] = None) -> List[Dict[str, str]]:
    """載入候選池；source: 'yaml' (預設 100 檔) | 'full' (yaml 100 檔 + FinMind 補充)"""
    if source == "yaml":
        path = _BASE_DIR / "config" / "stock_universe.yaml"
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        universe = cfg.get("universe", [])
        logger.info("候選池 [yaml]: %d 檔", len(universe))
        return universe

    if source == "full":
        if fm is None:
            logger.warning("source=full 但 FinMind 未啟用，fallback to yaml")
            return load_universe(source="yaml")

        # yaml 精選清單優先
        path = _BASE_DIR / "config" / "stock_universe.yaml"
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        yaml_universe = cfg.get("universe", [])
        yaml_symbols = {item["symbol"] for item in yaml_universe}

        # FinMind 全市場 - yaml 已有
        df = fm.get_universe()
        df = fm.filter_common_stocks(df)
        supplement = []
        for _, row in df.iterrows():
            sym = f"{row['stock_id']}.TW" if row["type"] == "twse" else f"{row['stock_id']}.TWO"
            if sym in yaml_symbols:
                continue
            supplement.append({
                "symbol": sym,
                "name": row["stock_name"],
                "sector": row["industry_category"],
            })

        universe = yaml_universe + supplement
        if limit:
            universe = universe[:limit]
        logger.info(
            "候選池 [full]: %d 檔（yaml %d + FinMind 補充 %d）",
            len(universe), len(yaml_universe), len(universe) - len(yaml_universe),
        )
        return universe

    raise ValueError(f"Unknown source: {source}")


# ─────────────────────────────────────────────────────
# yfinance 資料抓取
# ─────────────────────────────────────────────────────

def fetch_value_data(symbol: str, fm: Optional["FinMindHelper"] = None) -> Dict[str, Any]:
    """抓取價值五項所需資料，FinMind 優先、yfinance fallback"""
    result = {
        "trailing_pe": None,
        "forward_pe": None,
        "roe": None,
        "dividend_yield": None,
        "operating_margin": None,
        "profit_margin": None,
        "book_value": None,
        "price_to_book": None,
        "fifty_two_week_high": None,
        "current_price": None,
        "name": None,
        "industry": None,
        "industry_pe_median": None,
    }

    # 1. FinMind 優先（台股資料完整）
    if fm is not None:
        try:
            info = fm.get_stock_info(symbol)
            result["name"] = info.get("name")
            result["industry"] = info.get("industry")

            # P/E, P/B 從 FinMind 取
            per_df = fm.get_per_pbr(symbol)
            if per_df is not None and not per_df.empty:
                latest = per_df.iloc[-1]
                if "PER" in latest and latest["PER"] and latest["PER"] > 0:
                    result["trailing_pe"] = float(latest["PER"])
                if "PBR" in latest and latest["PBR"]:
                    result["price_to_book"] = float(latest["PBR"])
                if "dividend_yield" in latest and latest["dividend_yield"] is not None:
                    result["dividend_yield"] = float(latest["dividend_yield"]) / 100

            # 產業均值 P/E（用於 ① 對比）
            if result["industry"]:
                ind_pe = fm.get_industry_pe_mean(result["industry"])
                if ind_pe:
                    result["industry_pe_median"] = ind_pe
        except Exception as e:
            logger.debug("FinMind fetch_value_data(%s) 失敗: %s", symbol, e)

    # 2. yfinance fallback 補缺
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        if result["trailing_pe"] is None:
            result["trailing_pe"] = info.get("trailingPE")
        if result["price_to_book"] is None:
            result["price_to_book"] = info.get("priceToBook")
        if result["dividend_yield"] is None:
            result["dividend_yield"], _ = dividend_yield_ratio(info)
        result["forward_pe"] = info.get("forwardPE")
        result["roe"] = info.get("returnOnEquity")
        result["operating_margin"] = info.get("operatingMargins")
        result["profit_margin"] = info.get("profitMargins")
        result["book_value"] = info.get("bookValue")
        result["fifty_two_week_high"] = info.get("fiftyTwoWeekHigh")
        result["current_price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    except Exception as e:
        logger.debug("yfinance fallback(%s) 失敗: %s", symbol, e)

    return result


def fetch_dividend_history(symbol: str, years: int = 6, fm: Optional["FinMindHelper"] = None) -> List[int]:
    """抓近 N 年每年是否發放股息

    FinMind 優先，yfinance fallback

    Returns: 發放年度清單
    """
    # 1. FinMind
    if fm is not None:
        try:
            div_df = fm.get_dividends(symbol, years=years + 1)
            if div_df is not None and not div_df.empty:
                stats = fm.compute_dividend_stats(div_df, years=years)
                if stats.get("years_paid"):
                    return stats["years_paid"]
        except Exception as e:
            logger.debug("FinMind dividend(%s) 失敗: %s", symbol, e)

    # 2. yfinance fallback
    try:
        ticker = yf.Ticker(symbol)
        dividends = ticker.dividends
        if dividends is None or dividends.empty:
            return []
        current_year = datetime.now().year
        years_with_div = set()
        for dt, amount in dividends.items():
            if amount > 0 and (current_year - years) <= dt.year <= current_year:
                years_with_div.add(dt.year)
        return sorted(years_with_div, reverse=True)
    except Exception as e:
        logger.debug("yfinance dividend(%s) 失敗: %s", symbol, e)
        return []


def fetch_income_statement(symbol: str) -> Optional[pd.DataFrame]:
    """抓損益表（近四季營業利益）"""
    try:
        ticker = yf.Ticker(symbol)
        stmt = ticker.quarterly_income_stmt
        if stmt is None or stmt.empty:
            return None
        return stmt
    except Exception:
        return None


# ─────────────────────────────────────────────────────
# 價值五項檢查
# ─────────────────────────────────────────────────────

def check_value(
    symbol: str,
    name: str,
    sector: str,
    data: Dict[str, Any],
    dividend_years: List[int],
    income_stmt: Optional[pd.DataFrame],
) -> ValueResult:
    """對單檔股執行價值五項檢查"""
    r = ValueResult(symbol=symbol, name=name, sector=sector)

    # ① 估值吸引力: P/E 低估
    # 優先用「低於產業均值 20%」，無產業均值時 fallback 到絕對 P/E < 15
    pe = data.get("trailing_pe") or data.get("forward_pe")
    ind_pe = data.get("industry_pe_median")
    if pe is not None and pe > 0:
        if ind_pe is not None and ind_pe > 0:
            threshold = ind_pe * 0.8  # 低於產業均值 20%
            r.checks["① P/E 低於產業均值 20%"] = bool(pe < threshold)
            r.values["P/E"] = round(pe, 1)
            r.values["產業均值P/E"] = round(ind_pe, 1)
            r.values["低估門檻"] = round(threshold, 1)
        else:
            # fallback: 絕對 P/E < 15
            r.checks["① P/E<15 (MVP fallback)"] = bool(pe < THRESHOLDS["pe_max_absolute"])
            r.values["P/E"] = round(pe, 1)
            r.notes.append("無產業均值，用 P/E<15 絕對值")
    else:
        r.checks["① P/E 低估"] = None
        r.missing.append("P/E (yfinance 無或負值)")

    pb = data.get("price_to_book")
    if pb is not None:
        r.values["P/B"] = round(pb, 2)

    # ② ROE > 10%
    roe = data.get("roe")
    if roe is not None:
        r.checks["② ROE>10%"] = roe > THRESHOLDS["roe"]
        r.values["ROE"] = f"{roe*100:.1f}%"
    else:
        r.checks["② ROE>10%"] = None
        r.missing.append("ROE")

    # ③ 股息: 年年發放 + 股息率 > 3%
    div_yield = data.get("dividend_yield")
    years_count = len(dividend_years)

    if div_yield is not None and years_count > 0:
        # fetch_value_data 已將 FinMind／yfinance 統一正規化為小數比率。
        yield_decimal = div_yield
        yield_pct = div_yield * 100

        yield_ok = yield_decimal > THRESHOLDS["dividend_yield"]
        years_ok = years_count >= THRESHOLDS["dividend_years"]
        r.checks["③ 股息率>3% + 年年發放"] = yield_ok and years_ok
        r.values["股息率"] = f"{yield_pct:.2f}%"
        r.values["發放年數"] = years_count
        if not years_ok:
            r.notes.append(f"近六年只發放 {years_count} 年，未達 5 年門檻")
    else:
        r.checks["③ 股息率>3% + 年年發放"] = None
        r.missing.append("股息資料")

    # ④ 財務健康: 近四季無虧損 + 營業利益率為正
    op_margin = data.get("operating_margin")
    if op_margin is not None:
        margin_ok = op_margin > THRESHOLDS["operating_margin_min"]
        r.values["營業利益率"] = f"{op_margin*100:.1f}%"

        # 檢查近四季淨利
        quarters_without_loss = None
        if income_stmt is not None:
            try:
                if "Net Income" in income_stmt.index:
                    recent_4q = income_stmt.loc["Net Income"].iloc[:4]
                    quarters_without_loss = all(v > 0 for v in recent_4q.values if not pd.isna(v))
                    r.values["近4季淨利"] = "全正" if quarters_without_loss else "有虧損"
            except Exception:
                pass

        if quarters_without_loss is False:
            r.checks["④ 財務健康"] = False
            r.notes.append("近四季有虧損")
        else:
            r.checks["④ 財務健康"] = margin_ok
    else:
        r.checks["④ 財務健康"] = None
        r.missing.append("營業利益率")

    # ⑤ 排除過熱: 非景氣循環股 + 不在 52 週高點
    is_cyclical = symbol in CYCLICAL_STOCKS
    high = data.get("fifty_two_week_high")
    current = data.get("current_price")
    near_high = False
    if high and current:
        distance_to_high = (high - current) / high
        r.values["距52週高"] = f"{distance_to_high*100:.1f}%"
        near_high = distance_to_high < 0.10  # 距高點不到 10%

    if is_cyclical and near_high:
        r.checks["⑤ 非過熱景氣循環"] = False
        r.notes.append("景氣循環股 + 距 52 週高點 <10%，排除")
    elif is_cyclical:
        r.checks["⑤ 非過熱景氣循環"] = True
        r.notes.append("景氣循環股但未在高點")
    else:
        r.checks["⑤ 非過熱景氣循環"] = True

    # 判定 passed
    true_count = sum(1 for v in r.checks.values() if v is True)
    false_count = sum(1 for v in r.checks.values() if v is False)
    unknown_count = sum(1 for v in r.checks.values() if v is None)
    r.values["通過數"] = true_count
    r.values["未通過數"] = false_count
    r.values["無資料"] = unknown_count
    r.passed = (false_count == 0) and (unknown_count <= 1)  # 容忍 1 項無資料

    return r


# ─────────────────────────────────────────────────────
# 清單治理 State
# ─────────────────────────────────────────────────────

def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"symbols": {}, "updated_at": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_watchlist(
    state: Dict[str, Any],
    results: List[ValueResult],
    today: date,
) -> Dict[str, List[Dict]]:
    """依紀律更新觀察池 (確認期=月，單位不同於 SEPA 的週)"""
    symbols = state.setdefault("symbols", {})
    today_iso = today.isoformat()
    changes = {
        "new_entries": [],
        "in_pool": [],
        "forced_out": [],
        "confirming": [],
    }

    current_passed = {r.symbol: r for r in results if r.passed}

    for sym, r in current_passed.items():
        entry = symbols.setdefault(sym, {
            "name": r.name,
            "sector": r.sector,
            "first_pass_date": today_iso,
            "consecutive_months": 0,
            "in_pool": False,
            "pool_entry_date": None,
            "freshness_until": None,
            "last_check_date": today_iso,
            "last_passed": True,
        })
        entry["consecutive_months"] += 1
        entry["last_check_date"] = today_iso
        entry["last_passed"] = True
        entry["name"] = r.name

        if not entry["in_pool"] and entry["consecutive_months"] >= CONFIRM_MONTHS:
            entry["in_pool"] = True
            entry["pool_entry_date"] = today_iso
            entry["freshness_until"] = (today + timedelta(days=FRESHNESS_DAYS)).isoformat()
            changes["new_entries"].append({
                "symbol": sym, "name": r.name, "sector": r.sector,
                "freshness_until": entry["freshness_until"],
            })
        elif entry["in_pool"]:
            changes["in_pool"].append({
                "symbol": sym, "name": r.name,
                "freshness_remaining": _days_between(today_iso, entry["freshness_until"]),
            })
        else:
            changes["confirming"].append({
                "symbol": sym, "name": r.name,
                "months": entry["consecutive_months"],
                "needed": CONFIRM_MONTHS - entry["consecutive_months"],
            })

    for sym, entry in list(symbols.items()):
        if sym in current_passed:
            continue
        entry["last_check_date"] = today_iso
        entry["last_passed"] = False
        if not entry["in_pool"]:
            if entry.get("consecutive_months", 0) > 0:
                entry["consecutive_months"] = 0
            continue
        if entry.get("freshness_until"):
            freshness_until = date.fromisoformat(entry["freshness_until"])
            if today > freshness_until:
                entry["in_pool"] = False
                changes["forced_out"].append({
                    "symbol": sym, "name": entry["name"],
                    "reason": "保鮮期滿且本月未通過",
                })
            else:
                changes["in_pool"].append({
                    "symbol": sym, "name": entry["name"],
                    "freshness_remaining": _days_between(today_iso, entry["freshness_until"]),
                    "note": "本月未通過但保鮮期內",
                })

    return changes


def _days_between(d1_iso: str, d2_iso: Optional[str]) -> Optional[int]:
    if not d2_iso:
        return None
    return (date.fromisoformat(d2_iso) - date.fromisoformat(d1_iso)).days


# ─────────────────────────────────────────────────────
# 報告產生
# ─────────────────────────────────────────────────────

def generate_report(
    today: date,
    results: List[ValueResult],
    changes: Dict[str, List[Dict]],
) -> str:
    month_str = today.strftime("%Y-%m")
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    errors = sum(1 for r in results if r.error)

    lines = [
        f"# 💎 價值選股W 月報 {month_str} ({today})",
        "",
        f"**掃描範圍**: {total} 檔（MVP 候選池）",
        f"**本月價值五項通過**: {passed} 檔",
        f"**資料錯誤**: {errors} 檔",
        "",
        "## 🟢 本月新進池",
    ]
    if changes["new_entries"]:
        for e in changes["new_entries"]:
            lines.append(
                f"- **{format_name_with_code(e['name'], e['symbol'])}** [{e['sector']}] → 保鮮期至 {e['freshness_until']}"
            )
    else:
        lines.append("_本月無新進池個股_")

    lines.extend(["", "## 📊 觀察池當前"])
    if changes["in_pool"]:
        for e in changes["in_pool"]:
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(
                f"- {format_name_with_code(e['name'], e['symbol'])}｜保鮮期剩 {e.get('freshness_remaining')} 日{note}"
            )
    else:
        lines.append("_觀察池空（寧缺勿濫）_")

    lines.extend(["", "## 🔴 本月強制出池"])
    if changes["forced_out"]:
        for e in changes["forced_out"]:
            lines.append(f"- {format_name_with_code(e['name'], e['symbol'])}｜原因: {e['reason']}")
    else:
        lines.append("_本月無強制出池_")

    lines.extend(["", "## 🟡 確認期中（未滿 2 個月）"])
    if changes["confirming"]:
        for e in changes["confirming"]:
            lines.append(
                f"- {format_name_with_code(e['name'], e['symbol'])}｜已連續 {e['months']} 個月，再 {e['needed']} 月可進池"
            )
    else:
        lines.append("_無個股處於確認期_")

    # 本月通過明細
    passed_list = [r for r in results if r.passed]
    if passed_list:
        lines.extend(["", f"## ✅ 本月價值五項通過明細（共 {len(passed_list)} 檔）"])
        for r in passed_list[:10]:
            lines.append(f"### {format_name_with_code(r.name, r.symbol)} [{r.sector}]")
            for key, val in r.checks.items():
                icon = "✅" if val is True else ("❌" if val is False else "⚪")
                lines.append(f"- {icon} {key}")
            # 關鍵數值
            val_strs = []
            for k in ("P/E", "P/B", "ROE", "股息率", "發放年數", "營業利益率", "距52週高"):
                if k in r.values:
                    val_strs.append(f"{k}={r.values[k]}")
            if val_strs:
                lines.append(f"- 💰 {' | '.join(val_strs)}")
            if r.notes:
                lines.append(f"- 📝 {'; '.join(r.notes[:2])}")
            lines.append("")

    lines.extend([
        "",
        "---",
        "",
        "📌 **紀律提醒**:",
        "- 價值五項全通過 + 連續 2 個月 = 進池",
        "- 進池後保鮮期 90 天（1 季度），期內不因單月雜訊剔除",
        "- 觸發跌破 20% 無理由 / 任一項連兩季未達 → 強制出池",
        "- 分批建倉: 首筆 30%，回檔 10% 再加 30%",
        "- 所有結果請自行判斷是否執行，**後果自負**",
        "",
        '> "Price is what you pay. Value is what you get." — Warren Buffett',
    ])
    return "\n".join(lines)


# ─────────────────────────────────────────────────────
# 推送
# ─────────────────────────────────────────────────────

def format_name_with_code(name: str, symbol: str) -> str:
    suffix = f"（{symbol.replace('.TW', '').replace('.TWO', '').lstrip('^')}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


def push_notifications(report: str, dry_run: bool = False) -> None:
    if dry_run:
        logger.info("[DRY-RUN] 不推送，報告長度 %d 字", len(report))
        return

    # 飛書
    try:
        lark = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        chat_id = os.getenv("FEISHU_DEFAULT_CHAT_ID")
        title = "💎 價值選股 W 月報"
        if chat_id:
            lark.send_message(chat_id, title, report, receive_id_type="chat_id")
            logger.info("✓ 飛書已推送（群組 chat_id）")
        else:
            logger.warning("飛書群組目標未設定（FEISHU_DEFAULT_CHAT_ID）")
    except Exception as e:
        logger.error("飛書推送失敗: %s", e)

    # 微信
    if HAS_WECHAT:
        try:
            wechat = WechatNotifier()
            target = os.getenv("WECHAT_PUSH_TO_USER")  # None 時走 bridge creds 預設
            ok = wechat.send_message(report, to_user=target, typing_seconds=3)
            if ok:
                logger.info("✓ 微信已推送 (target=%s)", target or "bridge 預設")
            else:
                logger.warning("微信推送回傳 False：%s", getattr(wechat, "last_error", ""))
        except Exception as e:
            logger.error("微信推送失敗: %s", e)


# ─────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────

def cmd_scan(dry_run: bool = False, universe_source: str = "yaml", universe_limit: Optional[int] = None) -> None:
    today = date.today()
    month_str = today.strftime("%Y-%m")
    logger.info("=== Value Screener 掃描開始 %s ===", today)

    fm = None
    if HAS_FINMIND:
        try:
            fm = FinMindHelper()
            logger.info("✓ FinMind 已啟用（含 SQLite cache）")
        except Exception as e:
            logger.warning("FinMind 初始化失敗，僅用 yfinance: %s", e)

    universe = load_universe(source=universe_source, limit=universe_limit, fm=fm)
    results: List[ValueResult] = []
    for i, stock in enumerate(universe, 1):
        sym = stock["symbol"]
        if sym.startswith("^"):
            continue
        logger.info("[%d/%d] 掃描 %s", i, len(universe), sym)
        data = fetch_value_data(sym, fm=fm)
        div_years = fetch_dividend_history(sym, years=6, fm=fm)
        income_stmt = fetch_income_statement(sym)
        r = check_value(sym, stock.get("name", ""), stock.get("sector", ""), data, div_years, income_stmt)
        results.append(r)

    state = load_state()
    changes = update_watchlist(state, results, today)
    save_state(state)

    report = generate_report(today, results, changes)
    md_path = LOG_DIR / f"{month_str}.md"
    json_path = LOG_DIR / f"{month_str}.json"
    md_path.write_text(report, encoding="utf-8")
    json_out = {
        "month": month_str,
        "date": today.isoformat(),
        "universe_size": len(universe),
        "passed_count": sum(1 for r in results if r.passed),
        "changes": changes,
        "results": [asdict(r) for r in results],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2, default=str)
    logger.info("報告存檔: %s", md_path)

    push_notifications(report, dry_run=dry_run)


def cmd_single(symbol: str) -> None:
    logger.info("單檔檢查: %s", symbol)
    fm = FinMindHelper() if HAS_FINMIND else None
    data = fetch_value_data(symbol, fm=fm)
    div_years = fetch_dividend_history(symbol, years=6, fm=fm)
    income_stmt = fetch_income_statement(symbol)
    r = check_value(symbol, data.get("name") or symbol, data.get("industry") or "?", data, div_years, income_stmt)
    print(json.dumps(asdict(r), ensure_ascii=False, indent=2, default=str))


def cmd_status() -> None:
    state = load_state()
    symbols = state.get("symbols", {})
    in_pool = [s for s, e in symbols.items() if e.get("in_pool")]
    confirming = [s for s, e in symbols.items() if not e.get("in_pool") and e.get("consecutive_months", 0) > 0]
    print(f"=== 價值觀察池現況 (更新時間: {state.get('updated_at')}) ===")
    print(f"進池: {len(in_pool)} 檔")
    for sym in in_pool:
        e = symbols[sym]
        print(f"  {e['name']} ({sym}) — 進池 {e['pool_entry_date']}，保鮮至 {e['freshness_until']}")
    print(f"\n確認期中: {len(confirming)} 檔")
    for sym in confirming:
        e = symbols[sym]
        print(f"  {e['name']} ({sym}) — 已連續 {e['consecutive_months']} 個月")


def main() -> None:
    parser = argparse.ArgumentParser(description="價值選股 (長線)")
    parser.add_argument("--dry-run", action="store_true", help="不推播")
    parser.add_argument("--symbol", help="單檔檢查")
    parser.add_argument("--status", action="store_true", help="觀察池現況")
    parser.add_argument("--universe", default="yaml", choices=["yaml", "full"],
                        help="yaml=預設 100 檔, full=FinMind 全市場")
    parser.add_argument("--limit", type=int, help="限制掃描數量")
    args = parser.parse_args()

    if args.status:
        cmd_status()
    elif args.symbol:
        cmd_single(args.symbol)
    else:
        cmd_scan(dry_run=args.dry_run, universe_source=args.universe, universe_limit=args.limit)


if __name__ == "__main__":
    main()
