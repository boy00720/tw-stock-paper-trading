"""
automation/sim/feed.py — 報價來源（M1 Day 3）

QuoteFeed：
- 主源：yfinance（台股代號加 `.TW`，ETF 同上；~15 分鐘延遲）
- 5 秒快取，避免短時間重複拉
- 拉失敗時 fallback 到上一筆已知價（last known price）
- 真網路連線實測：2330.TW、0050.TW 能用 fast_info.lastPrice 拉到

設計原則：
- 純讀取，不對外狀態（除快取）
- 失敗安靜：log warning 但不 raise，回傳能給的就給
- 可被 mock：subclass override _fetch() 或直接傳 fake QuoteFeed
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class QuoteFeed:
    """yfinance 報價快取與 fallback 包裝。"""

    def __init__(self, cache_ttl_sec: float = 5.0):
        self.cache_ttl = float(cache_ttl_sec)
        # symbol -> (price, fetched_ts)
        self._cache: dict[str, tuple[float, float]] = {}
        # symbol -> last known price（即使快取過期、拉失敗，仍可用）
        self._last_known: dict[str, float] = {}

    # ─────────────────────────────────────────────────────
    # 公開介面
    # ─────────────────────────────────────────────────────

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """
        拉一批 symbols 的最新報價。
        - 快取內（< cache_ttl）直接回快取
        - 過期 → 重拉，成功就更新快取與 last_known
        - 拉失敗 → fallback 到 last_known（若有）
        - 從沒成功過的 symbol → 不放進回傳 dict（呼叫端要處理 missing key）
        """
        result: dict[str, float] = {}
        now = time.time()
        to_fetch: list[str] = []

        for sym in symbols:
            cached = self._cache.get(sym)
            if cached and (now - cached[1]) < self.cache_ttl:
                result[sym] = cached[0]
            else:
                to_fetch.append(sym)

        if to_fetch:
            fresh = self._fetch(to_fetch)
            for sym in to_fetch:
                if sym in fresh and fresh[sym] is not None:
                    price = float(fresh[sym])
                    result[sym] = price
                    self._cache[sym] = (price, now)
                    self._last_known[sym] = price
                elif sym in self._last_known:
                    # 拉失敗但有歷史 → fallback
                    fallback_price = self._last_known[sym]
                    result[sym] = fallback_price
                    logger.warning(
                        "QuoteFeed: %s 拉取失敗，fallback 到 last_known=%s",
                        sym, fallback_price,
                    )
                # 否則：第一次拉就失敗，這個 sym 不在 result 中

        return result

    def last_known(self, symbol: str) -> Optional[float]:
        """查 symbol 的最後成功報價（debug / report 用）"""
        return self._last_known.get(symbol)

    # ─────────────────────────────────────────────────────
    # 實際拉取（可被 subclass / 測試 override）
    # ─────────────────────────────────────────────────────

    def _fetch(self, symbols: list[str]) -> dict[str, Optional[float]]:
        """
        實際呼叫 yfinance 拉報價。
        回傳 dict[symbol, price or None]，失敗的 sym 為 None。
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.error("QuoteFeed: yfinance 未安裝，請 pip install yfinance")
            return {sym: None for sym in symbols}

        result: dict[str, Optional[float]] = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.fast_info
                # fast_info 比 info 快得多。lastPrice 是最近交易價
                price = info.get("lastPrice") if hasattr(info, "get") else getattr(info, "last_price", None)
                if price is None or (isinstance(price, float) and price != price):  # NaN check
                    # 退回 regularMarketPrice
                    price = info.get("regularMarketPrice") if hasattr(info, "get") else None
                result[sym] = float(price) if price else None
            except Exception as exc:
                logger.warning("QuoteFeed: yfinance fetch %s 失敗: %s", sym, exc)
                result[sym] = None
        return result
