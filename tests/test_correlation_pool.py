#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""correlation_pool loader 測試。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import correlation_pool as cp


def _write_pool(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "correlation_pool.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_real_config_has_core_anchor():
    """真實 config 應載入且含 00631L 本體與 0050 錨點。"""
    syms = cp.load_correlation_pool()
    assert syms, "真實 correlation_pool.yaml 應可載入"
    by_symbol = {s.symbol: s for s in syms}
    assert "00631L.TW" in by_symbol
    assert "0050.TW" in by_symbol
    assert "2330.TW" in by_symbol
    # 0050 是 core 錨點
    assert by_symbol["0050.TW"].importance == "core"
    assert by_symbol["0050.TW"].tier == "anchor"


def test_tier_order_preserved(tmp_path):
    """攤平後應依命運驅動鏈 tier 順序：anchor → weight_drivers → semiconductor_chain → us_overnight。"""
    pool = _write_pool(tmp_path, """
correlation_pool:
  us_overnight:
    - symbol: "TSM"
      name: "TSM ADR"
      markets: ["us", "adr"]
  anchor:
    - symbol: "0050.TW"
      name: "元大台灣50"
      markets: ["tw"]
  weight_drivers:
    - symbol: "2330.TW"
      name: "台積電"
      markets: ["tw"]
""")
    syms = cp.load_correlation_pool(pool)
    # 即使 YAML 裡 us_overnight 寫在最前，攤平後仍以 anchor 開頭
    assert [s.symbol for s in syms] == ["0050.TW", "2330.TW", "TSM"]
    assert [s.tier for s in syms] == ["anchor", "weight_drivers", "us_overnight"]


def test_symbols_for_market_filters(tmp_path):
    """market 過濾：tw 只回台股現貨；us 只回美股；adr 標 us+adr 的同時命中。"""
    pool = _write_pool(tmp_path, """
correlation_pool:
  anchor:
    - symbol: "0050.TW"
      name: "元大台灣50"
      markets: ["tw"]
  us_overnight:
    - symbol: "TSM"
      name: "TSM ADR"
      markets: ["us", "adr"]
    - symbol: "NVDA"
      name: "NVIDIA"
      markets: ["us"]
""")
    tw = [s.symbol for s in cp.symbols_for_market("tw", pool)]
    us = [s.symbol for s in cp.symbols_for_market("us", pool)]
    adr = [s.symbol for s in cp.symbols_for_market("adr", pool)]
    assert tw == ["0050.TW"]
    assert us == ["TSM", "NVDA"]
    assert adr == ["TSM"]  # 只有標了 adr 的命中


def test_dedup_symbol(tmp_path):
    """同 symbol 在多 tier 重複時只取第一次（依 tier 順序）。"""
    pool = _write_pool(tmp_path, """
correlation_pool:
  anchor:
    - symbol: "2330.TW"
      name: "台積電(錨點視角)"
      markets: ["tw"]
  weight_drivers:
    - symbol: "2330.TW"
      name: "台積電(權值視角)"
      markets: ["tw"]
""")
    syms = cp.load_correlation_pool(pool)
    assert len(syms) == 1
    assert syms[0].tier == "anchor"
    assert syms[0].name == "台積電(錨點視角)"


def test_comparison_params_from_config():
    """真實 config 的對比參數：跌門檻應比漲門檻敏感（配合 2 倍槓桿）。"""
    params = cp.load_comparison_params()
    assert params.restep_up_pct == 3.0
    assert params.restep_down_pct == 2.5
    assert params.restep_down_pct < params.restep_up_pct
    assert params.snapshot_max_age_hours == 10.0


def test_missing_file_safe_degradation(tmp_path):
    """缺檔/壞檔一律安全降級：pool 回 []、參數回 fallback 預設。"""
    missing = tmp_path / "nope.yaml"
    assert cp.load_correlation_pool(missing) == []
    assert cp.symbols_for_market("tw", missing) == []
    params = cp.load_comparison_params(missing)
    assert params.restep_up_pct == 3.0  # fallback
    assert params.restep_down_pct == 2.5
    assert params.snapshot_max_age_hours == 10.0


def test_malformed_config_returns_empty(tmp_path):
    """correlation_pool 非 dict（壞結構）→ 回 []，不拋例外。"""
    pool = _write_pool(tmp_path, "correlation_pool: [1, 2, 3]\n")
    assert cp.load_correlation_pool(pool) == []
