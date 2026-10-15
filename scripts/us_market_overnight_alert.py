#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股半夜風險提醒（給 W 個人飛書 DM）。

用途：W 實盤持有 00631L.TW，亞洲時間半夜需要知道美股/半導體/TSM ADR
是否出現會影響隔日台股風險偏好的大波動。

模式：
- open：台灣時間約 21:35（美股剛開盤）執行，固定推開盤雷達（不設門檻，資料夠新就推）。
- intraday：台灣時間約 01:30 執行，只在超過門檻時推送。
- close：台灣時間約 05:10 執行，固定推收盤摘要。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
import requests

# 避免本機代理造成 yfinance CONNECT 問題（沿用 stock_monitor.py 邏輯）
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import yfinance as yf  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim._notify import send_dm  # noqa: E402
from utils.market_data_contract import ChangeMetric, percent_change  # noqa: E402

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

LOG_PATH = _BASE_DIR / "logs" / "us_market_overnight_alert.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS: list[tuple[str, str, str]] = [
    ("^GSPC", "S&P 500", "index"),
    ("^IXIC", "Nasdaq", "index"),
    ("^DJI", "Dow Jones", "index"),
    ("QQQ", "QQQ", "tech"),
    ("SOXX", "SOXX 半導體", "semiconductor"),
    ("NVDA", "NVIDIA", "semiconductor"),
    ("TSM", "TSM ADR", "adr"),
    ("CL=F", "原油", "commodity"),
    ("GC=F", "黃金", "commodity"),
]
# 美股盤前（pre_us）：盤前指數無價，改用期貨＋個股 premarket（ES=F/NQ=F 已實測即時可抓）
PRE_US_SYMBOLS: list[tuple[str, str, str]] = [
    ("ES=F", "S&P500 期貨", "index"),
    ("NQ=F", "Nasdaq 期貨", "index"),
    ("NVDA", "NVIDIA", "semiconductor"),
    ("TSM", "TSM ADR", "adr"),
    ("SOXX", "SOXX 半導體", "semiconductor"),
    ("CL=F", "原油", "commodity"),
    ("GC=F", "黃金", "commodity"),
]
IMPORTANT_US_SYMBOLS = {"^GSPC", "^IXIC", "QQQ", "SOXX", "NVDA", "TSM", "ES=F", "NQ=F"}

WATCH_ONLY_CONFIG = _BASE_DIR / "config" / "sim_watch_only.yaml"
STATE_PATH = _BASE_DIR / "logs" / "us_market_overnight_state.json"
RESEARCH_PACK_US_CLOSE_ENABLED_ENV = "RESEARCH_PACK_US_CLOSE_ENABLED"
RESEARCH_PACK_US_CLOSE_SYMBOL_ENV = "RESEARCH_PACK_US_CLOSE_SYMBOL"
RESEARCH_PACK_US_MARKET_DATA_TYPE_ENV = "RESEARCH_PACK_US_MARKET_DATA_TYPE"
RESEARCH_PACK_US_CLOSE_CLIENT_ID_ENV = "RESEARCH_PACK_US_CLOSE_CLIENT_ID"
# 隔夜期貨領先指標（IBKR 主 + yfinance 兜底）：給 intraday/open/close 卡與台股盤前定調補期貨風向
OVERNIGHT_FUTURES_ENABLED_ENV = "OVERNIGHT_FUTURES_ENABLED"
OVERNIGHT_FUTURES_CLIENT_ID_ENV = "OVERNIGHT_FUTURES_CLIENT_ID"
OVERNIGHT_FUTURES_MARKET_DATA_TYPE_ENV = "OVERNIGHT_FUTURES_MARKET_DATA_TYPE"
# 全市場強勢股（IBKR 選股，技術掃榜）並入美股卡：flag 獨立於 CLI/飛書用的 US_STOCK_SCREENER_ENABLED
US_STOCK_SCREENER_CARD_ENABLED_ENV = "US_STOCK_SCREENER_CARD_ENABLED"
US_STOCK_SCREENER_CARD_CLIENT_ID_ENV = "US_STOCK_SCREENER_CARD_CLIENT_ID"
US_STOCK_SCREENER_CARD_TOP_ENV = "US_STOCK_SCREENER_CARD_TOP"


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    name: str
    category: str
    price: float
    change_pct: float
    previous_close: float | None = None
    asof: str = ""
    change_basis: str = "previous_close"
    source: str = "unknown"
    is_realtime: bool = False

    @property
    def change_metric(self) -> ChangeMetric:
        return ChangeMetric(
            value_pct=self.change_pct,
            basis=self.change_basis,
            source=self.source,
            asof=self.asof,
            is_realtime=self.is_realtime,
        )


@dataclass(frozen=True)
class MarketReasonNews:
    summary: str
    sources: list[str]


@dataclass(frozen=True)
class Position00631L:
    position_state: str = "holding"
    entry_price: float | None = None
    exit_price: float | None = None
    exit_date: str | None = None


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _fmt_price(value: float) -> str:
    return f"{value:,.2f}" if abs(value) < 1000 else f"{value:,.0f}"


def _arrow(value: float) -> str:
    if value > 0:
        return "▲"
    if value < 0:
        return "▼"
    return "▬"


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _redact_sensitive_text(value: Any) -> str:
    return re.sub(r"\bU\d{4,}\b", "U***", str(value or ""))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_00631l_position(path: Path = WATCH_ONLY_CONFIG) -> Position00631L:
    """讀 00631L 實盤人工帳本；失敗時保守視為 holding，但不硬猜價格。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = (data.get("watch_only") or {}).get("00631L.TW") or {}
        state = str(raw.get("position_state") or "holding").lower()
        if state not in {"holding", "flat"}:
            state = "holding"
        return Position00631L(
            position_state=state,
            entry_price=_to_float(raw.get("entry_price")),
            exit_price=_to_float(raw.get("exit_price")),
            exit_date=str(raw.get("exit_date")) if raw.get("exit_date") else None,
        )
    except Exception:
        logger.exception("讀取 00631L 實盤帳本失敗: %s", path)
        return Position00631L()


def fetch_us_market_quotes(
    symbols: list[tuple[str, str, str]] = DEFAULT_SYMBOLS,
    *,
    mode: str = "close",
    yf_download: Callable[..., Any] = yf.download,
    max_attempts: int = 3,
    retry_base_sec: float = 3.0,
) -> list[MarketQuote]:
    """抓美股行情。

    - close：用日線最後一根 close vs 前一日 close。
    - intraday：用 5m 最新價 vs 前一交易日 close，避免 01:30 盤中拿日線未完成資料。

    yfinance 半夜偶發 rate limit / 回空（曾導致該發的交易日靜默漏發一張卡），
    故對下載做重試 + 指數退避；全數失敗才放棄。
    """
    tickers = [s for s, _name, _cat in symbols]
    period = "2d" if mode in ("intraday", "open", "pre_us") else "5d"
    interval = "5m" if mode in ("intraday", "open", "pre_us") else "1d"
    raw = None
    for attempt in range(1, max_attempts + 1):
        try:
            raw = yf_download(
                tickers,
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
            if raw is not None and not getattr(raw, "empty", False):
                break
            logger.warning("yfinance 回傳空行情（第 %d/%d 次）", attempt, max_attempts)
        except Exception:
            logger.warning("yfinance 下載美股行情失敗（第 %d/%d 次）", attempt, max_attempts, exc_info=True)
        if attempt < max_attempts:
            time.sleep(retry_base_sec * (2 ** (attempt - 1)))
    if raw is None or getattr(raw, "empty", False):
        logger.error("yfinance 重試 %d 次仍無美股行情，放棄推送", max_attempts)
        return []

    out: list[MarketQuote] = []
    for symbol, name, category in symbols:
        try:
            if len(tickers) == 1:
                frame = raw
                if "Close" not in frame.columns and getattr(frame.columns, "nlevels", 1) > 1:
                    frame = frame[symbol]
            else:
                frame = raw[symbol]
            close = frame["Close"].dropna()
            if len(close) < 2:
                continue

            # Debug: 记录 mode=close 时的原始 close 序列（排查 08:15 系统性 previous_close 偏高 bug）
            if mode not in ("intraday", "open", "pre_us") and logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"{symbol} mode=close 原始 close 序列（共 {len(close)} 根）:\n"
                    + "\n".join(f"  [{i}] {idx.date() if hasattr(idx, 'date') else idx}: {val:.2f}"
                                for i, (idx, val) in enumerate(close.tail(5).items()))
                )

            # mode="close" 需过滤「今日未完成K线」，避免 pre_tw 08:15 拿到盘前/未来数据
            if mode not in ("intraday", "open", "pre_us"):
                # 取完整交易日收盘：排除「日期 >= 今天」的K线（无论当前几点）
                # Bug 场景：08:15 pre_tw 时 yfinance 可能返回「今日盘前/未完成K」，
                # 导致 price 正确但 previous_close 拿错（取到盘前 open/high 等），
                # 算出错误涨跌幅。严格只取 date < today 的K，确保收盘数据完整。
                from datetime import datetime as dt
                today_date = dt.now().date()
                last_k_date = close.index[-1].date() if hasattr(close.index[-1], 'date') else None
                if last_k_date and last_k_date >= today_date:
                    # 最后一根K是今天或更晚 → 跳过，取前两根；asof 也必须跟随实际采用的 last K。
                    if len(close) < 3:
                        continue  # 数据不足，跳过此标的
                    last_idx = -2
                    prev_idx = -3
                else:
                    # 正常情况：最后一根是完整收盘
                    last_idx = -1
                    prev_idx = -2
                last = float(close.iloc[last_idx])
                prev = float(close.iloc[prev_idx])
                asof_idx = close.index[last_idx]
            else:
                # intraday/open/pre_us：取当日最新价 vs 前交易日收盘（原有逻辑不变）
                last = float(close.iloc[-1])
                asof_idx = close.index[-1]
                dates = close.index.normalize()
                last_day = dates[-1]
                prev_close_series = close[dates < last_day]
                if prev_close_series.empty:
                    prev = float(close.iloc[-2])
                else:
                    prev = float(prev_close_series.iloc[-1])
            if prev <= 0 or last <= 0:
                continue
            metric = percent_change(
                last,
                prev,
                basis="previous_close",
                source="yfinance",
                asof=str(asof_idx),
                is_realtime=mode in {"intraday", "open", "pre_us"},
            )
            out.append(MarketQuote(
                symbol=symbol,
                name=name,
                category=category,
                price=last,
                previous_close=prev,
                change_pct=metric.value_pct or 0.0,
                asof=metric.asof,
                change_basis=metric.basis,
                source=metric.source,
                is_realtime=metric.is_realtime,
            ))
        except Exception:
            logger.warning("解析 %s 報價失敗", symbol, exc_info=True)
    logger.debug(f"fetch_us_market_quotes mode={mode} 返回 {len(out)} 个标的: {[q.symbol for q in out]}")
    return out


def fetch_quotes_via_yahoo_chart(
    symbols: list[tuple[str, str, str]] = DEFAULT_SYMBOLS,
    *,
    mode: str = "close",
    timeout: int = 10,
    requester: Callable[..., Any] = requests.get,
) -> list[MarketQuote]:
    """yfinance bulk 被限流/回空時的獨立備援：逐檔打 Yahoo chart v8。

    與 yfinance bulk 走不同路徑、較耐 rate limit；只取 meta 的最新價與前收，
    asof 用 regularMarketTime，讓 freshness gate 在週末仍能正確擋下舊資料。
    """
    out: list[MarketQuote] = []
    for symbol, name, category in symbols:
        try:
            resp = requester(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": "5d", "interval": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            results = (data.get("chart") or {}).get("result") or []
            meta = (results[0].get("meta") if results and isinstance(results[0], dict) else None) or {}
            last = _to_float(meta.get("regularMarketPrice"))
            prev = _to_float(meta.get("chartPreviousClose"))
            if prev is None:
                prev = _to_float(meta.get("previousClose"))
            if last is None or prev is None or prev <= 0 or last <= 0:
                continue
            asof = ""
            ts = meta.get("regularMarketTime")
            if ts:
                try:
                    asof = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                except (TypeError, ValueError, OSError):
                    asof = ""
            metric = percent_change(
                last,
                prev,
                basis="previous_close",
                source="yahoo_chart",
                asof=asof,
                is_realtime=mode in {"intraday", "open", "pre_us"},
            )
            out.append(MarketQuote(
                symbol=symbol,
                name=name,
                category=category,
                price=last,
                previous_close=prev,
                change_pct=metric.value_pct or 0.0,
                asof=metric.asof,
                change_basis=metric.basis,
                source=metric.source,
                is_realtime=metric.is_realtime,
            ))
        except Exception:
            logger.warning("Yahoo chart 備援讀取 %s 失敗", symbol, exc_info=True)
    if out:
        logger.info("Yahoo chart 備援取得 %d 檔美股行情", len(out))
    return out


# CNBC quote API 符號映射（第三層獨立兜底；非 Yahoo 系，Yahoo 全限流時仍可用）。
# 指數走 .SPX/.IXIC/.DJI、ETF/個股原樣、期貨走 @CL.1/@GC.1；未列出的不抓（不硬湊）。
_CNBC_SYMBOL_MAP: dict[str, str] = {
    "^GSPC": ".SPX",
    "^IXIC": ".IXIC",
    "^DJI": ".DJI",
    "QQQ": "QQQ",
    "SOXX": "SOXX",
    "NVDA": "NVDA",
    "TSM": "TSM",
    "CL=F": "@CL.1",
    "GC=F": "@GC.1",
}


def _normalize_cnbc_asof(raw: Any) -> str:
    """把 CNBC last_time（如 '2026-06-10T16:00:00.000-0400'）規範成可被 _latest_asof_dt
    解析的 ISO 字串：去毫秒、把無冒號時區偏移補成 '-04:00'。失敗回空字串。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    # 去毫秒：'...:00.000-0400' -> '...:00-0400'
    text = re.sub(r"\.\d+", "", text)
    # 無冒號時區偏移補冒號：'-0400' -> '-04:00'（fromisoformat 在舊版需要冒號）
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return ""
    return text


