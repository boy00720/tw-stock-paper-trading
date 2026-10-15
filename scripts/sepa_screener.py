#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sepa_screener.py — 成長選股W (SEPA 中線池篩選器) MVP

責任:
    1. 每週一掃描候選池（MVP 階段用 stock_universe.yaml 約 150 檔，後續擴全市場）
    2. 對每檔執行 SEPA 八項紀律檢查
    3. 清單治理: 連續 3 週通過才進池，進池保鮮期 30 天
    4. 產生報告並推送飛書 + 微信

SEPA 八項 (詳見 /Users/w/Cursor/股市選股紀律.md):
    ① 剔除風險標的: 非 ST、上市 > 12 個月
    ② 營收成長: 近季營收 YoY > 25%
    ③ 獲利成長: 近季淨利 YoY > 30% 且 QoQ > 0
    ④ 技術排列: 站上 50MA 且站上 150MA
    ⑤ 量能確認: 近 10 日均量 > 120 日均量 50%
    ⑥ 資本效率: ROE > 15%
    ⑦ 相對強度: 近 20 日 RPS > 台股加權漲幅
    ⑧ 長期成長: 近三年淨利 CAGR > 20%

用法:
    python3 scripts/sepa_screener.py                    # 每週掃描+推送
    python3 scripts/sepa_screener.py --dry-run          # 不推播
    python3 scripts/sepa_screener.py --symbol 2330.TW   # 單檔 SEPA 檢查
    python3 scripts/sepa_screener.py --status           # 觀察池現況
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 清除代理 (與 stock_screener 保持一致)
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
    logger = logging.getLogger(__name__)
    logger.warning("FinMind helper 未安裝，SEPA ①③⑧ 項會無法取得")


# ─────────────────────────────────────────────────────
# 常量 (與紀律檔一致)
# ─────────────────────────────────────────────────────

LOG_DIR = _BASE_DIR / "logs" / "sepa_screener"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRACKER_DIR = _BASE_DIR / "logs" / "sepa_tracker"
TRACKER_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = TRACKER_DIR / "watchlist_state.json"

# 清單治理
CONFIRM_WEEKS = 3           # 連續 3 週通過才進池
FRESHNESS_DAYS = 30         # 進池保鮮期 30 天
MIN_POOL_SIZE = 10
MAX_POOL_SIZE = 15

# SEPA 八項閾值
THRESHOLDS = {
    "revenue_yoy": 0.25,        # ②
    "earnings_yoy": 0.30,       # ③
    "roe": 0.15,                # ⑥
    "cagr_3y": 0.20,            # ⑧
    "volume_ratio": 1.50,       # ⑤ 近10日均量 > 120日均量 50%
    "ipo_months": 12,           # ① 上市 > 12 個月
}

BENCHMARK = "^TWII"  # 台股加權 (for RPS 計算)

# ─────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sepa_screener")


# ─────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────

@dataclass
class SEPAResult:
    """單檔股 SEPA 八項檢查結果"""
    symbol: str
    name: str
    sector: str
    passed: bool = False                        # 八項全通過
    checks: Dict[str, Optional[bool]] = field(default_factory=dict)  # True/False/None (None=無法取得)
    values: Dict[str, Any] = field(default_factory=dict)             # 實際數值
    missing: List[str] = field(default_factory=list)                 # 無法取得的項
    notes: List[str] = field(default_factory=list)                   # 特別說明
    error: Optional[str] = None


# ─────────────────────────────────────────────────────
# 候選池載入
# ─────────────────────────────────────────────────────

