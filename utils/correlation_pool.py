#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
correlation_pool.py — 00631L 相關性追蹤池載入器（單一事實源）

讀 config/correlation_pool.yaml，供盤前/盤中/盤後/美股各時段「00631L + 台積電 +
相關性股票」追蹤線共用。**不與選股池（stocks.yaml）混用**——這裡的標的只供風險連動
觀察，不進 sim advisor 選股評分、不自動下單。

任何失敗/缺檔一律回安全空值，由呼叫端優雅降級（不擋主流程）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_POOL_PATH = _BASE_DIR / "config" / "correlation_pool.yaml"

# tier 顯示順序（依命運驅動鏈：錨點 → 權值 → 半導體 → 美股隔夜）
_TIER_ORDER = ["anchor", "weight_drivers", "semiconductor_chain", "us_overnight"]

# 對比參數 fallback（config 缺漏時用）
_DEFAULT_RESTEP_UP = 3.0
_DEFAULT_RESTEP_DOWN = 2.5
_DEFAULT_SNAPSHOT_MAX_AGE_HOURS = 10.0


@dataclass(frozen=True)
class CorrelationSymbol:
    symbol: str
    name: str
    role: str = ""
    tier: str = ""
    markets: tuple[str, ...] = field(default_factory=tuple)
    importance: str = "normal"

    def in_market(self, market: str) -> bool:
        return market in self.markets


@dataclass(frozen=True)
class ComparisonParams:
    restep_up_pct: float = _DEFAULT_RESTEP_UP
    restep_down_pct: float = _DEFAULT_RESTEP_DOWN
    snapshot_max_age_hours: float = _DEFAULT_SNAPSHOT_MAX_AGE_HOURS


def _to_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_correlation_pool(path: Path = DEFAULT_POOL_PATH) -> list[CorrelationSymbol]:
    """讀相關性池，回依 tier 順序攤平的標的清單。失敗/缺檔回 []。"""
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("correlation_pool.yaml 不存在: %s", path)
        return []
    except Exception:
        logger.exception("讀取 correlation_pool.yaml 失敗: %s", path)
        return []

    pool = raw.get("correlation_pool") or {}
    if not isinstance(pool, dict):
        return []

    out: list[CorrelationSymbol] = []
    seen: set[str] = set()
    # 已知 tier 依固定順序，未知 tier 接在後面（不丟資料）
    tiers = _TIER_ORDER + [t for t in pool.keys() if t not in _TIER_ORDER]
    for tier in tiers:
        items = pool.get(tier) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            markets_raw = item.get("markets") or []
            markets = tuple(str(m).strip() for m in markets_raw if str(m).strip())
            out.append(CorrelationSymbol(
                symbol=sym,
                name=str(item.get("name") or sym).strip(),
                role=str(item.get("role") or "").strip(),
                tier=tier,
                markets=markets,
                importance=str(item.get("importance") or "normal").strip(),
            ))
    return out


def symbols_for_market(market: str, path: Path = DEFAULT_POOL_PATH) -> list[CorrelationSymbol]:
    """回該 market（tw/us/adr）抓得到報價的相關性標的，依 tier 順序。"""
    return [s for s in load_correlation_pool(path) if s.in_market(market)]


def load_comparison_params(path: Path = DEFAULT_POOL_PATH) -> ComparisonParams:
    """讀對比差異機制參數；缺漏回安全 fallback。"""
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ComparisonParams()

    comp = raw.get("comparison") or {}
    if not isinstance(comp, dict):
        return ComparisonParams()
    restep = comp.get("intraday_restep") or {}
    if not isinstance(restep, dict):
        restep = {}
    return ComparisonParams(
        restep_up_pct=_to_float(restep.get("up_threshold_pct"), _DEFAULT_RESTEP_UP),
        restep_down_pct=_to_float(restep.get("down_threshold_pct"), _DEFAULT_RESTEP_DOWN),
        snapshot_max_age_hours=_to_float(
            comp.get("snapshot_max_age_hours"), _DEFAULT_SNAPSHOT_MAX_AGE_HOURS
        ),
    )
