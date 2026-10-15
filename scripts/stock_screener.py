#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_screener.py — 短線選股子代理（股市 W 之分支）

責任：
1. 每日盤後掃描 150–200 檔高流動候選池
2. 雙引擎評分：量價突破（右側）+ 均值回歸（左側）
3. 結合三大法人 × 市場情緒 × 國際風險雷達 三維過濾
4. 輸出 top 10 + 10 候選清單，並用 Claude 產生紀律敘事
5. 週五復盤：勝率、盈虧比、α/β 歸因

用法：
    python3 scripts/stock_screener.py                  # 盤後掃描
    python3 scripts/stock_screener.py --dry-run        # 不推播
    python3 scripts/stock_screener.py --symbol 2330.TW # 單檔評分除錯
    python3 scripts/stock_screener.py --debug-sentiment
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 清除代理
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import numpy as np
import pandas as pd
import yaml

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

from utils.claude_helper import ClaudeHelper
from utils.config_loader import load_config
from utils.lark_notifier import LarkNotifier
from utils.master_agent_control import get_current_control, write_payload
from scripts.daily_stock_report import build_tracking_bucket_snapshot, render_tracking_bucket_digest, render_watchlist_pool_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_BASE_DIR / "logs" / "stock_screener.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# 壓制 yfinance 內部 logger：rate limit/下載失敗時它會自噴大量
# `[ERROR] ['<sym>']: TypeError("'NoneType' object is not subscriptable")` 污染 orchestrator.log
# （即使本層 fetch_history 已 graceful 回 None）。失敗統計改由本模組以 debug/彙總自行掌握。
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ──────────────────────────────────────────────────────────────────────────────
# 資料結構
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class StockCandidate:
    symbol: str
    name: str
    sector: str
    price: float
    score: int
    breakdown: Dict[str, int]
    entry_zone: Tuple[float, float]
    stop_loss: float
    take_profit: float
    time_stop: str
    engine: str  # "momentum" | "rebound"
    inst_signal: str  # "強買" | "偏多" | "中性" | "偏空"
    event_warning: str = ""
    catalyst_note: str = ""


@dataclass
class ScreenerState:
    date: str
    sentiment_score: int
    regime: str
    risk_radar: Dict[str, Any]
    momentum_picks: List[StockCandidate]
    rebound_picks: List[StockCandidate]
    sector_concentration: Dict[str, float]
    warnings: List[str]


# ──────────────────────────────────────────────────────────────────────────────
# 設定載入
# ──────────────────────────────────────────────────────────────────────────────


def load_universe() -> List[Dict[str, str]]:
    """載入候選池。"""
    path = _BASE_DIR / "config" / "stock_universe.yaml"
    cfg = load_config(str(path))
    return cfg.get("universe", [])


def load_rules() -> Dict[str, Any]:
    """載入篩選規則。"""
    path = _BASE_DIR / "config" / "screener_rules.yaml"
    return load_config(str(path))


