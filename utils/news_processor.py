#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞處理工具
"""

import logging
from typing import List, Dict, Any
import re
from html import unescape

logger = logging.getLogger(__name__)


class NewsProcessor:
    """新聞處理器"""

    def __init__(self, config: dict):
        self.config = config

    def process_news(self, raw_news: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """處理並去重新聞"""
        seen = set()
        processed = []

        for item in raw_news:
            title = " ".join(str(item.get("title", "")).split())
            url = str(item.get("url", "")).strip().lower()
            key = (title.lower(), url)
            if title and key not in seen:
                seen.add(key)
                processed.append(item)

        return processed

    def format_category_news(self, news_list: List[Dict[str, Any]], category: str, offset: int = 0) -> str:
        """格式化某類別新聞為純文字（不含 Markdown 符號）"""
        lines = []
        for i, item in enumerate(news_list, offset + 1):
            title = str(item.get("title", "（無標題）")).strip()
            source = str(item.get("source", "未知來源")).strip()
            lines.append(f"{i}. {title}")
            lines.append(f"   來源：{source} | {item.get('published_at', '')}")
            summary = str(item.get("summary", "")).strip()
            if summary:
                summary = unescape(re.sub(r"<[^>]+>", "", summary))
                summary = " ".join(summary.split())
                if summary:
                    lines.append(f"   {summary}")
            lines.append("")
        return '\n'.join(lines)
