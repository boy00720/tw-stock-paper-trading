#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""00631L 實盤盤中風險雷達。

用途：W 實盤持有 00631L.TW（成本由 config/sim_watch_only.yaml 讀取），
台股交易時段用短命令定期檢查 00631L / 0050 / 台積電，觸發黃/紅/強紅或
浮盈保護條件時推 W 個人飛書 DM。

重要邊界：只提醒人工風控，不自動下單，不修改模擬單與成交紀錄。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

# 避免本機代理造成 yfinance CONNECT 問題（沿用既有行情腳本邏輯）
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import yfinance as yf  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim._notify import send_dm  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "00631l_live_monitor.yaml"
WATCH_ONLY_CONFIG = BASE_DIR / "config" / "sim_watch_only.yaml"
LOG_PATH = BASE_DIR / "logs" / "00631l_live_risk_monitor.log"

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
logger = logging.getLogger("00631l_live_risk_monitor")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    name: str
    role: str
    price: float
    previous_close: float | None
    change_pct: float | None
    day_high: float | None = None
    day_low: float | None = None
    asof: str = ""


@dataclass(frozen=True)
class RiskEvaluation:
    level: str
    level_rank: int
    title_icon: str
    reasons: tuple[str, ...]
    suggestion: str
    target_return_pct: float | None
    target_drawdown_pct: float | None
    mode: str = "holding"  # holding | flat（flat=空手再進場觀察）


LEVEL_LABELS = {
    "strong_red": "強風險",
    "red": "風險升高",
    "yellow": "注意",
    "green": "正常",
    "data_missing": "資料缺失",
    "sell_watch": "賣出觀察",
    "re_entry_zone": "再進場觀察帶",
    "re_entry_watch": "再進場觀察",
    "flat_watch": "空手觀察",
}


def level_label(level: str) -> str:
    return LEVEL_LABELS.get(level, level)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return load_yaml(path).get("live_risk") or {}


def load_00631l_entry_price(path: Path = WATCH_ONLY_CONFIG) -> float | None:
    try:
        raw = (load_yaml(path).get("watch_only") or {}).get("00631L.TW") or {}
        price = raw.get("entry_price")
        return float(price) if price is not None else None
    except Exception:
        logger.exception("讀取 00631L 實盤成本失敗: %s", path)
        return None


def load_00631l_position(path: Path = WATCH_ONLY_CONFIG) -> dict[str, Any]:
    """讀 00631L 實盤帳本狀態：position_state / entry_price / exit_price / exit_date。

    向後相容：缺 position_state 時預設 'holding'（等同本次改動前行為）。
    position_state 僅接受 holding / flat，其餘值一律保守視為 holding（續跑持倉風控）。
    """
    try:
        raw = (load_yaml(path).get("watch_only") or {}).get("00631L.TW") or {}
    except Exception:
        logger.exception("讀取 00631L 實盤帳本狀態失敗: %s", path)
        raw = {}

    def _f(key: str) -> float | None:
        v = raw.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    state = str(raw.get("position_state") or "holding").strip().lower()
    if state not in ("holding", "flat"):
        state = "holding"
    return {
        "position_state": state,
        "entry_price": _f("entry_price"),
        "exit_price": _f("exit_price"),
        "exit_date": raw.get("exit_date"),
    }


def _resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_level_rank": 0, "last_sent_ts": 0, "events": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("state 讀取失敗，改用空 state: %s", path)
        return {"last_level_rank": 0, "last_sent_ts": 0, "events": []}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _parse_hhmm(value: str) -> time:
    hh, mm = str(value).split(":", 1)
    return time(int(hh), int(mm))


def is_trading_window(now: datetime, config: dict[str, Any]) -> bool:
    if now.weekday() >= 5:
        return False
    win = config.get("trading_window") or {}
    start = _parse_hhmm(win.get("start", "09:00"))
    end = _parse_hhmm(win.get("end", "13:30"))
    return start <= now.time() <= end


def is_close_check_window(now: datetime, config: dict[str, Any]) -> bool:
    win = config.get("trading_window") or {}
    start = _parse_hhmm(win.get("close_check_start", "13:20"))
    end = _parse_hhmm(win.get("end", "13:30"))
    return now.weekday() < 5 and start <= now.time() <= end