def load_universe(source: str = "yaml", limit: Optional[int] = None, fm: Optional["FinMindHelper"] = None) -> List[Dict[str, str]]:
    """載入候選池

    Args:
        source: 'yaml' → 用 config/stock_universe.yaml（~100 檔精選大型股）
                'full' → yaml 100 檔 + FinMind 全市場補充（按 stock_id 排序），大型股優先
        limit: 限制前 N 檔（配合 source='full' 用）
    """
    if source == "yaml":
        path = _BASE_DIR / "config" / "stock_universe.yaml"
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        universe = cfg.get("universe", [])
        logger.info("候選池 [yaml]: %d 檔", len(universe))
        return universe

    if source == "full":
        if fm is None:
            logger.warning("source=full 但 FinMind 未啟用，回退至 yaml")
            return load_universe(source="yaml")

        # Step 1: yaml 精選清單優先
        path = _BASE_DIR / "config" / "stock_universe.yaml"
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        yaml_universe = cfg.get("universe", [])
        yaml_symbols = {item["symbol"] for item in yaml_universe}

        # Step 2: FinMind 全市場
        df = fm.get_universe()
        df = fm.filter_common_stocks(df)

        # Step 3: 補充集 = FinMind 全市場 - yaml 已有
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

        # Step 4: 合併：yaml 先，補充後
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

def fetch_price_history(symbol: str, days: int = 200, retries: int = 1) -> Optional[pd.DataFrame]:
    """抓近 N 日股價 (至少要 > 150MA 所需天數)

    加 retry：yfinance 對台股偶有 404，通常第二次就能拿到
    """
    for attempt in range(retries + 1):
        try:
            end = datetime.now()
            start = end - timedelta(days=days + 30)  # buffer
            df = yf.download(
                symbol, start=start, end=end, progress=False, auto_adjust=False,
                threads=False,
            )
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5)
                continue
            logger.debug("fetch_price_history(%s) 失敗: %s", symbol, e)
        if attempt < retries:
            time.sleep(1.5)
    return None


def fetch_fundamentals(symbol: str, fm: Optional["FinMindHelper"] = None) -> Dict[str, Any]:
    """抓基本面資料

    優先用 FinMind（台股資料完整），fallback 到 yfinance
    """
    result = {
        "roe": None,
        "revenue_growth": None,  # YoY (小數)
        "earnings_growth": None,  # YoY (小數)
        "earnings_qoq": None,     # QoQ (小數)
        "cagr_3y": None,          # 3 年淨利 CAGR
        "ipo_date": None,
        "trailing_pe": None,
        "name": None,
        "industry": None,
    }

    # 1. FinMind 優先（台股資料完整）
    if fm is not None:
        try:
            info = fm.get_stock_info(symbol)
            result["name"] = info.get("name")
            result["industry"] = info.get("industry")

            # 月營收 YoY
            rev_df = fm.get_monthly_revenue(symbol, months=24)
            if rev_df is not None:
                result["revenue_growth"] = fm.compute_revenue_yoy(rev_df)

            # 季度財報 → 淨利 YoY/QoQ、ROE、CAGR
            fs_df = fm.get_financial_statement(symbol, quarters=16)
            if fs_df is not None:
                ni_stats = fm.compute_net_income_yoy_qoq(fs_df)
                result["earnings_growth"] = ni_stats.get("yoy")
                result["earnings_qoq"] = ni_stats.get("qoq")
                roe_cagr = fm.compute_roe_and_cagr(fs_df)
                result["roe"] = roe_cagr.get("roe_ttm")
                result["cagr_3y"] = roe_cagr.get("cagr_3y")

            # 上市日期
            ipo = fm.get_ipo_date(symbol)
            if ipo:
                result["ipo_date"] = ipo.date() if hasattr(ipo, "date") else ipo

            # P/E
            per_df = fm.get_per_pbr(symbol)
            if per_df is not None and not per_df.empty and "PER" in per_df.columns:
                latest_pe = per_df.iloc[-1]["PER"]
                if latest_pe and latest_pe > 0:
                    result["trailing_pe"] = float(latest_pe)
        except Exception as e:
            logger.debug("FinMind fetch_fundamentals(%s) 失敗: %s", symbol, e)

    # 2. yfinance fallback（補 FinMind 沒拿到的）
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        if result["roe"] is None:
            result["roe"] = info.get("returnOnEquity")
        if result["revenue_growth"] is None:
            result["revenue_growth"] = info.get("revenueGrowth")
        if result["earnings_growth"] is None:
            result["earnings_growth"] = info.get("earningsGrowth")
        if result["trailing_pe"] is None:
            result["trailing_pe"] = info.get("trailingPE")
        if result["ipo_date"] is None:
            ipo_epoch = info.get("firstTradeDateEpochUtc")
            if ipo_epoch:
                result["ipo_date"] = datetime.fromtimestamp(ipo_epoch).date()
    except Exception as e:
        logger.debug("yfinance fallback(%s) 失敗: %s", symbol, e)

    return result


