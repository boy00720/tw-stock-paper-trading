# -*- coding: utf-8 -*-
"""
數據驗證工具 — 確保股市數據新鮮度與可靠性
"""

import logging
from datetime import datetime, timedelta, time
from typing import Optional, Tuple, Dict, Any, List
import pandas as pd

logger = logging.getLogger(__name__)


class DataStaleError(Exception):
    """數據過期異常"""
    pass


class DataSourceError(Exception):
    """數據源異常"""
    pass


M1_REQUIRED_COLS = ["Open", "High", "Low", "Close", "Volume"]


def validate_stock_data_freshness(
    df: pd.DataFrame,
    symbol: str,
    max_delay_hours: int = 2,
    trading_hours_only: bool = True
) -> Tuple[bool, Optional[str]]:
    """
    驗證股票數據新鮮度

    Args:
        df: 股價 DataFrame，index 為 datetime
        symbol: 股票代號
        max_delay_hours: 最大允許延遲（小時）
        trading_hours_only: 是否只在交易時間檢查

    Returns:
        (is_fresh, error_msg)
    """
    if df is None or df.empty:
        return False, f"{symbol}: 無數據"

    try:
        last_timestamp = df.index[-1]
        if last_timestamp.tzinfo is not None:
            last_timestamp = last_timestamp.tz_localize(None)

        now = datetime.now()
        if last_timestamp.date() != now.date():
            if now.hour < 9:
                return True, None
            return False, f"{symbol}: 最後數據日期 {last_timestamp.date()} 非今日"

        delay = now - last_timestamp
        delay_hours = delay.total_seconds() / 3600
        if delay_hours > max_delay_hours:
            return False, f"{symbol}: 數據延遲 {delay_hours:.1f} 小時"

        return True, None
    except Exception as e:
        return False, f"{symbol}: 驗證失敗 - {e}"


def validate_market_data_completeness(
    df: pd.DataFrame,
    symbol: str,
    min_bars: int = 2
) -> Tuple[bool, Optional[str]]:
    """
    驗證市場數據完整性

    Args:
        df: 股價 DataFrame
        symbol: 股票代號
        min_bars: 最少需要的 K 線數

    Returns:
        (is_complete, error_msg)
    """
    if df is None:
        return False, f"{symbol}: DataFrame 為 None"

    if len(df) < min_bars:
        return False, f"{symbol}: 數據筆數 {len(df)} < 最少要求 {min_bars}"

    required_cols = ["Close", "Open", "High", "Low", "Volume"]
    available_cols = [c for c in required_cols if c in df.columns]
    if len(available_cols) < 2:
        return False, f"{symbol}: 缺少必要價格欄位，可用: {available_cols}"

    close_series = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
    nan_count = close_series.isna().sum()
    if nan_count > len(df) * 0.5:
        return False, f"{symbol}: 數據品質過差，{nan_count}/{len(df)} 為 NaN"

    return True, None


