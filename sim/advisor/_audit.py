"""automation/sim/advisor/_audit.py — advisor 共用審計輔助

提供 jsonl append + atomic JSON state 寫入。advisor 進程獨立，不跟
bot 進程共寫同一檔（advisor_audit.jsonl / advisor_state.json 都是 advisor 獨佔），
OS-level append 即足夠安全。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """append-only 寫一行 JSON。失敗 log 後不 raise（不要中斷主流程）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.exception("[advisor._audit] append jsonl failed: %s", path)


def write_state_atomic(path: Path, obj: dict[str, Any]) -> None:
    """atomic JSON state 寫入（先寫 tempfile 再 rename，避免 partial write）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, default=str, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        logger.exception("[advisor._audit] write state atomic failed: %s", path)


def read_state(path: Path) -> dict[str, Any]:
    """讀 JSON state；缺檔/解析失敗回 {}。"""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        logger.exception("[advisor._audit] read state failed: %s", path)
        return {}