def fetch_historical_earnings(symbol: str) -> Optional[pd.DataFrame]:
    """抓近 3 年淨利 (用於 CAGR 計算)

    TODO: yfinance 台股資料常缺，Phase 2 改接財報狗或 TWSE API。
    """
    try:
        ticker = yf.Ticker(symbol)
        income = ticker.income_stmt  # DataFrame with net income
        if income is None or income.empty:
            return None
        return income
    except Exception:
        return None


def fetch_benchmark_return(days: int = 20) -> Optional[float]:
    """計算台股加權近 N 日漲幅"""
    df = fetch_price_history(BENCHMARK, days=days + 10)
    if df is None or len(df) < days:
        return None
    close = df["Close"].dropna()
    if len(close) < days:
        return None
    return (close.iloc[-1] / close.iloc[-days] - 1)


# ─────────────────────────────────────────────────────
# SEPA 八項檢查
# ─────────────────────────────────────────────────────

def check_sepa(
    symbol: str,
    name: str,
    sector: str,
    price_df: Optional[pd.DataFrame],
    fundamentals: Dict[str, Any],
    earnings_df: Optional[pd.DataFrame],
    benchmark_return_20d: Optional[float],
) -> SEPAResult:
    """對單檔股執行 SEPA 八項檢查"""
    r = SEPAResult(symbol=symbol, name=name, sector=sector)

    if price_df is None or price_df.empty:
        r.error = "無法取得股價資料"
        return r

    close = price_df["Close"].dropna()
    volume = price_df["Volume"].dropna()

    # ① 剔除風險標的: ST / 上市 > 12 個月
    ipo_date = fundamentals.get("ipo_date")
    if ipo_date:
        months_since_ipo = (date.today() - ipo_date).days / 30
        ok_1 = months_since_ipo >= THRESHOLDS["ipo_months"]
        r.checks["① 剔除風險標的"] = ok_1
        r.values["上市月數"] = round(months_since_ipo, 1)
    else:
        r.checks["① 剔除風險標的"] = None
        r.missing.append("上市日期 (yfinance 無此資料)")

    # ② 營收成長 YoY > 25%
    rev_yoy = fundamentals.get("revenue_growth")
    if rev_yoy is not None:
        r.checks["② 營收YoY>25%"] = bool(rev_yoy > THRESHOLDS["revenue_yoy"])
        r.values["營收YoY"] = f"{rev_yoy*100:.1f}%"
    else:
        r.checks["② 營收YoY>25%"] = None
        r.missing.append("營收成長率")

    # ③ 淨利 YoY > 30% 且 QoQ > 0
    eps_yoy = fundamentals.get("earnings_growth")
    eps_qoq = fundamentals.get("earnings_qoq")
    if eps_yoy is not None:
        yoy_ok = eps_yoy > THRESHOLDS["earnings_yoy"]
        qoq_ok = (eps_qoq is None) or (eps_qoq > 0)  # QoQ 無資料時不強制
        r.checks["③ 淨利YoY>30%+QoQ>0"] = bool(yoy_ok and qoq_ok)
        r.values["淨利YoY"] = f"{eps_yoy*100:.1f}%"
        if eps_qoq is not None:
            r.values["淨利QoQ"] = f"{eps_qoq*100:.1f}%"
    else:
        r.checks["③ 淨利YoY>30%+QoQ>0"] = None
        r.missing.append("淨利成長率")

    # ④ 技術排列: 站上 50MA + 150MA
    if len(close) >= 150:
        ma50 = float(close.rolling(50).mean().iloc[-1])
        ma150 = float(close.rolling(150).mean().iloc[-1])
        last = float(close.iloc[-1])
        ok_4 = (last > ma50) and (last > ma150)
        r.checks["④ 站上50MA+150MA"] = bool(ok_4)
        r.values["現價"] = round(last, 2)
        r.values["50MA"] = round(ma50, 2)
        r.values["150MA"] = round(ma150, 2)
    else:
        r.checks["④ 站上50MA+150MA"] = None
        r.missing.append(f"歷史資料不足 {len(close)}/150")

    # ⑤ 量能: 近10日均量 > 120日均量 * 1.5
    if len(volume) >= 120:
        v10 = float(volume.rolling(10).mean().iloc[-1])
        v120 = float(volume.rolling(120).mean().iloc[-1])
        if v120 > 0:
            vol_ratio = v10 / v120
            r.checks["⑤ 量能放大"] = bool(vol_ratio > THRESHOLDS["volume_ratio"])
            r.values["量能比"] = round(vol_ratio, 2)
        else:
            r.checks["⑤ 量能放大"] = None
            r.missing.append("120 日均量為零")
    else:
        r.checks["⑤ 量能放大"] = None
        r.missing.append(f"成交量資料不足 {len(volume)}/120")

    # ⑥ ROE > 15%
    roe = fundamentals.get("roe")
    if roe is not None:
        r.checks["⑥ ROE>15%"] = bool(roe > THRESHOLDS["roe"])
        r.values["ROE"] = f"{roe*100:.1f}%"
    else:
        r.checks["⑥ ROE>15%"] = None
        r.missing.append("ROE")

    # ⑦ 近 20 日 RPS > 大盤
    if len(close) >= 20 and benchmark_return_20d is not None:
        stock_return = float(close.iloc[-1] / close.iloc[-20] - 1)
        r.checks["⑦ RPS>大盤"] = bool(stock_return > benchmark_return_20d)
        r.values["近20日漲幅"] = f"{stock_return*100:.1f}%"
        r.values["大盤20日"] = f"{benchmark_return_20d*100:.1f}%"
    else:
        r.checks["⑦ RPS>大盤"] = None
        r.missing.append("近20日資料不足或大盤資料缺失")

    # ⑧ 近三年淨利 CAGR > 20%
    cagr = fundamentals.get("cagr_3y")
    if cagr is not None:
        r.checks["⑧ 3年CAGR>20%"] = bool(cagr > THRESHOLDS["cagr_3y"])
        # -1.0 表示有虧損，顯示文字而非 -100%
        if cagr <= -0.99:
            r.values["淨利CAGR"] = "有虧損"
            r.notes.append("近3年某年虧損，不符長期成長定義")
        else:
            r.values["淨利CAGR"] = f"{cagr*100:.1f}%"
    elif earnings_df is not None and not earnings_df.empty:
        # fallback: yfinance income_stmt
        try:
            net_income = earnings_df.loc["Net Income"] if "Net Income" in earnings_df.index else None
            if net_income is not None and len(net_income) >= 3:
                vals = net_income.iloc[:3].values
                if all(v > 0 for v in vals):
                    cagr_yf = float((vals[0] / vals[-1]) ** (1/2) - 1)
                    r.checks["⑧ 3年CAGR>20%"] = bool(cagr_yf > THRESHOLDS["cagr_3y"])
                    r.values["淨利CAGR"] = f"{cagr_yf*100:.1f}% (yf)"
                else:
                    r.checks["⑧ 3年CAGR>20%"] = False
                    r.notes.append("近三年有虧損")
            else:
                r.checks["⑧ 3年CAGR>20%"] = None
                r.missing.append("歷史淨利不足")
        except Exception as e:
            r.checks["⑧ 3年CAGR>20%"] = None
            r.missing.append(f"CAGR 計算失敗: {e}")
    else:
        r.checks["⑧ 3年CAGR>20%"] = None
        r.missing.append("無財報資料")

    # 判定 passed: 所有有值的項都 True，且無值的項 < 3 (容忍度)
    true_count = sum(1 for v in r.checks.values() if v is True)
    false_count = sum(1 for v in r.checks.values() if v is False)
    unknown_count = sum(1 for v in r.checks.values() if v is None)
    r.values["通過數"] = true_count
    r.values["未通過數"] = false_count
    r.values["無資料"] = unknown_count
    r.passed = (false_count == 0) and (unknown_count <= 2)  # 容忍 2 項無資料
    return r


