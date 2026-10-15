#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intraday_screener.py — 日內選股W (池 ④ 日內動能) MVP

責任:
    1. 盤中 14:30 掃描，找出符合一夜持股法六項條件的個股
    2. 當日 14:30-14:50 進場，隔日 09:30 前集合競價出清
    3. 紀律最嚴格，機械化執行

池 ④ 日內動能六項 (詳見 /Users/w/Cursor/股市選股紀律.md):
    ① 漲幅區間: 當日漲幅 3%-7%
    ② 動能記憶: 近 3 日曾單日大漲 > 7%
    ③ 市值限制: 市值 < 3000 億
    ④ 量比確認: 量比 > 1.5
    ⑤ 換手率: 3%-10%
    ⑥ 分時圖信號: 14:30 後創日內新高 + 回踩不破均線

注意：
    - 純機械化，當日即時掃描
    - 不走清單治理（保鮮期/確認期），每日重新開始
    - 操作口號：「放棄思考，執行紀律」
    - 新手建議先模擬盤熟悉
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import pandas as pd
import yaml
import yfinance as yf

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

from utils.data_validator import (
    validate_m1_data_completeness,
    validate_m1_data_freshness,
    validate_m1_snapshot,
)
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

LOG_DIR = _BASE_DIR / "logs" / "intraday"
LOG_DIR.mkdir(parents=True, exist_ok=True)

THRESHOLDS = {
    "daily_gain_min": 0.03,
    "daily_gain_max": 0.07,
    "memory_surge_min": 0.07,
    "memory_surge_lookback": 3,
    "market_cap_max": 3000e8,
    "volume_ratio_min": 1.5,
    "turnover_min": 0.03,
    "turnover_max": 0.10,
}

MAX_PICKS = 5
INTRADAY_ENGINE_VERSION = "SE-009B-m1-snapshot"
M1_CUTOFF_TIME = "14:30"
M1_INTERVAL = "1m"
VALUE_LABELS = {
    "score": "分數",
    "snapshot_time": "快照時間",
    "breakout_time": "突破時間",
    "M1 bars": "M1 分鐘棒數",
    "m1_snapshot": "M1 快照",
    "metadata": "中繼資料",
}


def display_value_label(key: str) -> str:
    return VALUE_LABELS.get(key, key)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("intraday_screener")


def format_name_with_code(name: str, symbol: str) -> str:
    code = symbol.replace(".TWO", "").replace(".TW", "").lstrip("^")
    suffix = f"（{code}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


@dataclass
class IntradayResult:
    symbol: str
    name: str
    sector: str
    passed: bool = False
    strict_passed: bool = False
    checks: Dict[str, Optional[bool]] = field(default_factory=dict)
    values: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None


def load_universe(source: str = "yaml", limit: Optional[int] = None, fm: Optional["FinMindHelper"] = None) -> List[Dict[str, str]]:
    if source == "yaml":
        path = _BASE_DIR / "config" / "stock_universe.yaml"
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        universe = cfg.get("universe", [])
        return universe[:limit] if limit else universe

    if source == "full" and fm is not None:
        df = fm.get_universe()
        df = fm.filter_common_stocks(df)
        if limit:
            df = df.head(limit)
        return [
            {
                "symbol": f"{row['stock_id']}.TW" if row["type"] == "twse" else f"{row['stock_id']}.TWO",
                "name": row["stock_name"],
                "sector": row["industry_category"],
            }
            for _, row in df.iterrows()
        ]
    return []


def fetch_daily_history(symbol: str, days: int = 90) -> Optional[pd.DataFrame]:
    try:
        end = datetime.now()
        start = end - timedelta(days=days + 20)
        df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None


def fetch_m1_history(symbol: str, period: str = "1d", interval: str = M1_INTERVAL) -> Optional[pd.DataFrame]:
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None


