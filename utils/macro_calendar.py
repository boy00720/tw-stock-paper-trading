#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股宏觀日曆 / 消息面資料層。

本模組只負責讀取宏觀事件配置、Fed 官方 RSS 與 Tavily 宏觀查詢；
不接 IBKR / 盈透、不做下單、不直接推送。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
import yaml

logger = logging.getLogger(__name__)

AUTOMATION_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = AUTOMATION_DIR / "config" / "macro_radar.yaml"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
BLOCKED_NEWS_DOMAINS = {
    "facebook.com", "instagram.com", "youtube.com", "youtu.be", "tiktok.com",
    "linkedin.com", "reddit.com", "pinterest.com", "x.com", "twitter.com",
}
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
ET_TZ = ZoneInfo("America/New_York")
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class MacroNewsItem:
    title: str
    source: str
    url: str = ""
    summary: str = ""
    category: str = "macro"
    importance: str = "medium"
    published_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MacroWatchEvent:
    key: str
    label: str
    category: str
    importance: str
    usual_time_et: str = ""
    check_time_taipei_edt: str = ""
    check_time_taipei_est: str = ""
    keywords: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MacroSnapshot:
    mode: str
    title: str
    asof: str
    tavily_summary: str = ""
    tavily_sources: list[MacroNewsItem] = field(default_factory=list)
    fed_items: list[MacroNewsItem] = field(default_factory=list)
    watch_events: list[MacroWatchEvent] = field(default_factory=list)
    severity: str = "low"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tavily_sources"] = [item.to_dict() for item in self.tavily_sources]
        data["fed_items"] = [item.to_dict() for item in self.fed_items]
        data["watch_events"] = [item.to_dict() for item in self.watch_events]
        return data


def _now_local(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TAIPEI_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=TAIPEI_TZ)
    return now.astimezone(TAIPEI_TZ)


def _clean_text(value: Any, *, limit: int | None = None) -> str:
    text = " ".join(TAG_RE.sub("", str(value or "")).split())
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _source_from_url(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "unknown"


def _is_blocked_domain(url: str) -> bool:
    host = _source_from_url(url)
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_NEWS_DOMAINS)


def load_macro_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        logger.warning("macro config not found: %s", path)
        return {}


def flatten_watch_events(config: dict[str, Any]) -> list[MacroWatchEvent]:
    out: list[MacroWatchEvent] = []
    watchlist = config.get("event_watchlist") or {}
    if not isinstance(watchlist, dict):
        return out
    for importance, items in watchlist.items():
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "").strip()
            label = str(raw.get("label") or "").strip()
            if not key or not label:
                continue
            out.append(MacroWatchEvent(
                key=key,
                label=label,
                category=str(raw.get("category") or "macro"),
                importance=str(importance or raw.get("importance") or "medium"),
                usual_time_et=str(raw.get("usual_time_et") or ""),
                check_time_taipei_edt=str(raw.get("check_time_taipei_edt") or ""),
                check_time_taipei_est=str(raw.get("check_time_taipei_est") or ""),
                keywords=[str(k) for k in raw.get("keywords") or [] if str(k).strip()],
            ))
    return out


def _rss_datetime(value: str) -> str:
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TAIPEI_TZ).isoformat(timespec="minutes")
    except Exception:
        return value


def _iter_feed_configs(config: dict[str, Any]) -> list[dict[str, Any]]:
    feeds = ((config.get("rss_feeds") or {}).get("fed") or [])
    return [raw for raw in feeds if isinstance(raw, dict) and raw.get("url")]