def _get_frame(raw: Any, symbol: str, total: int) -> Any:
    if total == 1:
        frame = raw
        if "Close" not in frame.columns and getattr(frame.columns, "nlevels", 1) > 1:
            frame = frame[symbol]
        return frame
    return raw[symbol]


def fetch_live_quotes(
    symbols: list[dict[str, Any]],
    *,
    yf_download: Callable[..., Any] = yf.download,
) -> dict[str, LiveQuote]:
    tickers = [str(s["symbol"]) for s in symbols]
    try:
        raw = yf_download(
            tickers,
            period="2d",
            interval="5m",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception:
        logger.exception("yfinance 下載台股盤中行情失敗")
        return {}

    out: dict[str, LiveQuote] = {}
    for item in symbols:
        symbol = str(item["symbol"])
        try:
            frame = _get_frame(raw, symbol, len(tickers))
            close = frame["Close"].dropna()
            if len(close) < 2:
                continue
            last = float(close.iloc[-1])
            dates = close.index.normalize()
            last_day = dates[-1]
            prev_close_series = close[dates < last_day]
            prev = float(prev_close_series.iloc[-1]) if not prev_close_series.empty else float(close.iloc[-2])
            change_pct = ((last - prev) / prev * 100.0) if prev > 0 else None
            day_mask = dates == last_day
            high = None
            low = None
            if "High" in frame.columns:
                high_series = frame["High"].dropna()
                highs = high_series[high_series.index.normalize() == last_day]
                high = float(highs.max()) if len(highs) else None
            if "Low" in frame.columns:
                low_series = frame["Low"].dropna()
                lows = low_series[low_series.index.normalize() == last_day]
                low = float(lows.min()) if len(lows) else None
            out[symbol] = LiveQuote(
                symbol=symbol,
                name=str(item.get("name") or symbol),
                role=str(item.get("role") or ""),
                price=last,
                previous_close=prev,
                change_pct=change_pct,
                day_high=high,
                day_low=low,
                asof=str(close.index[-1]),
            )
        except Exception:
            logger.warning("解析 %s 盤中行情失敗", symbol, exc_info=True)
    return out


def _pct(value: float | None) -> str:
    return "未知" if value is None else f"{value:+.2f}%"


def _price(value: float | None) -> str:
    return "未知" if value is None else f"{value:.2f}"


def _parse_asof(value: str) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.split(".")[0])
        except ValueError:
            return None


def quotes_are_fresh(quotes: dict[str, LiveQuote], *, now: datetime, max_age_minutes: int) -> bool:
    target = quotes.get("00631L.TW")
    if not target:
        return False
    asof = _parse_asof(target.asof)
    if asof is None:
        return False
    # yfinance 台股 asof 常帶 UTC offset；若 now 無 tz，對齊本機時區。
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=now.astimezone().tzinfo)
    if now.tzinfo is None:
        now = now.astimezone()
    age_minutes = (now - asof.astimezone(now.tzinfo)).total_seconds() / 60.0
    return 0 <= age_minutes <= max_age_minutes


def _add_level(current: tuple[int, str], rank: int, level: str) -> tuple[int, str]:
    return (rank, level) if rank > current[0] else current