def fetch_quotes_via_cnbc(
    symbols: list[tuple[str, str, str]] = DEFAULT_SYMBOLS,
    *,
    mode: str = "close",
    timeout: int = 10,
    requester: Callable[..., Any] = requests.get,
) -> list[MarketQuote]:
    """第三層獨立兜底：CNBC quote API。與 Yahoo 完全不同基礎設施，Yahoo 系全限流時仍可用。

    只取最新價、前收與 last_time，逐檔請求；未在 _CNBC_SYMBOL_MAP 的標的直接跳過（不硬湊）。
    asof 用 CNBC last_time，讓 freshness gate 在週末仍能正確擋下舊資料。
    """
    out: list[MarketQuote] = []
    for symbol, name, category in symbols:
        cnbc_sym = _CNBC_SYMBOL_MAP.get(symbol)
        if not cnbc_sym:
            continue
        try:
            resp = requester(
                "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
                params={"symbols": cnbc_sym, "requestMethod": "itv", "output": "json"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            results = ((data.get("FormattedQuoteResult") or {}).get("FormattedQuote")) or []
            row = results[0] if results and isinstance(results[0], dict) else None
            if not row:
                continue
            last = _to_float(str(row.get("last") or "").replace(",", ""))
            prev = _to_float(str(row.get("previous_day_closing") or "").replace(",", ""))
            if last is None or prev is None or prev <= 0 or last <= 0:
                continue
            asof = _normalize_cnbc_asof(row.get("last_time"))
            metric = percent_change(
                last,
                prev,
                basis="previous_close",
                source="CNBC",
                asof=asof,
                is_realtime=mode in {"intraday", "open", "pre_us"},
            )
            out.append(MarketQuote(
                symbol=symbol,
                name=name,
                category=category,
                price=last,
                previous_close=prev,
                change_pct=metric.value_pct or 0.0,
                asof=metric.asof,
                change_basis=metric.basis,
                source=metric.source,
                is_realtime=metric.is_realtime,
            ))
        except Exception:
            logger.warning("CNBC 備援讀取 %s 失敗", symbol, exc_info=True)
    if out:
        logger.info("CNBC 備援取得 %d 檔美股行情", len(out))
    return out


def _alert_key(mode: str, quotes: list[MarketQuote]) -> str:
    """同一份行情資料同一模式只推一次，避免週末/重跑洗版。"""
    if not quotes:
        return f"{mode}:empty"
    latest_asof = max((q.asof or "") for q in quotes)
    return f"{mode}:{latest_asof}"


def _already_sent(key: str, path: Path = STATE_PATH) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return key in set(data.get("sent_keys") or [])


def _mark_sent(key: str, path: Path = STATE_PATH) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    keys = list(dict.fromkeys(list(data.get("sent_keys") or []) + [key]))[-50:]
    data["sent_keys"] = keys
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 跨輪快照：讓盤中/開盤卡能顯示「較上一輪」變化，避免連看三張幾乎一樣的卡 ──
ROUND_SNAPSHOT_KEY = "last_round"
_SNAPSHOT_CORE_SYMBOLS = ("^IXIC", "QQQ", "SOXX", "NVDA", "TSM", "^GSPC")
# 上一輪快照若超過此時數就視為跨日/跨大段，不再做變化對比（避免拿盤前對昨日收盤）
_PREV_ROUND_MAX_AGE_HOURS = 10.0


def _load_round_snapshot(path: Path = STATE_PATH) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    snap = data.get(ROUND_SNAPSHOT_KEY)
    return snap if isinstance(snap, dict) else None


def _save_round_snapshot(
    quotes: list[MarketQuote],
    mode: str,
    *,
    path: Path = STATE_PATH,
    now: datetime | None = None,
) -> None:
    """存當輪核心標的 change_pct 快照，供下一輪對比。失敗不擋主流程。"""
    now = now or datetime.now()
    snap_quotes = {
        q.symbol: round(q.change_pct, 2)
        for q in quotes
        if q.symbol in _SNAPSHOT_CORE_SYMBOLS
    }
    if not snap_quotes:
        return
    cur_asof = max((q.asof or "" for q in quotes), default="")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[ROUND_SNAPSHOT_KEY] = {
        "asof": cur_asof,
        "mode": mode,
        "saved_at": now.isoformat(timespec="minutes"),
        "quotes": snap_quotes,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("寫入 round snapshot 失敗")


def _delta_descriptor(prev_pct: float, cur_pct: float, *, flat_eps: float = 0.1) -> str:
    """描述本輪 change_pct 相對上一輪的變化（擴大/收斂/翻紅/翻黑/持平）。"""
    if abs(cur_pct - prev_pct) < flat_eps:
        return "大致持平"
    if prev_pct <= 0 < cur_pct:
        return "翻紅"
    if prev_pct >= 0 > cur_pct:
        return "翻黑"
    if cur_pct < 0:
        return "跌幅擴大" if cur_pct < prev_pct else "跌幅收斂"
    return "漲幅擴大" if cur_pct > prev_pct else "漲幅收斂"


def _prev_round_time_label(prev_round: dict) -> str:
    mode_label = {
        "intraday": "盤中", "open": "開盤", "pre_us": "盤前", "close": "收盤",
    }.get(str(prev_round.get("mode") or ""), "上輪")
    saved_at = str(prev_round.get("saved_at") or "")
    try:
        dt = datetime.fromisoformat(saved_at)
        return f"{mode_label} {dt.strftime('%H:%M')}"
    except ValueError:
        return mode_label


def _format_prev_round_line(
    quotes: list[MarketQuote],
    prev_round: dict | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """組『📊 較上一輪』變化行；無可比資料或跨日就回 None（不顯示）。"""
    if not prev_round:
        return None
    prev_quotes = prev_round.get("quotes") or {}
    if not isinstance(prev_quotes, dict) or not prev_quotes:
        return None
    # 跨日/跨大段（saved_at 太舊）不對比，避免拿盤前對昨日收盤
    saved_at = str(prev_round.get("saved_at") or "")
    try:
        saved_dt = datetime.fromisoformat(saved_at)
        ref = now or datetime.now()
        if (ref - saved_dt).total_seconds() / 3600.0 > _PREV_ROUND_MAX_AGE_HOURS:
            return None
    except ValueError:
        pass
    # 同一份行情（asof 相同）代表是重跑，沒有新變化可比
    cur_asof = max((q.asof or "" for q in quotes), default="")
    if prev_round.get("asof") and cur_asof and prev_round["asof"] == cur_asof:
        return None
    by_symbol = {q.symbol: q for q in quotes}
    order = [("^IXIC", "Nasdaq"), ("QQQ", "QQQ"), ("SOXX", "SOXX"), ("NVDA", "NVDA"), ("TSM", "TSM ADR")]
    # 每段必推後（無波動門檻），對比是區分「這張卡 vs 上一張」的關鍵：
    # 有變化的標的優先且全列，全體持平則明說「變化不大」而非讓卡看起來像重複推送。
    changed: list[str] = []
    flat: list[str] = []
    for sym, label in order:
        q = by_symbol.get(sym)
        if not q or sym not in prev_quotes:
            continue
        prev_pct = float(prev_quotes[sym])
        desc = _delta_descriptor(prev_pct, q.change_pct)
        entry = f"{label} {_fmt_pct(prev_pct)}→{_fmt_pct(q.change_pct)}（{desc}）"
        if desc == "大致持平":
            flat.append(entry)
        else:
            changed.append(entry)
    if not changed and not flat:
        return None
    label_prefix = f"📊 較上一輪（{_prev_round_time_label(prev_round)}）："
    if not changed:
        # 全體與上一輪持平：明確點出，避免連推顯得像重複卡
        return label_prefix + "核心標的較上一輪變化不大（" + "；".join(flat[:3]) + "）"
    # 有變化者全列在前；持平者收斂成一句尾註，避免洗版又不漏資訊
    line = label_prefix + "；".join(changed)
    if flat:
        line += f"；其餘 {len(flat)} 檔大致持平"
    return line


def _format_us_two_night_trend(
    quotes: list[MarketQuote],
    *,
    history_path: Path | None = None,
) -> str | None:
    """台股盤前專用：讀 logs/market_history_us.jsonl 最近兩晚美股『收盤』，
    把昨夜單點變成兩晚趨勢（以 Nasdaq 為主軸）。

    缺檔/不足兩晚/缺核心標的一律回 None（不擋整張卡，沿用零差錯原則）。
    """
    path = history_path or (_BASE_DIR / "logs" / "market_history_us.jsonl")
    try:
        if not path.exists():
            return None
        # 只看收盤筆，取最近兩個『不同交易日』的收盤，避免拿盤中/盤前混入趨勢
        close_rows: list[dict] = []
        seen_dates: list[str] = []
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if entry.get("mode") != "close":
                continue
            day = str(entry.get("ts") or "")[:10]
            if day in seen_dates:
                continue
            seen_dates.append(day)
            close_rows.append(entry)
            if len(close_rows) >= 2:
                break
    except Exception:
        logger.warning("讀取美股兩晚趨勢失敗（不擋盤前卡）", exc_info=True)
        return None

    if len(close_rows) < 2:
        return None
    # close_rows[0]=最新（昨夜）、close_rows[1]=前一晚
    last_q = (close_rows[0].get("quotes") or {})
    prev_q = (close_rows[1].get("quotes") or {})

    def _pct(qmap: dict, sym: str) -> float | None:
        raw = (qmap.get(sym) or {}).get("change_pct")
        return _to_float(raw)

    # 以 Nasdaq 為主軸；缺則退 QQQ
    sym, label = ("^IXIC", "Nasdaq")
    last_pct = _pct(last_q, sym)
    prev_pct = _pct(prev_q, sym)
    if last_pct is None or prev_pct is None:
        sym, label = ("QQQ", "QQQ")
        last_pct = _pct(last_q, sym)
        prev_pct = _pct(prev_q, sym)
    if last_pct is None or prev_pct is None:
        return None

    return (
        f"📈 美股兩晚趨勢：{label} 前夜 {_fmt_pct(prev_pct)} → 昨夜 {_fmt_pct(last_pct)}"
        f"（{_delta_descriptor(prev_pct, last_pct)}）"
    )


def _format_tw_correlation_close(
    *,
    history_path: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """台股盤前專用：讀 logs/market_history_tw.jsonl，取『昨日（排除今日）』最後一筆
    含相關性標的的紀錄，組一行 00631L/0050/2330 昨日收盤精簡對照。

    08:15 台股尚未開盤、抓不到即時現貨，故用本地軌跡回放昨日表現，讓盤前卡有本土
    相關性視角。缺檔/無昨日資料一律回 None（不擋整張卡，沿用零差錯原則）。
    """
    path = history_path or (_BASE_DIR / "logs" / "market_history_tw.jsonl")
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    try:
        if not path.exists():
            return None
        last_rec: dict | None = None
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            day = str(entry.get("ts") or "")[:10]
            if not day or day >= today:
                continue  # 排除今日與無時間戳，取昨日（或更早）最後一筆
            quotes = entry.get("quotes") or {}
            if any(s in quotes for s in ("00631L.TW", "0050.TW", "2330.TW")):
                last_rec = entry
                break
    except Exception:
        logger.warning("讀取台股相關性昨日收盤失敗（不擋盤前卡）", exc_info=True)
        return None

    if not last_rec:
        return None
    quotes = last_rec.get("quotes") or {}
    order = [("00631L.TW", "00631L"), ("0050.TW", "0050"), ("2330.TW", "台積電")]
    parts: list[str] = []
    for sym, label in order:
        raw = (quotes.get(sym) or {}).get("change_pct")
        pct = _to_float(raw)
        if pct is None:
            continue
        parts.append(f"{label} {_arrow(pct)} {_fmt_pct(pct)}")
    if not parts:
        return None
    return "🇹🇼 昨日台股相關性收盤：" + "；".join(parts)


FETCH_FAIL_ALERT_THRESHOLD = 2


def _record_fetch_outcome(
    success: bool,
    mode: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    path: Path = STATE_PATH,
    notifier: Callable[[str, str], bool] = send_dm,
    threshold: int = FETCH_FAIL_ALERT_THRESHOLD,
) -> bool:
    """記錄抓取結果，連續失敗時發飛書告警讓靜默漏發可視化。回傳是否發出告警。

    - 抓取成功（含週末抓到舊資料）→ 計數歸零、解除告警旗標，不發任何東西。
    - 連續失敗達門檻且該 streak 尚未告警 → 發一則 DM（streak 內只發一次），抓到行情自動解除。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    alerted = False
    if success:
        data["consecutive_fetch_fail"] = 0
        data["fetch_fail_alerted"] = False
    else:
        count = int(data.get("consecutive_fetch_fail") or 0) + 1
        data["consecutive_fetch_fail"] = count
        already = bool(data.get("fetch_fail_alerted"))
        if count >= threshold and not already and not dry_run:
            now = now or datetime.now()
            title = f"⚠️ 美股推播抓不到行情（連續 {count} 次）"
            content = (
                f"🕒 台灣時間 {now.strftime('%Y-%m-%d %H:%M')}\n"
                f"us_market_overnight_alert（mode={mode}）連續 {count} 次 "
                "yfinance + Yahoo chart 都抓不到美股行情，這幾次的美股/00631L 風險卡已靜默漏發。\n"
                "常見原因：被 Yahoo 限流（Too Many Requests）或網路問題。\n"
                f"建議：稍後手動重跑 python3 scripts/us_market_overnight_alert.py --mode {mode}；抓到行情後本告警自動解除。"
            )
            try:
                if notifier(title, content):
                    data["fetch_fail_alerted"] = True
                    alerted = True
            except Exception:
                logger.exception("發送抓取失敗告警 DM 失敗")

    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("寫入 us_market state 失敗")
    return alerted


def _latest_asof_dt(
    quotes: list[MarketQuote],
    *,
    symbols: set[str] | None = None,
) -> datetime | None:
    rows = [q for q in quotes if symbols is None or q.symbol in symbols]
    raw = max((q.asof or "") for q in rows) if rows else ""
    if not raw:
        return None
    # pandas Timestamp.__str__ 通常是 `YYYY-mm-dd HH:MM:SS[-04:00]`，fromisoformat 可解析。
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(raw.split(".")[0])
        except ValueError:
            return None


def is_intraday_data_fresh(
    quotes: list[MarketQuote],
    *,
    now: datetime | None = None,
    max_age_hours: float = 8.0,
) -> bool:
    """盤中提醒需確認美股/半導體 5m 資料夠新，避免週末拿週五收盤誤報。"""
    asof = _latest_asof_dt(quotes, symbols=IMPORTANT_US_SYMBOLS)
    if asof is None:
        return False
    if now is None:
        now = datetime.now(asof.tzinfo) if asof.tzinfo else datetime.now()
    if asof.tzinfo and now.tzinfo is None:
        now = now.replace(tzinfo=asof.tzinfo)
    if now.tzinfo and asof.tzinfo is None:
        asof = asof.replace(tzinfo=now.tzinfo)
    age_hours = (now - asof).total_seconds() / 3600.0
    return 0 <= age_hours <= max_age_hours


def is_close_data_recent(
    quotes: list[MarketQuote],
    *,
    now: datetime | None = None,
    max_age_hours: float = 40.0,
) -> bool:
    """收盤摘要只接受近期美股日線，避免週日/週一重推舊收盤。"""
    asof = _latest_asof_dt(quotes, symbols=IMPORTANT_US_SYMBOLS)
    logger.debug(f"is_close_data_recent: asof={asof}, now={now or datetime.now()}")
    if asof is None:
        return False
    if now is None:
        now = datetime.now(asof.tzinfo) if asof.tzinfo else datetime.now()
    if asof.tzinfo and now.tzinfo is None:
        now = now.replace(tzinfo=asof.tzinfo)
    if now.tzinfo and asof.tzinfo is None:
        asof = asof.replace(tzinfo=now.tzinfo)
    age_hours = (now - asof).total_seconds() / 3600.0
    logger.debug(f"is_close_data_recent: age_hours={age_hours:.1f}, max={max_age_hours}, 判断={'通过' if 0 <= age_hours <= max_age_hours else '过旧'}")
    return 0 <= age_hours <= max_age_hours


def should_push_intraday(quotes: list[MarketQuote], *, up_threshold_pct: float = 1.5, down_threshold_pct: float = 1.0) -> bool:
    """盤中只推較大波動、避免半夜洗版；門檻**不對稱**——下跌更敏感（跌 >=1% 就推、漲 >=1.5% 才推）。
    對 00631L 這種 2 倍槓桿/空手再進場，下跌風險（殺盤、接貨機會逼近）的及時性優先於上漲。"""
    return any(
        q.symbol in IMPORTANT_US_SYMBOLS
        and (q.change_pct >= up_threshold_pct or q.change_pct <= -down_threshold_pct)
        for q in quotes
    )


def _risk_level(quotes: list[MarketQuote]) -> tuple[str, str]:
    severe = any(q.symbol in {"^IXIC", "QQQ", "SOXX", "NVDA", "TSM"} and q.change_pct <= -2.0 for q in quotes)
    strong = any(q.symbol in {"^IXIC", "QQQ", "SOXX", "NVDA", "TSM"} and q.change_pct >= 2.0 for q in quotes)
    weak = any(q.symbol in {"^IXIC", "QQQ", "SOXX", "NVDA", "TSM"} and q.change_pct <= -1.0 for q in quotes)
    if severe:
        return "🚨", "美股科技/半導體明顯轉弱；若 00631L 仍持倉要優先檢查保護線，若已空手則再進場批次延後。"
    if strong:
        return "🟢", "美股科技/半導體偏強，00631L 隔日可觀察是否延續風險偏好，但仍避免追高。"
    if weak:
        return "⚠️", "美股科技/半導體偏弱，00631L 隔日需提高警覺，先看開盤承接與台積電權值表現。"
    return "🌙", "美股整體未見極端訊號，00631L 隔日以目前實盤狀態與既定風控/再進場規劃觀察即可。"


def _market_direction(quotes: list[MarketQuote]) -> str:
    core = [q for q in quotes if q.symbol in {"^IXIC", "QQQ", "SOXX", "NVDA", "TSM"}]
    if not core:
        return "mixed"
    avg = sum(q.change_pct for q in core) / len(core)
    if avg <= -1.0:
        return "falling"
    if avg >= 1.0:
        return "rising"
    return "mixed"


def _build_reason_query(quotes: list[MarketQuote], *, mode: str) -> str:
    direction = _market_direction(quotes)
    direction_terms = {
        "falling": "why are US stocks falling today Nasdaq semiconductor Nvidia TSM ADR Treasury yields Fed inflation",
        "rising": "why are US stocks rising today Nasdaq semiconductor Nvidia TSM ADR Fed inflation earnings",
        "mixed": "US stock market today Nasdaq semiconductor Nvidia TSM ADR market drivers Fed yields earnings",
    }[direction]
    mode_term = "intraday" if mode in ("intraday", "open") else "close recap"
    return f"{direction_terms} {mode_term} latest market news"


def _is_relevant_reason_title(title: str) -> bool:
    text = title.lower()  # 中文不受 lower() 影響，可與英文關鍵字同表比對
    market_terms = (
        "stock", "stocks", "market", "nasdaq", "s&p", "dow", "qqq", "russell", "futures",
        # 台股/中文市場詞：對台股更直接的新聞常用這些詞而非英文
        "台股", "加權", "台積電", "台積", "權值", "外資", "法人", "費半", "費城半導體", "大盤", "盤勢",
    )
    chip_terms = (
        "nvidia", "tsm", "tsmc", "semiconductor", "chip", "chips", "soxx", "amd", "broadcom",
        "asml", "foundry", "wafer", "hbm",
        # 供應鏈/中文半導體詞
        "半導體", "晶片", "晶圓", "封測", "ai 伺服器", "ai server", "供應鏈", "supply chain",
    )
    macro_terms = (
        "fed", "fomc", "powell", "rate", "rates", "yield", "yields", "inflation", "cpi", "ppi",
        "treasury", "jobs", "payroll", "recession",
        # 匯率/關稅/地緣政治（對台股估值與外資流向直接相關）
        "tariff", "tariffs", "trade war", "sanction", "sanctions", "export control", "geopolit",
        "dollar", "yen", "currency", "fx",
        "關稅", "貿易", "制裁", "出口管制", "升息", "降息", "通膨", "殖利率", "匯率", "台幣", "美元",
        "地緣", "升溫", "緊張",
    )
    move_terms = (
        "sell-off", "selloff", "slump", "drop", "drops", "fall", "falls", "sink", "sinks",
        "rally", "rise", "rises", "tumble", "tumbles", "surge", "surges", "plunge", "jump", "soar",
        # 中文漲跌詞
        "大漲", "大跌", "重挫", "崩跌", "暴跌", "走弱", "走強", "反彈", "回檔", "上漲", "下跌", "收紅", "收黑",
    )
    has_cjk = any("一" <= c <= "鿿" for c in text)
    # 英文用 word boundary 避免誤命中（如 said/maintain）；中文標題沒有英文 word boundary，放寬為含「ai」子字串
    ai_hit = re.search(r"\bai\b", text) is not None or (has_cjk and "ai" in text)
    score = 0
    score += 1 if any(k in text for k in market_terms) else 0
    score += 1 if any(k in text for k in chip_terms) else 0
    score += 1 if any(k in text for k in macro_terms) else 0
    score += 1 if any(k in text for k in move_terms) else 0
    # 單純「AI 投資建議」不算原因；AI 必須搭配市場/半導體/漲跌/宏觀線索。
    if ai_hit and score >= 1:
        score += 1
    return score >= 2


def _build_yahoo_reason_queries(quotes: list[MarketQuote], *, mode: str) -> list[str]:
    direction = _market_direction(quotes)
    if direction == "falling":
        return [
            "Stock Market Today Nasdaq Nvidia chip sell-off rate worries",
            "Nasdaq down today Nvidia semiconductor stocks why",
            "TSM ADR Nvidia Nasdaq market today falling",
        ]
    if direction == "rising":
        return [
            "Stock Market Today Nasdaq Nvidia chip rally Fed yields",
            "Nasdaq up today Nvidia semiconductor stocks why",
            "TSM ADR Nvidia Nasdaq market today rising",
        ]
    return [
        "Stock Market Today Nasdaq Nvidia semiconductor market drivers",
        "TSM ADR Nvidia Nasdaq market today",
    ]


def _parse_yahoo_news_items(items: list[Any]) -> list[str]:
    sources: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _truncate(item.get("title") or "", 110)
        if not title or not _is_relevant_reason_title(title):
            continue
        publisher = _truncate(item.get("publisher") or "", 30)
        if publisher:
            sources.append(f"{title}（{publisher}）")
        else:
            sources.append(title)
        if len(sources) >= 5:
            break
    return sources


def _extract_news_title(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content") if isinstance(item.get("content"), dict) else item
    if not isinstance(content, dict):
        return ""
    return str(content.get("title") or "").strip()


def fetch_yfinance_ticker_news(
    *,
    yf_ticker_factory: Callable[[str], Any] = yf.Ticker,
) -> MarketReasonNews | None:
    """免 key ticker news fallback；通常比 Yahoo search query 更貼近市場原因。"""
    collected: list[str] = []
    for symbol in ["^IXIC", "NVDA", "TSM", "QQQ"]:
        try:
            items = yf_ticker_factory(symbol).news or []
        except Exception:
            logger.exception("yfinance ticker news 讀取失敗: %s", symbol)
            continue
        for item in items[:10]:
            title = _truncate(_extract_news_title(item), 120)
            if not title or not _is_relevant_reason_title(title):
                continue
            if title not in collected:
                collected.append(title)
            if len(collected) >= 5:
                break
        if len(collected) >= 5:
            break
    if not collected:
        return None
    return MarketReasonNews(summary="新聞標題線索如下；實際因果仍需盤後確認。", sources=collected[:5])


def fetch_yahoo_reason_news(
    quotes: list[MarketQuote],
    *,
    mode: str,
    timeout: int = 10,
    requester: Callable[..., Any] = requests.get,
) -> MarketReasonNews | None:
    """免 key Yahoo Finance news fallback，只提供來源標題線索。"""
    collected: list[str] = []
    for query in _build_yahoo_reason_queries(quotes, mode=mode):
        try:
            resp = requester(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": query, "newsCount": 15, "quotesCount": 0},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Yahoo Finance 原因新聞搜尋失敗")
            continue
        for source in _parse_yahoo_news_items(data.get("news") or []):
            if source not in collected:
                collected.append(source)
            if len(collected) >= 5:
                break
        if len(collected) >= 5:
            break
    if not collected:
        return None
    return MarketReasonNews(summary="新聞標題線索如下；實際因果仍需盤後確認。", sources=collected[:5])


def _fallback_reason_news(quotes: list[MarketQuote], *, mode: str) -> MarketReasonNews | None:
    return fetch_yfinance_ticker_news() or fetch_yahoo_reason_news(quotes, mode=mode)


def fetch_market_reason_news(
    quotes: list[MarketQuote],
    *,
    mode: str,
    timeout: int = 12,
    requester: Callable[..., Any] = requests.post,
) -> MarketReasonNews | None:
    """搜尋美股漲跌可能原因。失敗回 None，不阻斷行情卡。"""
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        logger.info("TAVILY_API_KEY 未設定，改用 yfinance/Yahoo Finance news fallback")
        return _fallback_reason_news(quotes, mode=mode)
    query = _build_reason_query(quotes, mode=mode)
    try:
        resp = requester(
            "https://api.tavily.com/search",
            json={
                "api_key": tavily_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 4,
                "include_answer": True,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("美股原因搜尋失敗，改用 yfinance/Yahoo Finance news fallback")
        return _fallback_reason_news(quotes, mode=mode)

    answer = _truncate(data.get("answer") or "", 260)
    source_limit = 5 if mode in {"open", "intraday"} else 3
    sources: list[str] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = _truncate(item.get("title") or "", 120)
        if title:
            sources.append(title)
        if len(sources) >= source_limit:
            break
    if not answer and not sources:
        return _fallback_reason_news(quotes, mode=mode)
    return MarketReasonNews(summary=answer, sources=sources)


def _dominant_quote(quotes: list[MarketQuote], symbols: set[str]) -> MarketQuote | None:
    candidates = [q for q in quotes if q.symbol in symbols]
    if not candidates:
        return None
    return max(candidates, key=lambda q: abs(q.change_pct))


def _direction_word(change_pct: float, *, neutral_threshold: float = 0.3) -> str:
    if change_pct >= neutral_threshold:
        return "偏強"
    if change_pct <= -neutral_threshold:
        return "偏弱"
    return "震盪"


def _build_market_interpretation(quotes: list[MarketQuote], reason_news: MarketReasonNews | None, *, mode: str, llm_read: str | None = None) -> list[str]:
    """規則化中文摘要：只做同步解讀，不把新聞標題硬寫成確定因果。

    若有 llm_read（LLM 研判），優先放在最前面當『本輪研判』，規則化解讀仍保留為可追溯輔助。
    """
    by_symbol = {q.symbol: q for q in quotes}
    tech = _dominant_quote(quotes, {"^IXIC", "QQQ", "NQ=F"})
    semi = _dominant_quote(quotes, {"SOXX", "NVDA", "TSM"})
    macro_bits: list[str] = []
    oil = by_symbol.get("CL=F")
    gold = by_symbol.get("GC=F")
    if oil and abs(oil.change_pct) >= 0.8:
        macro_bits.append(f"原油{_direction_word(oil.change_pct)}（{_fmt_pct(oil.change_pct)}）")
    if gold and abs(gold.change_pct) >= 0.6:
        macro_bits.append(f"黃金{_direction_word(gold.change_pct)}（{_fmt_pct(gold.change_pct)}）")

    source_count = len(reason_news.sources) if reason_news else 0
    lines = ["", "二、新聞面與市場解讀（中文同步；因果需盤後再確認）"]
    if llm_read:
        lines.append(f"- 本輪研判：{llm_read}")
    if tech:
        lines.append(f"- 科技線目前{_direction_word(tech.change_pct)}：{tech.name} {_arrow(tech.change_pct)} {_fmt_pct(tech.change_pct)}，先判斷開盤第一小時是延續還是反轉。")
    if semi:
        lines.append(f"- 半導體線索目前{_direction_word(semi.change_pct)}：{semi.name} {_arrow(semi.change_pct)} {_fmt_pct(semi.change_pct)}，會直接牽動台積電/AI 鏈與 00631L 隔日風險偏好。")
    if macro_bits:
        lines.append(f"- 跨市場訊號：{'、'.join(macro_bits)}；若同時伴隨利率/美元新聞，隔日台股估值壓力要提高權重。")
    if reason_news and reason_news.summary:
        lines.append(f"- 新聞摘要：{_truncate(reason_news.summary, 260)}")
    elif source_count:
        lines.append("- 新聞摘要：目前主要來自標題線索，尚不足以斷定單一因果；先當作市場敘事方向，不當成交易依據。")
    else:
        lines.append("- 新聞摘要：暫無可靠新聞摘要，先以價格風險與核心標的同步性為主，不把單次價格波動直接視為確定因果。")
    if mode == "open" and source_count:
        lines.append(f"- 開盤新聞密度：已抓到 {source_count} 條相關來源，重點看是否集中在 Fed/利率、AI/晶片、財報或地緣政治，而不是只看指數漲跌。")
    if mode == "intraday" and source_count:
        lines.append(f"- 盤中新聞密度：已抓到 {source_count} 條相關來源；這張卡是依本輪行情與新聞重新整理，不是固定模板回覆。")
    return lines


def _format_reason_section(reason_news: MarketReasonNews | None, *, quotes: list[MarketQuote] | None = None, mode: str = "close", llm_read: str | None = None) -> list[str]:
    if quotes is not None and mode in {"open", "intraday", "pre_us", "pre_tw", "close"}:
        lines = _build_market_interpretation(quotes, reason_news, mode=mode, llm_read=llm_read)
        source_limit = 5 if mode in {"open", "intraday"} else 3
    else:
        lines = ["", "二、可能原因（市場解讀，需盤後再確認）"]
        source_limit = 5 if mode in {"open", "intraday"} else 3
        if reason_news is None:
            lines.append("- 暫無可靠新聞摘要，先以價格風險為主；不要把單次價格波動直接視為確定因果。")
            return lines
        if reason_news.summary:
            lines.append(f"- {_truncate(reason_news.summary, 260)}")
    if reason_news is None:
        return lines
    for source in reason_news.sources[:source_limit]:
        lines.append(f"- 來源線索：{_truncate(source, 120)}")
    return lines


def _format_flow_value(value: Any, *, unit: str) -> str:
    num = _to_float(value)
    if num is None:
        return "無資料"
    direction = "買超" if num >= 0 else "賣超"
    amount = abs(num)
    if unit == "億元":
        return f"{direction} {amount:.2f} 億元"
    return f"{direction} {amount:,.1f} {unit}"


def _format_tw_institutional_section(institutional: dict | None) -> list[str]:
    """台股盤前專用：格式化昨日法人與權值股籌碼；無資料時不擋主卡。"""
    if not institutional:
        return []

    date = str(institutional.get("date") or "").strip()
    title = "📌 昨日台股法人籌碼"
    if date:
        title += f"（{date}）"
    lines = ["", title]
    lines.append(
        "- 三大法人合計："
        f"{_format_flow_value(institutional.get('total_net'), unit='億元')}；"
        f"外資 {_format_flow_value(institutional.get('foreign'), unit='億元')}、"
        f"投信 {_format_flow_value(institutional.get('trust'), unit='億元')}、"
        f"自營商 {_format_flow_value(institutional.get('dealer'), unit='億元')}。"
    )

    chips = institutional.get("chips") or {}
    chip_lines: list[str] = []
    if isinstance(chips, dict):
        # 2330/2317 優先顯示（0050 最大權值），其餘相關性權值股依序接後
        priority = ["2330", "2317"]
        ordered_ids = priority + [sid for sid in chips.keys() if sid not in priority]
        for stock_id in ordered_ids:
            chip = chips.get(stock_id)
            if not isinstance(chip, dict):
                continue
            name = str(chip.get("name") or stock_id).strip()
            chip_lines.append(
                f"{name}({stock_id}) 三大法人{_format_flow_value(chip.get('total_lots'), unit='張')}"
                f"，外資{_format_flow_value(chip.get('foreign_lots'), unit='張')}"
                f"、投信{_format_flow_value(chip.get('trust_lots'), unit='張')}"
            )
    if chip_lines:
        lines.append("- 權值股籌碼：" + "；".join(chip_lines) + "。")
    return lines


def _format_dynamic_watch_section(
    quotes: list[MarketQuote],
    *,
    mode: str,
    position_00631l: Position00631L | None,
) -> list[str]:
    if mode not in {"open", "intraday", "pre_us", "pre_tw", "close"}:
        return []
    by_symbol = {q.symbol: q for q in quotes}
    nasdaq = by_symbol.get("^IXIC") or by_symbol.get("QQQ") or by_symbol.get("NQ=F")
    soxx = by_symbol.get("SOXX")
    nvda = by_symbol.get("NVDA")
    tsm = by_symbol.get("TSM")
    oil = by_symbol.get("CL=F")
    gold = by_symbol.get("GC=F")

    section_title = {
        "open": "四、開盤後優先觀察",
        "intraday": "四、盤中動態觀察",
        "close": "四、收盤定調與隔日準備",
        "pre_us": "四、盤前布局觀察",
    }.get(mode, "四、接下來優先觀察")
    lines = ["", section_title]
    if nasdaq:
        if mode == "intraday":
            lines.append(f"- 科技主線：{nasdaq.name} {_arrow(nasdaq.change_pct)} {_fmt_pct(nasdaq.change_pct)}，重點看跌幅/漲幅是否繼續擴大；若 30 分鐘內收斂，隔日台股壓力可降一級。")
        elif mode == "close":
            lines.append(f"- 科技主線：{nasdaq.name} {_arrow(nasdaq.change_pct)} {_fmt_pct(nasdaq.change_pct)}（今夜收盤定調）；這是隔日台股的隔夜基準，明早先對照台積電/0050 開盤是否同調。")
        elif mode == "pre_us":
            lines.append(f"- 科技主線：{nasdaq.name} {_arrow(nasdaq.change_pct)} {_fmt_pct(nasdaq.change_pct)}（盤前期貨/premarket）；正式開盤可能跳動，先當方向參考不當定論。")
        elif mode == "pre_tw":
            lines.append(f"- 科技主線：{nasdaq.name} {_arrow(nasdaq.change_pct)} {_fmt_pct(nasdaq.change_pct)}（昨夜收盤）；這是今日台股的隔夜基準，開盤先看台積電/0050 是否同步反應。")
        else:
            lines.append(f"- 科技主線：{nasdaq.name} {_arrow(nasdaq.change_pct)} {_fmt_pct(nasdaq.change_pct)}，先看開盤 30–60 分鐘能否站穩；若快速翻黑，隔日台股追價風險升高。")
    if soxx or nvda or tsm:
        parts = []
        if soxx:
            parts.append(f"SOXX {_fmt_pct(soxx.change_pct)}")
        if nvda:
            parts.append(f"NVDA {_fmt_pct(nvda.change_pct)}")
        if tsm:
            parts.append(f"TSM ADR {_fmt_pct(tsm.change_pct)}")
        tense = "今日" if mode == "pre_tw" else "隔日"
        lines.append(f"- 半導體同步性：{' / '.join(parts)}；三者同向比單一個股更重要，會影響台積電、0050 與 00631L {tense}情緒。")
    if oil or gold:
        macro = []
        if oil:
            macro.append(f"原油 {_fmt_pct(oil.change_pct)}")
        if gold:
            macro.append(f"黃金 {_fmt_pct(gold.change_pct)}")
        tense = "今日" if mode == "pre_tw" else "隔日"
        lines.append(f"- 風險資產背景：{' / '.join(macro)}；若避險資產走強但科技股也轉弱，{tense}台股先降低進攻假設。")
    if position_00631l is not None and position_00631l.position_state == "flat":
        if mode == "intraday":
            lines.append("- 00631L 空手盤中雷達：這輪只更新隔日風向；若半導體跌幅擴大，明天再進場條件要更嚴格，不能只因低接念頭提前預設買點。")
        elif mode == "close":
            lines.append("- 00631L 空手收盤定調：用今夜收盤把隔日再進場劇本定下來；明早開盤先看台積電/0050 承接與量能，不在收盤就鎖死買點。")
        elif mode == "pre_us":
            lines.append("- 00631L 空手盤前布局：盤前只先抓方向假設，等正式開盤站穩再驗證；別用盤前跳動就預設今晚的進場決定。")
        else:
            lines.append("- 00631L 空手雷達：今晚只判斷風向，不急著預設買點；明天仍要等台積電/0050 開盤承接、量能與既定再進場條件確認。")
    else:
        if mode == "intraday":
            lines.append("- 00631L 持倉盤中雷達：若 TSM ADR、SOXX/NVDA、Nasdaq 同步惡化，隔日先把防守情境放在進攻情境之前。")
        elif mode == "close":
            lines.append("- 00631L 持倉收盤定調：用今夜收盤把隔日保護線情境確定下來；若科技與半導體收盤同步走弱，明早先把防守權重放前面。")
        elif mode == "pre_us":
            lines.append("- 00631L 持倉盤前布局：盤前先預想隔日保護線情境；正式開盤前的跳動只當參考，不在盤前就調整倉位決定。")
        else:
            lines.append("- 00631L 持倉雷達：今晚先檢查隔日保護線情境；若美股科技與半導體不同步，不用只因單一新聞或單一 ADR 漲跌調整倉位。")
    return lines


_REGIME_STANCE = {
    ("flat", "risk_up"): "風險升溫，隔日再進場條件要更嚴格：先確認台股開盤承接與半導體止穩，不能只因低接念頭預設買點。",
    ("flat", "risk_down"): "風險降溫，可把低接觀察重新打開一級；但仍要等台積電/0050 開盤站穩與量能確認，不急著追第一根。",
    ("flat", "diverge"): "訊號分歧、方向未定，隔日先按兵不動：等半導體收斂為一致方向、台股開盤表態後再評估是否進場。",
    ("flat", "calm"): "波動平穩，維持原本再進場條件即可：沒有新的利空擴散前，不因單點消息提前預設買點。",
    ("holding", "risk_up"): "風險升溫，隔日把防守權重放到進攻之前：優先檢視保護線情境，若量縮下跌避免凹單，不靠單一反彈幻想撐倉。",
    ("holding", "risk_down"): "風險降溫，原本的防守情境可降一級：但別急著加碼翻多，先看隔日台股能否同步回穩再決定。",
    ("holding", "diverge"): "訊號分歧、方向未定，隔日不宜重壓任一方向：別只看單一 ADR 漲跌調倉，等台股開盤承接表態。",
    ("holding", "calm"): "波動平穩，維持原規劃：不因單點新聞或單一 ADR 漲跌調整倉位，保護線情境照既定計畫。",
}


def _format_trade_playbook(
    mode: str,
    *,
    position_00631l: Position00631L | None,
    title: str = "四、操作口徑",
    regime: dict[str, str] | None = None,
    playbook_read: str | None = None,
) -> list[str]:
    lines = ["", title]
    prefix = {"open": "開盤雷達", "intraday": "盤中動態雷達", "close": "收盤定調", "pre_us": "盤前雷達"}.get(mode, "風險提醒")
    is_flat = position_00631l is not None and position_00631l.position_state == "flat"
    state_key = "flat" if is_flat else "holding"

    # 開場：本輪盤勢一句（規則化判斷，跟著行情變）
    if regime and regime.get("regime_sentence"):
        lines.append(f"- 本輪盤勢：{regime['regime_sentence']}")

    # pre_tw 是當日早上 08:15 的卡，時態用「今日」；其餘時段（盤前/盤中/收盤定隔日）維持「隔日」。
    def _tense(text: str) -> str:
        if mode != "pre_tw":
            return text
        return text.replace("隔日", "今日").replace("明天", "今日")

    # 免責一句（保留原本邊界宣告）
    if is_flat:
        lines.append(_tense(f"- 這是{prefix}，不是自動交易指令；00631L 已空手，重點是隔日再進場條件與生命線防守，不是減碼/拉保護線。"))
    else:
        lines.append(_tense(f"- 這是{prefix}，不是自動交易指令；00631L 是否減碼/拉保護線，仍以隔日台股開盤、0050/台積電表現與你的實盤規劃為準。"))

    # 口徑主體：優先 LM 口徑，失敗退回規則化（依 regime × 持倉狀態）
    if playbook_read:
        lines.append(f"- 口徑建議：{playbook_read}（僅供人工參考）")
    else:
        regime_name = (regime or {}).get("regime", "calm")
        stance = _REGIME_STANCE.get((state_key, regime_name)) or _REGIME_STANCE[(state_key, "calm")]
        lines.append(_tense(f"- 口徑建議：{stance}"))

    # 待確認訊號（規則化，跟著行情變）
    if regime and regime.get("key_signal"):
        lines.append(f"- 待確認訊號：{regime['key_signal']}")
    return lines


def _format_latest_asof_line(quotes: list[MarketQuote], *, mode: str) -> str:
    asof = _latest_asof_dt(quotes, symbols=IMPORTANT_US_SYMBOLS)
    if asof is None:
        return "📡 資料時間：未取得核心行情時間戳，僅供保守參考"
    return f"📡 資料時間：{asof.isoformat(sep=' ', timespec='minutes')}（{mode} 本輪行情）"


def _format_quotes_for_research(quotes: list[MarketQuote]) -> str:
    if not quotes:
        return "（無行情資料）"
    order = ["^GSPC", "^IXIC", "^DJI", "QQQ", "SOXX", "NVDA", "TSM", "CL=F", "GC=F"]
    by_symbol = {q.symbol: q for q in quotes}
    lines: list[str] = []
    for sym in order:
        q = by_symbol.get(sym)
        if not q:
            continue
        asof = f"；asof={q.asof}" if q.asof else ""
        metric = q.change_metric
        lines.append(
            f"- {q.name}（{q.symbol}）：{_fmt_price(q.price)} {_arrow(q.change_pct)} {_fmt_pct(q.change_pct)}"
            f"；basis={metric.basis}；source={metric.source}；is_realtime={metric.is_realtime}{asof}"
        )
    return "\n".join(lines) if lines else "（無核心行情資料）"


def _env_flag_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _research_pack_close_settings() -> dict[str, Any]:
    return {
        "enabled": _env_flag_enabled(RESEARCH_PACK_US_CLOSE_ENABLED_ENV),
        "symbol": os.getenv(RESEARCH_PACK_US_CLOSE_SYMBOL_ENV, "SPY").strip().upper() or "SPY",
        "market_data_type": os.getenv(RESEARCH_PACK_US_MARKET_DATA_TYPE_ENV, "delayed").strip().lower() or "delayed",
        "client_id": int(os.getenv(RESEARCH_PACK_US_CLOSE_CLIENT_ID_ENV, "481")),
    }


_OVERNIGHT_FUTURES_DISPLAY = {
    # IBKR 連續期貨 symbol -> (卡片/yfinance symbol, 顯示名)
    "ES": ("ES=F", "S&P500 期貨"),
    "NQ": ("NQ=F", "Nasdaq 期貨"),
}
_OVERNIGHT_FUTURES_YF_SYMBOLS: list[tuple[str, str, str]] = [
    ("ES=F", "S&P500 期貨", "index"),
    ("NQ=F", "Nasdaq 期貨", "index"),
]


def _overnight_futures_settings() -> dict[str, Any]:
    return {
        "enabled": _env_flag_enabled(OVERNIGHT_FUTURES_ENABLED_ENV),
        "market_data_type": os.getenv(OVERNIGHT_FUTURES_MARKET_DATA_TYPE_ENV, "delayed").strip().lower() or "delayed",
        "client_id": int(os.getenv(OVERNIGHT_FUTURES_CLIENT_ID_ENV, "482")),
    }


def _fetch_overnight_futures_quotes(mode: str) -> list[MarketQuote]:
    """隔夜期貨領先指標：IBKR delayed 為主、yfinance ES=F/NQ=F 兜底；flag 關或全失敗回 []。

    期貨只供卡片快照顯示，不併入主 quotes（不影響 freshness / 去重 / market_history / Research W）。
    change_pct 相對前結算/前收，屬延遲領先參考、非即時行情。
    """
    settings = _overnight_futures_settings()
    if not settings["enabled"]:
        return []
    # IBKR 主源（read-only delayed）
    try:
        from utils.ibkr_provider import fetch_overnight_futures as _ibkr_overnight_futures

        out: list[MarketQuote] = []
        for f in _ibkr_overnight_futures(
            client_id=settings["client_id"],
            market_data_type=settings["market_data_type"],
        ):
            disp = _OVERNIGHT_FUTURES_DISPLAY.get(f.symbol)
            if not disp or f.permission_status != "ok" or f.last is None or not f.close:
                continue
            yf_symbol, yf_name = disp
            out.append(MarketQuote(
                symbol=yf_symbol,
                name=yf_name,
                category="index",
                price=float(f.last),
                change_pct=float(f.change_pct) if f.change_pct is not None else 0.0,
                previous_close=float(f.close),
                asof=f.asof,
            ))
        if out:
            return out
        logger.info("IBKR 隔夜期貨無有效報價，改用 yfinance 兜底")
    except Exception as exc:
        logger.warning("IBKR 隔夜期貨取價失敗，改用 yfinance 兜底: %s", _redact_sensitive_text(exc))
    # yfinance 兜底（與三層行情 fallback 哲學一致）
    try:
        return fetch_us_market_quotes(symbols=_OVERNIGHT_FUTURES_YF_SYMBOLS, mode=mode)
    except Exception:
        logger.warning("yfinance 隔夜期貨兜底亦失敗，略過期貨區塊", exc_info=True)
        return []


def _us_screener_card_settings() -> dict[str, Any]:
    return {
        "enabled": _env_flag_enabled(US_STOCK_SCREENER_CARD_ENABLED_ENV),
        "client_id": int(os.getenv(US_STOCK_SCREENER_CARD_CLIENT_ID_ENV, "485")),
        "top": int(os.getenv(US_STOCK_SCREENER_CARD_TOP_ENV, "5")),
    }


def _fetch_us_screener_picks(mode: str) -> list:
    """美股卡內嵌選股：IBKR 技術掃榜 top N。flag 關或全失敗回 []。

    flag 獨立於 CLI/飛書用的 US_STOCK_SCREENER_ENABLED，互不綁架；
    選股失敗絕不阻斷主卡。回傳 USCandidate 清單（供 format_picks 渲染）。
    """
    settings = _us_screener_card_settings()
    if not settings["enabled"]:
        return []
    scene_map = {"intraday": "intraday", "open": "premarket", "pre_us": "premarket", "close": "postclose"}
    scene = scene_map.get(mode)
    if not scene:
        return []
    try:
        from scripts.us_stock_screener import run_screener

        picks = run_screener(scene, top_n=settings["top"], provider=None, enabled_override=True)
        return picks or []
    except Exception as exc:
        logger.warning("美股卡選股取數失敗，略過強勢股區塊: %s", _redact_sensitive_text(exc))
        return []


def _build_us_close_research_pack_context() -> str:
    try:
        settings = _research_pack_close_settings()
        if not settings["enabled"]:
            return ""

        from scripts.researcher import build_research_pack_prompt_context

        return build_research_pack_prompt_context(
            settings["symbol"],
            market="US",
            market_data_type=settings["market_data_type"],
            client_id=settings["client_id"],
        )
    except Exception as exc:
        logger.warning("Research Pack close context unavailable: %s", _redact_sensitive_text(exc))
        return (
            "【Research Pack（結構化資料，供 Research W 使用）】\n"
            f"- unavailable: {type(exc).__name__}: {_truncate(_redact_sensitive_text(exc), 180)}\n"
            "- close Research W 已降級使用原本行情快照 / 新聞線索 / 00631L 狀態。\n"
            "- 不得因 Research Pack 缺失而自行補即時行情、新聞或基本面。"
        )


def _build_us_overnight_research_context(
    quotes: list[MarketQuote],
    *,
    reason_news: MarketReasonNews | None,
    position_00631l: Position00631L | None,
    research_pack_context: str = "",
) -> str:
    reason_lines = _format_reason_section(reason_news)
    pos_lines = []
    if position_00631l is not None:
        pos_lines.append(f"position_state={position_00631l.position_state}")
        if position_00631l.entry_price is not None:
            pos_lines.append(f"entry_price={position_00631l.entry_price:.2f}")
        if position_00631l.exit_price is not None:
            pos_lines.append(f"exit_price={position_00631l.exit_price:.2f}")
        if position_00631l.exit_date:
            pos_lines.append(f"exit_date={position_00631l.exit_date}")
    blocks = [
        "【美股收盤行情快照】\n" + _format_quotes_for_research(quotes),
        "【新聞/原因線索】\n" + "\n".join(reason_lines[2:]),
        "【00631L 實盤人工帳本狀態】\n" + ("；".join(pos_lines) if pos_lines else "（未讀到 00631L 帳本）"),
    ]
    if research_pack_context:
        blocks.append(research_pack_context)
    blocks.append(
        "【分析邊界】\n這份研究只作台股盤前風險定調與人工決策參考，不是自動交易指令；若新聞只來自標題線索，請明確標示因果仍需確認。"
    )
    return "\n\n".join(blocks)


def _brief_research_excerpt(report: str, *, limit: int = 900) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(report or "").strip())
    if not text:
        return "（Research W 未產生內容）"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n……（後略，完整內容已存檔）"


def run_us_overnight_research(
    quotes: list[MarketQuote],
    *,
    reason_news: MarketReasonNews | None,
    position_00631l: Position00631L | None,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """05:10 美股收盤固定研究：只存檔並回傳短摘要，由原本 DM 卡合併推送。"""
    try:
        from scripts.researcher import ResearchAgent

        topic = f"{datetime.now().strftime('%Y-%m-%d')} 美股收盤、半導體與台股盤前連動研究"
        context = _build_us_overnight_research_context(
            quotes,
            reason_news=reason_news,
            position_00631l=position_00631l,
            research_pack_context=_build_us_close_research_pack_context(),
        )
        result = ResearchAgent().run(topic=topic, context=context, report_kind="us_overnight", notify=False)
        if not result.get("success"):
            logger.warning("Research W 美股收盤研究失敗: %s", result.get("error"))
            return result
        return result
    except Exception as exc:
        logger.exception("Research W 美股收盤研究例外")
        return {"success": False, "error": str(exc)}


def _build_llm_read_context(
    quotes: list[MarketQuote],
    *,
    reason_news: MarketReasonNews | None,
    position_00631l: Position00631L | None,
    mode: str,
) -> str:
    """組給 LLM 的精簡上下文：核心行情 + 新聞線索 + 00631L 狀態。"""
    snapshot = _format_quotes_for_research(quotes)
    news_lines = []
    if reason_news and reason_news.summary:
        news_lines.append(f"摘要：{_truncate(reason_news.summary, 220)}")
    for src in (reason_news.sources[:5] if reason_news else []):
        news_lines.append(f"標題：{_truncate(src, 110)}")
    news_block = "\n".join(news_lines) if news_lines else "（暫無可靠新聞線索）"
    pos = "未知"
    if position_00631l is not None:
        pos = position_00631l.position_state
    mode_label = {"intraday": "美股盤中", "open": "美股開盤", "pre_us": "美股盤前", "close": "美股收盤", "pre_tw": "台股盤前"}.get(mode, mode)
    return (
        f"時段：{mode_label}\n"
        f"00631L 實盤狀態：{pos}\n\n"
        f"【核心行情】\n{snapshot}\n\n"
        f"【新聞線索】\n{news_block}"
    )


def generate_llm_market_read(
    quotes: list[MarketQuote],
    *,
    reason_news: MarketReasonNews | None,
    position_00631l: Position00631L | None,
    mode: str,
    max_tokens: int = 320,
) -> str | None:
    """用 ClaudeHelper 對本輪行情＋新聞生成 2-3 句中文研判；失敗回 None，由卡片退回規則化解讀。

    嚴格邊界：只做盤勢研判與隔日台股風險偏好，不得寫成買賣指令、不得編造未提供的數據。
    """
    if mode not in {"intraday", "open", "pre_us", "close", "pre_tw"}:
        return None
    try:
        from utils.claude_helper import ClaudeHelper

        system_prompt = (
            "你是一位穩健保守的美股/半導體對台股連動分析師。"
            "請依我提供的『核心行情』與『新聞線索』，用繁體中文寫 2-3 句盤勢研判，"
            "聚焦：科技/半導體方向、TSM ADR 與台積電連動、對隔日台股與 00631L 的風險偏好。"
            "規則：只做研判不下買賣指令；不得編造我沒給的數字或新聞；"
            "若新聞只有標題線索，要說因果待確認；輸出純文字、不要 Markdown、不要條列、不超過 3 句。"
        )
        prompt = _build_llm_read_context(
            quotes, reason_news=reason_news, position_00631l=position_00631l, mode=mode
        )
        text = ClaudeHelper().chat(system=system_prompt, prompt=prompt, max_tokens=max_tokens)
        text = re.sub(r"\s*\n\s*", " ", str(text or "").strip())
        if not text:
            return None
        return _truncate(text, 360)
    except Exception:
        logger.warning("盤中 LLM 研判生成失敗，退回規則化解讀", exc_info=True)
        return None


def _assess_market_regime(
    quotes: list[MarketQuote],
    *,
    prev_round: dict | None = None,
    mode: str = "intraday",
) -> dict[str, str]:
    """規則化判斷本輪盤勢狀態，給操作口徑當依據（零成本、不觸網）。

    回傳 {regime, regime_sentence, key_signal}：
    - regime: risk_up / risk_down / diverge / calm（供 LLM context 與規則化 fallback 分支）
    - regime_sentence: 一句中文盤勢描述
    - key_signal: 隔日待確認的關鍵訊號
    """
    by_symbol = {q.symbol: q for q in quotes}
    semis = [by_symbol[s] for s in ("SOXX", "NVDA", "TSM") if s in by_symbol]
    nasdaq = by_symbol.get("^IXIC") or by_symbol.get("QQQ") or by_symbol.get("NQ=F")

    # 半導體同步性：全弱 / 全強 / 分歧 / 未明
    semi_dir, semi_desc = 0, ""
    if semis:
        neg = sum(1 for q in semis if q.change_pct <= -0.3)
        pos = sum(1 for q in semis if q.change_pct >= 0.3)
        if neg == len(semis):
            semi_dir, semi_desc = -1, "半導體同步走弱"
        elif pos == len(semis):
            semi_dir, semi_desc = 1, "半導體同步走強"
        elif neg and pos:
            semi_dir, semi_desc = 0, "半導體訊號分歧"
        else:
            semi_dir, semi_desc = 0, "半導體方向未明"

    # 較上一輪變化（以 Nasdaq 為主軸；無前輪則略過）
    delta_desc = ""
    prev_quotes = (prev_round or {}).get("quotes") or {}
    if nasdaq and isinstance(prev_quotes, dict):
        prev_raw = prev_quotes.get(nasdaq.symbol)
        if prev_raw is not None:
            try:
                delta_desc = _delta_descriptor(float(prev_raw), nasdaq.change_pct)
            except (TypeError, ValueError):
                delta_desc = ""

    nq_pct = nasdaq.change_pct if nasdaq else 0.0

    # regime 綜合：分歧優先；其次看收斂/擴大；再看單輪跌幅
    if semi_desc == "半導體訊號分歧":
        regime = "diverge"
    elif delta_desc in ("跌幅收斂", "翻紅") or (nq_pct >= 0.5 and delta_desc != "漲幅收斂"):
        regime = "risk_down"
    elif (semi_dir < 0 or nq_pct <= -0.8) and delta_desc not in ("漲幅擴大", "漲幅收斂"):
        # 半導體同步走弱或 Nasdaq 明顯下挫＝絕對水位已偏風險；
        # 「跌幅收斂/翻紅」這類改善情形已被前面 risk_down 攔走，
        # 故剩下的擴大/翻黑/大致持平/無前輪都歸 risk_up（持平不代表安全）。
        regime = "risk_up"
    else:
        regime = "calm"

    parts: list[str] = []
    if semi_desc:
        parts.append(semi_desc)
    if delta_desc and delta_desc != "大致持平":
        parts.append(f"較上一輪{delta_desc}")
    tail = {
        "risk_up": "風險升溫",
        "risk_down": "風險降溫",
        "diverge": "方向未定",
        "calm": "波動平穩",
    }[regime]
    regime_sentence = ("、".join(parts) + f"，{tail}。") if parts else f"{tail}。"

    nq_name = nasdaq.name if nasdaq else "核心科技指數"
    if regime == "risk_up":
        key_signal = f"{nq_name} 能否守住本輪低點不再破底、半導體是否止穩。"
    elif regime == "risk_down":
        key_signal = f"{nq_name} 收斂是否延續、半導體是否同步回穩。"
    elif regime == "diverge":
        key_signal = "半導體分歧是否收斂為一致方向（看 SOXX 與 NVDA/TSM 是否歸隊）。"
    else:
        key_signal = f"{nq_name} 是否維持區間、有無新消息打破平衡。"

    return {"regime": regime, "regime_sentence": regime_sentence, "key_signal": key_signal}


def generate_llm_playbook_read(
    quotes: list[MarketQuote],
    *,
    reason_news: MarketReasonNews | None,
    position_00631l: Position00631L | None,
    mode: str,
    regime: str,
    max_tokens: int = 320,
) -> str | None:
    """用 ClaudeHelper 對本輪盤勢生成 2-3 句『隔日操作口徑建議』；失敗回 None，由卡片退回規則化口徑。

    嚴格邊界：只給口徑與權重傾向，不下明確買賣指令、不給具體價位/張數、不編造未提供的數據。
    """
    if mode not in {"intraday", "open", "pre_us", "close", "pre_tw"}:
        return None
    try:
        from utils.claude_helper import ClaudeHelper

        state = position_00631l.position_state if position_00631l is not None else None
        stance = "持倉" if state == "holding" else ("空手" if state == "flat" else "狀態未知")
        # pre_tw 是當日早上 08:15 的台股盤前定調，口徑用「今日」語氣；其餘時段用「隔日」。
        horizon = "今日" if mode == "pre_tw" else "隔日"
        system_prompt = (
            "你是一位穩健保守的台股槓桿 ETF（00631L）操作口徑顧問。"
            f"使用者目前{stance}。請依我提供的『本輪盤勢』『核心行情』『新聞線索』，"
            f"用繁體中文寫 2-3 句『{horizon}操作口徑建議』，"
            f"聚焦：{horizon}該把防守還是進攻權重放前面、優先盯哪個訊號、什麼條件才考慮動作。"
            "規則：只給口徑與權重傾向，不下明確買賣指令、不給具體價位與張數、不編造我沒給的數字；"
            "若新聞只有標題線索要說因果待確認；輸出純文字、不要 Markdown、不要條列、不超過 3 句。"
        )
        prompt = (
            f"【本輪盤勢】{regime}\n\n"
            + _build_llm_read_context(
                quotes, reason_news=reason_news, position_00631l=position_00631l, mode=mode
            )
        )
        text = ClaudeHelper().chat(system=system_prompt, prompt=prompt, max_tokens=max_tokens)
        text = re.sub(r"\s*\n\s*", " ", str(text or "").strip())
        if not text:
            return None
        return _truncate(text, 360)
    except Exception:
        logger.warning("盤中操作口徑 LLM 生成失敗，退回規則化口徑", exc_info=True)
        return None


def _format_research_section(research_result: dict[str, Any] | None, *, section_no: str = "五") -> list[str]:
    if not research_result:
        return []
    lines = ["", f"{section_no}、Research W 美股收盤研究摘要"]
    if not research_result.get("success"):
        lines.append(f"- Research W 研究產生失敗：{_truncate(research_result.get('error') or 'unknown error', 180)}")
        return lines
    lines.append(_brief_research_excerpt(str(research_result.get("report") or "")))
    if research_result.get("output_path"):
        lines.append(f"\n📁 完整研究已存檔：{Path(str(research_result['output_path'])).name}")
    return lines


def build_overnight_card(
    quotes: list[MarketQuote],
    *,
    mode: str,
    now: datetime | None = None,
    position_00631l: Position00631L | None = None,
    entry_00631l: float | None = None,
    reason_news: MarketReasonNews | None = None,
    research_result: dict[str, Any] | None = None,
    prev_round: dict | None = None,
    llm_read: str | None = None,
    playbook_read: str | None = None,
    institutional: dict | None = None,
    weight_chips: dict | None = None,
    us_trend_line: str | None = None,
    tw_correlation_line: str | None = None,
    overnight_futures: list[MarketQuote] | None = None,
    stock_picks: list | None = None,
) -> tuple[str, str]:
    """回傳 (title, content)。"""
    now = now or datetime.now()
    icon, risk_sentence = _risk_level(quotes)
    mode_label = {"intraday": "盤中風險", "open": "開盤雷達", "close": "收盤摘要", "pre_us": "盤前雷達"}.get(mode, "盤中風險")
    if mode == "pre_tw":
        title = f"{icon} 台股盤前定調｜昨夜美股＋台指期＋00631L"
    else:
        title = f"{icon} 美股{mode_label}｜00631L 隔日風險"

    # 隔夜期貨領先指標（IBKR 主 + yfinance 兜底）只供顯示，合併進 by_symbol 後由 order 機制
    # 渲染在「美股/商品快照」（ES=F/NQ=F 既有位置），不併入主 quotes 影響 freshness / 去重。
    by_symbol = {q.symbol: q for q in list(quotes) + list(overnight_futures or [])}
    order = ["TXFR1", "TWD=X", "ES=F", "NQ=F", "^GSPC", "^IXIC", "^DJI", "QQQ", "SOXX", "NVDA", "TSM", "CL=F", "GC=F"]
    lines = [
        f"🕒 台灣時間 {now.strftime('%Y-%m-%d %H:%M')}",
    ]
    if mode in {"intraday", "open", "pre_us"}:
        lines.append(_format_latest_asof_line(quotes, mode=mode))
        prev_line = _format_prev_round_line(quotes, prev_round, now=now)
        if prev_line:
            lines.append(prev_line)
    lines += [
        f"結論：{risk_sentence}",
        "",
        "一、美股/商品快照",
    ]
    for sym in order:
        q = by_symbol.get(sym)
        if not q:
            continue
        lines.append(f"- {q.name} {q.symbol}：{_fmt_price(q.price)} {_arrow(q.change_pct)} {_fmt_pct(q.change_pct)}")

    # 美股兩晚趨勢（pre_tw 專屬）：貼在快照下方，把「單點」變「趨勢」
    if us_trend_line:
        lines.append(us_trend_line)

    # 台股相關性昨日收盤（pre_tw 專屬）：0050/00631L/2330 昨日表現，本土連動視角
    if tw_correlation_line:
        lines.append(tw_correlation_line)

    lines.extend(_format_reason_section(reason_news, quotes=quotes, mode=mode, llm_read=llm_read))

    # 昨日三大法人（pre_tw 專屬本土視角）：插在新聞面與「對 00631L」之間，不帶數字編號
    lines.extend(_format_tw_institutional_section(institutional))

    # 全市場強勢股（IBKR 選股，delayed 技術掃榜）：不帶數字編號，作為二與三之間的發現補充
    if stock_picks:
        try:
            from scripts.us_stock_screener import format_picks
            lines.append("")
            lines.extend(format_picks(mode, stock_picks))
        except Exception:
            logger.warning("強勢股區塊格式化失敗，略過", exc_info=True)

    tsm = by_symbol.get("TSM")
    nvda = by_symbol.get("NVDA")
    nasdaq = by_symbol.get("^IXIC") or by_symbol.get("QQQ")
    soxx = by_symbol.get("SOXX")

    lines += ["", "三、對 00631L 實盤的提醒"]
    if position_00631l is None and entry_00631l is not None:
        position_00631l = Position00631L(entry_price=entry_00631l)
    if position_00631l is not None and position_00631l.position_state == "flat":
        lines.append("- 00631L 實盤狀態：已平倉空手；這張卡改看隔日再進場風向，不是持倉減碼提醒。")
        if position_00631l.exit_price is not None:
            exit_text = f"- 上輪賣出均價：{position_00631l.exit_price:.2f}"
            if position_00631l.exit_date:
                exit_text += f"（{position_00631l.exit_date}）"
            lines.append(exit_text)
        if position_00631l.entry_price is not None:
            lines.append(f"- 上輪進場成本：{position_00631l.entry_price:.2f}（僅供本輪結算參考，不代表目前仍持倉）")
    elif position_00631l is not None and position_00631l.entry_price is not None:
        lines.append(f"- 00631L 實盤成本：{position_00631l.entry_price:.2f}（持倉風控參考，不混入 sim 帳本）")
    if tsm:
        lines.append(f"- TSM ADR {_arrow(tsm.change_pct)} {_fmt_pct(tsm.change_pct)}：隔日台積電/權值股情緒需優先觀察。")
    if nvda:
        lines.append(f"- NVIDIA {_arrow(nvda.change_pct)} {_fmt_pct(nvda.change_pct)}：半導體風險偏好參考。")
    if soxx:
        lines.append(f"- SOXX {_arrow(soxx.change_pct)} {_fmt_pct(soxx.change_pct)}：費半方向參考。")
    if nasdaq:
        lines.append(f"- Nasdaq/科技線 {_arrow(nasdaq.change_pct)} {_fmt_pct(nasdaq.change_pct)}：影響隔日槓桿 ETF 波動預期。")

    dynamic_watch = _format_dynamic_watch_section(quotes, mode=mode, position_00631l=position_00631l)
    lines.extend(dynamic_watch)
    playbook_title = "五、操作口徑" if dynamic_watch else "四、操作口徑"
    regime = _assess_market_regime(quotes, prev_round=prev_round, mode=mode) if mode in {"open", "intraday", "pre_us", "close", "pre_tw"} else None
    lines.extend(_format_trade_playbook(
        mode,
        position_00631l=position_00631l,
        title=playbook_title,
        regime=regime,
        playbook_read=playbook_read,
    ))
    # Research W 摘要編號接在口徑之後：close 有動態觀察(四)+口徑(五) → 研究為六；無動態觀察則為五
    research_no = "六" if dynamic_watch else "五"
    lines.extend(_format_research_section(research_result, section_no=research_no))
    return title, "\n".join(lines)


def run(mode: str, *, dry_run: bool = False) -> int:
    if mode == "pre_tw":
        return run_tw_premarket(dry_run=dry_run)
    syms = PRE_US_SYMBOLS if mode == "pre_us" else DEFAULT_SYMBOLS
    quotes = fetch_us_market_quotes(symbols=syms, mode=mode)
    if not quotes:
        logger.warning("yfinance bulk 無行情，改用 Yahoo chart 備援路徑")
        quotes = fetch_quotes_via_yahoo_chart(symbols=syms, mode=mode)
    if not quotes:
        # 第三層：CNBC quote API（非 Yahoo 系基礎設施，Yahoo 全限流時仍可用）
        logger.warning("Yahoo chart 備援亦無行情，改用 CNBC 第三層備援路徑")
        quotes = fetch_quotes_via_cnbc(symbols=syms, mode=mode)
    if not quotes:
        logger.error("yfinance + Yahoo chart + CNBC 皆無美股行情，跳過推送")
        if not dry_run:
            _record_fetch_outcome(False, mode)
        return 2
    if not dry_run:
        _record_fetch_outcome(True, mode)

    # 行情軌跡落地（與推送解耦）；僅真實運行記，dry_run/測試不污染軌跡；失敗不擋主流程
    if not dry_run:
        try:
            from utils.market_history import record as _record_history

            _record_history("us", mode, [
                {"symbol": q.symbol, "name": q.name, "price": q.price, "change_pct": q.change_pct, "asof": q.asof}
                for q in quotes
            ])
        except Exception as exc:
            logger.warning("market history record failed: %s", exc)

    if mode in ("intraday", "open", "pre_us") and not is_intraday_data_fresh(quotes):
        logger.info("%s 行情資料不夠新（美股可能休市），跳過推送", mode)
        return 0
    if mode == "close" and not is_close_data_recent(quotes):
        logger.info("收盤行情資料過舊，跳過推送")
        return 0

    # 盤中改為每段都推（23:30/01:30/03:00 各一張）：拿掉波動門檻，靠卡頂
    # 「📊 較上一輪」對比差異避免「連看數張長一樣」的重複觀感。
    # 仍保留兩道閘：上方 freshness gate（休市/資料過舊不推）、下方 _already_sent
    #（同一份行情 asof 相同不重推，避免重跑/補跑洗版）。
    if mode == "intraday":
        logger.info("盤中每段固定推送（波動門檻已停用），波動達標=%s", should_push_intraday(quotes))

    key = _alert_key(mode, quotes)
    if not dry_run and _already_sent(key):
        logger.info("同一份美股行情已推送過，跳過: %s", key)
        return 0

    reason_news = fetch_market_reason_news(quotes, mode=mode)
    position_00631l = load_00631l_position()
    prev_round = _load_round_snapshot() if mode in {"intraday", "open", "pre_us"} else None
    llm_read = None
    playbook_read = None
    if mode in {"intraday", "open", "pre_us", "close"}:
        llm_read = generate_llm_market_read(
            quotes,
            reason_news=reason_news,
            position_00631l=position_00631l,
            mode=mode,
        )
        # 操作口徑 LM 增強：先規則化判斷盤勢，再讓 LM 依盤勢給隔日口徑；失敗回 None 退回規則化
        regime = _assess_market_regime(quotes, prev_round=prev_round, mode=mode)
        playbook_read = generate_llm_playbook_read(
            quotes,
            reason_news=reason_news,
            position_00631l=position_00631l,
            mode=mode,
            regime=regime["regime_sentence"],
        )
    research_result = None
    if mode == "close":
        research_result = run_us_overnight_research(
            quotes,
            reason_news=reason_news,
            position_00631l=position_00631l,
            dry_run=dry_run,
        )
    # 隔夜期貨領先指標：盤中/開盤/收盤卡補（盤前 pre_us 已含 ES=F/NQ=F）；flag 關回 []
    overnight_futures = (
        _fetch_overnight_futures_quotes(mode)
        if mode in {"intraday", "open", "close"}
        else None
    )
    # 全市場強勢股（IBKR 選股）：盤前/開盤/盤中/收盤卡補；flag 關回 []
    stock_picks = _fetch_us_screener_picks(mode) if mode in {"pre_us", "open", "intraday", "close"} else None
    title, content = build_overnight_card(
        quotes,
        mode=mode,
        position_00631l=position_00631l,
        reason_news=reason_news,
        research_result=research_result,
        prev_round=prev_round,
        llm_read=llm_read,
        playbook_read=playbook_read,
        overnight_futures=overnight_futures,
        stock_picks=stock_picks,
    )
    if dry_run:
        print(f"# {title}\n\n{content}")
        return 0

    ok = send_dm(title, content)
    if ok:
        _mark_sent(key)
        # 推送成功才存本輪快照，供下一輪盤中/開盤卡做「較上一輪」對比
        if mode in {"intraday", "open", "pre_us"}:
            _save_round_snapshot(quotes, mode)
    logger.info("美股半夜提醒推送結果: %s", ok)
    return 0 if ok else 1


def fetch_usdtwd() -> dict | None:
    """yfinance 抓 USD/TWD 匯率（台股盤前定調用）。失敗回 None。"""
    try:
        df = yf.download("TWD=X", period="5d", interval="1d", progress=False, auto_adjust=False)
        close = df["Close"].squeeze().dropna()
        if len(close) < 2:
            return None
        last = float(close.iloc[-1]); prev = float(close.iloc[-2])
        return {"price": last, "previous_close": prev,
                "change_pct": ((last - prev) / prev * 100.0) if prev > 0 else 0.0}
    except Exception:
        logger.warning("USD/TWD 匯率抓取失敗", exc_info=True)
        return None


def run_tw_premarket(*, dry_run: bool = False) -> int:
    """台股盤前 08:15：昨夜美股收盤＋台指期夜盤＋USD/TWD＋新聞＋對台積/00631L 定調。"""
    quotes = fetch_us_market_quotes(symbols=DEFAULT_SYMBOLS, mode="close")
    if not quotes:
        quotes = fetch_quotes_via_yahoo_chart(symbols=DEFAULT_SYMBOLS, mode="close")
    if not quotes:
        logger.error("台股盤前：無昨夜美股行情，跳過")
        return 2
    if not is_close_data_recent(quotes):
        logger.info("台股盤前：昨夜美股資料過舊（可能美股休市），跳過")
        return 0
    # 台指期夜盤（Shioaji；抓不到不擋整張卡）
    try:
        from utils.shioaji_provider import fetch_index_future
        txf = fetch_index_future("TXFR1")
    except Exception:
        txf = None
    if txf and txf.get("price"):
        quotes.append(MarketQuote(
            symbol="TXFR1", name="台指期夜盤", category="tw_future",
            price=float(txf["price"]), change_pct=float(txf.get("change_pct") or 0.0),
            previous_close=txf.get("previous_close"), asof=txf.get("asof") or ""))
    # USD/TWD（yfinance）
    fx = fetch_usdtwd()
    if fx and fx.get("price"):
        quotes.append(MarketQuote(
            symbol="TWD=X", name="美元/台幣", category="fx",
            price=float(fx["price"]), change_pct=float(fx.get("change_pct") or 0.0),
            previous_close=fx.get("previous_close"), asof=""))
    # 台股盤前快照落地（含昨夜美股＋台指期＋匯率）；僅真實運行記，dry_run 不污染
    if not dry_run:
        from utils.market_history import record as _record_history
        _record_history("tw", "pre_tw", [
            {"symbol": q.symbol, "name": q.name, "price": q.price, "change_pct": q.change_pct, "asof": q.asof}
            for q in quotes
        ])
    reason_news = fetch_market_reason_news(quotes, mode="pre_tw")
    # 昨日三大法人 + 權值股籌碼（TWSE 官方，抓不到/假日回 None，不擋整張卡）
    institutional = None
    try:
        from utils.tw_institutional import fetch_market_institutional, fetch_top_weight_chips
        institutional = fetch_market_institutional()
        if institutional is not None:
            # 權值股籌碼代號改從相關性池取（權值驅動 + 半導體鏈），不再寫死 2330/2317
            chip_ids = ["2330", "2317"]
            try:
                from utils.correlation_pool import symbols_for_market
                pool_ids = [
                    cs.symbol.split(".")[0]
                    for cs in symbols_for_market("tw")
                    if cs.tier in ("weight_drivers", "semiconductor_chain")
                ]
                if pool_ids:
                    chip_ids = list(dict.fromkeys(pool_ids))[:5]  # 去重、最多 5 檔避免卡片過長
            except Exception:
                logger.warning("相關性池讀取失敗，籌碼代號退回 2330/2317", exc_info=True)
            chips = fetch_top_weight_chips(chip_ids)
            if chips:
                institutional["chips"] = chips
    except Exception:
        logger.warning("台股盤前：三大法人抓取失敗，該段略過", exc_info=True)
        institutional = None
    # 美股兩晚趨勢（讀本地 jsonl，把昨夜單點變趨勢；缺資料回 None）
    us_trend_line = _format_us_two_night_trend(quotes)
    # 昨日台股相關性收盤（0050/00631L/2330；讀 market_history_tw，缺資料回 None）
    tw_correlation_line = _format_tw_correlation_close()
    overnight_futures = _fetch_overnight_futures_quotes("pre_tw")
    title, content = build_overnight_card(
        quotes, mode="pre_tw", position_00631l=load_00631l_position(),
        reason_news=reason_news, institutional=institutional, us_trend_line=us_trend_line,
        tw_correlation_line=tw_correlation_line, overnight_futures=overnight_futures)
    if dry_run:
        print(f"# {title}\n\n{content}")
        return 0
    key = _alert_key("pre_tw", quotes)
    if _already_sent(key):
        logger.info("台股盤前已推送過，跳過: %s", key)
        return 0
    ok = send_dm(title, content)
    if ok:
        _mark_sent(key)
    logger.info("台股盤前推送結果: %s", ok)
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="美股半夜行情/00631L 風險提醒")
    parser.add_argument("--mode", choices=["intraday", "open", "close", "pre_us", "pre_tw"], default="close")
    parser.add_argument("--dry-run", action="store_true", help="只輸出，不推送飛書")
    args = parser.parse_args()
    raise SystemExit(run(args.mode, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
