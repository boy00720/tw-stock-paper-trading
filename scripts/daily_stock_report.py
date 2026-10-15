#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_stock_report.py — 台股每日專業報告生成器

由股市 W 盤後數據 + 新聞 W 當日新聞，生成一份固定骨架的精華 briefing。

輸出：
  - ~/Desktop/台股每日報告_YYYY-MM-DD.md
  - 飛書摘要推播
  - 微信摘要推播
"""

import os
import sys
import re
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional

for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

_AUTOMATION_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_AUTOMATION_ROOT))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(_AUTOMATION_ROOT), ".env"))

from utils.watchlist_schema import cross_signals, load_watchlist, pool_items, watchlist_summary
from utils.claude_helper import ClaudeHelper
from utils.master_agent_control import get_current_control, write_payload
from utils.pdf_generator import generate_stock_report_pdf

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "daily_stock_report.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("daily_stock_report")

CHAT_ID = "oc_your_feishu_chat_id"
WATCHLIST_POOL_LIMIT = 5
WATCHLIST_POOL_TITLES = [
    ("short_term", "短線主池"),
    ("medium_term", "中線主池"),
    ("long_term", "長線主池"),
    ("intraday", "日內前置信號"),
    ("cross_signals", "跨池訊號"),
]
SECTION_ORDER = [
    "一句話判斷",
    "市場看點",
    "短線選股日報",
    "跨池與日內焦點",
    "異常與風險",
    "明日行動清單",
]
TRACKING_BUCKET_TITLES = [
    ("today_first_watch", "今天先盯"),
    ("this_week_keep_watch", "本週持續盯"),
    ("next_month_verify", "下月驗證"),
    ("no_overinterpretation", "禁止過度解讀"),
]


def today(path: str) -> str:
    return datetime.now().strftime(path)


def proj_root() -> Path:
    return Path(os.path.dirname(_AUTOMATION_ROOT))


def extract_code(symbol: str) -> str:
    return (symbol or "").replace(".TW", "").replace(".TWO", "").lstrip("^")


def normalize_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return ""
    symbol = symbol.replace(".TWO", "").replace(".TW", "")
    return symbol.lstrip("^")


def build_symbol_aliases(symbol: str) -> List[str]:
    raw = (symbol or "").strip()
    normalized = normalize_symbol(raw)
    aliases = [raw, normalized]
    if normalized:
        aliases.extend([f"{normalized}.TW", f"{normalized}.TWO"])
    return [item for item in aliases if item]


def resolve_institutional_stats(stock_institutional: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    for key in build_symbol_aliases(symbol):
        item = stock_institutional.get(key)
        if isinstance(item, dict):
            return item

    code = normalize_symbol(symbol)
    if code:
        for key, item in stock_institutional.items():
            if normalize_symbol(key) == code and isinstance(item, dict):
                return item
    return {}


def summarize_breakdown_signal(breakdown: Dict[str, Any]) -> str:
    if not isinstance(breakdown, dict) or not breakdown:
        return ""

    labels = {
        "ret_5d": "5日動能",
        "vol_ratio": "量能放大",
        "break_20dh": "突破20日高點",
        "ma_stack": "均線多頭排列",
        "rsi_5": "短RSI轉強",
        "macd_hist": "MACD動能",
        "inst_buy": "法人買盤",
        "sector_rps": "族群相對強度",
        "rps20": "20日相對強度",
        "liquidity": "流動性達標",
        "catalyst": "題材催化",
    }
    parts: List[str] = []
    for key, value in sorted(breakdown.items(), key=lambda pair: float(pair[1] or 0), reverse=True):
        try:
            score = float(value or 0)
        except (TypeError, ValueError):
            continue
        if score <= 0:
            continue
        label = labels.get(str(key), str(key))
        parts.append(f"{label}{score:.0f}分")
        if len(parts) >= 4:
            break
    return "技術訊號：" + "｜".join(parts) if parts else ""


def summarize_technical_signal(item: Dict[str, Any], signal: Dict[str, Any], strategy_loaded: bool) -> str:
    required = ["breakout_pct", "volume_ratio", "close_position", "ma_retest"]
    breakdown_text = summarize_breakdown_signal(item.get("breakdown") or {})

    if not signal:
        fallback_detail = item.get("detail") or ""
        return fallback_detail or breakdown_text or "技術訊號：觀察價格與量能結構"

    normalized_signal = dict(signal)
    if normalized_signal.get("volume_ratio") is None and normalized_signal.get("vol_ratio") is not None:
        normalized_signal["volume_ratio"] = normalized_signal.get("vol_ratio")

    available: List[str] = []
    for key in required:
        value = normalized_signal.get(key)
        if value is None:
            continue
        if key == "breakout_pct":
            available.append(f"突破{float(value):.2f}%")
        elif key == "volume_ratio":
            available.append(f"量比 {float(value):.2f} 倍")
        elif key == "close_position":
            available.append(f"收盤位置{float(value):.2f}")
        elif key == "ma_retest":
            available.append(f"均線回踩{value}")

    if available:
        return "技術訊號：" + "｜".join(available)

    fallback_detail = normalized_signal.get("detail") or item.get("detail") or ""
    return fallback_detail or breakdown_text or "技術訊號：觀察價格與量能結構"


def format_name_with_code(name: str, symbol: str) -> str:
    clean_name = (name or "").strip()
    code = extract_code(symbol)
    if not code:
        return clean_name or symbol
    if not clean_name or normalize_symbol(clean_name) == normalize_symbol(symbol):
        return code
    suffix = f"（{code}）"
    return clean_name if clean_name.endswith(suffix) else f"{clean_name}{suffix}"


def infer_currency(symbol: str) -> str:
    symbol = symbol or ""
    if symbol.startswith("^"):
        return "INDEX"
    if symbol.endswith(".TW") or symbol.endswith(".TWO") or symbol.replace(".", "").isdigit():
        return "TWD"
    return "USD"


def format_price_text(price: float, currency: str) -> str:
    if currency == "USD":
        return f"{price:,.1f} USD"
    if currency == "INDEX":
        return f"{price:,.1f}"
    return f"{price:,.1f}元"


def format_lots(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0張"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:,.0f}張"


def summarize_institutional_stats(stats: Dict[str, Any]) -> str:
    if not stats:
        return "法人資料暫缺，先以價格結構觀察"

    rows = []
    for investor in ["外資", "投信", "自營商"]:
        item = stats.get(investor) or {}
        today = float(item.get("today") or 0)
        month = float(item.get("month") or 0)
        if abs(today) < 0.5 and abs(month) < 0.5:
            continue
        rows.append(f"{investor}今{format_lots(today)}／月{format_lots(month)}")
    return "；".join(rows) if rows else "法人資料暫缺，先以價格結構觀察"


def humanize_engine(engine: str) -> str:
    mapping = {
        "momentum": "量價突破",
        "rebound": "均值回歸",
        "growth": "成長趨勢",
        "value": "價值布局",
        "intraday": "日內強勢",
    }
    return mapping.get((engine or "").strip(), "策略觀察")


def humanize_score(score: Any) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "優先級未知"
    if value >= 85:
        level = "極高"
    elif value >= 75:
        level = "高"
    elif value >= 65:
        level = "中高"
    elif value >= 55:
        level = "中"
    else:
        level = "保守"
    return f"優先級{level}（{value:.0f}分）"


def direction_symbol(raw: str) -> str:
    return "▲" if raw in "▲△" else "▼"


def load_watchlist_summary() -> Dict[str, Any]:
    return load_watchlist(proj_root() / "logs" / "watchlist" / "current.json")


def _load_tracker_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("symbols") or {}
    except Exception as e:
        logger.warning("讀取 tracker state 失敗 %s: %s", path.name, e)
        return {}


def load_medium_term_state() -> Dict[str, Any]:
    return _load_tracker_state(proj_root() / "logs" / "sepa_tracker" / "watchlist_state.json")


def load_long_term_state() -> Dict[str, Any]:
    return _load_tracker_state(proj_root() / "logs" / "value_tracker" / "watchlist_state.json")


def build_watchlist_digest(
    watchlist: Dict[str, Any],
    stock_institutional: Dict[str, Any],
    strategy_signals: List[Dict[str, Any]],
) -> Dict[str, Any]:
    summary = watchlist_summary(watchlist)
    short_term = pool_items(watchlist, "short_term")
    medium_term = pool_items(watchlist, "medium_term")
    long_term = pool_items(watchlist, "long_term")
    cross_map = cross_signals(watchlist)
    intraday = pool_items(watchlist, "intraday")

    strategy_map = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in strategy_signals
        if normalize_symbol(item.get("symbol") or "")
    }
    strategy_loaded = len(strategy_signals) > 0

    def institutional_note(symbol: str) -> str:
        stats = resolve_institutional_stats(stock_institutional, symbol)
        return summarize_institutional_stats(stats)

    def summarize_cross_signal(item: Dict[str, Any]) -> str:
        pools = list(item.get("pools") or [])
        pool_label_map = {
            "short_term": "短線",
            "medium_term": "中線",
            "long_term": "長線",
            "intraday": "日內",
        }
        pool_text = "、".join(pool_label_map.get(pool, pool) for pool in pools) if pools else "跨池"
        level = str(item.get("level") or "").strip()
        notes = item.get("notes") or {}
        note_parts = [str(value).strip() for value in notes.values() if str(value).strip()]
        note_text = "；".join(note_parts[:2])
        parts = [part for part in [pool_text, level, note_text] if part]
        return "｜".join(parts) if parts else "跨池觀察"

    top_short = []
    for item in short_term[:WATCHLIST_POOL_LIMIT]:
        symbol = item.get("symbol") or ""
        signal = strategy_map.get(normalize_symbol(symbol)) or {}
        top_short.append({
            "name": format_name_with_code(item.get("name") or symbol, symbol),
            "symbol": symbol,
            "score": item.get("score"),
            "score_label": humanize_score(item.get("score")),
            "engine": item.get("engine") or "unknown",
            "engine_label": humanize_engine(item.get("engine") or "unknown"),
            "price": item.get("price") if item.get("price") is not None else item.get("entry_price"),
            "entry_price": item.get("entry_price"),
            "stop_loss": item.get("stop_loss"),
            "take_profit": item.get("take_profit"),
            "breakdown": item.get("breakdown"),
            "entry_zone": item.get("entry_zone"),
            "technical_note": summarize_technical_signal(item, signal, strategy_loaded),
            "institutional_note": institutional_note(symbol),
        })

    top_medium = []
    for item in medium_term[:WATCHLIST_POOL_LIMIT]:
        symbol = item.get("symbol") or ""
        top_medium.append({
            "name": format_name_with_code(item.get("name") or symbol, symbol),
            "symbol": symbol,
            "institutional_note": institutional_note(symbol),
        })

    top_long = []
    for item in long_term[:WATCHLIST_POOL_LIMIT]:
        symbol = item.get("symbol") or ""
        top_long.append({
            "name": format_name_with_code(item.get("name") or symbol, symbol),
            "symbol": symbol,
            "institutional_note": institutional_note(symbol),
        })

    top_cross = []
    if isinstance(cross_map, dict):
        for symbol, item in list(cross_map.items())[:WATCHLIST_POOL_LIMIT]:
            cross_symbol = item.get("symbol") or symbol
            top_cross.append({
                "symbol": cross_symbol,
                "name": format_name_with_code(item.get("name") or symbol, cross_symbol),
                "summary": item.get("summary") or summarize_cross_signal(item),
                "price": item.get("price"),
                "stop_loss": item.get("stop_loss"),
                "institutional_note": institutional_note(cross_symbol),
            })

    top_intraday = []
    for item in intraday[:WATCHLIST_POOL_LIMIT]:
        symbol = item.get("symbol") or ""
        top_intraday.append({
            "name": format_name_with_code(item.get("name") or symbol, symbol),
            "symbol": symbol,
            "strict_passed": item.get("strict_passed"),
            "strict_hard_fails": item.get("strict_hard_fails") or [],
            "institutional_note": institutional_note(symbol),
        })

    return enforce_watchlist_truth({
        "summary": summary,
        "top_short": top_short,
        "top_medium": top_medium,
        "top_long": top_long,
        "top_cross": top_cross,
        "top_intraday": top_intraday,
    })


def _bucket_row(name: str, main_axis: str, evidence: List[str], boundary: List[str]) -> str:
    parts = [name]
    if main_axis:
        parts.append(main_axis)
    if evidence:
        parts.append(f"依據:{'、'.join(evidence[:2])}")
    if boundary:
        parts.append(f"邊界:{'、'.join(boundary[:1])}")
    return "｜".join(parts)




def _find_bucket_line(buckets: Dict[str, List[str]], bucket_key: str, keyword: str) -> str:
    for line in buckets.get(bucket_key) or []:
        if keyword in line:
            return line
    return ""


def build_tracking_bucket_snapshot(
    stock_institutional: Optional[Dict[str, Any]] = None,
    strategy_signals: Optional[List[Dict[str, Any]]] = None,
    watchlist_raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    watchlist_raw = watchlist_raw or load_watchlist_summary()
    signals = strategy_signals if strategy_signals is not None else load_strategy_signals()
    watchlist_digest = build_watchlist_digest(
        watchlist_raw,
        stock_institutional or {},
        signals,
    )
    stock_focus = build_stock_analysis_focus(watchlist_digest, signals)
    buckets = build_tracking_buckets(
        watchlist_raw,
        watchlist_digest,
        stock_focus,
        load_medium_term_state(),
        load_long_term_state(),
    )
    return {
        "watchlist_raw": watchlist_raw,
        "watchlist_digest": watchlist_digest,
        "stock_focus": stock_focus,
        "tracking_buckets": buckets,
    }


def render_watchlist_pool_digest(watchlist_digest: Dict[str, Any], *, limit_per_pool: int = WATCHLIST_POOL_LIMIT) -> str:
    lines: List[str] = ["【觀察池與跨池訊號｜每類最多 5 檔】"]
    pool_items = {
        "short_term": [item.get("name") or item.get("symbol") or "—" for item in (watchlist_digest.get("top_short") or [])[:limit_per_pool]],
        "medium_term": [item.get("name") or item.get("symbol") or "—" for item in (watchlist_digest.get("top_medium") or [])[:limit_per_pool]],
        "long_term": [item.get("name") or item.get("symbol") or "—" for item in (watchlist_digest.get("top_long") or [])[:limit_per_pool]],
        "intraday": [item.get("name") or item.get("symbol") or "—" for item in (watchlist_digest.get("top_intraday") or [])[:limit_per_pool]],
        "cross_signals": [item.get("name") or item.get("symbol") or "—" for item in (watchlist_digest.get("top_cross") or [])[:limit_per_pool]],
    }
    for key, title in WATCHLIST_POOL_TITLES:
        items = pool_items.get(key) or []
        if items:
            suffix = "（輔助訊號）" if key == "intraday" else ""
            lines.append(f"- {title}{suffix}：" + "；".join(items))
        else:
            lines.append(f"- {title}：暫無")
    return "\n".join(lines)


def render_watchlist_pool_digest_from_payload(watchlist: Dict[str, Any], *, limit_per_pool: int = WATCHLIST_POOL_LIMIT) -> str:
    lines: List[str] = ["【觀察池與跨池訊號｜每類最多 5 檔】"]
    pools = [
        ("短線主池", list(watchlist.get("short_focus") or [])[:limit_per_pool]),
        ("中線主池", list(watchlist.get("medium_focus") or [])[:limit_per_pool]),
        ("長線主池", list(watchlist.get("long_focus") or [])[:limit_per_pool]),
        ("日內前置信號（輔助）", list(watchlist.get("intraday_focus") or [])[:limit_per_pool]),
        ("跨池訊號", list(watchlist.get("cross_focus") or [])[:limit_per_pool]),
    ]
    for title, items in pools:
        if items:
            lines.append(f"- {title}：" + "；".join(items))
        else:
            lines.append(f"- {title}：暫無")
    return "\n".join(lines)


def render_tracking_bucket_digest(
    buckets: Dict[str, List[str]],
    *,
    include_titles: tuple[str, ...] = ("today_first_watch", "next_month_verify", "no_overinterpretation"),
    limit_per_bucket: int = 3,
) -> str:
    title_map = dict(TRACKING_BUCKET_TITLES)
    lines: List[str] = ["【追蹤動作清單】"]
    for key in include_titles:
        title = title_map.get(key, key)
        bucket_items = list(buckets.get(key) or [])
        items = bucket_items[:limit_per_bucket]
        if key == "next_month_verify" and limit_per_bucket > 0:
            umc_line = next((line for line in bucket_items if "聯電" in line), "")
            if umc_line and umc_line not in items:
                items = items[:-1] + [umc_line] if len(items) >= limit_per_bucket else items + [umc_line]
        if items:
            lines.append(f"- {title}：" + "；".join(items))
        else:
            lines.append(f"- {title}：暫無")
    return "\n".join(lines)


def validate_tracking_buckets() -> Dict[str, Any]:
    snapshot = build_tracking_bucket_snapshot()
    buckets = snapshot["tracking_buckets"]
    checks = {
        "today_has_wei_ying": bool(_find_bucket_line(buckets, "today_first_watch", "緯穎")),
        "today_has_inventec": bool(_find_bucket_line(buckets, "today_first_watch", "英業達")),
        "next_month_has_umc": bool(_find_bucket_line(buckets, "next_month_verify", "聯電")),
        "no_overinterpretation_has_formosa_plastics": bool(_find_bucket_line(buckets, "no_overinterpretation", "台化")),
        "cross_signal_count_is_zero": int(((snapshot["watchlist_digest"].get("summary") or {}).get("cross_signals_count") or 0)) == 0,
        "today_has_no_duplicate_symbols": len({line.split("｜", 1)[0] for line in buckets.get("today_first_watch") or []}) == len(buckets.get("today_first_watch") or []),
    }
    passed = all(checks.values())
    return {
        "ok": passed,
        "checks": checks,
        "tracking_buckets": buckets,
    }


def build_tracking_buckets(
    watchlist_raw: Dict[str, Any],
    watchlist_digest: Dict[str, Any],
    stock_focus: List[Dict[str, Any]],
    medium_state: Dict[str, Any],
    long_state: Dict[str, Any],
) -> Dict[str, List[str]]:
    focus_map = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in stock_focus
        if normalize_symbol(item.get("symbol") or "")
    }
    buckets: Dict[str, List[str]] = {key: [] for key, _ in TRACKING_BUCKET_TITLES}
    seen_symbols: Dict[str, set[str]] = {key: set() for key, _ in TRACKING_BUCKET_TITLES}

    def push(bucket_key: str, symbol: str, name: str, fallback_axis: str, extra_evidence: Optional[List[str]] = None, extra_boundary: Optional[List[str]] = None) -> None:
        code = normalize_symbol(symbol)
        if code and code in seen_symbols[bucket_key]:
            return
        focus = focus_map.get(code) or {}
        display_name = name or focus.get("name") or symbol
        axis = focus.get("main_axis") or fallback_axis
        evidence = list(focus.get("evidence") or [])
        boundary = list(focus.get("boundary") or [])
        if extra_evidence:
            evidence.extend(extra_evidence)
        if extra_boundary:
            boundary.extend(extra_boundary)
        row = _bucket_row(display_name, axis, evidence, boundary)
        buckets[bucket_key].append(row)
        if code:
            seen_symbols[bucket_key].add(code)

    short_symbols = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in pool_items(watchlist_raw, "short_term")
        if normalize_symbol(item.get("symbol") or "")
    }
    intraday_symbols = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in pool_items(watchlist_raw, "intraday")
        if normalize_symbol(item.get("symbol") or "")
    }
    medium_symbols = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in pool_items(watchlist_raw, "medium_term")
        if normalize_symbol(item.get("symbol") or "")
    }
    long_symbols = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in pool_items(watchlist_raw, "long_term")
        if normalize_symbol(item.get("symbol") or "")
    }

    for code, item in intraday_symbols.items():
        if item.get("strict_passed"):
            push("today_first_watch", item.get("symbol") or code, item.get("name") or code, "日內觀察")
        else:
            blockers = item.get("strict_hard_fails") or []
            push("no_overinterpretation", item.get("symbol") or code, item.get("name") or code, "日內觀察", extra_boundary=[f"日內阻塞:{','.join(blockers[:2])}"] if blockers else ["日內候選未通過嚴格條件"])

    for code, item in short_symbols.items():
        long_entry = long_state.get(item.get("symbol") or "") or long_state.get(code) or {}
        medium_entry = medium_state.get(item.get("symbol") or "") or medium_state.get(code) or {}
        if (long_entry.get("consecutive_months") or 0) >= 1 and not long_entry.get("in_pool"):
            push("today_first_watch", item.get("symbol") or code, item.get("name") or code, "短線動能", extra_evidence=[f"長線確認期 {long_entry.get('consecutive_months')}/2"])
            continue
        if (medium_entry.get("consecutive_weeks") or 0) >= 1 and not medium_entry.get("in_pool"):
            push("today_first_watch", item.get("symbol") or code, item.get("name") or code, "短線動能", extra_evidence=[f"中線確認期 {medium_entry.get('consecutive_weeks')}/3"])
            continue
        push("this_week_keep_watch", item.get("symbol") or code, item.get("name") or code, "短線動能")
        if str(item.get("engine") or "") == "rebound":
            push("no_overinterpretation", item.get("symbol") or code, item.get("name") or code, "短線動能", extra_boundary=["反彈股不可直接升格為跨池"])

    for code, item in medium_symbols.items():
        push("this_week_keep_watch", item.get("symbol") or code, item.get("name") or code, "中線趨勢")

    for code, item in long_symbols.items():
        push("this_week_keep_watch", item.get("symbol") or code, item.get("name") or code, "長線價值")

    for symbol, item in long_state.items():
        code = normalize_symbol(symbol)
        if not code or code in long_symbols:
            continue
        if item.get("in_pool"):
            push("this_week_keep_watch", symbol, format_name_with_code(item.get("name") or symbol, symbol), "長線價值", extra_evidence=[f"長線正式在池 {item.get('pool_entry_date') or ''}".strip()])
            continue
        if (item.get("consecutive_months") or 0) >= 1 and item.get("last_passed"):
            bucket = "today_first_watch" if code in short_symbols else "next_month_verify"
            push(bucket, symbol, format_name_with_code(item.get("name") or symbol, symbol), "長線價值", extra_evidence=[f"長線確認期 {item.get('consecutive_months')}/2"])

    for symbol, item in medium_state.items():
        code = normalize_symbol(symbol)
        if not code or code in medium_symbols:
            continue
        if item.get("in_pool"):
            push("this_week_keep_watch", symbol, format_name_with_code(item.get("name") or symbol, symbol), "中線趨勢", extra_evidence=[f"中線正式在池 {item.get('pool_entry_date') or ''}".strip()])
            continue
        if (item.get("consecutive_weeks") or 0) >= 1 and item.get("last_passed"):
            bucket = "today_first_watch" if code in short_symbols else "next_month_verify"
            push(bucket, symbol, format_name_with_code(item.get("name") or symbol, symbol), "中線趨勢", extra_evidence=[f"中線確認期 {item.get('consecutive_weeks')}/3"])

    for code, item in short_symbols.items():
        if code not in intraday_symbols and code not in long_state and code not in medium_state:
            focus = focus_map.get(code) or {}
            if str(item.get("engine") or "") == "rebound" or focus.get("main_axis") in {"僅觀察", "日內觀察"}:
                push("no_overinterpretation", item.get("symbol") or code, item.get("name") or code, "短線動能", extra_boundary=["單池強勢不可直接解讀為跨池"])

    return buckets


def build_stock_analysis_focus(
    watchlist_digest: Dict[str, Any],
    strategy_signals: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    focus_map: Dict[str, Dict[str, Any]] = {}
    signal_map = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in strategy_signals
        if normalize_symbol(item.get("symbol") or "")
    }

    def ensure(symbol: str, name: str) -> Dict[str, Any]:
        code = normalize_symbol(symbol)
        if not code:
            return {}
        existing = focus_map.get(code)
        if existing:
            return existing
        focus_map[code] = {
            "symbol": symbol,
            "name": name,
            "pool_fact": {
                "short_term": "不在池",
                "medium_term": "不在池",
                "long_term": "不在池",
                "intraday": "不在池",
                "cross_signal": "不成立",
            },
            "main_axis": "僅觀察",
            "evidence": [],
            "boundary": [],
        }
        return focus_map[code]

    for item in watchlist_digest.get("top_short") or []:
        entry = ensure(item.get("symbol") or "", item.get("name") or item.get("symbol") or "")
        if not entry:
            continue
        entry["pool_fact"]["short_term"] = "在池"
        entry["main_axis"] = "短線動能"
        if item.get("score") is not None:
            entry["evidence"].append(f"短線分數{int(float(item.get('score')))}")
        if item.get("entry_zone"):
            entry["evidence"].append(f"進場區{_format_entry_zone_text(item.get('entry_zone'))}")
        if item.get("stop_loss") is not None:
            entry["evidence"].append(f"停損{_format_trade_text(item.get('stop_loss'), '元')}")
        signal = signal_map.get(normalize_symbol(item.get("symbol") or "")) or {}
        if signal.get("strength"):
            entry["evidence"].append(f"訊號強度 {signal.get('strength')}")
        entry["boundary"].append("不可延伸為中長線或跨池成立")

    for item in watchlist_digest.get("top_medium") or []:
        entry = ensure(item.get("symbol") or "", item.get("name") or item.get("symbol") or "")
        if not entry:
            continue
        entry["pool_fact"]["medium_term"] = "在池"
        if entry["main_axis"] == "僅觀察":
            entry["main_axis"] = "中線趨勢"
        note = item.get("institutional_note") or ""
        if note and "資料暫缺" not in note:
            entry["evidence"].append(note)

    for item in watchlist_digest.get("top_long") or []:
        entry = ensure(item.get("symbol") or "", item.get("name") or item.get("symbol") or "")
        if not entry:
            continue
        entry["pool_fact"]["long_term"] = "在池"
        if entry["main_axis"] == "僅觀察":
            entry["main_axis"] = "長線價值"
        note = item.get("institutional_note") or ""
        if note and "資料暫缺" not in note:
            entry["evidence"].append(note)

    for item in watchlist_digest.get("top_intraday") or []:
        entry = ensure(item.get("symbol") or "", item.get("name") or item.get("symbol") or "")
        if not entry:
            continue
        entry["pool_fact"]["intraday"] = "在池" if item.get("strict_passed") else "候選"
        if entry["main_axis"] == "僅觀察":
            entry["main_axis"] = "日內觀察"
        blockers = item.get("strict_hard_fails") or []
        if blockers:
            entry["boundary"].append(f"日內阻塞:{','.join(blockers[:3])}")

    for item in watchlist_digest.get("top_cross") or []:
        entry = ensure(item.get("symbol") or "", item.get("name") or item.get("symbol") or "")
        if not entry:
            continue
        entry["pool_fact"]["cross_signal"] = "成立"
        entry["main_axis"] = "跨池訊號"
        summary_text = (item.get("summary") or "").strip()
        if summary_text:
            entry["evidence"].append(summary_text)

    ordered: List[Dict[str, Any]] = []
    for key in ["top_short", "top_cross", "top_intraday", "top_medium", "top_long"]:
        for item in watchlist_digest.get(key) or []:
            code = normalize_symbol(item.get("symbol") or "")
            if not code or code not in focus_map:
                continue
            candidate = focus_map[code]
            if candidate not in ordered:
                candidate["evidence"] = candidate["evidence"][:3]
                candidate["boundary"] = candidate["boundary"][:2]
                ordered.append(candidate)
    return ordered[:6]


def enforce_watchlist_truth(watchlist_digest: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(watchlist_digest or {})
    summary = dict(normalized.get("summary") or {})

    short_count = int(summary.get("short_term_count") or 0)
    cross_count = int(summary.get("cross_signals_count") or 0)
    intraday_count = int(summary.get("intraday_count") or 0)

    normalized["summary"] = summary
    normalized["top_short"] = list(normalized.get("top_short") or []) if short_count > 0 else []
    normalized["top_medium"] = list(normalized.get("top_medium") or [])
    normalized["top_long"] = list(normalized.get("top_long") or [])
    normalized["top_cross"] = list(normalized.get("top_cross") or []) if cross_count > 0 else []
    normalized["top_intraday"] = list(normalized.get("top_intraday") or []) if intraday_count > 0 else []
    return normalized


def extract_allowed_stock_names(rows: List[str]) -> List[str]:
    names: List[str] = []
    for row in rows or []:
        text = str(row or "").strip()
        if not text:
            continue
        name = text.split("｜", 1)[0].strip().lstrip("-• ")
        if name:
            names.append(name)
    return names


def contains_out_of_scope_stock(text: str, allowed_names: List[str]) -> bool:
    content = (text or "").strip()
    if not content:
        return False
    if not re.search(r"[（(]\d{4,6}[)）]", content):
        return False
    return not any(name and name in content for name in allowed_names)


def build_cross_intraday_commentary(cross_focus: List[str], intraday_focus: List[str]) -> str:
    entries: List[str] = []
    for row in cross_focus or []:
        parts = [part.strip() for part in str(row or "").split("｜") if part.strip()]
        if not parts:
            continue
        name = parts[0]
        price_text = next((part.replace("現價 ", "", 1) for part in parts if part.startswith("現價 ")), "未提供")
        stop_text = next((part.replace("停損 ", "", 1) for part in parts if part.startswith("停損 ")), "跨池觀察未生成交易停損")
        institutional_note = parts[-1] if any(marker in parts[-1] for marker in ["外資", "投信", "自營商"]) else "法人欄位未提供"
        entries.append(f"{name}跨池成立：現價{price_text}；{institutional_note}；{stop_text}，不升格短線池。")

    if intraday_focus:
        names = "、".join(extract_allowed_stock_names(intraday_focus)[:3])
        entries.append(f"日內候選先看 {names}。")
    elif entries:
        entries.append("日內候選0檔。")

    return " ".join(entries) if entries else "今日無跨池或日內焦點。"


def sanitize_analysis_by_truth(payload: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(analysis or {})
    watchlist = payload.get("watchlist") or {}
    summary = watchlist.get("summary") or {}
    short_focus = watchlist.get("short_focus") or []
    cross_focus = watchlist.get("cross_focus") or []
    intraday_focus = watchlist.get("intraday_focus") or []

    short_count = int(summary.get("short_term_count") or 0)
    cross_count = int(summary.get("cross_signals_count") or 0)
    intraday_count = int(summary.get("intraday_count") or 0)

    short_allowed = extract_allowed_stock_names(short_focus)
    cross_allowed = extract_allowed_stock_names(cross_focus) + extract_allowed_stock_names(intraday_focus)

    short_text = (sanitized.get("短線選股日報") or "").strip()
    if short_count <= 0 or not short_focus:
        sanitized["短線選股日報"] = "今日短線池無明確焦點，維持紀律觀察。"
    else:
        deterministic_shortline = build_shortline_commentary_from_focus(short_focus)
        if contains_out_of_scope_stock(short_text, short_allowed) or not short_text:
            sanitized["短線選股日報"] = deterministic_shortline
        else:
            sanitized["短線選股日報"] = deterministic_shortline

    cross_text = (sanitized.get("跨池與日內焦點") or "").strip()
    if (cross_count <= 0 and intraday_count <= 0) or (not cross_focus and not intraday_focus):
        sanitized["跨池與日內焦點"] = "今日無跨池或日內焦點。"
    elif cross_focus:
        sanitized["跨池與日內焦點"] = build_cross_intraday_commentary(cross_focus, intraday_focus)
    elif contains_out_of_scope_stock(cross_text, cross_allowed):
        primary = "、".join(cross_allowed[: min(len(cross_allowed), 5)])
        sanitized["跨池與日內焦點"] = f"跨池與日內焦點僅對齊實際名單，先看 {primary}。"

    return sanitized


def parse_stock_log() -> Dict[str, Any]:
    log_path = proj_root() / "logs" / "stock_monitor.log"
    if not log_path.exists():
        return {}

    content = log_path.read_text(encoding="utf-8")
    blocks = re.split(r"(?=^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.*Morning brief:)", content, flags=re.MULTILINE)
    if not blocks:
        return {}

    latest_block = blocks[-1] if blocks else ""
    quote_pattern = re.compile(r"^\s*([^\(（]+?)(?:[\(（]([^\)）]+)[\)）])?\s+([\d,]+(?:\.\d+)?)\s+([▲▼△▽])([\d.]+)%")
    quotes = []

    for line in latest_block.splitlines():
        m = quote_pattern.match(line.strip())
        if not m:
            continue
        name = m.group(1).strip()
        symbol = m.group(2) or ""
        price_str = m.group(3).replace(",", "")
        direction = m.group(4)
        change_pct = float(m.group(5))
        currency = infer_currency(symbol)
        quotes.append({
            "name": name,
            "symbol": symbol,
            "price": float(price_str),
            "direction": direction,
            "change_pct": change_pct,
            "currency": currency,
        })

    inst_section = ""
    inst_match = re.search(r"(?:──|--)\s*大盤籌碼動向\s*──(.*?)(?:──|⚠️|💡|$)", latest_block, re.DOTALL)
    if inst_match:
        inst_section = inst_match.group(1).strip()

    alerts = re.findall(r"⚠️\s*(.*?)\s+([\d,]+(?:\.\d+)?)\s+\((.*?)\)", latest_block)

    return {
        "quotes": quotes,
        "institutional": inst_section,
        "alerts": alerts,
        "raw_block": latest_block[-3000:],
    }




def build_latest_stock_notification_quotes() -> Dict[str, Dict[str, Any]]:
    payload_root = proj_root() / "logs" / "master_agent_payloads"
    if not payload_root.exists():
        return {}

    payload_paths = sorted(
        payload_root.glob("*/stock_afternoon.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    line_pattern = re.compile(r"^\s*([^\n（(]+?)(?:（([^）]+)）|\(([^)]+)\))(?:\s+\[[^\]]+\])*\s+([\d,]+(?:\.\d+)?)\s+([▲▼△▽])\s*([\d.]+)%")

    for path in payload_paths[:5]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("讀取 stock payload 失敗 %s: %s", path, e)
            continue
        content = (((payload.get("notification") or {}).get("feishu") or {}).get("content") or "")
        if not content:
            continue
        quotes: Dict[str, Dict[str, Any]] = {}
        for line in content.splitlines():
            match = line_pattern.match(line)
            if not match:
                continue
            name = match.group(1).strip()
            raw_symbol = (match.group(2) or match.group(3) or "").strip()
            alias_map = {
                "TWII": "^TWII",
                "DJI": "^DJI",
                "IXIC": "^IXIC",
                "GC=F": "GC=F",
                "CL=F": "CL=F",
                "TSM": "TSM",
                "NVDA": "NVDA",
            }
            symbol = alias_map.get(raw_symbol, raw_symbol)
            price = float(match.group(4).replace(",", ""))
            direction = match.group(5)
            change_pct = float(match.group(6))
            normalized = normalize_symbol(symbol)
            if not normalized:
                continue
            full_symbol = symbol if "." in symbol or symbol.startswith("^") or symbol in {"NVDA", "TSM", "GC=F", "CL=F"} else f"{normalized}.TW"
            quotes[normalized] = {
                "name": name,
                "symbol": full_symbol,
                "price": price,
                "direction": direction,
                "change_pct": change_pct,
                "currency": infer_currency(full_symbol),
            }
        if quotes:
            return quotes
    return {}


def apply_quote_overrides(stock_data: Dict[str, Any], watchlist: Dict[str, Any]) -> Dict[str, Any]:
    latest_quotes = build_latest_stock_notification_quotes()
    if not latest_quotes:
        return stock_data

    merged_by_code = {normalize_symbol(q.get("symbol") or ""): q for q in stock_data.get("quotes") or [] if normalize_symbol(q.get("symbol") or "")}
    for code, quote in latest_quotes.items():
        merged_by_code[code] = quote

    existing_codes = set(merged_by_code)
    for section in ["short_term", "medium_term", "long_term", "intraday"]:
        for item in pool_items(watchlist, section):
            code = normalize_symbol(item.get("symbol") or "")
            price = item.get("price") if item.get("price") is not None else item.get("entry_price")
            if code and price is not None and code not in existing_codes:
                symbol = item.get("symbol") or f"{code}.TW"
                merged_by_code[code] = {
                    "name": item.get("name") or symbol,
                    "symbol": symbol,
                    "price": float(price),
                    "direction": "▲",
                    "change_pct": 0.0,
                    "currency": infer_currency(symbol),
                }

    stock_data["quotes"] = list(merged_by_code.values())
    return stock_data


def resolve_quote(stock_data: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    code = normalize_symbol(symbol)
    if not code:
        return {}
    for item in stock_data.get("quotes") or []:
        if normalize_symbol(item.get("symbol") or "") == code:
            return item
    return {}


def _recalculate_short_trade_levels(item: Dict[str, Any], price: float) -> Dict[str, Any]:
    updated = dict(item)
    updated["price"] = price
    updated["entry_price"] = price
    updated["entry_zone"] = [round(price * 0.995, 2), round(price * 1.01, 2)]
    updated["stop_loss"] = round(price * 0.95, 2)
    updated["take_profit"] = round(price * 1.10, 2)
    return updated


def enrich_watchlist_with_quotes(watchlist: Dict[str, Any], stock_data: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(watchlist or {})
    quote_map = {
        normalize_symbol(item.get("symbol") or ""): item
        for item in stock_data.get("quotes") or []
        if normalize_symbol(item.get("symbol") or "")
    }

    for section in ["short_term", "medium_term", "long_term", "intraday"]:
        rows = []
        for raw_item in pool_items(normalized, section):
            item = dict(raw_item)
            quote = quote_map.get(normalize_symbol(item.get("symbol") or ""))
            if quote and quote.get("price") is not None:
                price = float(quote.get("price"))
                if section == "short_term":
                    item = _recalculate_short_trade_levels(item, price)
                else:
                    item["price"] = price
                    item["entry_price"] = price if item.get("entry_price") is None else item.get("entry_price")
            rows.append(item)
        normalized[section] = rows

    cross_signals = normalized.get("cross_signals") or {}
    if isinstance(cross_signals, dict):
        enriched_cross = {}
        for symbol, raw_item in cross_signals.items():
            item = dict(raw_item or {})
            quote = quote_map.get(normalize_symbol(symbol)) or quote_map.get(normalize_symbol(item.get("symbol") or ""))
            if quote:
                item["symbol"] = quote.get("symbol") or symbol
                item["price"] = quote.get("price")
            elif not item.get("symbol"):
                item["symbol"] = symbol
            enriched_cross[symbol] = item
        normalized["cross_signals"] = enriched_cross
    return normalized


def load_institutional_snapshot() -> Dict[str, Any]:
    snapshot_dir = proj_root() / "logs" / "institutional_snapshots"
    today_prefix = today("%Y-%m-%d")
    today_paths = sorted(snapshot_dir.glob(f"{today_prefix}_*.json"), reverse=True)
    candidates = today_paths + [snapshot_dir / "current.json"]

    success_payloads: List[Dict[str, Any]] = []

    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("讀取法人 snapshot 失敗 %s: %s", path.name, e)
            continue

        market_total_text = (payload.get("market_total_text") or "").strip()
        stock_institutional = payload.get("stocks") or {}
        leaders = payload.get("leaders") or {}
        if not (market_total_text or stock_institutional or leaders):
            continue
        if (payload.get("status") or "").lower() == "ok":
            success_payloads.append({
                "institutional": market_total_text,
                "stock_institutional": stock_institutional,
                "leaders": leaders,
                "snapshot_meta": {
                    "path": str(path),
                    "status": payload.get("status") or "unknown",
                    "source": payload.get("source") or "institutional_snapshot",
                    "generated_at": payload.get("generated_at") or "",
                },
            })

    if success_payloads:
        return max(
            success_payloads,
            key=lambda item: (
                len(item.get("stock_institutional") or {}),
                item.get("snapshot_meta", {}).get("generated_at") or "",
            ),
        )

    snapshot_files = sorted(snapshot_dir.glob("*.json"), reverse=True)
    for path in snapshot_files:
        if path.name == "current.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("讀取法人 snapshot 失敗 %s: %s", path.name, e)
            continue

        if (payload.get("status") or "").lower() != "ok":
            continue
        market_total_text = (payload.get("market_total_text") or "").strip()
        stock_institutional = payload.get("stocks") or {}
        leaders = payload.get("leaders") or {}
        if market_total_text or stock_institutional or leaders:
            logger.warning("今日法人 snapshot 不可用，已回退到最近成功快照：%s", path.name)
            return {
                "institutional": market_total_text,
                "stock_institutional": stock_institutional,
                "leaders": leaders,
                "snapshot_meta": {
                    "path": str(path),
                    "status": payload.get("status") or "unknown",
                    "source": payload.get("source") or "institutional_snapshot",
                    "generated_at": payload.get("generated_at") or "",
                },
            }
    return {}


def read_today_news() -> str:
    news_path = proj_root() / "logs" / "daily_news" / today("%Y-%m-%d.md")
    if news_path.exists():
        return news_path.read_text(encoding="utf-8")
    return ""



def load_strategy_signals() -> List[Dict[str, Any]]:
    signal_path = proj_root() / "logs" / "strategy_signals" / f"{today('%Y-%m-%d')}.json"
    if not signal_path.exists():
        return []
    try:
        payload = json.loads(signal_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except Exception as e:
        logger.warning("讀取 strategy_signals 失敗: %s", e)
        return []




def summarize_news_items(news_text: str, limit: int = 5) -> List[str]:
    if not news_text:
        return []

    items: List[str] = []
    for raw in news_text.splitlines():
        line = raw.strip().lstrip("-•* ").strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if len(line) < 8:
            continue
        if line not in items:
            items.append(line)
        if len(items) >= limit:
            break
    return items




def render_institutional_rows(inst_text: str) -> List[str]:
    text = (inst_text or "").strip()
    if not text:
        return []
    if "無三大法人資料" in text:
        return ["- 三大法人：資料缺失"]

    rows: List[str] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    rows.append(f"- 三大法人：{lines[0]}")
    for line in lines[1:]:
        cleaned = line.lstrip("-• ").strip()
        if cleaned:
            rows.append(f"  - {cleaned}")
    return rows




def format_reason_text(item: Dict[str, Any]) -> str:
    breakdown = item.get("breakdown") or {}
    entry_zone = item.get("entry_zone") or []
    reason_labels = {
        "vol_ratio": "量能放大",
        "break_20dh": "接近或突破20日高點",
        "inst_buy": "法人偏多買盤",
        "rps20": "相對強度優勢",
        "catalyst": "題材催化",
        "liquidity": "流動性達標",
        "ret_5d": "短期漲勢延續",
        "ma_stack": "均線多頭排列",
    }
    engine_label = item.get("engine_label") or item.get("engine") or "策略觀察"
    score_label = item.get("score_label") or "優先級未知"
    if breakdown:
        top_parts = sorted(breakdown.items(), key=lambda pair: pair[1], reverse=True)[:3]
        top_pairs = [(reason_labels.get(key, key), value) for key, value in top_parts if value and value > 0]
        if top_pairs:
            reason = f"{engine_label}｜{score_label}｜" + "、".join(f"{label}（{value}分）" for label, value in top_pairs)
        else:
            reason = f"{engine_label}｜{score_label}"
    else:
        reason = f"{engine_label}｜{score_label}"
    if isinstance(entry_zone, list) and len(entry_zone) >= 2:
        reason += f"｜進場區 {entry_zone[0]:.2f}-{entry_zone[1]:.2f}"
    return reason


def _resolve_shortline_institutional_note(item: Dict[str, Any], stock_institutional: Dict[str, Any]) -> str:
    note = (item.get("institutional_note") or "").strip()
    if note and "資料暫缺" not in note:
        return note

    stats = resolve_institutional_stats(stock_institutional, item.get("symbol") or "")
    resolved = summarize_institutional_stats(stats)
    return resolved.strip() if resolved else (note or "法人資料暫缺，先以價格結構觀察")


def build_short_focus_markdown_row(item: Dict[str, Any]) -> str:
    return (
        f"{item['name']}｜{item['engine_label']}｜{item['score_label']}｜"
        f"現價 {item['price_text']}｜進場 {item['entry_text']}｜停損 {item['stop_text']}｜"
        f"目標 {item['target_text']}｜技術 {item['technical_note']}｜法人 {item['institutional_note']}"
    )


def build_canonical_shortline_items(top_short: List[Dict[str, Any]], stock_institutional: Dict[str, Any]) -> List[Dict[str, Any]]:
    shortline_items: List[Dict[str, Any]] = []
    for raw_item in top_short or []:
        price = raw_item.get("price") if raw_item.get("price") is not None else raw_item.get("entry_price")
        item = {
            "name": raw_item.get("name") or raw_item.get("symbol") or "—",
            "symbol": raw_item.get("symbol") or "",
            "engine_label": raw_item.get("engine_label") or "策略觀察",
            "score_label": raw_item.get("score_label") or "優先級未知",
            "price_text": _format_trade_text(price, "元") if price is not None else "未提供",
            "entry_text": _format_entry_zone_text(raw_item.get("entry_zone")),
            "stop_text": _format_trade_text(raw_item.get("stop_loss"), "元"),
            "target_text": _format_trade_text(raw_item.get("take_profit"), "元"),
            "technical_note": raw_item.get("technical_note") or "技術欄位未提供",
            "institutional_note": _resolve_shortline_institutional_note(raw_item, stock_institutional),
            "reason_text": format_reason_text(raw_item),
        }
        item["markdown_row"] = build_short_focus_markdown_row(item)
        shortline_items.append(item)
    return shortline_items


def build_shortline_summary(shortline_items: List[Dict[str, Any]], limit: int = 2) -> str:
    highlights: List[str] = []
    for item in (shortline_items or [])[:limit]:
        signal = item.get("technical_note") or item.get("reason_text") or "訊號未提供"
        signal = _trim_text(signal, 28, "訊號未提供")
        highlights.append(
            f"{item['name']}：{signal}；進場 {item['entry_text']}；停損 {item['stop_text']}"
        )
    return "；".join(highlights) if highlights else "短線摘要暫缺"


def build_shortline_commentary(shortline_items: List[Dict[str, Any]]) -> str:
    entries: List[str] = []
    for item in shortline_items or []:
        action = f"進場 {item['entry_text']}" if item.get("entry_text") != "未提供" else "進場條件未提供，先觀望"
        entries.append(
            f"{item['name']}：{item['technical_note']}；{action}；停損 {item['stop_text']}；法人 {item['institutional_note']}"
        )
    return "\n".join(entries) if entries else "今日短線池無明確焦點，維持紀律觀察。"


def format_leader_rows(leaders: Dict[str, Any]) -> List[str]:
    if not leaders:
        return ["- 全市場法人個股榜未生成"]

    investor_labels = {
        "foreign": "外資",
        "investment": "投信",
        "dealer": "自營商",
    }
    rows: List[str] = []
    for key in ["foreign", "investment", "dealer"]:
        label = investor_labels[key]
        group = leaders.get(key) or {}
        buy_top = group.get("buy_top") or []
        sell_top = group.get("sell_top") or []

        buy_text = "、".join(
            f"{format_name_with_code(item.get('name') or item.get('symbol') or '', item.get('symbol') or '')}{format_lots(item.get('today_lots'))}"
            for item in buy_top[:5]
        ) or "未提供"
        sell_text = "、".join(
            f"{format_name_with_code(item.get('name') or item.get('symbol') or '', item.get('symbol') or '')}{format_lots(item.get('today_lots'))}"
            for item in sell_top[:5]
        ) or "未提供"
        rows.append(f"- {label}買超前五：{buy_text}")
        rows.append(f"- {label}賣超前五：{sell_text}")
    return rows


def classify_quote_bucket(symbol: str) -> str:
    code = extract_code(symbol)
    if symbol in {"^TWII", "TWII"}:
        return "tw_index"
    if symbol in {"^DJI", "^IXIC", "NVDA", "TSM"}:
        return "us_key"
    if code in {"2330", "2454", "2412", "1303"}:
        return "tw_key"
    return "other"


def build_briefing_payload(
    stock_data: Dict[str, Any],
    news_text: str,
    watchlist_digest: Dict[str, Any],
    strategy_signals: List[Dict[str, Any]],
    profile: str,
) -> Dict[str, Any]:
    quotes = stock_data.get("quotes", [])
    grouped = {"tw_index": [], "tw_key": [], "us_key": [], "other": []}
    for q in quotes:
        grouped[classify_quote_bucket(q.get("symbol", ""))].append({
            "name": format_name_with_code(q["name"], q["symbol"]),
            "symbol": q["symbol"],
            "price_text": format_price_text(q["price"], q.get("currency", "TWD")),
            "change_text": f"{direction_symbol(q['direction'])}{abs(q['change_pct']):.2f}%",
            "change_pct": q["change_pct"],
        })

    alerts = [
        {"name": name, "price": price, "reason": reason}
        for name, price, reason in stock_data.get("alerts", [])[:3]
    ]

    summary = watchlist_digest.get("summary") or {}
    stock_institutional = stock_data.get("stock_institutional") or {}
    short_focus_items = build_canonical_shortline_items(watchlist_digest.get("top_short") or [], stock_institutional)
    short_rows = [item["markdown_row"] for item in short_focus_items]

    medium_rows = []
    for item in watchlist_digest.get("top_medium") or []:
        medium_rows.append(f"{item['name']}｜{item.get('institutional_note', '法人明細缺失')}")

    long_rows = []
    for item in watchlist_digest.get("top_long") or []:
        long_rows.append(f"{item['name']}｜{item.get('institutional_note', '法人明細缺失')}")

    cross_rows = []
    for item in watchlist_digest.get("top_cross") or []:
        summary_text = item.get("summary") or ""
        note = item.get("institutional_note", "法人明細缺失")
        price_text = _format_trade_text(item.get("price"), "元") if item.get("price") is not None else "未提供"
        stop_text = _format_trade_text(item.get("stop_loss"), "元") if item.get("stop_loss") is not None else "跨池觀察未生成交易停損"
        parts = [item["name"], f"現價 {price_text}", f"停損 {stop_text}"]
        if summary_text:
            parts.append(summary_text)
        parts.append(note)
        cross_rows.append("｜".join(parts))

    intraday_rows = []
    for item in watchlist_digest.get("top_intraday") or []:
        strict_text = "嚴格條件通過" if item.get("strict_passed") else "候選"
        blockers = item.get("strict_hard_fails") or []
        blocker_text = f"｜阻塞：{','.join(blockers)}" if blockers and not item.get("strict_passed") else ""
        intraday_rows.append(f"{item['name']}｜{strict_text}{blocker_text}｜{item.get('institutional_note', '法人明細缺失')}")

    strategy_rows = []
    for item in strategy_signals[:5]:
        strategy_rows.append({
            "name": item.get("name") or item.get("symbol") or "",
            "symbol": item.get("symbol") or "",
            "strategy": item.get("strategy") or "",
            "strength": item.get("strength") or "",
            "price": round(float(item.get("price") or 0), 2),
            "stop_loss": round(float(item.get("stop_loss") or 0), 2),
            "detail": item.get("detail") or "",
            "tags": item.get("tags") or [],
        })

    stock_focus = build_stock_analysis_focus(watchlist_digest, strategy_signals)
    medium_state = load_medium_term_state()
    long_state = load_long_term_state()
    tracking_buckets = build_tracking_buckets(
        load_watchlist_summary(),
        watchlist_digest,
        stock_focus,
        medium_state,
        long_state,
    )

    return {
        "date": datetime.now().strftime("%Y/%m/%d"),
        "weekday": datetime.now().strftime("%A"),
        "user_profile": (profile or "").strip()[:500],
        "stock_analysis_focus": stock_focus,
        "tracking_buckets": tracking_buckets,
        "market": {
            "tw_index": grouped["tw_index"],
            "tw_key": grouped["tw_key"],
            "us_key": grouped["us_key"],
            "institutional": stock_data.get("institutional") or "（今日無三大法人資料）",
            "institutional_leaders": stock_data.get("leaders") or {},
            "stock_institutional": stock_data.get("stock_institutional") or {},
            "alerts": alerts,
        },
        "watchlist": {
            "summary": {
                "short_term_count": summary.get("short_term_count", 0),
                "medium_term_count": summary.get("medium_term_count", 0),
                "long_term_count": summary.get("long_term_count", 0),
                "intraday_count": summary.get("intraday_count", 0),
                "cross_signals_count": summary.get("cross_signals_count", 0),
            },
            "short_focus_items": short_focus_items,
            "short_focus": short_rows,
            "medium_focus": medium_rows,
            "long_focus": long_rows,
            "cross_focus": cross_rows,
            "intraday_focus": intraday_rows,
            "tracking_buckets": tracking_buckets,
        },
        "strategy_signals": strategy_rows,
        "news": summarize_news_items(news_text, limit=5),
    }


def build_shortline_commentary_from_focus(short_focus_rows: List[str]) -> str:
    shortline_items: List[Dict[str, Any]] = []
    for row in short_focus_rows or []:
        text = str(row or "").strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split("｜") if part.strip()]
        if not parts:
            continue
        shortline_items.append({
            "name": parts[0],
            "engine_label": parts[1] if len(parts) > 1 else "策略觀察",
            "score_label": parts[2] if len(parts) > 2 else "優先級未知",
            "price_text": parts[3].replace("現價 ", "", 1) if len(parts) > 3 else "未提供",
            "entry_text": parts[4].replace("進場 ", "", 1) if len(parts) > 4 else "未提供",
            "stop_text": parts[5].replace("停損 ", "", 1) if len(parts) > 5 else "未提供",
            "target_text": parts[6].replace("目標 ", "", 1) if len(parts) > 6 else "未提供",
            "technical_note": parts[7].replace("技術 ", "", 1) if len(parts) > 7 else "技術欄位未提供",
            "institutional_note": parts[8].replace("法人 ", "", 1) if len(parts) > 8 else "法人欄位未提供",
            "reason_text": "",
        })
    return build_shortline_commentary(shortline_items)


def _safe_json_loads(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        compact = re.sub(r"[\r\n]+", " ", cleaned)
        try:
            return json.loads(compact)
        except json.JSONDecodeError:
            logger.warning("JSON parse failed, raw preview: %s", cleaned[:500])
            raise e


def _fallback_analysis(payload: Dict[str, Any]) -> Dict[str, Any]:
    tw_index = payload["market"].get("tw_index") or []
    tw_key = payload["market"].get("tw_key") or []
    us_key = payload["market"].get("us_key") or []
    watch_summary = payload["watchlist"]["summary"]
    alerts = payload["market"].get("alerts") or []

    news = payload.get("news") or []
    twii = tw_index[0] if tw_index else None
    tsm = next((item for item in tw_key if "2330" in item["name"]), None)
    lead = "依強勢股篩選、台股籌碼確認與進退條件執行，短線只做最強與最清楚的標的。"
    if twii and twii.get("change_pct", 0) <= -1.5:
        lead = "指數回檔時維持強勢股優先，搭配法人籌碼確認與嚴格停損，不在弱勢股抄底。"
    if tsm and tsm.get("change_pct", 0) <= -2:
        lead = "權值股壓盤時，主軸改看強勢突破、跨池訊號與低風險回踩，不追隨大盤雜訊。"

    market_points = []
    if twii:
        market_points.append(f"台股加權 {twii['change_text']}，先看指數是否止跌。")
    if tsm:
        market_points.append(f"台積電 {tsm['change_text']}，權值仍是風向球。")
    inst_text = payload["market"].get("institutional") or ""
    if "外資:" in inst_text or "投信:" in inst_text:
        market_points.append("籌碼判讀以外資、投信與自營商方向確認，不只看價格紅黑。")
    if us_key:
        market_points.append(f"美股關鍵標的以 {us_key[0]['name']} 為代表，隔夜方向續看。")
    if watch_summary.get("cross_signals_count", 0) > 0:
        market_points.append(f"跨池訊號仍有 {watch_summary['cross_signals_count']} 檔，代表至少部分標的仍存在跨框架或短期時點共振。")

    short_focus = payload["watchlist"].get("short_focus") or []
    short_names = extract_allowed_stock_names(short_focus)
    if short_focus:
        primary = "、".join(short_names[: min(len(short_names), 5)])
        shortline_take = f"短線解讀只對齊實際焦點，先看 {primary}，依原列進場區、停損與技術訊號執行。"
    else:
        shortline_take = "今日短線池無明確焦點，維持紀律觀察。"

    cross_focus = payload["watchlist"].get("cross_focus") or []
    intraday_focus = payload["watchlist"].get("intraday_focus") or []
    cross_names = extract_allowed_stock_names(cross_focus)
    intraday_names = extract_allowed_stock_names(intraday_focus)
    if cross_focus and intraday_focus:
        cross_take = f"跨池先看 {'、'.join(cross_names[:3])}；日內先看 {'、'.join(intraday_names[:3])}。"
    elif cross_focus:
        cross_take = f"跨池訊號只對齊實際名單，先看 {'、'.join(cross_names[:3])}；若含日內池，僅視為輔助時點訊號。"
    elif intraday_focus:
        cross_take = f"日內候選只對齊實際名單，先看 {'、'.join(intraday_names[:3])}。"
    else:
        cross_take = "今日無跨池或日內焦點。"

    risk_points = []
    if alerts:
        for item in alerts[:3]:
            risk_points.append(f"{item['name']}：{item['reason']}")
    if news:
        risk_points.append(f"新聞面：{news[0][:40]}")
    if "無三大法人資料" in payload["market"]["institutional"]:
        risk_points.append("法人資料缺失，B 籌碼確認不足，降槓桿處理。")

    actions = [
        "用 A 強勢股標準先過濾弱股。",
        "用 B 法人方向確認是否續抱或進場。",
        "用 C 進退條件執行停損與觀望。",
    ]

    return {
        "一句話判斷": lead,
        "市場看點": market_points[:5],
        "短線選股日報": shortline_take,
        "跨池與日內焦點": cross_take,
        "異常與風險": risk_points[:5] or ["暫無明顯異常，但仍需留意高檔震盪。"],
        "明日行動清單": actions,
    }


def generate_report(payload: Dict[str, Any], profile: str) -> Dict[str, Any]:
    system_prompt = """你是台股專業分析師，採固定混合框架輸出：