def evaluate_risk(
    quotes: dict[str, LiveQuote],
    *,
    entry_price: float | None,
    config: dict[str, Any],
    now: datetime | None = None,
) -> RiskEvaluation:
    thresholds = config.get("thresholds") or {}
    target = quotes.get("00631L.TW")
    baseline = quotes.get("0050.TW")
    key_weight = quotes.get("2330.TW")
    reasons: list[str] = []
    level = (0, "normal")
    target_return_pct: float | None = None
    target_drawdown_pct: float | None = None

    def thr(name: str, default: float) -> float:
        return float(thresholds.get(name, default))

    if target and entry_price and entry_price > 0:
        target_return_pct = (target.price - entry_price) / entry_price * 100.0
        loss_decimal = (target.price - entry_price) / entry_price
        if loss_decimal <= -thr("strong_red_loss_pct", 0.03):
            level = _add_level(level, 3, "strong_red")
            reasons.append(f"00631L 跌破實盤成本 {entry_price:.2f} 已達 {target_return_pct:+.2f}%（強紅成本防線）")
        elif loss_decimal <= -thr("red_loss_pct", 0.02):
            level = _add_level(level, 2, "red")
            reasons.append(f"00631L 跌破實盤成本 {entry_price:.2f} 已達 {target_return_pct:+.2f}%（紅燈成本防線）")
        elif loss_decimal <= -thr("yellow_loss_pct", 0.01):
            level = _add_level(level, 1, "yellow")
            reasons.append(f"00631L 跌破實盤成本 {entry_price:.2f} 已達 {target_return_pct:+.2f}%（黃燈成本防線）")
        elif loss_decimal >= thr("profit_trail_pct", 0.05):
            level = _add_level(level, 1, "profit_watch")
            reasons.append(f"00631L 浮盈 {target_return_pct:+.2f}% 已達上移保護線觀察區")
        elif loss_decimal >= thr("profit_protect_pct", 0.03):
            level = _add_level(level, 1, "profit_watch")
            reasons.append(f"00631L 浮盈 {target_return_pct:+.2f}% 已達拉保本觀察區")

    if target and target.change_pct is not None:
        day_drop = target.change_pct / 100.0
        if day_drop <= -thr("target_strong_day_drop_pct", 0.06):
            level = _add_level(level, 3, "strong_red")
            reasons.append(f"00631L 單日跌幅 {target.change_pct:+.2f}% 已達強紅門檻")
        elif day_drop <= -thr("target_red_day_drop_pct", 0.04):
            level = _add_level(level, 2, "red")
            reasons.append(f"00631L 單日跌幅 {target.change_pct:+.2f}% 已達紅燈門檻")
        elif day_drop <= -thr("target_yellow_day_drop_pct", 0.025):
            level = _add_level(level, 1, "yellow")
            reasons.append(f"00631L 單日跌幅 {target.change_pct:+.2f}% 已達黃燈門檻")

    if target and target.day_high and target.day_high > 0:
        target_drawdown_pct = (target.price - target.day_high) / target.day_high * 100.0
        drawdown_decimal = -target_drawdown_pct / 100.0
        if drawdown_decimal >= thr("intraday_drawdown_red_pct", 0.04):
            level = _add_level(level, 2, "red")
            reasons.append(f"00631L 自日內高點回撤 {target_drawdown_pct:+.2f}%（紅燈回撤）")
        elif drawdown_decimal >= thr("intraday_drawdown_yellow_pct", 0.025):
            level = _add_level(level, 1, "yellow")
            reasons.append(f"00631L 自日內高點回撤 {target_drawdown_pct:+.2f}%（黃燈回撤）")
        if (
            target_return_pct is not None
            and target_return_pct / 100.0 >= thr("profit_trail_pct", 0.05)
            and drawdown_decimal >= thr("profit_drawdown_trigger_pct", 0.02)
        ):
            level = _add_level(level, 2, "red")
            reasons.append("00631L 已有較大浮盈但從高點回撤，建議人工檢查是否減碼或上移保護線")

    # 賣點提示（選配）：holding 時協助抓賣出時機，非自動賣出指令。
    sell_cfg = config.get("sell_signal") or {}
    if sell_cfg.get("enabled") and target_return_pct is not None:
        sell_take = float(sell_cfg.get("profit_take_pct", 0.08))
        sell_tb = float(sell_cfg.get("trend_break_drawdown_pct", 0.03))
        sell_hit = False
        if target_return_pct / 100.0 >= sell_take:
            sell_hit = True
            reasons.append(
                f"00631L 浮盈 {target_return_pct:+.2f}% 已達止盈提示線 {sell_take:.0%}，"
                "可考慮分批獲利了結（人工判斷，非自動賣出）"
            )
        if (
            target_drawdown_pct is not None
            and target_return_pct > 0
            and (-target_drawdown_pct / 100.0) >= sell_tb
        ):
            sell_hit = True
            reasons.append(
                f"00631L 仍浮盈但自日內高點回撤 {target_drawdown_pct:+.2f}%，趨勢轉弱，可考慮減碼鎖利"
            )
        # 凸顯止盈語境：覆蓋同級的 profit_watch，但不蓋過下行紅燈（rank>=2）
        if sell_hit and level[0] <= 1:
            level = (1, "sell_watch")

    if baseline and baseline.change_pct is not None:
        base_drop = baseline.change_pct / 100.0
        if base_drop <= -thr("baseline_red_drop_pct", 0.02):
            level = _add_level(level, 2, "red")
            reasons.append(f"0050 轉弱 {baseline.change_pct:+.2f}%（臺灣50 背景紅燈）")
        elif base_drop <= -thr("baseline_yellow_drop_pct", 0.012):
            level = _add_level(level, 1, "yellow")
            reasons.append(f"0050 轉弱 {baseline.change_pct:+.2f}%（臺灣50 背景黃燈）")

    if key_weight and key_weight.change_pct is not None:
        key_drop = key_weight.change_pct / 100.0
        if key_drop <= -thr("key_weight_red_drop_pct", 0.03):
            level = _add_level(level, 2, "red")
            reasons.append(f"台積電轉弱 {key_weight.change_pct:+.2f}%（權值股紅燈）")
        elif key_drop <= -thr("key_weight_yellow_drop_pct", 0.018):
            level = _add_level(level, 1, "yellow")
            reasons.append(f"台積電轉弱 {key_weight.change_pct:+.2f}%（權值股黃燈）")

    if target is None:
        level = _add_level(level, 1, "data_missing")
        reasons.insert(0, "00631L 報價不可得，無法判斷實盤成本風險；請人工確認資料源或券商報價")

    if not reasons:
        reasons.append("00631L / 0050 / 台積電未觸發實盤風控門檻")

    rank, level_name = level
    icon_map = {0: "🟢", 1: "🟡", 2: "🔴", 3: "🚨"}
    if level_name == "data_missing":
        icon = "🟡"
        suggestion = "資料警示：00631L 報價不可得，本輪不做價格判斷；請以券商/交易所報價人工核對。"
    elif level_name == "profit_watch":
        icon = "🟡"
        suggestion = "浮盈保護觀察：可人工檢查是否拉保本或上移保護線，避免槓桿 ETF 回撤吃掉獲利。"
    elif level_name == "sell_watch":
        icon = "🟡"
        suggestion = "賣點提示：00631L 浮盈已達止盈線或自高點轉弱，可人工評估分批獲利了結；非自動賣出指令。"
    elif rank >= 3:
        icon = "🚨"
        suggestion = "強紅：優先人工檢查 00631L 是否需要減碼、拉保護線或暫停加碼；先看 0050/台積電是否共振轉弱。"
    elif rank == 2:
        icon = "🔴"
        suggestion = "紅燈：請人工檢查實盤風控，避免槓桿 ETF 擴大虧損；不是自動賣出指令。"
    elif rank == 1:
        icon = "🟡"
        suggestion = "黃燈：提高警覺，觀察 0050/台積電承接與 00631L 是否跌破下一道成本防線。"
    else:
        icon = "🟢"
        suggestion = "目前未觸發實盤風控門檻，照既有計畫觀察即可。"

    return RiskEvaluation(
        level=level_name,
        level_rank=rank,
        title_icon=icon,
        reasons=tuple(reasons),
        suggestion=suggestion,
        target_return_pct=target_return_pct,
        target_drawdown_pct=target_drawdown_pct,
    )