def fetch_fed_rss_items(
    config: dict[str, Any] | None = None,
    *,
    limit: int = 8,
    timeout: int = 12,
    requester: Callable[..., Any] = requests.get,
) -> list[MacroNewsItem]:
    """讀 Fed 官方 RSS。任一 feed 失敗只略過，不阻斷整體。"""
    cfg = config or load_macro_config()
    items: list[MacroNewsItem] = []
    for feed in _iter_feed_configs(cfg):
        url = str(feed.get("url") or "")
        name = str(feed.get("name") or _source_from_url(url))
        category = str(feed.get("category") or "fed")
        try:
            resp = requester(url, timeout=timeout, headers=HEADERS)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:
            logger.warning("Fed RSS 讀取失敗 %s: %s", name, exc)
            continue
        for node in root.findall(".//item")[:limit]:
            title = _clean_text(node.findtext("title"), limit=140)
            if not title:
                continue
            link = _clean_text(node.findtext("link"))
            desc = _clean_text(node.findtext("description"), limit=240)
            published_at = _rss_datetime(node.findtext("pubDate") or "")
            importance = "high" if re.search(r"FOMC|monetary policy|rate", title, re.I) else "medium"
            items.append(MacroNewsItem(
                title=title,
                source=name,
                url=link,
                summary=desc,
                category=category,
                importance=importance,
                published_at=published_at,
            ))
    dedup: dict[tuple[str, str], MacroNewsItem] = {}
    for item in items:
        dedup.setdefault((item.title.lower(), item.url.lower()), item)
    return list(dedup.values())[:limit]


