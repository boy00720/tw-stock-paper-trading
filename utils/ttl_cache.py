"""Small JSON TTL cache for read-only market research data."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any


def cache_root() -> Path:
    root = Path(os.getenv("MARKET_DATA_CACHE_DIR", "logs/cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_json(key: str, *, ttl_seconds: float) -> Any | None:
    path = cache_root() / f"{key}.json"
    try:
        if ttl_seconds >= 0 and time.time() - path.stat().st_mtime > ttl_seconds:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def write_json(key: str, value: Any) -> None:
    path = cache_root() / f"{key}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