def evaluate_re_entry(
    quotes: dict[str, LiveQuote],
    *,
    exit_price: float | None,
    config: dict[str, Any],
    now: datetime | None = None,
) -> RiskEvaluation:
    """flat（已平倉空手）時的再進場機會觀察。

    以 exit_price 為錨：現價需回到「賣出價下方 band_low~band_high」觀察帶，且
    0050/台積電轉強、00631L 日內止跌，才升級為再進場機會（仍須人工分批判斷）。
    空手沒有成本風險可言，因此不報跌破成本、不喊減碼。
    """
    re_cfg = config.get("re_entry") or {}
    target = quotes.get("00631L.TW")
    baseline = quotes.get("0050.TW")
    key_weight = quotes.get("2330.TW")
    reasons: list[str] = []
    level = (0, "flat_watch")
    deviation_pct: float | None = None
    drawdown_pct: float | None = None

    def rcfg(name: str, default: float) -> float:
        return float(re_cfg.get(name, default))

    band_low = rcfg("band_low_pct", 0.05)
    band_high = rcfg("band_high_pct", 0.10)

    in_band = False
    if target is None:
        reasons.append("00631L 報價不可得，無法評估再進場位置；請人工確認資料源或券商報價")
    elif not (exit_price and exit_price > 0):
        reasons.append("尚未設定賣出價，無法錨定再進場帶；請於 sim_watch_only.yaml 填入 exit_price")
    else:
        deviation = (target.price - exit_price) / exit_price
        deviation_pct = deviation * 100.0
        if -band_high <= deviation <= -band_low:
            in_band = True
            reasons.append(
                f"00631L 現價 {target.price:.2f} 已低於賣出價 {exit_price:.2f} {-deviation_pct:.2f}%，"
                f"進入再進場觀察帶（{band_low:.0%}~{band_high:.0%}）"
            )
        elif deviation < -band_high:
            reasons.append(
                f"00631L 現價較賣出價低 {-deviation_pct:.2f}%，已深於觀察帶下緣 {band_high:.0%}；"
                "留意是否趨勢下行而非回檔，勿急於接刀"
            )
        else:
            reasons.append(
                f"00631L 現價較賣出價 {deviation_pct:+.2f}%，尚未回到再進場觀察帶（需回檔至 -{band_low:.0%} 以下）"
            )

    if target and target.day_high and target.day_high > 0:
        drawdown_pct = (target.price - target.day_high) / target.day_high * 100.0

    baseline_strong = (
        baseline is not None
        and baseline.change_pct is not None
        and baseline.change_pct / 100.0 >= rcfg("baseline_strength_pct", 0.003)
    )
    key_ok = (
        key_weight is not None
        and key_weight.change_pct is not None
        and key_weight.change_pct / 100.0 >= rcfg("key_weight_strength_pct", 0.0)
    )
    stabilize_thr = rcfg("target_stabilize_drawdown_pct", 0.015)
    target_stabilize = drawdown_pct is not None and (-drawdown_pct / 100.0) <= stabilize_thr

    confirm_bits: list[str] = []
    if baseline_strong and baseline and baseline.change_pct is not None:
        confirm_bits.append(f"0050 {baseline.change_pct:+.2f}%")
    if key_ok and key_weight and key_weight.change_pct is not None:
        confirm_bits.append(f"台積電 {key_weight.change_pct:+.2f}%")

    if in_band and baseline_strong and key_ok and target_stabilize:
        level = _add_level(level, 2, "re_entry_zone")
        reasons.append(
            "0050/台積電同步轉強"
            + (f"（{', '.join(confirm_bits)}）" if confirm_bits else "")
            + " + 00631L 日內止跌，符合再進場觀察條件——仍須人工判斷，建議分批、不要一次重壓"
        )
    elif in_band:
        missing: list[str] = []
        if not baseline_strong:
            missing.append("0050 尚未轉強")
        if not key_ok:
            missing.append("台積電仍弱")
        if not target_stabilize:
            missing.append("00631L 日內仍在殺低")
        level = _add_level(level, 1, "re_entry_watch")
        reasons.append("已在觀察帶但進場條件未齊：" + "、".join(missing) + "；續觀察，先別進")

    rank, level_name = level
    if level_name == "re_entry_zone":
        icon = "🟢"
        suggestion = "再進場機會：已回觀察帶且背景轉強、日內止跌，可人工評估分批進場；非自動買進指令。"
    elif level_name == "re_entry_watch":
        icon = "🟡"
        suggestion = "再進場觀察：已回到觀察帶但確認條件未齊，續觀察 0050/台積電與 00631L 日內止跌訊號。"
    else:
        icon = "⚪"
        suggestion = "空手觀察中：尚未進入再進場觀察帶，照計畫等回檔與轉強訊號即可。"

    if not reasons:
        reasons.append("00631L 空手觀察中，未觸發再進場訊號")

    return RiskEvaluation(
        level=level_name,
        level_rank=rank,
        title_icon=icon,
        reasons=tuple(reasons),
        suggestion=suggestion,
        target_return_pct=deviation_pct,
        target_drawdown_pct=drawdown_pct,
        mode="flat",
    )