def load_risk_radar() -> Dict[str, Any]:
    """載入 News W 產出的風險雷達（若存在）。"""
    path = _BASE_DIR / "logs" / "risk_radar.json"
    if not path.exists():
        return {"international": [], "sector_risks": {}, "blacklist": [], "sentiment_hint": "neutral"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("無法讀取 risk_radar.json: %s", e)
        return {"international": [], "sector_risks": {}, "blacklist": [], "sentiment_hint": "neutral"}


REGIME_LABELS = {
    "extreme_fear": "極度恐慌",
    "fear": "恐慌",
    "neutral": "中性",
    "greed": "貪婪",
    "extreme_greed": "極度貪婪",
}

BREAKDOWN_LABELS = {
    "ret_5d": "5日漲跌",
    "break_20dh": "突破20日高點",
    "inst_buy": "法人買超",
    "vol_ratio": "量比",
    "rsi_5": "短RSI",
    "macd_hist": "MACD 柱狀體",
    "drawdown_20d": "20日回撤",
    "support_distance": "支撐距離",
    "inst_sell_absorb": "法人賣壓吸收",
}


def regime_label(regime: str) -> str:
    return REGIME_LABELS.get(regime, regime)


def breakdown_label(key: str) -> str:
    return BREAKDOWN_LABELS.get(key, key)


def load_institutional_snapshot() -> Dict[str, Dict[str, Any]]:
    path = _BASE_DIR / "logs" / "institutional_snapshots" / "current.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("stocks") or {}
    except Exception as e:
        logger.warning("無法讀取法人 snapshot: %s", e)
        return {}


def compute_event_overlay(
    symbol: str,
    sector: str,
    inst: Optional[Dict[str, Any]],
    risk_radar: Dict[str, Any],
    rules: Dict[str, Any],
    engine: str,
) -> Tuple[int, str, str]:
    overlay_cfg = (rules.get("event_overlay") or {})
    if not overlay_cfg.get("enabled", True):
        return 0, "", ""

    engine_cfg = overlay_cfg.get(engine, {})
    total_net = (inst or {}).get("total_net", 0)
    sentiment_hint = str(risk_radar.get("sentiment_hint") or "neutral")
    sector_risks = risk_radar.get("sector_risks") or {}
    sector_text = str(sector or "")
    event_warning = ""
    catalyst_note_parts: List[str] = []
    bonus = 0

    if sector_text and sector_text in sector_risks:
        penalty = int(engine_cfg.get("sector_risk_penalty", 0) or 0)
        bonus -= penalty
        event_warning = f"產業風險：{sector_risks.get(sector_text)}"
        catalyst_note_parts.append(f"產業風險 -{penalty}")

    if total_net >= 3000:
        catalyst_bonus = int(engine_cfg.get("catalyst_bonus", 0) or 0)
        if catalyst_bonus:
            bonus += catalyst_bonus
            catalyst_note_parts.append(f"法人強買 +{catalyst_bonus}")
    elif total_net <= -3000 and engine == "momentum":
        penalty = int(engine_cfg.get("catalyst_bonus", 0) or 0)
        if penalty:
            bonus -= penalty
            catalyst_note_parts.append(f"法人偏空 -{penalty}")

    if sentiment_hint == "fear":
        fear_bonus = int(engine_cfg.get("fear_bonus", 0) or 0)
        if fear_bonus:
            bonus += fear_bonus
            catalyst_note_parts.append(f"情緒恐慌 +{fear_bonus}")
    elif sentiment_hint == "greed":
        greed_penalty = int(engine_cfg.get("greed_penalty", 0) or 0)
        if greed_penalty:
            bonus -= greed_penalty
            catalyst_note_parts.append(f"情緒貪婪 -{greed_penalty}")

    return bonus, event_warning, "；".join(catalyst_note_parts)


# ──────────────────────────────────────────────────────────────────────────────
# 行情抓取（重用 strategy_scanner 邏輯）
# ──────────────────────────────────────────────────────────────────────────────


# yfinance 抓取韌性：rate limit / 暫時性下載失敗時退避重試，最終仍失敗則回 None
# graceful skip（不拋、不中斷整批掃描）。盤後逐檔密集抓最易踩 Yahoo 限流。
_YF_FETCH_RETRIES = 2        # download 空/例外時額外重試次數
_YF_FETCH_BACKOFF = 1.5      # 退避基數秒（遞增：1.5s, 3.0s）


def fetch_history(symbol: str, days: int = 60) -> Optional[pd.DataFrame]:
    """拉取歷史日線資料（rate-limit 退避重試；最終失敗回 None 不拋）。"""
    import yfinance as yf

    last_err: Any = None
    for attempt in range(_YF_FETCH_RETRIES + 1):
        try:
            df = yf.download(
                symbol,
                period=f"{days}d",
                interval="1d",
                auto_adjust=True,
                progress=False,
            )
            if df is not None and not df.empty and len(df) >= 20:
                df.index = pd.to_datetime(df.index)
                # 處理 MultiIndex columns
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0].lower() for c in df.columns]
                else:
                    df.columns = [c.lower() for c in df.columns]
                return df
            last_err = "empty_or_insufficient"
        except Exception as e:  # 抓取失敗一律退避重試，不讓單檔中斷整批掃描
            last_err = e
        if attempt < _YF_FETCH_RETRIES:
            time.sleep(_YF_FETCH_BACKOFF * (attempt + 1))
    logger.debug("fetch_history %s 最終失敗（試 %d 次）: %s", symbol, _YF_FETCH_RETRIES + 1, last_err)
    return None


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """計算技術指標。"""
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["vol20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol20"]

    # RSI(5)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(5).mean()
    loss = (-delta.clip(upper=0)).rolling(5).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi5"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # 乖離率
    df["bias20"] = (df["close"] - df["ma20"]) / df["ma20"] * 100

    # ATR%
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr20"] = tr.rolling(20).mean()
    df["atr_pct"] = df["atr20"] / df["close"] * 100

    # 近 5 日報酬
    df["ret5d"] = df["close"].pct_change(5) * 100

    # 20 日高點
    df["high20"] = df["high"].rolling(20).max()

    return df


def batch_fetch_quotes(symbols: List[str]) -> Dict[str, pd.DataFrame]:
    """批次抓取多檔歷史資料（單線程，yfinance 自帶快取）。"""
    result = {}
    for sym in symbols:
        df = fetch_history(sym, days=70)
        if df is not None and len(df) >= 20:
            result[sym] = add_indicators(df)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 法人資料（重用 stock_monitor 邏輯）
# ──────────────────────────────────────────────────────────────────────────────


def _fetch_single_stock_inst(dl: Any, symbol: str, first_day: str) -> Optional[Tuple[str, Dict]]:
    """抓單檔個股三大法人。"""
    stock_id = symbol.replace(".TW", "").replace(".TWO", "")
    try:
        df = dl.taiwan_stock_institutional_investors(
            stock_id=stock_id,
            start_date=first_day,
        )
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")

        # 取近 5 日
        recent = df.tail(5)
        foreign_buy = recent[recent["name"] == "Foreign_Investor"]["buy"].sum()
        foreign_sell = recent[recent["name"] == "Foreign_Investor"]["sell"].sum()
        trust_buy = recent[recent["name"] == "Investment_Trust"]["buy"].sum()
        trust_sell = recent[recent["name"] == "Investment_Trust"]["sell"].sum()
        dealer_buy = recent[recent["name"] == "Dealer_self"]["buy"].sum()
        dealer_sell = recent[recent["name"] == "Dealer_self"]["sell"].sum()

        return (symbol, {
            "foreign_net": foreign_buy - foreign_sell,
            "trust_net": trust_buy - trust_sell,
            "dealer_net": dealer_buy - dealer_sell,
            "total_net": (foreign_buy - foreign_sell) + (trust_buy - trust_sell) + (dealer_buy - dealer_sell),
        })
    except Exception as e:
        logger.debug("FinMind %s failed: %s", symbol, e)
        return None