def prepare_m1_frame(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    frame = df.copy()
    frame.index = pd.to_datetime(frame.index)
    if frame.index.tz is not None:
        frame.index = frame.index.tz_convert(ZoneInfo("Asia/Taipei")).tz_localize(None)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    if not any(col in frame.columns for col in ["Open", "High", "Low", "Close", "Volume"]):
        return None
    frame = frame.dropna(subset=[col for col in ["Open", "High", "Low", "Close"] if col in frame.columns])
    if "Volume" in frame.columns:
        frame["Volume"] = frame["Volume"].fillna(0)
    return frame


def _derive_effective_cutoff_time(index: pd.DatetimeIndex, configured_cutoff: str) -> str:
    if len(index) == 0:
        return configured_cutoff
    cutoff_h, cutoff_m = map(int, configured_cutoff.split(":"))
    configured_dt = datetime.combine(index[-1].date(), time(cutoff_h, cutoff_m))
    last_bar_dt = index[-1].to_pydatetime() if hasattr(index[-1], "to_pydatetime") else index[-1]
    if last_bar_dt >= configured_dt:
        return configured_cutoff
    fallback_dt = max(index[0].to_pydatetime() if hasattr(index[0], "to_pydatetime") else index[0], last_bar_dt - timedelta(minutes=30))
    return fallback_dt.strftime("%H:%M")


def fetch_market_profile(symbol: str) -> Dict[str, Optional[float]]:
    try:
        info = yf.Ticker(symbol).info or {}
        return {
            "market_cap": info.get("marketCap"),
            "shares_outstanding": info.get("sharesOutstanding") or info.get("impliedSharesOutstanding"),
        }
    except Exception:
        return {"market_cap": None, "shares_outstanding": None}


def _cutoff_datetime(index: pd.DatetimeIndex, cutoff_time: str) -> Optional[datetime]:
    if len(index) == 0:
        return None
    cutoff_h, cutoff_m = map(int, cutoff_time.split(":"))
    return datetime.combine(index[-1].date(), time(cutoff_h, cutoff_m))


def build_m1_snapshot(
    symbol: str,
    minute_df: Optional[pd.DataFrame],
    daily_df: Optional[pd.DataFrame],
    market_profile: Dict[str, Optional[float]],
    cutoff_time: str = M1_CUTOFF_TIME,
) -> Optional[Dict[str, Any]]:
    frame = prepare_m1_frame(minute_df)
    if frame is None or frame.empty or daily_df is None or daily_df.empty:
        return None

    close = daily_df["Close"].dropna()
    volume = daily_df["Volume"].dropna()
    if len(close) < 20 or len(volume) < 20:
        return None

    prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
    if prev_close is None or prev_close <= 0:
        return None

    last_bar = frame.iloc[-1]
    last_price = float(last_bar["Close"])
    open_price = float(frame.iloc[0]["Open"])
    intraday_high = float(frame["High"].max())
    intraday_low = float(frame["Low"].min())
    volume_cum = float(frame["Volume"].sum())
    avg_volume_20d = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else float(volume.iloc[-20:].mean())
    volume_ratio = volume_cum / avg_volume_20d if avg_volume_20d > 0 else None
    shares_outstanding = market_profile.get("shares_outstanding")
    turnover = volume_cum / shares_outstanding if shares_outstanding and shares_outstanding > 0 else None

    typical_price = (frame["High"] + frame["Low"] + frame["Close"]) / 3
    cumulative_volume = frame["Volume"].replace(0, pd.NA).cumsum()
    cumulative_turnover = (typical_price * frame["Volume"]).cumsum()
    vwap_series = cumulative_turnover / cumulative_volume
    vwap = float(vwap_series.iloc[-1]) if cumulative_volume.notna().any() else None
    m1_ma = float(frame["Close"].tail(min(5, len(frame))).mean())
    ma20_series = frame["Close"].rolling(window=20, min_periods=5).mean()

    cutoff_dt = _cutoff_datetime(frame.index, cutoff_time)
    post_cutoff = frame[frame.index >= cutoff_dt] if cutoff_dt is not None else frame.iloc[0:0]
    post_cutoff_high = float(post_cutoff["High"].max()) if not post_cutoff.empty else None
    post_cutoff_low = float(post_cutoff["Low"].min()) if not post_cutoff.empty else None
    post_cutoff_bar_count = int(len(post_cutoff))
    pre_cutoff = frame[frame.index < cutoff_dt] if cutoff_dt is not None else frame
    pre_cutoff_high = float(pre_cutoff["High"].max()) if not pre_cutoff.empty else intraday_high

    breakout_epsilon = 0.001
    breakout_threshold = pre_cutoff_high * (1 + breakout_epsilon)
    breakout_candidates = post_cutoff[post_cutoff["High"] > breakout_threshold] if not post_cutoff.empty else post_cutoff
    breakout_time = breakout_candidates.index[0] if not breakout_candidates.empty else None
    breakout_price = float(breakout_candidates.iloc[0]["High"]) if breakout_time is not None else None

    after_breakout = frame[frame.index >= breakout_time] if breakout_time is not None else frame.iloc[0:0]
    pullback_low = float(after_breakout["Low"].min()) if not after_breakout.empty else None
    pullback_low_time = after_breakout["Low"].idxmin() if not after_breakout.empty else None
    vwap_at_pullback = float(vwap_series.loc[pullback_low_time]) if pullback_low_time is not None and pullback_low_time in vwap_series.index and pd.notna(vwap_series.loc[pullback_low_time]) else None
    ma20_at_pullback = float(ma20_series.loc[pullback_low_time]) if pullback_low_time is not None and pullback_low_time in ma20_series.index and pd.notna(ma20_series.loc[pullback_low_time]) else None
    support_candidates = [value for value in [vwap_at_pullback, ma20_at_pullback, open_price] if value is not None]
    support_at_pullback = max(support_candidates) if support_candidates else None
    pullback_hold = pullback_low is not None and support_at_pullback is not None and pullback_low >= support_at_pullback * 0.995

    current_ma20 = float(ma20_series.iloc[-1]) if pd.notna(ma20_series.iloc[-1]) else None
    current_support_candidates = [value for value in [vwap, current_ma20, open_price] if value is not None]
    current_support = max(current_support_candidates) if current_support_candidates else None
    last_close_above_support = current_support is not None and last_price >= current_support * 0.998
    last_close_vs_support_pct = ((last_price - current_support) / current_support) if current_support else None
    post_breakout_bar_count = int(len(after_breakout)) if breakout_time is not None else 0

    intraday_signal_ok = bool(
        post_cutoff_bar_count >= 5
        and breakout_time is not None
        and pullback_hold
        and last_close_above_support
    )
    signal_reason = "breakout_then_hold_support" if intraday_signal_ok else "breakout_or_pullback_condition_not_met"

    snapshot = {
        "source": "yfinance_m1",
        "interval": M1_INTERVAL,
        "cutoff_time": cutoff_time,
        "snapshot_time": frame.index[-1].isoformat(),
        "first_bar_time": frame.index[0].isoformat(),
        "last_bar_time": frame.index[-1].isoformat(),
        "bar_count": int(len(frame)),
        "prev_close": prev_close,
        "last_price": last_price,
        "open_price": open_price,
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
        "volume_cum": volume_cum,
        "avg_volume_20d": avg_volume_20d,
        "volume_ratio": volume_ratio,
        "shares_outstanding": shares_outstanding,
        "turnover": turnover,
        "vwap": vwap,
        "m1_ma": m1_ma,
        "m1_ma20": current_ma20,
        "post_cutoff_bar_count": post_cutoff_bar_count,
        "post_cutoff_high": post_cutoff_high,
        "post_cutoff_low": post_cutoff_low,
        "pre_cutoff_high": pre_cutoff_high,
        "breakout_threshold": breakout_threshold,
        "breakout_time": breakout_time.isoformat() if breakout_time is not None else None,
        "breakout_price": breakout_price,
        "pullback_low_after_breakout": pullback_low,
        "pullback_low_time": pullback_low_time.isoformat() if pullback_low_time is not None else None,
        "vwap_at_pullback": vwap_at_pullback,
        "support_at_pullback": support_at_pullback,
        "current_support": current_support,
        "last_close_vs_support_pct": last_close_vs_support_pct,
        "post_breakout_bar_count": post_breakout_bar_count,
        "intraday_signal_ok": intraday_signal_ok,
        "signal_reason": signal_reason,
    }
    ok, errors, warnings = validate_m1_snapshot(snapshot, symbol)
    snapshot["quality_ok"] = ok
    snapshot["quality_errors"] = errors
    snapshot["quality_warnings"] = warnings
    return snapshot


def apply_legacy_daily_checks_for_2_3(
    result: IntradayResult,
    daily_df: pd.DataFrame,
    market_profile: Dict[str, Optional[float]],
) -> None:
    close = daily_df["Close"].dropna()
    lookback = THRESHOLDS["memory_surge_lookback"]
    recent_returns = close.pct_change().dropna().iloc[-lookback - 1:-1]
    max_surge = float(recent_returns.max()) if not recent_returns.empty else 0.0
    result.checks["② 近3日動能>7%"] = bool(max_surge > THRESHOLDS["memory_surge_min"])
    result.values["近3日最大漲幅"] = f"{max_surge * 100:.2f}%"

    market_cap = market_profile.get("market_cap")
    if market_cap is not None:
        cap_ok = market_cap < THRESHOLDS["market_cap_max"]
        result.checks["③ 市值<3000億"] = bool(cap_ok)
        result.values["市值(億)"] = round(market_cap / 1e8, 0)
    else:
        result.checks["③ 市值<3000億"] = None
        result.missing.append("市值")


def apply_m1_checks(
    result: IntradayResult,
    snapshot: Optional[Dict[str, Any]],
    daily_df: pd.DataFrame,
    market_profile: Dict[str, Optional[float]],
) -> None:
    apply_legacy_daily_checks_for_2_3(result, daily_df, market_profile)

    if not snapshot:
        for key in ["① 漲幅 3%-7%", "④ 量比>1.5", "⑤ 換手率 3%-10%", "⑥ 收在開盤上+近5日新高 (代理)"]:
            result.checks[key] = None
        result.missing.append("M1 快照")
        result.notes.append("①④⑤⑥ 無 M1 快照，未回退日線代理")
        return

    result.values["引擎版本"] = INTRADAY_ENGINE_VERSION
    result.values["snapshot_time"] = snapshot.get("snapshot_time")
    result.values["M1 bars"] = snapshot.get("bar_count")
    result.values["M1累計量"] = int(snapshot.get("volume_cum", 0)) if snapshot.get("volume_cum") is not None else None
    result.values["前收"] = round(snapshot["prev_close"], 2)
    result.values["現價"] = round(snapshot["last_price"], 2)

    daily_gain = (snapshot["last_price"] - snapshot["prev_close"]) / snapshot["prev_close"]
    result.checks["① 漲幅 3%-7%"] = bool(THRESHOLDS["daily_gain_min"] <= daily_gain <= THRESHOLDS["daily_gain_max"])
    result.values["當日漲幅"] = f"{daily_gain * 100:.2f}%"
    result.values["當日漲幅來源"] = "M1 快照"

    volume_ratio = snapshot.get("volume_ratio")
    if volume_ratio is None:
        result.checks["④ 量比>1.5"] = None
        result.missing.append("M1量比")
    else:
        result.checks["④ 量比>1.5"] = bool(volume_ratio > THRESHOLDS["volume_ratio_min"])
        result.values["量比"] = round(volume_ratio, 2)
        result.values["量比來源"] = "M1累計量/20日均量"

    turnover = snapshot.get("turnover")
    if turnover is None:
        result.checks["⑤ 換手率 3%-10%"] = None
        result.values["換手率來源"] = "M1 資料不足／缺流通股數（sharesOutstanding）"
        result.missing.append("M1換手率")
    else:
        result.checks["⑤ 換手率 3%-10%"] = bool(THRESHOLDS["turnover_min"] <= turnover <= THRESHOLDS["turnover_max"])
        result.values["換手率估"] = f"{turnover * 100:.2f}%"
        result.values["換手率來源"] = "M1累計量/sharesOutstanding"

    result.values["開盤"] = round(snapshot["open_price"], 2)
    result.values["今日高"] = round(snapshot["intraday_high"], 2)
    result.values["intraday_high"] = round(snapshot["intraday_high"], 2)
    result.values["post_cutoff_high"] = round(snapshot["post_cutoff_high"], 2) if snapshot.get("post_cutoff_high") is not None else None
    result.values["post_cutoff_low"] = round(snapshot["post_cutoff_low"], 2) if snapshot.get("post_cutoff_low") is not None else None
    result.values["vwap"] = round(snapshot["vwap"], 2) if snapshot.get("vwap") is not None else None
    result.values["m1_ma"] = round(snapshot["m1_ma"], 2) if snapshot.get("m1_ma") is not None else None
    result.values["m1_ma20"] = round(snapshot["m1_ma20"], 2) if snapshot.get("m1_ma20") is not None else None
    result.values["breakout_time"] = snapshot.get("breakout_time")
    result.values["breakout_price"] = round(snapshot["breakout_price"], 2) if snapshot.get("breakout_price") is not None else None
    result.values["pullback_low_after_breakout"] = round(snapshot["pullback_low_after_breakout"], 2) if snapshot.get("pullback_low_after_breakout") is not None else None
    result.values["pullback_low_time"] = snapshot.get("pullback_low_time")
    result.values["support_at_pullback"] = round(snapshot["support_at_pullback"], 2) if snapshot.get("support_at_pullback") is not None else None
    result.values["current_support"] = round(snapshot["current_support"], 2) if snapshot.get("current_support") is not None else None
    result.values["last_close_vs_support_pct"] = f"{snapshot['last_close_vs_support_pct'] * 100:.2f}%" if snapshot.get("last_close_vs_support_pct") is not None else None
    result.values["分時信號來源"] = "M1 快照"
    result.values["分時信號定義"] = "截點後有效突破，且突破後回踩不破動態支撐，最後仍站回支撐上方"
    result.values["分時信號理由"] = snapshot.get("signal_reason")

    if snapshot.get("post_cutoff_bar_count", 0) <= 0:
        result.checks["⑥ 收在開盤上+近5日新高 (代理)"] = None
        result.missing.append("M1 cutoff後資料不足")
    else:
        result.checks["⑥ 收在開盤上+近5日新高 (代理)"] = bool(snapshot.get("intraday_signal_ok"))

    result.notes.append("①④⑤⑥ 使用 M1 快照；②③ 仍使用日線/中繼資料")
    result.notes.append("⑥ 已使用 M1 事件序列規則；檢查欄位暫保留舊名稱，以相容重算與下游流程")
    for warning in snapshot.get("quality_warnings", []):
        result.notes.append(warning)
    for error in snapshot.get("quality_errors", []):
        result.missing.append(error)
    result.values["m1_snapshot"] = snapshot


def check_intraday(
    symbol: str,
    name: str,
    sector: str,
    daily_df: Optional[pd.DataFrame],
    market_profile: Dict[str, Optional[float]],
    m1_snapshot: Optional[Dict[str, Any]] = None,
) -> IntradayResult:
    result = IntradayResult(symbol=symbol, name=name, sector=sector)
    if daily_df is None or daily_df.empty:
        result.error = "無法取得日線資料"
        return result

    close = daily_df["Close"].dropna()
    if len(close) < 20:
        result.error = f"歷史資料不足 ({len(close)}/20)"
        return result

    apply_m1_checks(result, m1_snapshot, daily_df, market_profile)
    return recompute_result_flags(result)


def recompute_result_flags(result: IntradayResult) -> IntradayResult:
    true_count = sum(1 for v in result.checks.values() if v is True)
    false_count = sum(1 for v in result.checks.values() if v is False)
    unknown_count = sum(1 for v in result.checks.values() if v is None)
    core_ok = all(result.checks.get(k) is True for k in ["① 漲幅 3%-7%", "④ 量比>1.5"])
    hard_fails = [k for k, v in result.checks.items() if v is False and k != "⑥ 收在開盤上+近5日新高 (代理)"]
    strict_hard_fails = [k for k in hard_fails if k != "③ 市值<3000億"]
    soft_fail_6 = result.checks.get("⑥ 收在開盤上+近5日新高 (代理)") is False
    score = true_count * 10 - false_count * 5 - unknown_count * 2

    result.values["通過數"] = true_count
    result.values["未通過數"] = false_count
    result.values["無資料"] = unknown_count
    result.values["score"] = score
    result.values["core_ok"] = core_ok
    result.values["hard_fails"] = hard_fails
    result.values["strict_hard_fails"] = strict_hard_fails
    result.strict_passed = core_ok and len(strict_hard_fails) == 0 and unknown_count <= 1 and (soft_fail_6 or false_count == 0)
    result.passed = core_ok and len(hard_fails) <= 1 and unknown_count <= 1 and (soft_fail_6 or false_count <= 1)
    return result


def generate_report(today: date, results: List[IntradayResult]) -> str:
    passed = sorted(
        [r for r in results if r.passed],
        key=lambda r: (r.values.get("score", 0), r.values.get("通過數", 0), -r.values.get("無資料", 0)),
        reverse=True,
    )
    strict = [r for r in passed if r.strict_passed]
    near = sorted(
        [r for r in results if not r.passed and not r.error and r.values.get("core_ok")],
        key=lambda r: (r.values.get("score", 0), r.values.get("通過數", 0), -r.values.get("無資料", 0)),
        reverse=True,
    )[:5]

    lines = [
        f"# 🎯 日內選股W 盤中快報 {today}",
        "",
        f"**掃描範圍**: {len(results)} 檔",
        f"**主池通過**: {len(passed)} 檔",
        f"**嚴格六項全過**: {len(strict)} 檔",
        f"**接近通過（核心條件已過）**: {len(near)} 檔",
        "",
        "## ✅ 日內池候選（當日 14:30-14:50 進場）",
    ]
    if passed:
        for r in passed[:MAX_PICKS]:
            lines.append(f"### {format_name_with_code(r.name, r.symbol)} [{r.sector}]")
            for k in ("當日漲幅", "近3日最大漲幅", "市值(億)", "量比", "換手率估", "換手率來源", "score", "snapshot_time", "breakout_time", "分時信號理由"):
                if k in r.values:
                    lines.append(f"- {display_value_label(k)}: {r.values[k]}")
            lines.append(f"- 嚴格六項全過: {'是' if r.strict_passed else '否'}")
            if r.values.get("hard_fails"):
                lines.append(f"- 非核心未過: {r.values['hard_fails']}")
            lines.append("")
    else:
        lines.append("_今日無主池通過個股_")

    if near:
        lines.extend(["", "## 🟡 接近通過（參考）"])
        for r in near:
            miss = [k for k, v in r.checks.items() if v is False]
            unknown = [k for k, v in r.checks.items() if v is None]
            lines.append(
                f"- {format_name_with_code(r.name, r.symbol)}: 分數 {r.values.get('score')}，未過 {miss or ['無']}，缺資料 {unknown or ['無']}"
            )

    lines.extend([
        "",
        "---",
        "",
        "📌 **日內池紀律口號**：「放棄思考，執行紀律」",
        "- 進場：14:30-14:50 收盤價",
        "- 離場：**隔日 09:30 前集合競價出清，零例外**",
        "- 倉位：單筆 ≤ 5%",
        "- 新手建議先模擬盤熟悉",
        "",
        "⚠️ **①④⑤⑥ 使用 M1 快照；② 近3日動能、③ 市值仍使用日線/中繼資料；⑥ 已升級為突破後回踩事件序列判斷**",
    ])
    return "\n".join(lines)


def push_notifications(report: str, dry_run: bool = False) -> None:
    if dry_run:
        logger.info("[試跑] 不推送，報告長度 %d 字", len(report))
        return

    try:
        lark = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        chat_id = os.getenv("FEISHU_DEFAULT_CHAT_ID")
        title = "🎯 日內選股 W 盤中快報"
        if chat_id:
            lark.send_message(chat_id, title, report, receive_id_type="chat_id")
            logger.info("✓ 飛書已推送（群組 chat_id）")
        else:
            logger.warning("飛書群組目標未設定（FEISHU_DEFAULT_CHAT_ID）")
    except Exception as e:
        logger.error("飛書推送失敗: %s", e)

    if HAS_WECHAT:
        try:
            wechat = WechatNotifier()
            target = os.getenv("WECHAT_PUSH_TO_USER")
            ok = wechat.send_message(report, to_user=target, typing_seconds=3)
            if ok:
                logger.info("✓ 微信已推送")
        except Exception as e:
            logger.error("微信推送失敗: %s", e)


def _build_snapshot_payload(today: date, universe_size: int, results: List[IntradayResult], source: str) -> Dict[str, Any]:
    return {
        "date": today.isoformat(),
        "source": source,
        "engine_version": INTRADAY_ENGINE_VERSION if source == "m1_snapshot" else source,
        "universe_size": universe_size,
        "passed_count": sum(1 for r in results if r.passed),
        "strict_passed_count": sum(1 for r in results if r.strict_passed),
        "results": [asdict(r) for r in results],
    }


def cmd_scan(dry_run: bool = False, universe_source: str = "yaml", limit: Optional[int] = None) -> None:
    today = date.today()
    logger.info("=== 日內選股掃描 %s ===", today)

    fm = FinMindHelper() if HAS_FINMIND else None
    universe = load_universe(source=universe_source, limit=limit, fm=fm)
    logger.info("候選池: %d 檔", len(universe))

    results: List[IntradayResult] = []
    for i, stock in enumerate(universe, 1):
        sym = stock["symbol"]
        if sym.startswith("^"):
            continue
        logger.info("[%d/%d] %s", i, len(universe), sym)
        daily = fetch_daily_history(sym, days=90)
        market_profile = fetch_market_profile(sym)
        minute_raw = fetch_m1_history(sym)
        minute_df = prepare_m1_frame(minute_raw)
        snapshot = None
        if minute_df is not None:
            effective_cutoff = _derive_effective_cutoff_time(minute_df.index, M1_CUTOFF_TIME)
            fresh_ok, fresh_error = validate_m1_data_freshness(minute_df, sym)
            complete_ok, complete_error = validate_m1_data_completeness(minute_df, sym, cutoff_time=effective_cutoff)
            if fresh_ok and complete_ok:
                snapshot = build_m1_snapshot(sym, minute_df, daily, market_profile, cutoff_time=effective_cutoff)
            else:
                logger.info("%s M1 驗證未過: %s / %s", sym, fresh_error, complete_error)
        result = check_intraday(sym, stock.get("name", ""), stock.get("sector", ""), daily, market_profile, m1_snapshot=snapshot)
        if snapshot is None:
            result.notes.append("M1 驗證未通過或資料不足，未回退日線代理")
        results.append(result)

    report = generate_report(today, results)
    md_path = LOG_DIR / f"{today.isoformat()}.md"
    json_path = LOG_DIR / f"{today.isoformat()}.json"
    md_path.write_text(report, encoding="utf-8")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(_build_snapshot_payload(today, len(universe), results, "m1_snapshot"), f, ensure_ascii=False, indent=2, default=str)
    logger.info("報告存檔: %s", md_path)

    push_notifications(report, dry_run=dry_run)


def cmd_single(symbol: str) -> None:
    daily = fetch_daily_history(symbol, days=90)
    market_profile = fetch_market_profile(symbol)
    minute_df = prepare_m1_frame(fetch_m1_history(symbol))
    effective_cutoff = _derive_effective_cutoff_time(minute_df.index, M1_CUTOFF_TIME) if minute_df is not None else M1_CUTOFF_TIME
    snapshot = build_m1_snapshot(symbol, minute_df, daily, market_profile, cutoff_time=effective_cutoff) if minute_df is not None else None
    result = check_intraday(symbol, symbol, "?", daily, market_profile, m1_snapshot=snapshot)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))


