"""Resolve US ticker -> SEC CIK with a cached SEC ticker map."""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from utils.ttl_cache import read_json, write_json

logger = logging.getLogger(__name__)
_MAP_KEY = "sec_company_tickers"
_MAP_TTL = 7 * 24 * 60 * 60


def _user_agent() -> str:
    return os.getenv("SEC_USER_AGENT", "").strip()


def _load_map() -> dict[str, str]:
    cached = read_json(_MAP_KEY, ttl_seconds=_MAP_TTL)
    if isinstance(cached, dict) and cached:
        return {str(k).upper(): str(v).zfill(10) for k, v in cached.items()}

    user_agent = _user_agent()
    if not user_agent:
        return {}
    try:
        response = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": user_agent}, timeout=15,
        )
        response.raise_for_status()
        raw = response.json()
        mapping = {
            str(item.get("ticker", "")).upper(): str(item.get("cik_str", "")).zfill(10)
            for item in raw.values()
            if item.get("ticker") and item.get("cik_str")
        }
        if mapping:
            write_json(_MAP_KEY, mapping)
        return mapping
    except Exception as exc:
        logger.warning("SEC ticker map unavailable: %s", type(exc).__name__)
        return {}


def resolve_cik(symbol: str) -> str | None:
    normalized = str(symbol or "").upper().split(".")[0]
    explicit = os.getenv(f"SEC_CIK_{normalized}", "").strip()
    if explicit:
        return explicit.zfill(10)
    return _load_map().get(normalized)