def fetch_institutional_data(symbols: List[str]) -> Dict[str, Dict]:
    """並發抓取個股三大法人。"""
    try:
        from FinMind.data import DataLoader

        dl = DataLoader()
        token = os.getenv("FINMIND_TOKEN")
        if token:
            dl.login_by_token(api_token=token)

        first_day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        tw_stocks = [s for s in symbols if s.endswith(".TW") or s.endswith(".TWO")]
        inst_results: Dict[str, Dict] = {}

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(_fetch_single_stock_inst, dl, sym, first_day): sym
                for sym in tw_stocks
            }
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    inst_results[result[0]] = result[1]

        return inst_results
    except Exception as e:
        logger.warning("FinMind fetch failed: %s", e)
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# 情緒分數（VIX / DXY / TWD / TNX / 大盤量能）
# ──────────────────────────────────────────────────────────────────────────────


def compute_sentiment_score(rules: Dict[str, Any]) -> Tuple[int, str]:
    """
    計算市場情緒綜合分（0–100，高=恐慌）。
    """
    import yfinance as yf

    components = rules.get("sentiment", {}).get("components", {})
    bands = rules.get("sentiment", {}).get("bands", {})

    scores = {}

    # VIX
    try:
        vix = yf.download("^VIX", period="5d", progress=False)
        if not vix.empty:
            vix_val = vix["Close"].iloc[-1]
            # VIX 正規化：12=0, 30=100
            scores["vix"] = min(100, max(0, (vix_val - 12) / 18 * 100))
    except Exception:
        scores["vix"] = 50

    # DXY 變化（美元走強 = 風險偏空）
    try:
        dxy = yf.download("DX-Y.NYB", period="10d", progress=False)
        if len(dxy) >= 2:
            chg = (dxy["Close"].iloc[-1] / dxy["Close"].iloc[-5] - 1) * 100
            # +2% = 100 分（恐慌），-2% = 0 分
            scores["dxy_change"] = min(100, max(0, (chg + 2) / 4 * 100))
    except Exception:
        scores["dxy_change"] = 50

    # TWD 變化（台幣貶值 = 外資流出）
    try:
        twd = yf.download("TWD=X", period="10d", progress=False)
        if len(twd) >= 2:
            chg = (twd["Close"].iloc[-1] / twd["Close"].iloc[-5] - 1) * 100
            # TWD=X 是 1 USD = X TWD，台幣貶值 = 數值上升
            scores["twd_change"] = min(100, max(0, chg / 2 * 100 + 50))
    except Exception:
        scores["twd_change"] = 50

    # TNX（10Y 殖利率）
    try:
        tnx = yf.download("^TNX", period="10d", progress=False)
        if len(tnx) >= 2:
            chg = tnx["Close"].iloc[-1] - tnx["Close"].iloc[-5]
            # +0.2 = 恐慌 100, -0.2 = 貪婪 0
            scores["tnx_change"] = min(100, max(0, (chg + 0.2) / 0.4 * 100))
    except Exception:
        scores["tnx_change"] = 50

    # 大盤量能（^TWII）
    try:
        twii = yf.download("^TWII", period="30d", progress=False)
        if len(twii) >= 20:
            vol_now = twii["Volume"].iloc[-1]
            vol_avg = twii["Volume"].tail(20).mean()
            ratio = vol_now / vol_avg if vol_avg > 0 else 1
            # 量縮 < 0.8 → 恐慌 80，量增 > 1.5 → 貪婪 20
            scores["twii_vol_ratio"] = min(100, max(0, (1.5 - ratio) / 0.7 * 80))
    except Exception:
        scores["twii_vol_ratio"] = 50

    # 融資變化（put_call / margin_change 暫用固定值，未來可接 TWSE API）
    scores["put_call"] = 50
    scores["margin_change"] = 50

    # 加權平均
    total = 0.0
    weight_sum = 0.0
    for key, weight in components.items():
        if key in scores:
            total += scores[key] * weight
            weight_sum += weight

    final_score = int(total / weight_sum) if weight_sum > 0 else 50

    # 判斷 regime
    if final_score >= bands.get("extreme_fear", 80):
        regime = "extreme_fear"
    elif final_score >= bands.get("fear", 60):
        regime = "fear"
    elif final_score >= bands.get("neutral", 40):
        regime = "neutral"
    elif final_score >= bands.get("greed", 20):
        regime = "greed"
    else:
        regime = "extreme_greed"

    logger.info("情緒分數: %d（%s）| 明細: %s", final_score, regime_label(regime), scores)
    return final_score, regime