def _normalize_timestamp(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    value = pd.Timestamp(ts)
    if value.tzinfo is not None:
        value = value.tz_localize(None)
    return value.to_pydatetime()


def validate_m1_data_freshness(
    df: pd.DataFrame,
    symbol: str,
    max_delay_minutes: int = 5,
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[str]]:
    if df is None or df.empty:
        return False, f"{symbol}: 無 M1 數據"

    if not isinstance(df.index, pd.DatetimeIndex):
        return False, f"{symbol}: M1 index 非 DatetimeIndex"

    current = now or datetime.now()
    last_timestamp = _normalize_timestamp(df.index[-1])
    if last_timestamp is None:
        return False, f"{symbol}: 無法解析最後一筆 M1 時間"

    if last_timestamp.date() != current.date():
        return False, f"{symbol}: 最後 M1 日期 {last_timestamp.date()} 非今日"

    delay = current - last_timestamp
    delay_minutes = delay.total_seconds() / 60
    if delay_minutes > max_delay_minutes:
        return False, f"{symbol}: M1 數據延遲 {delay_minutes:.1f} 分鐘"

    return True, None


def validate_m1_data_completeness(
    df: pd.DataFrame,
    symbol: str,
    min_bars: int = 30,
    max_gap_minutes: int = 3,
    cutoff_time: str = "14:30",
) -> Tuple[bool, Optional[str]]:
    if df is None or df.empty:
        return False, f"{symbol}: 無 M1 數據"

    if not isinstance(df.index, pd.DatetimeIndex):
        return False, f"{symbol}: M1 index 非 DatetimeIndex"

    if len(df) < min_bars:
        return False, f"{symbol}: M1 數據筆數 {len(df)} < 最少要求 {min_bars}"

    missing_cols = [col for col in M1_REQUIRED_COLS if col not in df.columns]
    if missing_cols:
        return False, f"{symbol}: 缺少 M1 欄位 {missing_cols}"

    if df[M1_REQUIRED_COLS].isna().any().any():
        return False, f"{symbol}: M1 存在 NaN 欄位"

    gaps = df.index.to_series().diff().dropna()
    if not gaps.empty and gaps.max() > pd.Timedelta(minutes=max_gap_minutes):
        return False, f"{symbol}: M1 間隔過大 {gaps.max()}"

    cutoff_h, cutoff_m = map(int, cutoff_time.split(":"))
    cutoff = time(cutoff_h, cutoff_m)
    if not any(ts.time() >= cutoff for ts in df.index):
        return False, f"{symbol}: 無 {cutoff_time} 後 M1 資料"

    return True, None


def validate_m1_snapshot(snapshot: Dict[str, Any], symbol: str) -> Tuple[bool, List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not snapshot:
        return False, [f"{symbol}: snapshot 為空"], warnings

    required_keys = [
        "snapshot_time",
        "last_price",
        "prev_close",
        "volume_cum",
        "avg_volume_20d",
        "post_cutoff_bar_count",
    ]
    for key in required_keys:
        if key not in snapshot:
            errors.append(f"{symbol}: snapshot 缺少 {key}")

    if snapshot.get("post_cutoff_bar_count", 0) <= 0:
        warnings.append(f"{symbol}: cutoff 後無 M1 bars")

    return len(errors) == 0, errors, warnings


def get_twse_realtime_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """
    從證交所 API 取得即時報價（備援數據源）

    Args:
        symbol: 股票代號（不含 .TW）

    Returns:
        包含 price, change, volume 的字典，或 None
    """
    import requests
    import json

    try:
        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={datetime.now().strftime('%Y%m%d')}&stockNo={symbol}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("stat") != "OK":
            return None

        data_list = data.get("data", [])
        if not data_list:
            return None

        latest = data_list[-1]
        return {
            "source": "twse",
            "date": latest[0],
            "open": float(latest[3].replace(",", "")),
            "high": float(latest[4].replace(",", "")),
            "low": float(latest[5].replace(",", "")),
            "close": float(latest[6].replace(",", "")),
            "volume": int(latest[1].replace(",", "")),
            "change": float(latest[7].replace(",", "")),
        }
    except Exception as e:
        logger.debug(f"證交所 API 取得 {symbol} 失敗: {e}")
        return None


def validate_with_fallback(
    symbol: str,
    primary_data: pd.DataFrame,
    max_delay_hours: int = 2
) -> Tuple[bool, Optional[str], Optional[Dict]]:
    """
    驗證數據新鮮度，若失敗嘗試備援數據源

    Returns:
        (is_valid, error_msg, fallback_data)
    """
    is_fresh, error = validate_stock_data_freshness(primary_data, symbol, max_delay_hours)
    if is_fresh:
        return True, None, None

    logger.warning(f"主要數據源驗證失敗: {error}，嘗試備援...")
    if symbol.endswith(".TW"):
        stock_id = symbol.replace(".TW", "")
        twse_data = get_twse_realtime_quote(stock_id)
        if twse_data:
            logger.info(f"成功取得 {symbol} 證交所備援數據")
            return True, None, twse_data

    return False, error, None


class DataQualityReport:
    """數據品質報告"""

    def __init__(self):
        self.errors: list = []
        self.warnings: list = []
        self.fallback_used: list = []

    def add_error(self, symbol: str, msg: str):
        self.errors.append({"symbol": symbol, "msg": msg, "time": datetime.now().isoformat()})

    def add_warning(self, symbol: str, msg: str):
        self.warnings.append({"symbol": symbol, "msg": msg, "time": datetime.now().isoformat()})

    def add_fallback(self, symbol: str, source: str):
        self.fallback_used.append({"symbol": symbol, "source": source, "time": datetime.now().isoformat()})

    def to_dict(self) -> Dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "total_errors": len(self.errors),
            "total_warnings": len(self.warnings),
            "fallback_count": len(self.fallback_used),
            "errors": self.errors,
            "warnings": self.warnings,
            "fallback_used": self.fallback_used,
        }

    def is_healthy(self) -> bool:
        return len(self.errors) == 0