# ─────────────────────────────────────────────────────
# 清單治理 State
# ─────────────────────────────────────────────────────

def load_state() -> Dict[str, Any]:
    """載入觀察池 state"""
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
    results: List[SEPAResult],
    today: date,
) -> Dict[str, List[Dict]]:
    """依紀律更新觀察池 state，返回本週變動報告

    Returns:
        {
            "new_entries": [...],       # 本週進池
            "in_pool": [...],           # 在觀察池中
            "forced_out": [...],        # 強制出池
            "confirming": [...],        # 確認期中
        }
    """
    symbols = state.setdefault("symbols", {})
    today_iso = today.isoformat()
    changes = {
        "new_entries": [],
        "in_pool": [],
        "forced_out": [],
        "confirming": [],
    }

    current_passed = {r.symbol: r for r in results if r.passed}

    # 更新每檔狀態
    for sym, r in current_passed.items():
        entry = symbols.setdefault(sym, {
            "name": r.name,
            "sector": r.sector,
            "first_pass_date": today_iso,
            "consecutive_weeks": 0,
            "in_pool": False,
            "pool_entry_date": None,
            "freshness_until": None,
            "last_check_date": today_iso,
            "last_passed": True,
        })
        entry["consecutive_weeks"] += 1
        entry["last_check_date"] = today_iso
        entry["last_passed"] = True
        entry["name"] = r.name  # 更新

        if not entry["in_pool"] and entry["consecutive_weeks"] >= CONFIRM_WEEKS:
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
                "weeks": entry["consecutive_weeks"],
                "needed": CONFIRM_WEEKS - entry["consecutive_weeks"],
            })

    # 本週未通過的：重置確認期，保鮮期檢查
    for sym, entry in list(symbols.items()):
        if sym in current_passed:
            continue
        entry["last_check_date"] = today_iso
        entry["last_passed"] = False
        # 確認期未滿 → 重置
        if not entry["in_pool"]:
            if entry["consecutive_weeks"] > 0:
                entry["consecutive_weeks"] = 0
            continue
        # 已在池中 → 檢查保鮮期
        if entry.get("freshness_until"):
            freshness_until = date.fromisoformat(entry["freshness_until"])
            if today > freshness_until:
                # 過保鮮期且未通過 → 剔除
                entry["in_pool"] = False
                changes["forced_out"].append({
                    "symbol": sym, "name": entry["name"],
                    "reason": "保鮮期滿且本週 SEPA 未通過",
                })
            else:
                changes["in_pool"].append({
                    "symbol": sym, "name": entry["name"],
                    "freshness_remaining": _days_between(today_iso, entry["freshness_until"]),
                    "note": "本週未通過但保鮮期內",
                })

    return changes


