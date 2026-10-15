"""
策略掃描模組 — 台股技術面信號偵測
=======================================
策略參考：ZhuLinsen/daily_stock_analysis 的策略 YAML
適配台股（yfinance .TW 格式），整合進 stock_monitor.py 晨報流程。

支援策略：
  1. 均線金叉   — MA5 上穿 MA10，量比 > 1.2
  2. 放量突破   — 收盤創 20 日新高，量比 > 2.0
  3. 縮量回踩   — 多頭排列中回踩 MA5/MA10，量能萎縮
  4. 底部放量   — 從 20 日高點跌逾 15% 後，量比 > 3.0 且收陽
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class StrategySignal:
    symbol: str
    name: str
    strategy: str          # 策略名稱
    price: float
    detail: str            # 具體觸發原因
    strength: str          # "強" / "中" / "弱"
    stop_loss: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    breakout_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    close_position: Optional[float] = None
    ma_retest: Optional[str] = None


def _fetch_history(symbol: str, days: int = 60) -> Optional[pd.DataFrame]:
    """拉取歷史日線資料，回傳標準化 DataFrame（含 close/open/high/low/volume）"""
    try:
        import yfinance as yf
        df = yf.download(
            symbol,
            period=f"{days}d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            multi_level_index=False,
        )
        if df.empty or len(df) < 20:
            return None
        df.index = pd.to_datetime(df.index)
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        logger.warning(f"[策略掃描] {symbol} 歷史資料取得失敗: {e}")
        return None


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """計算技術指標：MA5/10/20、量比、MACD"""
    df = df.copy()
    df["ma5"]  = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol5"] = df["volume"].rolling(5).mean()
    df["vol_ratio"] = df["volume"] / df["vol5"]

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df


# ── 策略 1：均線金叉 ──────────────────────────────────────────────────────────

def check_ma_golden_cross(df: pd.DataFrame, symbol: str, name: str) -> Optional[StrategySignal]:
    """
    MA5 在近 3 根 K 棒內上穿 MA10，且金叉日量比 > 1.2。
    強信號：同時 MACD 在零軸上方金叉。
    """
    if len(df) < 22:
        return None

    recent = df.dropna(subset=["ma5", "ma10", "vol_ratio"]).tail(5)
    if len(recent) < 4:
        return None

    # 找最近 3 根內是否有 MA5 從下方穿越 MA10
    crossed = False
    cross_idx = -1
    for i in range(-3, 0):
        prev = recent.iloc[i - 1]
        curr = recent.iloc[i]
        if prev["ma5"] <= prev["ma10"] and curr["ma5"] > curr["ma10"]:
            crossed = True
            cross_idx = i
            break

    if not crossed:
        return None

    cross_bar = recent.iloc[cross_idx]
    vol_ratio = cross_bar["vol_ratio"]
    if vol_ratio < 1.2:
        return None

    last = recent.iloc[-1]
    price = float(last["close"])
    ma20  = float(last["ma20"])

    # MACD 加成
    macd_bonus = ""
    if last["macd"] > last["macd_signal"] and last["macd"] > 0:
        macd_bonus = "，MACD 零軸上方金叉（強信號）"
        strength = "強"
    elif last["macd"] > last["macd_signal"]:
        macd_bonus = "，MACD 金叉確認"
        strength = "中"
    else:
        strength = "中"

    detail = (
        f"MA5({last['ma5']:.1f}) 上穿 MA10({last['ma10']:.1f})"
        f"，金叉日量比 {vol_ratio:.2f}x{macd_bonus}"
    )

    return StrategySignal(
        symbol=symbol, name=name, strategy="均線金叉",
        price=price, detail=detail, strength=strength,
        stop_loss=round(ma20 * 0.99, 1),
        tags=["趨勢反轉", "量能確認"],
        volume_ratio=round(float(vol_ratio), 4),
    )


# ── 策略 2：放量突破 ───────────────────────────────────────────────────────────

def check_volume_breakout(df: pd.DataFrame, symbol: str, name: str) -> Optional[StrategySignal]:
    """
    收盤創 20 日新高（突破壓力），量比 > 2.0，收盤在當日振幅上方 30%。
    """
    if len(df) < 22:
        return None

    df_clean = df.dropna(subset=["ma20", "vol_ratio"])
    if len(df_clean) < 20:
        return None

    last = df_clean.iloc[-1]
    price    = float(last["close"])
    high20   = float(df_clean["high"].iloc[-20:-1].max())  # 前 20 日高點（不含今日）
    vol_ratio = float(last["vol_ratio"])

    if price <= high20:
        return None
    if vol_ratio < 2.0:
        return None

    # 強勢收盤：收在振幅上方 30%
    day_range = float(last["high"]) - float(last["low"])
    close_position = (price - float(last["low"])) / day_range if day_range > 0 else 0
    if close_position < 0.3:
        return None

    breakout_pct = (price - high20) / high20 * 100
    strength = "強" if vol_ratio >= 3.0 else "中"

    detail = (
        f"收盤 {price:.1f} 突破 20 日高點 {high20:.1f}"
        f"（+{breakout_pct:.1f}%），量比 {vol_ratio:.2f}x"
        f"，強勢收盤位置 {close_position*100:.0f}%"
    )

    return StrategySignal(
        symbol=symbol, name=name, strategy="放量突破",
        price=price, detail=detail, strength=strength,
        stop_loss=round(high20 * 0.97, 1),
        tags=["突破", "強勢放量"],
        breakout_pct=round(float(breakout_pct), 4),
        volume_ratio=round(float(vol_ratio), 4),
        close_position=round(float(close_position), 4),
    )


# ── 策略 3：縮量回踩 ───────────────────────────────────────────────────────────

def check_shrink_pullback(df: pd.DataFrame, symbol: str, name: str) -> Optional[StrategySignal]:
    """
    多頭排列（MA5 > MA10 > MA20）中，價格回踩至 MA5±1% 或 MA10±2%，
    且近 3 日均量 < 5 日均量的 70%。
    """
    if len(df) < 22:
        return None

    last = df.dropna(subset=["ma5", "ma10", "ma20", "vol_ratio"]).iloc[-1]
    price = float(last["close"])
    ma5   = float(last["ma5"])
    ma10  = float(last["ma10"])
    ma20  = float(last["ma20"])

    # 必須多頭排列
    if not (ma5 > ma10 > ma20):
        return None

    # 近 3 日均量縮量
    recent_vol_ratio = float(df["vol_ratio"].tail(3).mean())
    if recent_vol_ratio >= 0.70:
        return None

    # 價格接近 MA5 或 MA10
    near_ma5  = abs(price - ma5)  / ma5  < 0.01
    near_ma10 = abs(price - ma10) / ma10 < 0.02

    if not (near_ma5 or near_ma10):
        return None

    support = "MA5" if near_ma5 else "MA10"
    support_val = ma5 if near_ma5 else ma10
    strength = "強" if near_ma5 and recent_vol_ratio < 0.5 else "中"

    detail = (
        f"多頭排列中縮量回踩 {support}({support_val:.1f})"
        f"，近 3 日均量比 {recent_vol_ratio:.2f}x（萎縮）"
    )

    return StrategySignal(
        symbol=symbol, name=name, strategy="縮量回踩",
        price=price, detail=detail, strength=strength,
        stop_loss=round(ma20 * 0.99, 1),
        tags=["趨勢延續", "低吸機會"],
        volume_ratio=round(float(recent_vol_ratio), 4),
        ma_retest=support,
    )


# ── 策略 4：底部放量 ───────────────────────────────────────────────────────────

def check_bottom_volume(df: pd.DataFrame, symbol: str, name: str) -> Optional[StrategySignal]:
    """
    從 20 日高點跌逾 15%，當日量比 > 3.0，且收陽（收盤 > 開盤）。
    潛在趨勢反轉，風險偏高，倉位建議偏小。
    """
    if len(df) < 22:
        return None

    df_clean = df.dropna(subset=["vol_ratio"])
    last = df_clean.iloc[-1]
    price     = float(last["close"])
    open_p    = float(last["open"])
    high20    = float(df_clean["high"].iloc[-20:-1].max())
    vol_ratio = float(last["vol_ratio"])

    # 從高點跌幅須 > 15%
    drop_pct = (high20 - price) / high20 * 100
    if drop_pct < 15:
        return None

    # 量比 > 3.0
    if vol_ratio < 3.0:
        return None

    # 收陽
    if price <= open_p:
        return None

    recent_low = float(df_clean["low"].tail(5).min())
    strength = "中"  # 反轉信號風險本就偏高，不給強

    detail = (
        f"從 20 日高點 {high20:.1f} 跌 {drop_pct:.1f}%，"
        f"今日放量收陽（量比 {vol_ratio:.2f}x）"
        f"，潛在底部反轉｜⚠️ 風險偏高，建議小倉"
    )

    return StrategySignal(
        symbol=symbol, name=name, strategy="底部放量",
        price=price, detail=detail, strength=strength,
        stop_loss=round(recent_low * 0.99, 1),
        tags=["底部反轉", "高風險"],
        volume_ratio=round(float(vol_ratio), 4),
        breakout_pct=round(float(-drop_pct), 4),
    )


# ── 主掃描入口 ─────────────────────────────────────────────────────────────────

STRATEGIES = [
    check_ma_golden_cross,
    check_volume_breakout,
    check_shrink_pullback,
    check_bottom_volume,
]


def scan(watch_list: list) -> List[StrategySignal]:
    """
    掃描 watch_list 中所有台股（.TW 結尾），回傳觸發的策略信號清單。

    Args:
        watch_list: [(symbol, name, lower, upper), ...] 格式，同 stock_monitor.py
    """
    signals: List[StrategySignal] = []

    tw_stocks = [(sym, name) for sym, name, *_ in watch_list if sym.endswith(".TW")]
    logger.info(f"[策略掃描] 開始掃描 {len(tw_stocks)} 支台股")

    for symbol, name in tw_stocks:
        df_raw = _fetch_history(symbol)
        if df_raw is None:
            continue
        df = _add_indicators(df_raw)

        for strategy_fn in STRATEGIES:
            try:
                signal = strategy_fn(df, symbol, name)
                if signal:
                    signals.append(signal)
                    logger.info(f"[策略掃描] ✓ {name}({symbol}) 觸發「{signal.strategy}」({signal.strength})")
            except Exception as e:
                logger.warning(f"[策略掃描] {symbol} {strategy_fn.__name__} 執行失敗: {e}")

    logger.info(f"[策略掃描] 完成，共 {len(signals)} 個信號")
    return signals


def format_signals(signals: List[StrategySignal]) -> str:
    """將信號清單格式化為飛書/微信訊息"""
    if not signals:
        return "（今日無明顯技術面策略信號）"

    strength_order = {"強": 0, "中": 1, "弱": 2}
    signals = sorted(signals, key=lambda s: strength_order.get(s.strength, 9))

    lines = []
    for s in signals:
        icon = "🔥" if s.strength == "強" else "📌"
        stop = f"，止損參考 {s.stop_loss}" if s.stop_loss else ""
        lines.append(
            f"{icon} 【{s.strategy}｜{s.strength}】{s.name}（{s.symbol}） @ {s.price:.1f}\n"
            f"   {s.detail}{stop}"
        )

    return "\n\n".join(lines)


def save_signals(signals: List[StrategySignal], now=None) -> None:
    """
    將今日信號存入 logs/strategy_signals/YYYY-MM-DD.json。
    格式：[{date, symbol, name, strategy, strength, price, stop_loss, detail, tags}, ...]
    每次執行會覆蓋當日檔案（晨報只跑一次，不會重複）。
    """
    import json
    import os
    from datetime import datetime

    if now is None:
        now = datetime.now()

    date_str = now.strftime("%Y-%m-%d")
    log_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "automation", "logs", "strategy_signals",
    )
    os.makedirs(log_dir, exist_ok=True)
    filepath = os.path.join(log_dir, f"{date_str}.json")

    records = []
    for s in signals:
        records.append({
            "date": date_str,
            "symbol": s.symbol,
            "name": s.name,
            "strategy": s.strategy,
            "strength": s.strength,
            "price": s.price,
            "stop_loss": s.stop_loss,
            "detail": s.detail,
            "tags": s.tags,
            "breakout_pct": s.breakout_pct,
            "volume_ratio": s.volume_ratio,
            "vol_ratio": s.volume_ratio,
            "close_position": s.close_position,
            "ma_retest": s.ma_retest,
            # 留空供事後填寫驗證結果
            "outcome_price": None,
            "outcome_note": None,
        })

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    logger.info(f"[策略掃描] 信號記錄已存至 {filepath}")

    # 自動加入臨時監控清單
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from utils.signal_watch_manager import get_signal_watch_manager
        manager = get_signal_watch_manager()
        added = manager.add_signals(records, duration_days=5)
        if added > 0:
            logger.info(f"[策略掃描] 已自動將 {added} 個信號加入臨時監控（5天）")
    except Exception as e:
        logger.warning(f"[策略掃描] 自動加入監控失敗: {e}")


def backfill_outcomes(quotes: list, now=None) -> str:
    """
    盤後執行：用當日收盤價回填今日 strategy_signals JSON 的 outcome_price。
    回傳簡短的驗證摘要字串，供插入盤後報告。

    判斷邏輯：
      - 收盤價 > 信號價：成功（漲）
      - 收盤價 < stop_loss：觸及止損（失敗）
      - 其餘：持平觀察
    """
    import json
    import os
    from datetime import datetime

    if now is None:
        now = datetime.now()

    date_str = now.strftime("%Y-%m-%d")
    log_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "automation", "logs", "strategy_signals",
    )
    filepath = os.path.join(log_dir, f"{date_str}.json")

    if not os.path.exists(filepath):
        return ""

    # 建立 symbol -> 收盤價 快查表
    price_map = {q["symbol"]: q["price"] for q in quotes if "symbol" in q and "price" in q}

    with open(filepath, "r", encoding="utf-8") as f:
        records = json.load(f)

    if not records:
        return ""

    summary_lines = ["📊 【今日策略信號驗證】"]
    for rec in records:
        symbol = rec["symbol"]
        signal_price = rec["price"]
        stop_loss = rec.get("stop_loss")
        close_price = price_map.get(symbol)

        if close_price is None:
            rec["outcome_note"] = "無收盤資料"
            continue

        rec["outcome_price"] = round(float(close_price), 2)
        chg_pct = (close_price - signal_price) / signal_price * 100

        if stop_loss and close_price < stop_loss:
            outcome = "❌ 觸及止損"
        elif close_price > signal_price:
            outcome = "✅ 收漲"
        else:
            outcome = "➖ 收跌，持續觀察"

        rec["outcome_note"] = outcome
        summary_lines.append(
            f"  {outcome}｜{rec['name']}（{rec['strategy']}）"
            f" 信號價 {signal_price:.1f} → 收盤 {close_price:.1f}"
            f"（{'+' if chg_pct >= 0 else ''}{chg_pct:.1f}%）"
        )

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    logger.info(f"[策略掃描] 盤後回填完成：{filepath}")
    return "\n".join(summary_lines) if len(summary_lines) > 1 else ""