def should_send(evaluation: RiskEvaluation, state: dict[str, Any], *, now: datetime, cooldown_minutes: int, force: bool = False) -> bool:
    if force:
        return True
    if evaluation.level_rank <= 0:
        return False
    last_rank = int(state.get("last_level_rank", 0) or 0)
    last_ts = float(state.get("last_sent_ts", 0) or 0)
    if evaluation.level_rank > last_rank:
        return True
    elapsed = now.timestamp() - last_ts
    return elapsed >= cooldown_minutes * 60


def mark_sent(state: dict[str, Any], evaluation: RiskEvaluation, *, now: datetime) -> dict[str, Any]:
    events = list(state.get("events") or [])
    events.append({
        "timestamp": now.isoformat(timespec="seconds"),
        "level": evaluation.level,
        "level_rank": evaluation.level_rank,
        "reasons": list(evaluation.reasons),
    })
    state["last_level"] = evaluation.level
    state["last_level_rank"] = evaluation.level_rank
    state["last_sent_ts"] = now.timestamp()
    state["updated_at"] = _now_iso()
    state["events"] = events[-50:]
    return state


def _format_card(
    quotes: dict[str, LiveQuote],
    evaluation: RiskEvaluation,
    *,
    entry_price: float | None,
    now: datetime,
    config: dict[str, Any],
    exit_price: float | None = None,
) -> tuple[str, str]:
    is_flat = evaluation.mode == "flat"
    radar_name = "空手再進場雷達" if is_flat else "實盤風控雷達"
    title = f"{evaluation.title_icon} 00631L {radar_name}｜{level_label(evaluation.level)}"
    target = quotes.get("00631L.TW")
    baseline = quotes.get("0050.TW")
    key_weight = quotes.get("2330.TW")
    lines = [
        f"🕒 台灣時間 {now.strftime('%Y-%m-%d %H:%M')}",
        f"結論：{evaluation.suggestion}",
        "",
        "一、觸發原因",
    ]
    for reason in evaluation.reasons:
        lines.append(f"- {reason}")

    lines.extend(["", "二、行情快照"])
    if is_flat:
        if exit_price is not None and entry_price is not None:
            lines.append(
                f"- 上輪賣出價：00631L {exit_price:.2f}（已平倉空手；上輪成本 {entry_price:.2f}，僅供結算）"
            )
        elif exit_price is not None:
            lines.append(f"- 上輪賣出價：00631L {exit_price:.2f}（已平倉空手）")
    elif entry_price is not None:
        lines.append(f"- 實盤成本：00631L {entry_price:.2f}（人工帳本；不混入 sim 帳本）")
    for q in [target, baseline, key_weight]:
        if not q:
            continue
        lines.append(
            f"- {q.name} {q.symbol}：{_price(q.price)} / 日內 {_pct(q.change_pct)}"
            f" / 日高 {_price(q.day_high)} / 資料時間 {q.asof or '未知'}"
        )
    if evaluation.target_return_pct is not None:
        label = "00631L 相對賣出價偏離" if is_flat else "00631L 相對成本報酬"
        lines.append(f"- {label}：{evaluation.target_return_pct:+.2f}%")
    if evaluation.target_drawdown_pct is not None:
        lines.append(f"- 00631L 日內高點回撤：{evaluation.target_drawdown_pct:+.2f}%")

    if is_flat:
        op_msg = (config.get("re_entry") or {}).get("message") or "00631L 已空手，再進場機會觀察；非自動買進指令。"
    else:
        op_msg = config.get("message") or "請人工檢查 00631L 實盤風控。"
    lines.extend([
        "",
        "三、操作口徑",
        f"- {op_msg}",
        "- 這是人工風控提醒，不是自動交易指令；不會修改模擬單與成交紀錄。",
    ])
    return title, "\n".join(lines)