# ──────────────────────────────────────────────────────────────────────────────
# 動能評分
# ──────────────────────────────────────────────────────────────────────────────


def compute_momentum_score(
    df: pd.DataFrame,
    inst: Optional[Dict],
    weights: Dict[str, int],
) -> Tuple[int, Dict[str, int]]:
    """量價突破引擎評分（總分 100）。"""
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    breakdown: Dict[str, int] = {}

    # ret_5d（20 分）
    ret5d = last.get("ret5d", 0)
    if pd.isna(ret5d):
        ret5d = 0
    if ret5d >= 10:
        breakdown["ret_5d"] = weights.get("ret_5d", 20)
    elif ret5d >= 5:
        breakdown["ret_5d"] = int(weights.get("ret_5d", 20) * 0.7)
    elif ret5d >= 2:
        breakdown["ret_5d"] = int(weights.get("ret_5d", 20) * 0.4)
    else:
        breakdown["ret_5d"] = 0

    # vol_ratio（15 分）
    vol_ratio = last.get("vol_ratio", 0)
    if pd.isna(vol_ratio):
        vol_ratio = 0
    if vol_ratio >= 2.0:
        breakdown["vol_ratio"] = weights.get("vol_ratio", 15)
    elif vol_ratio >= 1.5:
        breakdown["vol_ratio"] = int(weights.get("vol_ratio", 15) * 0.7)
    elif vol_ratio >= 1.2:
        breakdown["vol_ratio"] = int(weights.get("vol_ratio", 15) * 0.4)
    else:
        breakdown["vol_ratio"] = 0

    # break_20dh（15 分）
    close = last.get("close", 0)
    high20 = last.get("high20", 0)
    if close >= high20 * 0.995:  # 0.5% 容錯
        breakdown["break_20dh"] = weights.get("break_20dh", 15)
    else:
        breakdown["break_20dh"] = 0

    # ma_stack（10 分）
    ma5, ma10, ma20 = last.get("ma5", 0), last.get("ma10", 0), last.get("ma20", 0)
    if ma5 > ma10 > ma20:
        breakdown["ma_stack"] = weights.get("ma_stack", 10)
    elif ma5 > ma10:
        breakdown["ma_stack"] = int(weights.get("ma_stack", 10) * 0.5)
    else:
        breakdown["ma_stack"] = 0

    # rsi_5（10 分）
    rsi5 = last.get("rsi5", 50)
    if pd.isna(rsi5):
        rsi5 = 50
    if 55 <= rsi5 <= 80:
        breakdown["rsi_5"] = weights.get("rsi_5", 10)
    elif 50 <= rsi5 < 55 or 80 < rsi5 <= 85:
        breakdown["rsi_5"] = int(weights.get("rsi_5", 10) * 0.5)
    else:
        breakdown["rsi_5"] = 0

    # macd_hist（10 分）
    macd_hist = last.get("macd_hist", 0)
    prev_macd_hist = prev.get("macd_hist", 0)
    if pd.isna(macd_hist):
        macd_hist = 0
    if pd.isna(prev_macd_hist):
        prev_macd_hist = 0
    if macd_hist > 0 and macd_hist > prev_macd_hist:
        breakdown["macd_hist"] = weights.get("macd_hist", 10)
    elif macd_hist > 0:
        breakdown["macd_hist"] = int(weights.get("macd_hist", 10) * 0.5)
    else:
        breakdown["macd_hist"] = 0

    # inst_buy（15 分）
    if inst:
        total_net = inst.get("total_net", 0)
        if total_net >= 3000:
            breakdown["inst_buy"] = weights.get("inst_buy", 15)
        elif total_net >= 1000:
            breakdown["inst_buy"] = int(weights.get("inst_buy", 15) * 0.7)
        elif total_net > 0:
            breakdown["inst_buy"] = int(weights.get("inst_buy", 15) * 0.3)
        else:
            breakdown["inst_buy"] = 0
    else:
        breakdown["inst_buy"] = 0

    # sector_rps（5 分）— 暫時給固定分
    breakdown["sector_rps"] = 3

    total = sum(breakdown.values())
    return total, breakdown


# ──────────────────────────────────────────────────────────────────────────────
# 反彈評分
# ──────────────────────────────────────────────────────────────────────────────


