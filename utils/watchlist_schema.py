from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

WATCHLIST_POOLS = ("short_term", "medium_term", "long_term", "intraday")
DEFAULT_SUMMARY: Dict[str, int] = {
    "short_term_count": 0,
    "medium_term_count": 0,
    "long_term_count": 0,
    "intraday_count": 0,
    "intraday_strict_count": 0,
    "cross_signals_count": 0,
}


def default_watchlist() -> Dict[str, Any]:
    return {
        "updated_at": "",
        "schema_version": 1,
        "summary": dict(DEFAULT_SUMMARY),
        "formal_pools": {
            "short_term": [],
            "medium_term": [],
            "long_term": [],
        },
        "auxiliary_signals": {
            "intraday": [],
        },
        "short_term": [],
        "medium_term": [],
        "long_term": [],
        "intraday": [],
        "cross_signals": {},
    }


def default_watchlist_path(base_dir: Optional[Path] = None) -> Path:
    root = base_dir or Path(__file__).resolve().parent.parent
    return root / "logs" / "watchlist" / "current.json"


def load_watchlist(path: Optional[Path] = None) -> Dict[str, Any]:
    watchlist_path = path or default_watchlist_path()
    if not watchlist_path.exists():
        return default_watchlist()
    try:
        payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
    except Exception:
        return default_watchlist()
    if not isinstance(payload, dict):
        return default_watchlist()
    merged = default_watchlist()
    merged.update(payload)
    if isinstance(payload.get("formal_pools"), dict):
        merged["formal_pools"] = payload["formal_pools"]
    if isinstance(payload.get("auxiliary_signals"), dict):
        merged["auxiliary_signals"] = payload["auxiliary_signals"]
    return merged


def watchlist_summary(watchlist: Mapping[str, Any]) -> Dict[str, int]:
    summary = watchlist.get("summary") or {}
    result = dict(DEFAULT_SUMMARY)
    for key in DEFAULT_SUMMARY:
        value = summary.get(key, 0)
        try:
            result[key] = int(value or 0)
        except Exception:
            result[key] = 0
    return result


def pool_items(watchlist: Mapping[str, Any], pool: str) -> List[Dict[str, Any]]:
    formal_pools = watchlist.get("formal_pools") or {}
    auxiliary_signals = watchlist.get("auxiliary_signals") or {}

    if pool in ("short_term", "medium_term", "long_term"):
        items = formal_pools.get(pool)
        if not items:
            items = watchlist.get(pool)
    elif pool == "intraday":
        items = auxiliary_signals.get("intraday")
        if not items:
            items = watchlist.get("intraday")
    else:
        items = watchlist.get(pool)

    return list(items) if isinstance(items, list) else []


def index_pool_by_symbol(watchlist: Mapping[str, Any], pool: str) -> Dict[str, Dict[str, Any]]:
    return {
        item.get("symbol") or "": item
        for item in pool_items(watchlist, pool)
        if item.get("symbol")
    }


def cross_signals(watchlist: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    cross = watchlist.get("cross_signals") or {}
    return dict(cross) if isinstance(cross, dict) else {}


def cross_signals_count(watchlist: Optional[Mapping[str, Any]] = None) -> int:
    payload = watchlist if watchlist is not None else load_watchlist()
    return watchlist_summary(payload).get("cross_signals_count", 0)
