#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞抓取工具（真實 RSS 版）
"""

import logging
import requests
from xml.etree import ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any
import re
from html import unescape
import time

logger = logging.getLogger(__name__)

# 已驗證可用的預設備援 RSS 來源（當 config 未提供可用來源時）
DEFAULT_RSS_SOURCES = [
    {
        "name": "BBC 中文",
        "url": "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml",
        "region": "international",
        "category": "politics",
        "lang": "zh"
    },
    {
        "name": "Google 新聞 台灣",
        "url": "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "region": "taiwan",
        "category": "society",
        "lang": "zh"
    },
    {
        "name": "自由時報",
        "url": "https://news.ltn.com.tw/rss/all.xml",
        "region": "taiwan",
        "category": "society",
        "lang": "zh"
    },
    {
        "name": "Google 新聞 中文",
        "url": "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
        "region": "china",
        "category": "politics",
        "lang": "zh"
    },
    {
        "name": "新華社",
        "url": "http://www.xinhuanet.com/politics/news_politics.xml",
        "region": "china",
        "category": "politics",
        "lang": "zh"
    },
    {
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/rss/topstories",
        "region": "international",
        "category": "economy",
        "lang": "en"
    },
    {
        "name": "Google 新聞 國際",
        "url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
        "region": "international",
        "category": "politics",
        "lang": "en"
    },
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
TAG_RE = re.compile(r"<[^>]+>")
RSS_URL_HINT_RE = re.compile(r"(rss|feed|\.xml($|\?))", re.IGNORECASE)


class NewsFetcher:
    """新聞抓取器（RSS 版）"""

    def __init__(self, config: dict):
        self.config = config
        self.max_per_source = max(1, int(config.get("crawling", {}).get("max_articles", 10)))
        self.timeout = max(3, int(config.get("crawling", {}).get("timeout", 15)))
        self.retry_attempts = max(1, int(config.get("crawling", {}).get("retry_attempts", 2)))
        self.retry_backoff_seconds = max(
            0.1,
            float(config.get("crawling", {}).get("retry_backoff_seconds", 1.5)),
        )
        self.retry_backoff_multiplier = max(
            1.0,
            float(config.get("crawling", {}).get("retry_backoff_multiplier", 2.0)),
        )
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.sources = self._build_sources_from_config(config)

    @staticmethod
    def _is_rss_like_url(url: str) -> bool:
        if not url:
            return False
        return bool(RSS_URL_HINT_RE.search(url))

    def _build_sources_from_config(self, config: Dict[str, Any]) -> List[Dict[str, str]]:
        sources: List[Dict[str, str]] = []

        # 優先使用 config.rss_sources（推薦結構）
        for raw in config.get("rss_sources", []) or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            url = str(raw.get("url", "")).strip()
            if not name or not url:
                continue
            sources.append({
                "name": name,
                "url": url,
                "region": str(raw.get("region", "international")).strip() or "international",
                "category": str(raw.get("category", "society")).strip() or "society",
                "lang": str(raw.get("lang", "zh")).strip() or "zh",
            })

        if sources:
            logger.info("NewsFetcher 使用 config.rss_sources，共 %s 個來源", len(sources))
            return sources

        # 次選：相容舊結構 (international/china/taiwan -> sources)
        for region_key in ("international", "china", "taiwan"):
            for raw in (config.get(region_key, {}) or {}).get("sources", []) or []:
                if not isinstance(raw, dict):
                    continue
                source_type = str(raw.get("type", "")).strip().lower()
                if source_type not in {"rss", "rss_feed"}:
                    continue
                name = str(raw.get("name", "")).strip()
                url = str(raw.get("url", "")).strip()
                if not name or not url:
                    continue
                if not self._is_rss_like_url(url):
                    logger.warning("跳過疑似非 RSS URL: %s (%s)", name, url)
                    continue
                categories = raw.get("categories") or []
                category = str(categories[0]).strip() if categories else "society"
                sources.append({
                    "name": name,
                    "url": url,
                    "region": region_key,
                    "category": category,
                    "lang": "zh" if region_key in {"china", "taiwan"} else "en",
                })

        if sources:
            logger.info("NewsFetcher 使用相容舊結構來源，共 %s 個來源", len(sources))
            return sources

        logger.warning("config 中無可用 RSS 來源，改用預設備援來源")
        return list(DEFAULT_RSS_SOURCES)

    def fetch_all_news(self) -> List[Dict[str, Any]]:
        """抓取所有來源的新聞"""
        all_news = []

        for source in self.sources:
            try:
                items = self._fetch_rss(source)
                all_news.extend(items)
                logger.info(f"Fetched {len(items)} items from {source['name']}")
            except Exception as e:
                logger.error(f"Failed to fetch from {source['name']}: {e}")

        return all_news

    def _fetch_rss(self, source: dict) -> List[Dict[str, Any]]:
        """抓取單一 RSS 來源"""
        last_error = None
        delay = self.retry_backoff_seconds
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return self._fetch_rss_once(source)
            except Exception as e:
                last_error = e
                if attempt >= self.retry_attempts:
                    break
                logger.warning(
                    "RSS 抓取失敗 %s (%s/%s): %s，%.1fs 後重試",
                    source.get("name", "unknown"),
                    attempt,
                    self.retry_attempts,
                    e,
                    delay,
                )
                time.sleep(delay)
                delay *= self.retry_backoff_multiplier
        raise last_error or RuntimeError(f"Unknown fetch error for {source.get('name')}")

    def _fetch_rss_once(self, source: dict) -> List[Dict[str, Any]]:
        resp = self.session.get(source["url"], timeout=self.timeout)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        items = root.findall(".//item")

        results = []
        for item in items[: self.max_per_source]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()

            if not title:
                continue

            # 清除 HTML 標籤與實體
            desc = unescape(TAG_RE.sub("", desc))
            desc = " ".join(desc.split())

            # 截斷摘要
            summary = desc[:200] + "..." if len(desc) > 200 else desc

            results.append({
                "title": title,
                "url": link,
                "summary": summary,
                "source": source.get("name", "unknown"),
                "region": source.get("region", "international"),
                "category": source.get("category", "society"),
                "lang": source.get("lang", "zh"),
                "published_at": pub_date or datetime.now().isoformat(),
            })

        return results