def fetch_tavily_macro_news(
    mode: str,
    config: dict[str, Any] | None = None,
    *,
    api_key: str | None = None,
    timeout: int = 18,
    requester: Callable[..., Any] = requests.post,
) -> tuple[str, list[MacroNewsItem], str | None]:
    """查 Tavily 宏觀新聞；回傳 (summary, sources, warning)。"""
    cfg = config or load_macro_config()
    modes = cfg.get("modes") or {}
    mode_cfg = modes.get(mode) or {}
    now_taipei = _now_local()
    now_et = now_taipei.astimezone(ET_TZ)
    raw_query = str(mode_cfg.get("query") or "US markets macro Fed inflation economic calendar").strip()
    query = raw_query.format(
        date_taipei=now_taipei.date().isoformat(),
        date_et=now_et.date().isoformat(),
    )
    key = api_key if api_key is not None else os.getenv("TAVILY_API_KEY")
    if not key:
        return "", [], "TAVILY_API_KEY 未設定"
    days = 1 if mode == "event_check" else 7
    preferred_domains = [str(d).strip() for d in cfg.get("preferred_news_domains") or [] if str(d).strip()]
    payload = {
        "api_key": key,
        "query": query,
        "search_depth": "basic",
        "topic": "news",
        "days": days,
        "max_results": 8,
        "include_answer": True,
        "exclude_domains": sorted(BLOCKED_NEWS_DOMAINS),
    }
    if preferred_domains:
        payload["include_domains"] = preferred_domains
    try:
        resp = requester(
            "https://api.tavily.com/search",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Tavily 宏觀查詢失敗: %s", exc)
        return "", [], f"Tavily 查詢失敗: {type(exc).__name__}"

    summary = _clean_text(data.get("answer"), limit=700)
    sources: list[MacroNewsItem] = []
    for raw in data.get("results") or []:
        if not isinstance(raw, dict):
            continue
        title = _clean_text(raw.get("title"), limit=160)
        if not title:
            continue
        url = str(raw.get("url") or "")
        if _is_blocked_domain(url):
            continue
        content = _clean_text(raw.get("content") or raw.get("raw_content") or "", limit=260)
        sources.append(MacroNewsItem(
            title=title,
            source=_source_from_url(url),
            url=url,
            summary=content,
            category="macro",
            importance="medium",
            published_at=str(raw.get("published_date") or ""),
        ))
        if len(sources) >= 6:
            break
    return summary, sources, None


def classify_severity(text: str, config: dict[str, Any] | None = None, *, mode: str = "") -> str:
    cfg = config or load_macro_config()
    lowered = text.lower()
    if mode == "event_check":
        no_event_markers = [
            "no major release", "no major economic release", "no major us macroeconomic data release",
            "no major macroeconomic data release", "no new releases or decisions", "no new release",
            "none scheduled", "no scheduled", "not scheduled", "沒有重大", "無重大",
        ]
        no_event_patterns = [
            r"no\s+major.*(?:release|decision)",
            r"no\s+new.*(?:release|decision)",
        ]
        if any(marker in lowered for marker in no_event_markers):
            return "low"
        if any(re.search(pattern, lowered) for pattern in no_event_patterns):
            return "low"
        trigger_markers = [
            "released", "actual", "forecast", "hotter than expected", "cooler than expected",
            "higher than expected", "lower than expected", "fed leaves", "fed cuts",
            "fed hikes", "fomc statement", "rate decision", "jobs report showed",
        ]
        if not any(marker in lowered for marker in trigger_markers):
            return "low"
    rules = cfg.get("severity_keywords") or {}
    for kw in rules.get("high") or []:
        if str(kw).lower() in lowered:
            return "high"
    for kw in rules.get("medium") or []:
        if str(kw).lower() in lowered:
            return "medium"
    return "low"


def build_macro_snapshot(
    mode: str,
    *,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
    tavily_requester: Callable[..., Any] = requests.post,
    rss_requester: Callable[..., Any] = requests.get,
    tavily_api_key: str | None = None,
) -> MacroSnapshot:
    cfg = config or load_macro_config()
    mode_cfg = (cfg.get("modes") or {}).get(mode) or {}
    title = str(mode_cfg.get("title") or f"美股宏觀雷達 {mode}")
    asof = _now_local(now).isoformat(timespec="minutes")
    warnings: list[str] = []

    tavily_summary, tavily_sources, warning = fetch_tavily_macro_news(
        mode,
        cfg,
        api_key=tavily_api_key,
        requester=tavily_requester,
    )
    if warning:
        warnings.append(warning)

    fed_items = fetch_fed_rss_items(cfg, requester=rss_requester)
    watch_events = flatten_watch_events(cfg)
    if mode == "event_check":
        # 事件檢查只看 Tavily 的「今天/24h 是否已發布」查詢；Fed RSS 可能有數日前的舊 FOMC 標題，
        # 不能用舊官方動態把無事件日或資料源失敗誤判成 high。
        combined = "\n".join([tavily_summary] + [item.title for item in tavily_sources])
    else:
        combined = "\n".join(
            [tavily_summary]
            + [item.title for item in tavily_sources]
            + [item.title for item in fed_items]
        )
    severity = classify_severity(combined, cfg, mode=mode)
    return MacroSnapshot(
        mode=mode,
        title=title,
        asof=asof,
        tavily_summary=tavily_summary,
        tavily_sources=tavily_sources,
        fed_items=fed_items,
        watch_events=watch_events,
        severity=severity,
        warnings=warnings,
    )


def format_macro_snapshot(snapshot: MacroSnapshot, *, max_sources: int = 6) -> str:
    lines = [f"截至：{snapshot.asof}", f"事件分級：{snapshot.severity}", ""]
    if snapshot.tavily_summary:
        lines.append("【宏觀摘要】")
        lines.append(snapshot.tavily_summary)
        lines.append("")

    if snapshot.watch_events:
        lines.append("【高優先事件監控】")
        high_events = [e for e in snapshot.watch_events if e.importance == "high"][:6]
        for event in high_events:
            time_hint = event.usual_time_et or ""
            check_hint = event.check_time_taipei_edt or event.check_time_taipei_est or ""
            suffix = f"（美東 {time_hint} / 台灣檢查 {check_hint}）" if time_hint or check_hint else ""
            lines.append(f"- {event.label}{suffix}")
        lines.append("")

    if snapshot.fed_items:
        lines.append("【Fed 官方動態】")
        for item in snapshot.fed_items[:4]:
            date_part = f" | {item.published_at}" if item.published_at else ""
            lines.append(f"- {item.title}（{item.source}{date_part}）")
        lines.append("")

    if snapshot.tavily_sources:
        lines.append("【消息來源】")
        for item in snapshot.tavily_sources[:max_sources]:
            lines.append(f"- {item.title}（{item.source}）")
        lines.append("")

    if snapshot.warnings:
        lines.append("【資料源提醒】")
        for warning in snapshot.warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="美股宏觀日曆 / 消息面資料層 dry-run")
    parser.add_argument("--mode", choices=["midday", "afterhours", "weekend", "event_check"], default="midday")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 而非文字卡片")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(AUTOMATION_DIR / ".env")
    except Exception:
        pass

    snapshot = build_macro_snapshot(args.mode)
    if args.json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"# {snapshot.title}\n\n{format_macro_snapshot(snapshot)}")


if __name__ == "__main__":
    main()
