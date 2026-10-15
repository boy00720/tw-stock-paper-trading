#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
text_chunker.py — Markdown 文本切段工具

策略：
1. 先按 Markdown 標題（## / ### 等）切段，保留語義邊界
2. 若單一段落仍 > max_chars，再以字元長度切分並加 overlap
3. 小於 min_chars 的段會嘗試併入前後段

用於記憶管家 W，把日報 / 研究報告 / 股市 brief 切成可嵌入的小段。
"""

from __future__ import annotations

import re
from typing import List, TypedDict


class Chunk(TypedDict):
    heading: str       # 該 chunk 所屬標題（最近的 ## 或以上）
    content: str       # chunk 文字
    order: int         # chunk 在整份文件中的順序（從 0 開始）


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _split_by_headings(text: str) -> List[dict]:
    """
    依 Markdown 標題切段。回傳 [{"heading": "...", "content": "...", "level": 1}, ...]
    第一段若在第一個標題之前，heading 為 "（引言）"。
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [{"heading": "（無標題）", "level": 0, "content": text.strip()}]

    segments: List[dict] = []

    # 第一個標題前的內容
    first = matches[0]
    if first.start() > 0:
        preface = text[: first.start()].strip()
        if preface:
            segments.append({"heading": "（引言）", "level": 0, "content": preface})

    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body or heading:
            segments.append({"heading": heading, "level": level, "content": body})

    return segments


def _split_long_text(text: str, max_chars: int, overlap: int) -> List[str]:
    """把過長文字以字元窗口切段，保留 overlap。"""
    if len(text) <= max_chars:
        return [text]

    pieces: List[str] = []
    step = max(1, max_chars - overlap)
    idx = 0
    while idx < len(text):
        piece = text[idx : idx + max_chars]
        if piece.strip():
            pieces.append(piece)
        if idx + max_chars >= len(text):
            break
        idx += step
    return pieces


def chunk_markdown(
    text: str,
    *,
    max_chars: int = 800,
    overlap: int = 100,
    min_chars: int = 50,
) -> List[Chunk]:
    """
    把 Markdown 文本切成 chunk 清單。

    Args:
        text: 原始 markdown 文本
        max_chars: 單一 chunk 的最大字元數（超過就再切）
        overlap: 長段切分時的重疊字元數
        min_chars: 小於此字元數的 chunk 會嘗試併入前段

    Returns:
        list[Chunk]
    """
    text = (text or "").strip()
    if not text:
        return []

    raw_segments = _split_by_headings(text)
    if not raw_segments:
        return []

    chunks: List[Chunk] = []
    order = 0

    for seg in raw_segments:
        heading = seg["heading"] or "（無標題）"
        content = seg["content"] or ""
        if not content:
            continue

        # 在 chunk 開頭夾一行 heading，讓嵌入模型能學到上下文
        if heading and heading not in ("（引言）", "（無標題）"):
            prefixed = f"# {heading}\n{content}"
        else:
            prefixed = content

        if len(prefixed) <= max_chars:
            if chunks and len(prefixed) < min_chars:
                # 併入前段
                prev = chunks[-1]
                merged = (prev["content"] + "\n\n" + prefixed).strip()
                if len(merged) <= max_chars:
                    chunks[-1] = Chunk(
                        heading=prev["heading"],
                        content=merged,
                        order=prev["order"],
                    )
                    continue
            chunks.append(Chunk(heading=heading, content=prefixed.strip(), order=order))
            order += 1
        else:
            for piece in _split_long_text(prefixed, max_chars, overlap):
                if not piece.strip():
                    continue
                chunks.append(Chunk(heading=heading, content=piece.strip(), order=order))
                order += 1

    return chunks


__all__ = ["chunk_markdown", "Chunk"]