def compute_rebound_score(
    df: pd.DataFrame,
    inst: Optional[Dict],
    weights: Dict[str, int],
    sentiment_score: int,
) -> Tuple[int, Dict[str, int]]:
    """均值回歸引擎評分（總分 100）。"""
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    breakdown: Dict[str, int] = {}

    # neg_bias_20（20 分）
    bias20 = last.get("bias20", 0)
    if pd.isna(bias20):
        bias20 = 0
    if bias20 <= -10:
        breakdown["neg_bias_20"] = weights.get("neg_bias_20", 20)
    elif bias20 <= -8:
        breakdown["neg_bias_20"] = int(weights.get("neg_bias_20", 20) * 0.7)
    elif bias20 <= -5:
        breakdown["neg_bias_20"] = int(weights.get("neg_bias_20", 20) * 0.4)
    else:
        breakdown["neg_bias_20"] = 0

    # rsi_5_oversold（15 分）
    rsi5 = last.get("rsi5", 50)
    prev_rsi5 = prev.get("rsi5", 50)
    if pd.isna(rsi5):
        rsi5 = 50
    if pd.isna(prev_rsi5):
        prev_rsi5 = 50
    if rsi5 < 30 and rsi5 > prev_rsi5:  # 超賣且上翹
        breakdown["rsi_5_oversold"] = weights.get("rsi_5_oversold", 15)
    elif rsi5 < 35:
        breakdown["rsi_5_oversold"] = int(weights.get("rsi_5_oversold", 15) * 0.5)
    else:
        breakdown["rsi_5_oversold"] = 0

    # bullish_reversal（15 分）— 長下影
    high, low, close, open_ = last.get("high", 0), last.get("low", 0), last.get("close", 0), last.get("open", 0)
    body = abs(close - open_)
    lower_shadow = min(close, open_) - low
    if body > 0 and lower_shadow / body >= 2:
        breakdown["bullish_reversal"] = weights.get("bullish_reversal", 15)
    elif lower_shadow > body:
        breakdown["bullish_reversal"] = int(weights.get("bullish_reversal", 15) * 0.5)
    else:
        breakdown["bullish_reversal"] = 0

    # vol_shrink_then_up（10 分）
    vol_ratio = last.get("vol_ratio", 1)
    prev_vol_ratio = prev.get("vol_ratio", 1) if len(df) >= 2 else 1
    if pd.isna(vol_ratio):
        vol_ratio = 1
    if pd.isna(prev_vol_ratio):
        prev_vol_ratio = 1
    if prev_vol_ratio < 0.7 and vol_ratio >= 1.2:
        breakdown["vol_shrink_then_up"] = weights.get("vol_shrink_then_up", 10)
    elif vol_ratio >= 1.0:
        breakdown["vol_shrink_then_up"] = int(weights.get("vol_shrink_then_up", 10) * 0.3)
    else:
        breakdown["vol_shrink_then_up"] = 0

    # inst_stop_selling（15 分）
    if inst:
        total_net = inst.get("total_net", 0)
        if total_net > 0:  # 轉買
            breakdown["inst_stop_selling"] = weights.get("inst_stop_selling", 15)
        elif total_net > -500:
            breakdown["inst_stop_selling"] = int(weights.get("inst_stop_selling", 15) * 0.5)
        else:
            breakdown["inst_stop_selling"] = 0
    else:
        breakdown["inst_stop_selling"] = 0

    # distance_to_ma60（10 分）
    ma60 = last.get("ma60", 0)
    close = last.get("close", 0)
    if pd.isna(ma60) or ma60 == 0:
        breakdown["distance_to_ma60"] = 5  # 中性
    elif close >= ma60:
        breakdown["distance_to_ma60"] = weights.get("distance_to_ma60", 10)
    elif close >= ma60 * 0.95:
        breakdown["distance_to_ma60"] = int(weights.get("distance_to_ma60", 10) * 0.5)
    else:
        breakdown["distance_to_ma60"] = 0

    # support_bounce（10 分）— 暫給固定分
    breakdown["support_bounce"] = 5

    # sentiment_bonus（5 分）
    if sentiment_score >= 70:
        breakdown["sentiment_bonus"] = weights.get("sentiment_bonus", 5)
    elif sentiment_score >= 60:
        breakdown["sentiment_bonus"] = int(weights.get("sentiment_bonus", 5) * 0.5)
    else:
        breakdown["sentiment_bonus"] = 0

    total = sum(breakdown.values())
    return total, breakdown


# ──────────────────────────────────────────────────────────────────────────────
# 法人訊號標籤
# ──────────────────────────────────────────────────────────────────────────────


def classify_inst_signal(inst: Optional[Dict]) -> str:
    """將法人資料轉為可讀標籤。"""
    if not inst:
        return "無資料"
    total = inst.get("total_net", 0)
    if total >= 3000:
        return "強買"
    elif total >= 1000:
        return "偏多"
    elif total >= -1000:
        return "中性"
    elif total >= -3000:
        return "偏空"
    else:
        return "強賣"


def apply_event_overlay(
    base_score: int,
    symbol: str,
    sector: str,
    inst: Optional[Dict[str, Any]],
    risk_radar: Dict[str, Any],
    rules: Dict[str, Any],
    engine: str,
) -> Tuple[int, str, str]:
    bonus, event_warning, catalyst_note = compute_event_overlay(symbol, sector, inst, risk_radar, rules, engine)
    final_score = max(0, min(100, base_score + bonus))
    return final_score, event_warning, catalyst_note


# ──────────────────────────────────────────────────────────────────────────────
# 相關性與集中度檢查
# ──────────────────────────────────────────────────────────────────────────────


def check_sector_concentration(picks: List[StockCandidate], max_ratio: float = 0.4) -> Tuple[Dict[str, float], List[str]]:
    """檢查類股集中度。"""
    if not picks:
        return {}, []

    sector_counts: Dict[str, int] = {}
    for p in picks:
        sector_counts[p.sector] = sector_counts.get(p.sector, 0) + 1

    total = len(picks)
    concentration = {k: v / total for k, v in sector_counts.items()}
    warnings = [f"類股「{k}」占比 {v:.0%} 超過 {max_ratio:.0%}" for k, v in concentration.items() if v > max_ratio]

    return concentration, warnings