def result_from_snapshot(data: Dict[str, Any]) -> IntradayResult:
    return IntradayResult(
        symbol=data.get("symbol", ""),
        name=data.get("name", ""),
        sector=data.get("sector", ""),
        passed=data.get("passed", False),
        strict_passed=data.get("strict_passed", False),
        checks=data.get("checks", {}),
        values=data.get("values", {}),
        missing=data.get("missing", []),
        notes=data.get("notes", []),
        error=data.get("error"),
    )


def cmd_recompute_snapshot(snapshot_path: str) -> None:
    path = Path(snapshot_path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    source = data.get("source", "recomputed_snapshot")
    engine_version = data.get("engine_version")
    results = []
    for raw in data.get("results", []):
        result = result_from_snapshot(raw)
        if engine_version == INTRADAY_ENGINE_VERSION or result.values.get("m1_snapshot"):
            result.notes = [note for note in result.notes if "⑥ 為日線代理" not in note]
            result.notes.append("快照重算：保留既有 M1 派生值後重算旗標")
        result = recompute_result_flags(result)
        results.append(result)

    payload = {
        "date": data.get("date"),
        "source": source,
        "engine_version": engine_version or source,
        "passed_count": sum(1 for r in results if r.passed),
        "strict_passed_count": sum(1 for r in results if r.strict_passed),
        "results": [asdict(r) for r in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="日內動能選股 (池 ④)")
    parser.add_argument("--dry-run", action="store_true", help="不推播")
    parser.add_argument("--symbol", help="單檔檢查")
    parser.add_argument("--universe", default="yaml", choices=["yaml", "full"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--recompute-snapshot", help="用目前規則重算既有 intraday snapshot")
    args = parser.parse_args()

    if args.recompute_snapshot:
        cmd_recompute_snapshot(args.recompute_snapshot)
    elif args.symbol:
        cmd_single(args.symbol)
    else:
        cmd_scan(dry_run=args.dry_run, universe_source=args.universe, limit=args.limit)


if __name__ == "__main__":
    main()
