"""
automation/sim/runner.py — 撮合迴圈（M1 Day 3 / M5 增 on_fill + equity_curve_path）

提供兩個函數：
- run_once(engine, feed)：跑一輪撮合（拉報價 → match → apply_fills），回傳成交筆數
- run_loop(engine, feed, interval_sec, stop_after_iterations, *, on_fill, equity_curve_path)：
  迴圈跑；每輪 match 後若成交，對每筆 fill 呼叫 on_fill callback；若 equity_curve_path
  非 None，每輪結尾 append 一筆權益曲線記錄（**跟 runner 同節奏，沒成交也寫**，避免
  「持倉浮動但曲線不動」的鋸齒）

設計原則：
- run_once 是純函數風格，便於測試與排程器整合（M5 不動 run_once 簽名 / 行為）
- run_loop 內部 try/except，單輪失敗不中斷整個迴圈（log 後繼續）
- stop_after_iterations 給測試用，None 表示無限跑
- M5 新增的 on_fill / equity_curve_path 為 keyword-only（保 M1-M4 既有 caller 零回歸）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Protocol

from sim.calendar import should_run_now
from sim.engine import MatchEngine

logger = logging.getLogger(__name__)


class FeedProto(Protocol):
    """QuoteFeed 介面（為了 runner 也可吃 mock feed）"""
    def get_quotes(self, symbols: list[str]) -> dict[str, float]: ...


def run_once(engine: MatchEngine, feed: FeedProto) -> int:
    """
    跑一輪撮合：拉 watchlist 報價 → match → apply_fills。
    回傳本輪成交筆數。
    """
    symbols = engine.watchlist()
    if not symbols:
        return 0

    quotes = feed.get_quotes(symbols)
    if not quotes:
        return 0

    fills = engine.match(quotes)
    if not fills:
        return 0

    engine.apply_fills(fills)
    return len(fills)


def run_loop(
    engine: MatchEngine,
    feed: FeedProto,
    interval_sec: Optional[int] = None,
    stop_after_iterations: Optional[int] = None,
    *,
    on_fill: Optional[Callable[[dict, dict], None]] = None,
    equity_curve_path: Optional[Path] = None,
    ignore_market_hours: bool = False,
) -> None:
    """
    撮合迴圈。

    - interval_sec：未指定時讀 engine.match_interval_sec()（預設 30）
    - stop_after_iterations：跑幾輪後停（測試用），None = 永遠跑
    - 單輪內部 try/except，失敗 log 後繼續下一輪，不中斷迴圈
    - **on_fill**（M5）：每筆成交回呼 `on_fill(fill_dict, snapshot)`；fill_dict 是
      `Fill.to_dict()`、snapshot 是 `engine.snapshot(quotes)`。callback 自身例外被
      捕獲（不影響後續 fill 與 equity_curve）。
    - **equity_curve_path**（M5）：非 None 時每輪結尾 append 一筆權益曲線記錄；
      append 失敗（IO / 格式錯誤）log 後繼續。
    """
    interval = int(interval_sec) if interval_sec is not None else engine.match_interval_sec()
    iteration = 0

    logger.info(
        "[sim runner] 啟動撮合迴圈 interval=%ss watchlist=%s on_fill=%s equity_curve=%s",
        interval,
        engine.watchlist(),
        "on" if on_fill else "off",
        equity_curve_path or "off",
    )

    while True:
        iteration += 1
        # 本輪期間用的最新報價（給 equity_curve 算 mark-to-market 與 snapshot 共用）
        quotes: dict[str, float] = {}
        try:
            symbols = engine.watchlist()
            if symbols:
                quotes = feed.get_quotes(symbols) or {}

            # 盤外/假日不撮合：避免 bot 常駐的記憶體 sim 在半夜（pending LMT/STOP
            # 等到隔夜報價）撮合並推「advisor 成交」DM，造成「盤外下單」錯覺。
            # equity_curve 仍照寫（權益曲線不應因盤外而斷）。canonical 時段判斷在
            # sim.calendar.should_run_now（盤中 09:00–13:30 + 非假日 + 非週末）。
            if not ignore_market_hours and not should_run_now():
                fills = []
            else:
                fills = engine.match(quotes) if quotes else []
            if fills:
                engine.apply_fills(fills)
                logger.info("[sim runner] iter=%s fills=%s", iteration, len(fills))

                # M5：每筆 fill 呼叫 on_fill callback（包 try/except 不中斷下筆）
                if on_fill is not None:
                    snapshot = engine.snapshot(quotes)
                    for fill in fills:
                        try:
                            on_fill(fill.to_dict(), snapshot)
                        except Exception:
                            logger.exception(
                                "[sim runner] on_fill callback failed iter=%s fill_id=%s",
                                iteration, getattr(fill, "fill_id", "?"),
                            )
        except Exception:
            # 不中斷迴圈，記錄完整 traceback
            logger.exception("[sim runner] iter=%s 例外，繼續下一輪", iteration)

        # M5：每輪結尾寫 equity_curve（沒成交也寫；獨立 try/except 避免 IO 錯誤
        # 中斷 runner）。放在 match try 區塊外是為了即使 match 失敗也能寫快照。
        if equity_curve_path is not None:
            try:
                # 避免 module 頂層循環匯入（reports 不該依賴 runner，但保險起見 lazy）
                from sim.reports import equity_curve_append

                cash = engine.account.cash
                equity = engine.account.equity(quotes if quotes else None)
                position_value = equity - cash
                equity_curve_append(
                    equity_curve_path,
                    time=datetime.now().isoformat(timespec="seconds"),
                    cash=cash,
                    position_value=position_value,
                    equity=equity,
                    total_realized_pnl=engine.account.total_realized_pnl,
                )
            except Exception:
                logger.exception(
                    "[sim runner] equity_curve_append failed iter=%s path=%s",
                    iteration, equity_curve_path,
                )

        if stop_after_iterations is not None and iteration >= stop_after_iterations:
            logger.info("[sim runner] 達到 stop_after_iterations=%s，結束", stop_after_iterations)
            break

        # interval=0 主要給測試用（不睡）
        if interval > 0:
            time.sleep(interval)