# ──────────────────────────────────────────────────────────────────────────────
# Claude 敘事
# ──────────────────────────────────────────────────────────────────────────────


def format_name_with_code(name: str, symbol: str) -> str:
    suffix = f"（{symbol.replace('.TW', '').replace('.TWO', '').lstrip('^')}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


def build_narrative(state: ScreenerState, rules: Dict[str, Any]) -> str:
    """用 Claude 產生當日選股敘事。"""
    claude = ClaudeHelper()
    now_str = datetime.now().strftime("%Y-%m-%d")

    # 組合候選摘要
    momentum_summary = "\n".join([
        f"- {format_name_with_code(p.name, p.symbol)}（{p.sector}）: 分數 {p.score}，法人 {p.inst_signal}" + (f"，事件 {p.catalyst_note}" if p.catalyst_note else "") + (f"，警示 {p.event_warning}" if p.event_warning else "")
        for p in state.momentum_picks[:5]
    ]) or "（無）"

    rebound_summary = "\n".join([
        f"- {format_name_with_code(p.name, p.symbol)}（{p.sector}）: 分數 {p.score}，法人 {p.inst_signal}" + (f"，事件 {p.catalyst_note}" if p.catalyst_note else "") + (f"，警示 {p.event_warning}" if p.event_warning else "")
        for p in state.rebound_picks[:5]
    ]) or "（無）"

    risk_text = ", ".join(state.risk_radar.get("international", [])) or "無重大風險"
    warnings_text = "\n".join(state.warnings) or "無"

    prompt = f"""你是短線股票分析師，今天（{now_str}）的市場情緒分數為 {state.sentiment_score}（{regime_label(state.regime)}）。

【國際風險】{risk_text}
【量價突破前五名】
{momentum_summary}

【均值回歸前五名】
{rebound_summary}

【風險警示】
{warnings_text}

請撰寫一份 300–500 字的「當日短線選股紀律提醒」：
1. 根據情緒狀態提醒應該積極或保守
2. 如有國際風險，提醒可能影響的類股
3. 針對上榜股票，提醒執行紀律（停損、停利、時間停損）
4. 如類股過度集中，提醒分散風險
5. 只能描述本次短線候選名單，不可把任何股票升格寫成中線成立、長線成立或跨池共振

語氣專業、簡潔、一針見血。不要過度樂觀或悲觀。直接輸出 Markdown。
"""
    try:
        return claude.chat(prompt=prompt, max_tokens=1500)
    except Exception as e:
        logger.error("Claude narrative failed: %s", e)
        return f"（敘事生成失敗：{e}）"


# ──────────────────────────────────────────────────────────────────────────────
# 核心掃描流程
# ──────────────────────────────────────────────────────────────────────────────