A = Minervini / O'Neil 式強勢股標準（強者恆強、突破與收盤強度優先）
B = 台股短線籌碼突破判讀（外資 / 投信 / 自營商方向作確認）
C = 趨勢追蹤 + 回踩確認表達格式（每檔都要有買進 / 觀望 / 賣出或避開條件）
全程繁體中文。
你可以參考 `stock_analysis_focus` 做事實校準，但最終只能輸出 JSON 物件，不要輸出 Markdown，不要輸出任何額外說明。
JSON 必須包含以下鍵：
- 一句話判斷: string
- 市場看點: array of 3-5 strings
- 短線選股日報: string
- 跨池與日內焦點: string
- 異常與風險: array of 2-5 strings
- 明日行動清單: array of exactly 3 strings
要求：
1. 只能基於輸入資料，不可腦補未提供的法人數字或價格。
2. 語氣要像交易台簡報，短、準、可執行。
3. 若法人資料缺失，要明確點出「資料缺失」，不要裝作有資料。
4. 重點優先順序：短線焦點、跨池訊號、異常警示、明日動作。
5. 保留今日簡表，不要重寫簡表內容。
6. 選股判準以 A 為主：優先解讀突破、量比、收盤位置、回踩均線。
7. 籌碼判讀以 B 為主：明確引用外資、投信、自營商或總法人數字做確認。
8. 表達格式以 C 為主：買進要有進場依據與停損；觀望要有等待條件；賣出/避開要有明確退出條件。
9. 籌碼面與技術訊號解讀必須引用具體數字，例如買超金額、現價、停損、突破幅度、量比、收盤位置。
10. 每個條列不超過 40 個字。
11. 若 `watchlist.short_focus` 已提供進池理由、技術訊號、停損或法人明細，`短線選股日報` 必須優先引用這些已提供內容，不可改寫成「法人資料暫缺」或「進池理由缺失」。
12. 只有在輸入資料本身真的沒有該欄位時，才可說資料缺失。
13. 短線選股日報必須對齊實際顯示的 `watchlist.short_focus`，不可引用未列出的股票。
14. 若 `watchlist.summary.cross_signals_count` 為 0，不得提及任何跨池訊號股票。
15. 若 `watchlist.summary.intraday_count` 為 0，不得提及任何日內候選股票。
16. 若 `watchlist.cross_focus` 與 `watchlist.intraday_focus` 都為空，只能輸出「今日無跨池或日內焦點」等空狀態描述。
17. 若提供 `stock_analysis_focus`，必須先依其中的池位事實與邊界做判斷，不得把法人強、訊號確認或題材補充越級寫成池位成立。
18. `stock_analysis_focus` 只是摘要防呆邊界，不是最終輸出欄位；禁止把單檔分析逐檔展開成全文。
19. 回覆第一個字必須是 `{`，最後一個字必須是 `}`。"""

    user_prompt = json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        helper = ClaudeHelper(max_retries=0)
        raw = helper.chat(prompt=user_prompt, system=system_prompt, max_tokens=1500)
        logger.info("台股日報 Claude 呼叫完成")
        analysis = _safe_json_loads(raw)
        for key in SECTION_ORDER:
            if key not in analysis:
                raise ValueError(f"missing key: {key}")
        return analysis
    except Exception as e:
        logger.error("AI 報告生成失敗，改用程式化備援: %s", e)
        return _fallback_analysis(payload)


def _to_paragraph(value: Any, fallback: str) -> str:
    text = (value or "").strip() if isinstance(value, str) else ""
    return text or fallback


def _join_points(items: List[str], fallback: str, limit: int = 3) -> str:
    cleaned = [str(item).strip() for item in (items or []) if str(item).strip()]
    return "；".join(cleaned[:limit]) if cleaned else fallback


def _trim_text(text: str, limit: int, fallback: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return fallback
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip("；，,、 ") + "…"


def _format_trade_text(value: Any, unit: str = "") -> str:
    if value is None or value == "":
        return "未提供"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    suffix = unit if unit else ""
    return f"{num:.1f}{suffix}"


def _format_entry_zone_text(entry_zone: Any) -> str:
    if isinstance(entry_zone, list) and len(entry_zone) >= 2:
        try:
            return f"{float(entry_zone[0]):.1f}-{float(entry_zone[1]):.1f}"
        except (TypeError, ValueError):
            return "-".join(str(x) for x in entry_zone[:2])
    return "未提供"


def _build_data_notes(payload: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    market = payload.get("market") or {}
    watchlist = payload.get("watchlist") or {}
    if "無三大法人資料" in (market.get("institutional") or ""):
        notes.append("三大法人總覽缺失，市場籌碼判讀需保守。")
    if not (market.get("tw_index") or market.get("tw_key") or market.get("us_key")):
        notes.append("今日無完整行情資料，可能為非交易日或來源未更新。")
    if not (watchlist.get("short_focus") or []):
        notes.append("今日短線池無可用焦點標的。")
    return notes


def build_formal_report_payload(payload: Dict[str, Any], analysis: Dict[str, Any], watchlist_digest: Dict[str, Any], strategy_signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    market = payload.get("market") or {}
    watchlist = payload.get("watchlist") or {}
    summary = watchlist.get("summary") or {}
    now = datetime.now()

    snapshot_rows = []
    for row in (market.get("tw_index") or []) + (market.get("tw_key") or []) + (market.get("us_key") or []):
        snapshot_rows.append({
            "name": row.get("name") or row.get("symbol") or "—",
            "symbol": row.get("symbol") or "",
            "price_text": row.get("price_text") or "—",
            "change_text": row.get("change_text") or "—",
            "change_class": "is-up" if "▲" in (row.get("change_text") or "") else "is-down" if "▼" in (row.get("change_text") or "") else "",
            "bucket_label": "台股指數" if row.get("symbol") in {"^TWII", "TWII"} else "美股關鍵" if row.get("symbol") in {"^DJI", "^IXIC", "NVDA", "TSM"} else "重點觀察",
        })

    investor_labels = {
        "foreign": "外資",
        "investment": "投信",
        "dealer": "自營商",
    }
    institutional_leaders = []
    leaders = market.get("institutional_leaders") or {}
    for key in ["foreign", "investment", "dealer"]:
        group = leaders.get(key) or {}
        buy_top = group.get("buy_top") or []
        sell_top = group.get("sell_top") or []
        buy_text = "、".join(
            f"{format_name_with_code(item.get('name') or item.get('symbol') or '', item.get('symbol') or '')}{format_lots(item.get('today_lots'))}"
            for item in buy_top[:5]
        ) or "未提供"
        sell_text = "、".join(
            f"{format_name_with_code(item.get('name') or item.get('symbol') or '', item.get('symbol') or '')}{format_lots(item.get('today_lots'))}"
            for item in sell_top[:5]
        ) or "未提供"
        institutional_leaders.append({
            "title": investor_labels[key],
            "buy_text": buy_text,
            "sell_text": sell_text,
        })

    shortline_focus = list(watchlist.get("short_focus_items") or [])

    cross_pool_focus = []
    for item in watchlist_digest.get("top_cross") or []:
        price_text = _format_trade_text(item.get("price"), "元") if item.get("price") is not None else "未提供"
        stop_text = _format_trade_text(item.get("stop_loss"), "元") if item.get("stop_loss") is not None else "跨池觀察未生成交易停損"
        cross_pool_focus.append({
            "name": item.get("name") or item.get("symbol") or "—",
            "price_text": price_text,
            "stop_text": stop_text,
            "summary": item.get("summary") or "未提供",
            "institutional_note": item.get("institutional_note") or "法人欄位未提供",
        })

    intraday_focus = []
    for item in watchlist_digest.get("top_intraday") or []:
        blockers = item.get("strict_hard_fails") or []
        intraday_focus.append({
            "name": item.get("name") or item.get("symbol") or "—",
            "strict_text": "嚴格條件通過" if item.get("strict_passed") else "候選",
            "blockers_text": "、".join(blockers) if blockers else "—",
            "institutional_note": item.get("institutional_note") or "法人欄位未提供",
        })

    risk_alerts = []
    for alert in market.get("alerts") or []:
        risk_alerts.append({
            "name": alert.get("name") or "風險項目",
            "price": alert.get("price") or "—",
            "reason": alert.get("reason") or "未提供",
        })
    if not risk_alerts:
        for item in analysis.get("異常與風險") or []:
            risk_alerts.append({"name": "風險提示", "price": "—", "reason": item})

    market_points = analysis.get("市場看點") or []
    risk_points = analysis.get("異常與風險") or []
    action_items = analysis.get("明日行動清單") or []
    tracking_buckets = payload.get("tracking_buckets") or watchlist.get("tracking_buckets") or {}
    tracking_sections = []
    for key, title in TRACKING_BUCKET_TITLES:
        rows = list(tracking_buckets.get(key) or [])
        tracking_sections.append({
            "title": title,
            "items": rows or [f"暫無{title}名單。"],
        })

    cover_kpis = [
        {"label": "短線", "value": summary.get("short_term_count", 0), "tone": "primary"},
        {"label": "中線", "value": summary.get("medium_term_count", 0), "tone": "neutral"},
        {"label": "長線", "value": summary.get("long_term_count", 0), "tone": "neutral"},
        {"label": "日內", "value": summary.get("intraday_count", 0), "tone": "accent"},
        {"label": "跨池", "value": summary.get("cross_signals_count", 0), "tone": "accent"},
    ]
    cover_market_snapshot = snapshot_rows[:4]
    cover_action_items = action_items[:3]
    shortline_commentary = _to_paragraph(analysis.get("短線選股日報"), "短線解讀暫缺")
    shortline_commentary_parts = [part.strip() for part in re.split(r"\n+|[。]\s*", shortline_commentary) if part.strip()]

    return {
        "meta": {
            "title": "台股每日報告",
            "subtitle": "券商式盤後摘要 / 可歸檔版",
            "report_date": now.strftime("%Y-%m-%d"),
            "generated_at": now.strftime("%Y-%m-%d %H:%M"),
            "version": "v2",
            "source_note": "資料來源：盤後行情、法人籌碼快照、觀察池、策略訊號與 AI 交易台簡報分析。",
        },
        "headline": {
            "judgment": analysis.get("一句話判斷") or "今日無判讀資料",
            "stance": "短線優先看強勢、籌碼與進退條件，不在雜訊區硬做。",
        },
        "executive_summary": {
            "market_summary": _trim_text(_join_points(market_points, "市場摘要暫缺", limit=2), 70, "市場摘要暫缺"),
            "shortline_summary": build_shortline_summary(shortline_focus, limit=2),
            "risk_summary": _trim_text(_join_points(risk_points, "風險摘要暫缺", limit=2), 70, "風險摘要暫缺"),
            "next_action_summary": _trim_text(_join_points(action_items, "明日行動摘要暫缺", limit=2), 72, "明日行動摘要暫缺"),
        },
        "cover": {
            "kpis": cover_kpis,
            "market_snapshot": cover_market_snapshot,
            "action_items": cover_action_items,
        },
        "market_overview": {
            "snapshot_rows": snapshot_rows,
            "institutional_summary": market.get("institutional") or "無三大法人資料",
            "institutional_leaders": institutional_leaders,
        },
        "watchlist_overview": {
            "short_term_count": summary.get("short_term_count", 0),
            "medium_term_count": summary.get("medium_term_count", 0),
            "long_term_count": summary.get("long_term_count", 0),
            "intraday_count": summary.get("intraday_count", 0),
            "cross_signals_count": summary.get("cross_signals_count", 0),
        },
        "shortline_focus": shortline_focus,
        "cross_pool_focus": cross_pool_focus,
        "intraday_focus": intraday_focus,
        "risk_alerts": risk_alerts,
        "action_items": action_items,
        "tracking_sections": tracking_sections,
        "appendix": {
            "medium_term_focus": watchlist_digest.get("top_medium") or [],
            "long_term_focus": watchlist_digest.get("top_long") or [],
            "data_notes": _build_data_notes(payload),
            "method_note": "本報告以強勢股、法人方向與進退條件為主軸整理，不以單一消息面決策。",
        },
        "narrative": {
            "market_commentary": _join_points(market_points, "市場判讀暫缺", limit=5),
            "shortline_commentary": shortline_commentary,
            "shortline_commentary_parts": shortline_commentary_parts,
            "cross_intraday_commentary": _to_paragraph(analysis.get("跨池與日內焦點"), "跨池與日內焦點暫缺"),
            "risk_commentary": _join_points(risk_points, "風險說明暫缺", limit=5),
        },
        "disclaimer": "本報告僅供資訊整理與研究參考，不構成任何投資建議。",
    }


def render_briefing_markdown(payload: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    market = payload["market"]
    watchlist = payload["watchlist"]
    summary = watchlist["summary"]

    def render_quote_rows(rows: List[Dict[str, Any]]) -> List[str]:
        return [f"- {item['name']}：{item['price_text']} {item['change_text']}" for item in rows]

    lines: List[str] = []
    lines.append("## 一、今日一句話判斷")
    lines.append(analysis["一句話判斷"])
    lines.append("")

    lines.append("## 二、市場看點")
    for row in render_quote_rows(market.get("tw_index") or []):
        lines.append(row)
    for row in render_quote_rows(market.get("tw_key") or []):
        lines.append(row)
    for row in render_quote_rows(market.get("us_key") or []):
        lines.append(row)
    inst_text = (market.get("institutional") or "").strip()
    for row in render_institutional_rows(inst_text):
        lines.append(row)
    for row in format_leader_rows(market.get("institutional_leaders") or {}):
        lines.append(row)
    lines.append("")
    for item in analysis["市場看點"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 三、短線選股日報")
    lines.append("### 觀察池總覽")
    lines.append(
        f"- 今日觀察池：短線 {summary['short_term_count']} 檔｜中線 {summary['medium_term_count']} 檔｜長線 {summary['long_term_count']} 檔｜日內前置信號 {summary['intraday_count']} 檔｜跨池訊號 {summary['cross_signals_count']} 檔"
    )
    lines.append("")

    lines.append("### 短線焦點")
    short_focus_items = watchlist.get("short_focus_items") or []
    if short_focus_items:
        for item in short_focus_items:
            lines.append(f"- {item['markdown_row']}")
    else:
        lines.append("- 今日無短線選股資料")
    lines.append("")

    lines.append("### 中長線補充")
    medium_focus = watchlist.get("medium_focus") or []
    long_focus = watchlist.get("long_focus") or []
    if medium_focus:
        for item in medium_focus:
            lines.append(f"- 中線｜{item}")
    if long_focus:
        for item in long_focus:
            lines.append(f"- 長線｜{item}")
    if not medium_focus and not long_focus:
        lines.append("- 今日無中長線補充。")
    lines.append("")

    lines.append("### 解讀")
    short_take = (analysis.get("短線選股日報") or "").strip().replace("今日簡表：", "").strip()
    if short_take:
        paragraphs = [p.strip() for p in re.split(r"\n+", short_take) if p.strip()]
        for para in paragraphs:
            lines.append(f"- {para}")
    else:
        lines.append("- 今日短線解讀資料不足，維持紀律觀察。")
    lines.append("")

    lines.append("## 四、跨池與日內焦點")
    cross_focus = watchlist.get("cross_focus") or []
    intraday_focus = watchlist.get("intraday_focus") or []
    if cross_focus:
        lines.append("### 跨池訊號")
        for item in cross_focus:
            lines.append(f"- {item}")
    if intraday_focus:
        lines.append("### 日內候選")
        for item in intraday_focus:
            lines.append(f"- {item}")
    cross_take = (analysis.get("跨池與日內焦點") or "").strip() or "今日無跨池或日內焦點。"
    if not cross_focus and not intraday_focus:
        lines.append("- 今日無跨池或日內焦點。")
        lines.append("- 解讀：今日無跨池或日內焦點。")
    else:
        lines.append(f"- 解讀：{cross_take}")
    lines.append("")

    lines.append("## 五、異常與風險")
    alerts = market.get("alerts") or []
    if alerts:
        for item in alerts:
            lines.append(f"- {item['name']} {item['price']}（{item['reason']}）")
    else:
        lines.append("- （今日無異常警示）")
    for item in analysis["異常與風險"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 六、觀察池與跨池訊號")
    lines.append(render_watchlist_pool_digest_from_payload(watchlist))
    lines.append("")

    lines.append("## 七、追蹤動作清單")
    tracking_buckets = payload.get("tracking_buckets") or watchlist.get("tracking_buckets") or {}
    for key, title in TRACKING_BUCKET_TITLES:
        lines.append(f"### {title}")
        items = tracking_buckets.get(key) or []
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append(f"- 暫無{title}名單。")
        lines.append("")

    lines.append("## 八、明日行動清單")
    for idx, item in enumerate(analysis["明日行動清單"], start=1):
        lines.append(f"{idx}. {item}")

    return "\n".join(lines).strip() + "\n"


def save_to_desktop(report: str, formal_payload: Dict[str, Any]) -> tuple[Path, Path]:
    """儲存 Markdown 報告到桌面，並同時生成正式 PDF"""
    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    date_str = datetime.now().strftime('%Y-%m-%d')
    base_name = f"台股每日報告_{timestamp}"

    md_filename = f"{base_name}.md"
    desktop_md = Path.home() / "Desktop" / md_filename
    header = f"---\ntitle: 台股每日報告\ndate: {date_str}\nsummary: 由 AI 助理團隊自動生成\n---\n\n"
    desktop_md.write_text(header + report, encoding="utf-8")
    logger.info("✅ Markdown 報告已存檔至：%s", desktop_md)

    pdf_path = generate_stock_report_pdf(
        base_path=desktop_md.with_suffix(""),
        report_payload=formal_payload,
        md_content=report,
        date_str=date_str,
    )
    if not pdf_path:
        raise RuntimeError("PDF 生成失敗，報告流程中止")

    logger.info("✅ PDF 報告已生成：%s", pdf_path)
    return desktop_md, pdf_path


def extract_section(report: str, title: str) -> List[str]:
    lines = report.splitlines()
    collected: List[str] = []
    target = f"## {title}"
    in_section = False
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            if line == target:
                in_section = True
                collected.append(line)
                continue
        elif in_section:
            collected.append(line)
    return [line for line in collected if line.strip()]


def is_institutional_leader_line(line: str) -> bool:
    markers = ["三大法人：", "外資買超前五", "外資賣超前五", "投信買超前五", "投信賣超前五", "自營商買超前五", "自營商賣超前五"]
    return any(marker in line for marker in markers)


def pick_market_section_lines(lines: List[str], max_lines: int) -> List[str]:
    if not lines:
        return []

    header = lines[0]
    body = [line for line in lines[1:] if line.strip()]
    leaders = [line for line in body if is_institutional_leader_line(line)]
    non_leaders = [line for line in body if not is_institutional_leader_line(line)]

    # 先保留市場主要價格與一句摘要，再強制帶入法人榜
    keep_non_leaders = min(max_lines - 1, 6)
    picked = [header] + non_leaders[:keep_non_leaders]

    for line in leaders:
        if len(picked) >= max_lines:
            break
        picked.append(line)

    # 若法人榜仍未帶齊，優先以法人榜替換尾端非法人行
    for line in leaders:
        if line in picked:
            continue
        replace_idx = None
        for idx in range(len(picked) - 1, 0, -1):
            if not is_institutional_leader_line(picked[idx]):
                replace_idx = idx
                break
        if replace_idx is not None:
            picked[replace_idx] = line

    # 去重並保序
    deduped: List[str] = []
    seen = set()
    for line in picked:
        if line in seen:
            continue
        deduped.append(line)
        seen.add(line)

    return deduped[:max_lines]


def build_im_preview(report: str, section_line_limit: int, market_line_limit: int) -> str:
    picked: List[str] = []
    ordered_sections = [
        "一、今日一句話判斷",
        "二、市場看點",
        "三、短線選股日報",
        "四、跨池與日內焦點",
        "六、觀察池與跨池訊號",
        "七、追蹤動作清單",
        "八、明日行動清單",
    ]
    for title in ordered_sections:
        section_lines = extract_section(report, title)
        if title == "二、市場看點":
            section_lines = pick_market_section_lines(section_lines, market_line_limit)
        elif title in {"六、觀察池與跨池訊號", "七、追蹤動作清單", "八、明日行動清單"}:
            section_lines = section_lines[: max(4, min(section_line_limit, 10))]
        else:
            section_lines = section_lines[:section_line_limit]
        picked.extend(section_lines)
        picked.append("")
    return "\n".join(line for line in picked if line is not None).strip()


def build_feishu_summary(report: str, desktop_path: Path):
    preview = build_im_preview(report, section_line_limit=8, market_line_limit=18)

    footer = (
        "\n\n━━━━━━━━━━━━━━━\n"
        "📋 完整報告已自動存至桌面\n"
        f"📄 {desktop_path.name}\n"
        "📎 PDF 版本同步生成\n"
        "━━━━━━━━━━━━━━━\n"
        "📌 原則：穩健・理性・不薦股\n"
        "投資有風險，決策請自行判斷。"
    )
    title = f"📊 台股每日報告 · {datetime.now().strftime('%Y/%m/%d')}"
    return title, (preview + footer)[:4000]


def build_wechat_summary(report: str) -> str:
    preview = build_im_preview(report, section_line_limit=7, market_line_limit=14)
    return (
        f"📊 台股每日報告 · {datetime.now().strftime('%Y/%m/%d')}\n"
        f"{'─'*20}\n"
        f"{preview[:900]}\n"
        f"{'─'*20}\n"
        "📋 完整報告已存至桌面\n"
        "📎 PDF 版本同步生成"
    )



def send_feishu_summary(report: str, desktop_path: Path, pdf_path: Optional[Path] = None) -> Dict[str, bool]:
    result = {"text_ok": False, "pdf_ok": False}
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier("config/lark_config.yaml")
        title, message = build_feishu_summary(report, desktop_path)
        sent = notifier.send_message(CHAT_ID, title, message)
        result["text_ok"] = bool(sent)
        if sent:
            logger.info("✅ 飛書摘要已發送")
        else:
            logger.warning("⚠️ 飛書摘要發送失敗")

        if pdf_path and pdf_path.exists():
            file_key = notifier.upload_file(str(pdf_path))
            if file_key:
                file_ok = notifier.send_file(CHAT_ID, file_key)
                result["pdf_ok"] = bool(file_ok)
                if file_ok:
                    logger.info("✅ 飛書 PDF 報告已發送")
                else:
                    logger.warning("⚠️ 飛書 PDF 檔案訊息發送失敗")
            else:
                logger.warning("⚠️ 飛書 PDF 上傳失敗")
        else:
            logger.warning("⚠️ 飛書 PDF 不存在，無法發送附件")
            result["pdf_ok"] = False

        return result
    except Exception as e:
        logger.error("飛書推播失敗: %s", e)
        return result


def send_wechat_summary(report: str, pdf_path: Optional[Path] = None):
    try:
        from utils.wechat_notifier import WechatNotifier
        wx = WechatNotifier()
        summary = build_wechat_summary(report)
        text_ok = wx.send_message(summary, typing_seconds=15)
        text_error = "" if text_ok else (wx.last_error or "")
        if text_ok:
            logger.info("✅ 微信摘要已發送")
        else:
            logger.warning("⚠️ 微信摘要發送失敗")

        if pdf_path and pdf_path.exists():
            logger.info("ℹ️ 微信通道僅發文字，略過 PDF：%s", pdf_path)

        return {
            "text_ok": bool(text_ok),
            "pdf_ok": False,
            "text_error": text_error,
            "pdf_error": "",
            "pdf_path": str(pdf_path) if pdf_path else "",
        }
    except Exception as e:
        logger.error("微信推播失敗: %s", e)
        return {
            "text_ok": False,
            "pdf_ok": False,
            "text_error": str(e),
            "pdf_error": "",
            "pdf_path": str(pdf_path) if pdf_path else "",
        }


def send_wechat_failure_notice(error_message: str, pdf_path: str = "", text_sent: bool = False) -> bool:
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier("config/lark_config.yaml")
        detail = error_message or "unknown error"
        text_status = "成功" if text_sent else "失敗"
        path_line = f"\nPDF 路徑：{pdf_path}" if pdf_path else ""
        return notifier.send_message(
            CHAT_ID,
            "⚠️ 微信摘要發送失敗",
            "台股每日報告已成功發送到飛書，但微信通道未完成文字送達。\n"
            f"微信文字狀態：{text_status}\n"
            f"原因：{detail}"
            f"{path_line}",
        )
    except Exception as e:
        logger.error("微信失敗補發飛書通知失敗: %s", e)
        return False


def load_profile() -> str:
    profile_path = proj_root() / "profile.md"
    if profile_path.exists():
        return profile_path.read_text(encoding="utf-8")
    return ""


def archive_report(report: str) -> Path:
    arch_dir = proj_root() / "logs" / "stock_reports"
    os.makedirs(arch_dir, exist_ok=True)
    filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    path = arch_dir / filename
    path.write_text(report, encoding="utf-8")
    logger.info("📦 Markdown 報告已歸檔：%s", path)
    return path


def archive_pdf(pdf_path: Optional[Path]) -> Optional[Path]:
    """將 PDF 複製到歸檔目錄"""
    if not pdf_path or not pdf_path.exists():
        return None
    arch_dir = proj_root() / "logs" / "stock_reports"
    os.makedirs(arch_dir, exist_ok=True)
    dest = arch_dir / pdf_path.name
    dest.write_bytes(pdf_path.read_bytes())
    logger.info("📦 PDF 報告已歸檔：%s", dest)
    return dest


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--validate-tracking-buckets":
        result = validate_tracking_buckets()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    started = datetime.now()
    logger.info("=" * 50)
    logger.info("📊 每日台股報告生成器啟動")
    logger.info("=" * 50)

    logger.info("→ 讀取今日股市數據...")
    stock_data = parse_stock_log()
    snapshot_data = load_institutional_snapshot()
    if snapshot_data:
        stock_data["stock_institutional"] = snapshot_data.get("stock_institutional") or {}
        stock_data["leaders"] = snapshot_data.get("leaders") or {}

    if snapshot_data.get("institutional"):
        stock_data["institutional"] = snapshot_data["institutional"]
        logger.info("→ 法人資料來源：snapshot (%s)", snapshot_data.get("snapshot_meta", {}).get("path", ""))
    elif stock_data.get("institutional"):
        logger.info("→ 法人資料來源：stock_monitor.log 備援")
    else:
        logger.warning("→ 法人資料缺失：快照與日誌備援均無資料")
    if not stock_data.get("quotes"):
        logger.warning("今日無股價資料，仍繼續生成（可能為非交易日）")

    logger.info("→ 讀取今日新聞...")
    news_text = read_today_news()
    if not news_text:
        logger.warning("今日無新聞資料")

    logger.info("→ 讀取今日策略訊號...")
    strategy_signals = load_strategy_signals()

    logger.info("→ 讀取今日觀察池...")
    watchlist_source = load_watchlist_summary()
    stock_data = apply_quote_overrides(stock_data, watchlist_source)
    watchlist_summary = enrich_watchlist_with_quotes(watchlist_source, stock_data)
    watchlist_digest = build_watchlist_digest(
        watchlist_summary,
        stock_data.get("stock_institutional") or {},
        strategy_signals,
    )

    logger.info("→ 載入 W 的個人背景資料...")
    profile = load_profile()
    master_control = get_current_control()
    master_mode = master_control is not None

    logger.info("→ 建立簡報資料包...")
    payload = build_briefing_payload(stock_data, news_text, watchlist_digest, strategy_signals, profile)

    logger.info("→ AI 生成精華分析中（可能需 10-30 秒）...")
    analysis = sanitize_analysis_by_truth(payload, generate_report(payload, profile))
    formal_payload = build_formal_report_payload(payload, analysis, watchlist_digest, strategy_signals)
    report = render_briefing_markdown(payload, analysis)
    logger.info("報告長度：%s 字", len(report))

    logger.info("→ 儲存報告到桌面...")
    try:
        desktop_path, pdf_path = save_to_desktop(report, formal_payload)
    except Exception as e:
        logger.error("❌ 報告輸出失敗：%s", e)
        return 1
    archive_path = archive_report(report)
    archive_pdf(pdf_path)

    # 組裝 payload meta
    meta = {
        "desktop_file": str(desktop_path),
        "archive_file": str(archive_path),
        "pdf_file": str(pdf_path),
    }

    if master_mode:
        feishu_title, feishu_message = build_feishu_summary(report, desktop_path)
        wechat_summary = build_wechat_summary(report)
        payload_path = write_payload(
            {
                "agent": "daily_stock_report",
                "meta": meta,
                "artifacts": {
                    "pdf_file": str(pdf_path),
                },
                "notification": {
                    "feishu": {
                        "title": feishu_title,
                        "content": feishu_message,
                        "attach_pdf": True,
                    },
                    "wechat": {
                        "text": wechat_summary,
                        "typing_seconds": 15,
                        "attach_pdf": False,
                    },
                },
            },
            control=master_control,
        )
        logger.info("Master agent 接管通知：已輸出 payload=%s，略過子 agent 直接推播", payload_path)
        feishu_ok = True
        wx_ok = True
    else:
        logger.info("→ 發送飛書摘要...")
        feishu_result = send_feishu_summary(report, desktop_path, pdf_path)
        feishu_ok = feishu_result.get("text_ok") and feishu_result.get("pdf_ok")

        logger.info("→ 發送微信摘要...")
        wx_result = send_wechat_summary(report, pdf_path)
        wx_ok = bool(wx_result.get("text_ok"))
        if not wx_ok:
            send_wechat_failure_notice(
                wx_result.get("text_error") or "unknown error",
                pdf_path=wx_result.get("pdf_path") or "",
                text_sent=bool(wx_result.get("text_ok")),
            )

    elapsed = (datetime.now() - started).total_seconds()
    logger.info("✅ 完成｜耗時 %.1fs｜桌面：%s", elapsed, desktop_path.name)

    if not feishu_ok and not wx_ok:
        logger.warning("⚠️ 所有推播均失敗，請檢查網路與 API 設定")

    return 0 if report and not report.startswith("⚠️ 報告生成失敗") else 1


if __name__ == "__main__":
    sys.exit(main())