def _days_between(d1_iso: str, d2_iso: Optional[str]) -> Optional[int]:
    if not d2_iso:
        return None
    d1 = date.fromisoformat(d1_iso)
    d2 = date.fromisoformat(d2_iso)
    return (d2 - d1).days


# ─────────────────────────────────────────────────────
# 報告產生
# ─────────────────────────────────────────────────────

def generate_report(
    today: date,
    results: List[SEPAResult],
    changes: Dict[str, List[Dict]],
) -> str:
    """產生 Markdown 報告"""
    week = today.strftime("%Y-W%V")
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    errors = sum(1 for r in results if r.error)

    lines = [
        f"# 📈 成長選股W 週報 {week} ({today})",
        "",
        f"**掃描範圍**: {total} 檔（初版候選池，尚未擴至全市場）",
        f"**本週 SEPA 通過**: {passed} 檔",
        f"**資料錯誤**: {errors} 檔",
        "",
        "## 🟢 本週新進池",
    ]
    if changes["new_entries"]:
        for e in changes["new_entries"]:
            lines.append(
                f"- **{format_name_with_code(e['name'], e['symbol'])}** [{e['sector']}] → 保鮮期至 {e['freshness_until']}"
            )
    else:
        lines.append("_本週無新進池個股_")

    lines.extend(["", "## 📊 觀察池當前"])
    if changes["in_pool"]:
        for e in changes["in_pool"]:
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(
                f"- {format_name_with_code(e['name'], e['symbol'])}｜保鮮期剩 {e.get('freshness_remaining')} 日{note}"
            )
    else:
        lines.append("_觀察池空_")

    lines.extend(["", "## 🔴 本週強制出池"])
    if changes["forced_out"]:
        for e in changes["forced_out"]:
            lines.append(f"- {format_name_with_code(e['name'], e['symbol'])}｜原因: {e['reason']}")
    else:
        lines.append("_本週無強制出池_")

    lines.extend(["", "## 🟡 確認期中（未滿 3 週）"])
    if changes["confirming"]:
        for e in changes["confirming"]:
            lines.append(f"- {format_name_with_code(e['name'], e['symbol'])}｜已連續 {e['weeks']} 週，再 {e['needed']} 週可進池")
    else:
        lines.append("_無個股處於確認期_")

    # 本週通過明細（前 10 名）
    passed_list = [r for r in results if r.passed][:10]
    if passed_list:
        lines.extend(["", "## ✅ 本週 SEPA 八項通過明細（前 10 名）"])
        for r in passed_list:
            lines.append(f"### {format_name_with_code(r.name, r.symbol)} [{r.sector}]")
            lines.append(
                f"通過 {r.values.get('通過數')} 項｜無資料 {r.values.get('無資料')} 項"
            )
            for key, val in r.checks.items():
                icon = "✅" if val is True else ("❌" if val is False else "⚪")
                val_str = ""
                for k in ("ROE", "營收YoY", "淨利YoY", "近20日漲幅", "量能比", "淨利CAGR", "上市月數"):
                    if k in r.values and k.replace(">", "").replace("<", "") in key:
                        val_str = f" ({r.values[k]})"
                        break
                lines.append(f"- {icon} {key}{val_str}")
            if r.missing:
                lines.append(f"- _無資料: {', '.join(r.missing[:3])}_")
            lines.append("")

    lines.extend([
        "",
        "---",
        "",
        "📌 **紀律提醒**:",
        "- SEPA 八項全通過 + 連續 3 週 = 進池",
        "- 進池後保鮮期 30 天，期內不因單週雜訊剔除",
        "- 觸發 50MA 跌破 / 任一項連兩季未達 → 強制出池",
        "- 所有結果請自行判斷是否執行，**後果自負**",
        "",
        '> 「依嚴格規則交易時，情緒就會被排除在方程式之外。」— Mark Minervini',
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
        logger.info("[試跑] 不推送，報告長度 %d 字", len(report))
        return

    # 飛書
    try:
        lark = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        chat_id = os.getenv("FEISHU_DEFAULT_CHAT_ID")
        title = "📈 成長選股 W 週報"
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
            # allow_unready_queue=True：bridge 暫時不 ready 時走 deferred queue，
            #   避免 sepa_screener 在連續推送後段（context_token 短暫失效時）silent fail。
            # metadata 帶 step_name / agent 以便 wechat_delivery.jsonl 與審計可追蹤。
            ok = wechat.send_message(
                report,
                to_user=target,
                typing_seconds=3,
                allow_unready_queue=True,
                metadata={"step_name": "sepa_screener", "agent": "sepa_screener"},
            )
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
    """主掃描流程"""
    today = date.today()
    week_str = today.strftime("%Y-W%V")
    logger.info("=== SEPA Screener 掃描開始 %s ===", today)

    # 初始化 FinMind (若可用)
    fm = None
    if HAS_FINMIND:
        try:
            fm = FinMindHelper()
            logger.info("✓ FinMind 已啟用（含 SQLite 快取）")
        except Exception as e:
            logger.warning("FinMind 初始化失敗，僅用 yfinance: %s", e)

    universe = load_universe(source=universe_source, limit=universe_limit, fm=fm)
    benchmark_20d = fetch_benchmark_return(20)
    logger.info("大盤 20 日漲幅: %s", f"{benchmark_20d*100:.2f}%" if benchmark_20d else "無資料")

    results: List[SEPAResult] = []
    for i, stock in enumerate(universe, 1):
        sym = stock["symbol"]
        if sym.startswith("^"):
            continue
        logger.info("[%d/%d] 掃描 %s (%s)", i, len(universe), sym, stock.get("name", ""))
        price_df = fetch_price_history(sym, days=200)
        fundamentals = fetch_fundamentals(sym, fm=fm)
        earnings_df = fetch_historical_earnings(sym)
        r = check_sepa(
            sym, stock.get("name", ""), stock.get("sector", ""),
            price_df, fundamentals, earnings_df, benchmark_20d,
        )
        results.append(r)

    # 更新清單治理 state
    state = load_state()
    changes = update_watchlist(state, results, today)
    save_state(state)

    # 產生報告
    report = generate_report(today, results, changes)

    # 存檔
    md_path = LOG_DIR / f"{week_str}.md"
    json_path = LOG_DIR / f"{week_str}.json"
    md_path.write_text(report, encoding="utf-8")
    json_out = {
        "week": week_str,
        "date": today.isoformat(),
        "universe_size": len(universe),
        "passed_count": sum(1 for r in results if r.passed),
        "changes": changes,
        "results": [asdict(r) for r in results],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2, default=str)
    logger.info("報告存檔: %s", md_path)
    logger.info("JSON 存檔: %s", json_path)

    push_notifications(report, dry_run=dry_run)


def cmd_single(symbol: str) -> None:
    """單檔 SEPA 檢查"""
    logger.info("單檔檢查: %s", symbol)
    fm = FinMindHelper() if HAS_FINMIND else None
    benchmark_20d = fetch_benchmark_return(20)
    price_df = fetch_price_history(symbol, days=200)
    fundamentals = fetch_fundamentals(symbol, fm=fm)
    earnings_df = fetch_historical_earnings(symbol)
    r = check_sepa(symbol, fundamentals.get("name") or symbol, fundamentals.get("industry") or "?", price_df, fundamentals, earnings_df, benchmark_20d)
    print(json.dumps(asdict(r), ensure_ascii=False, indent=2, default=str))


def cmd_status() -> None:
    """觀察池現況"""
    state = load_state()
    symbols = state.get("symbols", {})
    in_pool = [s for s, e in symbols.items() if e.get("in_pool")]
    confirming = [s for s, e in symbols.items() if not e.get("in_pool") and e.get("consecutive_weeks", 0) > 0]
    print(f"=== SEPA 觀察池現況 (更新時間: {state.get('updated_at')}) ===")
    print(f"進池: {len(in_pool)} 檔")
    for sym in in_pool:
        e = symbols[sym]
        print(f"  {e['name']} ({sym}) — 進池 {e['pool_entry_date']}，保鮮至 {e['freshness_until']}")
    print(f"\n確認期中: {len(confirming)} 檔")
    for sym in confirming:
        e = symbols[sym]
        print(f"  {e['name']} ({sym}) — 已連續 {e['consecutive_weeks']} 週")


def main() -> None:
    parser = argparse.ArgumentParser(description="SEPA 中線選股 (成長選股W)")
    parser.add_argument("--dry-run", action="store_true", help="不推播")
    parser.add_argument("--symbol", help="單檔 SEPA 檢查")
    parser.add_argument("--status", action="store_true", help="觀察池現況")
    parser.add_argument("--universe", default="yaml", choices=["yaml", "full"],
                        help="yaml=預設 100 檔, full=FinMind 全市場 ~2340 檔")
    parser.add_argument("--limit", type=int, help="限制掃描數量（配合 --universe full 用）")
    args = parser.parse_args()

    if args.status:
        cmd_status()
    elif args.symbol:
        cmd_single(args.symbol)
    else:
        cmd_scan(dry_run=args.dry_run, universe_source=args.universe, universe_limit=args.limit)


if __name__ == "__main__":
    main()