def run_screener(dry_run: bool = False, single_symbol: Optional[str] = None, debug_sentiment: bool = False) -> Optional[ScreenerState]:
    """執行短線選股掃描。"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    # 載入設定
    universe = load_universe()
    rules = load_rules()
    risk_radar = load_risk_radar()

    logger.info("候選池 %d 檔，開始掃描...", len(universe))

    # 計算情緒
    sentiment_score, regime = compute_sentiment_score(rules)
    if debug_sentiment:
        print(f"情緒分數：{sentiment_score}（{regime_label(regime)}）")
        return None

    # 單檔除錯模式
    if single_symbol:
        df = fetch_history(single_symbol, days=70)
        if df is None:
            print(f"無法取得 {single_symbol} 資料")
            return None
        df = add_indicators(df)
        inst_data = fetch_institutional_data([single_symbol])
        if not inst_data and (rules.get("event_overlay") or {}).get("snapshot_fallback", True):
            inst_data = load_institutional_snapshot()
        inst = inst_data.get(single_symbol)

        m_score, m_bd = compute_momentum_score(df, inst, rules["momentum"]["weights"])
        r_score, r_bd = compute_rebound_score(df, inst, rules["rebound"]["weights"], sentiment_score)
        m_final, m_warning, m_note = apply_event_overlay(m_score, single_symbol, "?", inst, risk_radar, rules, "momentum")
        r_final, r_warning, r_note = apply_event_overlay(r_score, single_symbol, "?", inst, risk_radar, rules, "rebound")

        print(f"=== {single_symbol} 評分 ===")
        print(f"量價突破分數: {m_final} / 100")
        for k, v in m_bd.items():
            print(f"  {breakdown_label(k)}: {v}")
        if m_note:
            print(f"  事件加權: {m_note}")
        if m_warning:
            print(f"  事件警示: {m_warning}")
        print(f"\n均值回歸分數: {r_final} / 100")
        for k, v in r_bd.items():
            print(f"  {breakdown_label(k)}: {v}")
        if r_note:
            print(f"  事件加權: {r_note}")
        if r_warning:
            print(f"  事件警示: {r_warning}")
        print(f"\n法人: {classify_inst_signal(inst)}")
        return None

    # 過濾黑名單
    blacklist = set(risk_radar.get("blacklist", []))

    # 批次抓取行情
    symbols = [s["symbol"] for s in universe if s["symbol"] not in blacklist and not s["symbol"].startswith("^")]
    logger.info("抓取 %d 檔行情...", len(symbols))
    quotes = batch_fetch_quotes(symbols)
    logger.info("成功取得 %d 檔", len(quotes))

    # 抓取法人
    logger.info("抓取法人資料...")
    inst_data = fetch_institutional_data(list(quotes.keys()))
    if not inst_data and (rules.get("event_overlay") or {}).get("snapshot_fallback", True):
        logger.info("即時法人資料不足，改讀法人快照備援")
        inst_data = load_institutional_snapshot()
    logger.info("法人資料 %d 檔", len(inst_data))

    # 評分
    momentum_candidates: List[Tuple[int, StockCandidate]] = []
    rebound_candidates: List[Tuple[int, StockCandidate]] = []

    symbol_to_meta = {s["symbol"]: s for s in universe}
    exit_rules = rules.get("exit_rules", {})
    event_overlay_rules = rules.get("event_overlay") or {}
    event_warnings: List[str] = []

    for symbol, df in quotes.items():
        meta = symbol_to_meta.get(symbol, {})
        name = meta.get("name", symbol)
        sector = meta.get("sector", "其他")
        inst = inst_data.get(symbol)

        last = df.iloc[-1]
        price = float(last["close"])

        # 動能
        m_score, m_bd = compute_momentum_score(df, inst, rules["momentum"]["weights"])
        m_score, m_warning, m_note = apply_event_overlay(m_score, symbol, sector, inst, risk_radar, rules, "momentum")
        min_m = rules["momentum"]["min_total_score"]

        # 情緒調整
        if regime == "extreme_greed":
            min_m += 10  # 過熱時提高門檻
        elif regime == "extreme_fear":
            min_m += 5   # 恐慌時也稍微保守

        if m_warning and event_overlay_rules.get("warnings", {}).get("include_summary", True):
            event_warnings.append(f"{format_name_with_code(name, symbol)}｜{m_warning}")

        if m_score >= min_m:
            m_exit = exit_rules.get("momentum", {})
            candidate = StockCandidate(
                symbol=symbol,
                name=name,
                sector=sector,
                price=price,
                score=m_score,
                breakdown=m_bd,
                entry_zone=(round(price * 0.995, 2), round(price * 1.01, 2)),
                stop_loss=round(price * (1 + m_exit.get("stop_loss_pct", -5) / 100), 2),
                take_profit=round(price * (1 + m_exit.get("take_profit_pct", 10) / 100), 2),
                time_stop=(now + timedelta(days=m_exit.get("time_stop_days", 7))).strftime("%Y-%m-%d"),
                engine="momentum",
                inst_signal=classify_inst_signal(inst),
                event_warning=m_warning,
                catalyst_note=m_note,
            )
            momentum_candidates.append((m_score, candidate))

        # 反彈
        r_score, r_bd = compute_rebound_score(df, inst, rules["rebound"]["weights"], sentiment_score)
        r_score, r_warning, r_note = apply_event_overlay(r_score, symbol, sector, inst, risk_radar, rules, "rebound")
        min_r = rules["rebound"]["min_total_score"]

        # 情緒調整
        if regime == "extreme_fear":
            min_r -= 5  # 恐慌時降低門檻（撿便宜機會）
        elif regime == "extreme_greed":
            min_r += 10  # 過熱時反彈策略要更嚴格

        if r_warning and event_overlay_rules.get("warnings", {}).get("include_summary", True):
            event_warnings.append(f"{format_name_with_code(name, symbol)}｜{r_warning}")

        if r_score >= min_r:
            r_exit = exit_rules.get("rebound", {})
            candidate = StockCandidate(
                symbol=symbol,
                name=name,
                sector=sector,
                price=price,
                score=r_score,
                breakdown=r_bd,
                entry_zone=(round(price * 0.99, 2), round(price * 1.005, 2)),
                stop_loss=round(price * (1 + r_exit.get("stop_loss_pct", -4) / 100), 2),
                take_profit=round(price * (1 + r_exit.get("take_profit_pct", 7) / 100), 2),
                time_stop=(now + timedelta(days=r_exit.get("time_stop_days", 5))).strftime("%Y-%m-%d"),
                engine="rebound",
                inst_signal=classify_inst_signal(inst),
                event_warning=r_warning,
                catalyst_note=r_note,
            )
            rebound_candidates.append((r_score, candidate))

    # 排序並取 top N
    top_n = rules["momentum"].get("pick_top_n", 10)
    momentum_candidates.sort(key=lambda x: -x[0])
    rebound_candidates.sort(key=lambda x: -x[0])

    momentum_picks = [c for _, c in momentum_candidates[:top_n]]
    rebound_picks = [c for _, c in rebound_candidates[:top_n]]

    # 集中度檢查
    all_picks = momentum_picks + rebound_picks
    max_concentration = rules.get("correlation", {}).get("max_sector_concentration", 0.4)
    sector_concentration, warnings = check_sector_concentration(all_picks, max_concentration)

    warnings.extend(event_warnings[: int(event_overlay_rules.get("warnings", {}).get("max_items", 3) or 3)])

    state = ScreenerState(
        date=date_str,
        sentiment_score=sentiment_score,
        regime=regime,
        risk_radar=risk_radar,
        momentum_picks=momentum_picks,
        rebound_picks=rebound_picks,
        sector_concentration=sector_concentration,
        warnings=warnings,
    )

    logger.info(
        "掃描完成：量價突破 %d 檔，均值回歸 %d 檔，情緒 %d (%s)",
        len(momentum_picks), len(rebound_picks), sentiment_score, regime
    )

    # 產生敘事
    narrative = build_narrative(state, rules)

    # 儲存結果
    log_dir = _BASE_DIR / "logs" / "screener"
    log_dir.mkdir(parents=True, exist_ok=True)

    result_file = log_dir / f"{date_str}.json"
    result_data = {
        "date": date_str,
        "sentiment_score": sentiment_score,
        "regime": regime,
        "risk_radar": risk_radar,
        "momentum_picks": [asdict(p) for p in momentum_picks],
        "rebound_picks": [asdict(p) for p in rebound_picks],
        "sector_concentration": sector_concentration,
        "warnings": warnings,
    }
    result_file.write_text(json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("結果已存至 %s", result_file)

    narrative_file = log_dir / f"{date_str}_narrative.md"
    narrative_file.write_text(narrative, encoding="utf-8")
    logger.info("敘事已存至 %s", narrative_file)

    # 加入績效追蹤
    try:
        from utils.screener_tracker import get_screener_tracker
        tracker = get_screener_tracker()
        tracker.add_daily_picks(state)
        logger.info("[ScreenerTracker] 選股結果已加入追蹤")
    except Exception as e:
        logger.warning(f"[ScreenerTracker] 加入追蹤失敗: {e}")

    bucket_digest = ""
    pool_digest = ""
    try:
        from utils.watchlist_merger import build_watchlist
        live_watchlist = build_watchlist()
        bucket_snapshot = build_tracking_bucket_snapshot(strategy_signals=[], watchlist_raw=live_watchlist)
        pool_digest = render_watchlist_pool_digest(bucket_snapshot.get("watchlist_digest") or {})
        bucket_digest = render_tracking_bucket_digest(
            bucket_snapshot.get("tracking_buckets") or {},
            include_titles=("today_first_watch", "next_month_verify", "no_overinterpretation"),
            limit_per_bucket=2,
        )
    except Exception as e:
        logger.debug(f"五池追蹤摘要生成失敗: {e}")

    if dry_run:
        logger.info("試跑模式，跳過推播")
        print("\n=== 當日選股敘事 ===")
        print(narrative)
        return state

    master_control = get_current_control()

    # 組合推播內容
    push_content = f"""# 📊 短線選股日報 · {date_str}