def run_once(
    *,
    dry_run: bool = False,
    ignore_time: bool = False,
    force: bool = False,
    config_path: Path = CONFIG_PATH,
    yf_download: Callable[..., Any] = yf.download,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now()
    config = load_config(config_path)
    if not config.get("enabled", True):
        logger.info("00631L 實盤風控監控已停用")
        return 0
    if not ignore_time and not dry_run and not is_trading_window(now, config):
        logger.info("非台股交易時段，跳過")
        return 0

    symbols = list(config.get("symbols") or [])
    quotes = fetch_live_quotes(symbols, yf_download=yf_download)
    position = load_00631l_position()
    state_name = position["position_state"]
    entry = position["entry_price"]
    exit_price = position["exit_price"]

    state_path = _resolve_path(config.get("state_path"), BASE_DIR / "logs" / "00631l_live_risk_state.json")
    audit_path = _resolve_path(config.get("audit_path"), BASE_DIR / "logs" / "00631l_live_risk_audit.jsonl")
    max_age = int(config.get("max_quote_age_minutes", 30))
    if not dry_run and quotes and not quotes_are_fresh(quotes, now=now, max_age_minutes=max_age):
        append_jsonl(audit_path, {
            "timestamp": _now_iso(),
            "event": "stale_quotes_skipped",
            "max_quote_age_minutes": max_age,
            "quotes": {k: q.__dict__ for k, q in quotes.items()},
        })
        logger.info("00631L 報價資料過舊，跳過價格風險推送")
        return 0

    if state_name == "flat":
        evaluation = evaluate_re_entry(quotes, exit_price=exit_price, config=config, now=now)
        cooldown = int((config.get("re_entry") or {}).get("cooldown_minutes", config.get("cooldown_minutes", 120)))
    else:
        evaluation = evaluate_risk(quotes, entry_price=entry, config=config, now=now)
        cooldown = int(config.get("cooldown_minutes", 60))

    state = load_state(state_path)
    close_check_force = state_name != "flat" and is_close_check_window(now, config) and evaluation.level_rank > 0
    send = should_send(evaluation, state, now=now, cooldown_minutes=cooldown, force=force or close_check_force)

    title, content = _format_card(quotes, evaluation, entry_price=entry, exit_price=exit_price, now=now, config=config)
    append_jsonl(audit_path, {
        "timestamp": _now_iso(),
        "event": "evaluated",
        "level": evaluation.level,
        "level_rank": evaluation.level_rank,
        "send": send,
        "reasons": list(evaluation.reasons),
        "quotes": {k: q.__dict__ for k, q in quotes.items()},
    })

    if dry_run:
        print(f"# {title}\n\n{content}\n\n（推送判定={send}）")
        return 0
    if not send:
        logger.info("風險未達推送條件或仍在冷卻時間：等級=%s 排序=%s", level_label(evaluation.level), evaluation.level_rank)
        return 0

    ok = send_dm(title, content)
    append_jsonl(audit_path, {"timestamp": _now_iso(), "event": "sent" if ok else "send_failed", "level": evaluation.level})
    if ok:
        save_state(state_path, mark_sent(state, evaluation, now=now))
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="00631L 實盤盤中風險雷達")
    parser.add_argument("--once", action="store_true", help="跑一輪後退出（預設行為）")
    parser.add_argument("--dry-run", action="store_true", help="只輸出，不推送、不更新狀態檔")
    parser.add_argument("--ignore-time", action="store_true", help="忽略交易時段限制（手動驗證用）")
    parser.add_argument("--force", action="store_true", help="忽略冷卻時間，只要有風險等級就推送")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()
    raise SystemExit(run_once(
        dry_run=args.dry_run,
        ignore_time=args.ignore_time,
        force=args.force,
        config_path=Path(args.config),
    ))


if __name__ == "__main__":
    main()