**情緒分數**：{sentiment_score}（{regime_label(regime)}）

## 🚀 量價突破前五名
"""
    for i, p in enumerate(momentum_picks[:5], 1):
        extra = f"｜事件 {p.catalyst_note}" if p.catalyst_note else ""
        warn = f"｜警示 {p.event_warning}" if p.event_warning else ""
        push_content += f"{i}. **{format_name_with_code(p.name, p.symbol)}** - 分數 {p.score}，停損 {p.stop_loss}{extra}{warn}\n"

    push_content += "\n## 🔻 均值回歸前五名\n"
    for i, p in enumerate(rebound_picks[:5], 1):
        extra = f"｜事件 {p.catalyst_note}" if p.catalyst_note else ""
        warn = f"｜警示 {p.event_warning}" if p.event_warning else ""
        push_content += f"{i}. **{format_name_with_code(p.name, p.symbol)}** - 分數 {p.score}，停損 {p.stop_loss}{extra}{warn}\n"

    if pool_digest:
        push_content += f"\n## 五池觀察清單\n{pool_digest}\n"

    if bucket_digest:
        push_content += f"\n\n---\n{bucket_digest}"

    push_content += f"\n---\n{narrative}"

    if master_control is not None:
        payload_path = write_payload(
            {
                "agent": "stock_screener",
                "artifacts": {
                    "result_file": str(result_file),
                    "narrative_file": str(narrative_file),
                },
                "notification": {
                    "feishu": {
                        "title": f"📊 短線選股日報 · {date_str}",
                        "content": push_content,
                    },
                    "wechat": {
                        "text": push_content[:1500],
                        "typing_seconds": 5,
                    },
                },
            },
            control=master_control,
        )
        logger.info("Master agent 接管通知：%s", payload_path)
    else:
        # 直接推送
        notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
        notifier.send_message(
            "oc_your_feishu_chat_id",
            f"📊 短線選股日報 · {date_str}",
            push_content,
        )

    return state


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="股市 W · 短線選股子代理")
    parser.add_argument("--dry-run", action="store_true", help="不推播")
    parser.add_argument("--symbol", type=str, help="單檔評分除錯")
    parser.add_argument("--debug-sentiment", action="store_true", help="只輸出情緒分數")
    args = parser.parse_args()

    run_screener(
        dry_run=args.dry_run,
        single_symbol=args.symbol,
        debug_sentiment=args.debug_sentiment,
    )


if __name__ == "__main__":
    main()
