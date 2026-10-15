# -*- coding: utf-8 -*-
"""
測試資料品質驗證器
"""

import pytest
import pandas as pd
from datetime import datetime, timedelta

from utils.data_validator import (
    validate_stock_data_freshness,
    validate_market_data_completeness,
    validate_m1_data_freshness,
    validate_m1_data_completeness,
    validate_m1_snapshot,
)


class TestValidateStockDataFreshness:
    """測試股票資料新鮮度驗證"""

    def test_valid_fresh_data(self):
        """測試新鮮資料通過驗證"""
        dates = pd.date_range(end=datetime.now(), periods=5, freq="h")
        df = pd.DataFrame({
            "Close": [100, 101, 102, 103, 104],
            "Volume": [1000, 1100, 1200, 1300, 1400],
        }, index=dates)

        is_fresh, error = validate_stock_data_freshness(df, "2330.TW")
        assert is_fresh is True
        assert error is None

    def test_empty_dataframe(self):
        """測試空 DataFrame"""
        df = pd.DataFrame()
        is_fresh, error = validate_stock_data_freshness(df, "2330.TW")
        assert is_fresh is False
        assert "無數據" in error


class TestValidateMarketDataCompleteness:
    """測試市場資料完整性驗證"""

    def test_complete_data(self):
        """測試完整資料"""
        df = pd.DataFrame({
            "Close": [100, 101, 102],
            "Open": [99, 100, 101],
            "High": [101, 102, 103],
            "Low": [98, 99, 100],
            "Volume": [1000, 1100, 1200],
        })

        is_complete, error = validate_market_data_completeness(df, "2330.TW")
        assert is_complete is True
        assert error is None

    def test_insufficient_bars(self):
        """測試 K 線數不足"""
        df = pd.DataFrame({"Close": [100]})
        is_complete, error = validate_market_data_completeness(df, "2330.TW", min_bars=2)
        assert is_complete is False
        assert "數據筆數" in error


class TestValidateM1Data:
    def test_validate_m1_freshness_passes(self):
        now = datetime(2026, 4, 30, 14, 35)
        idx = pd.date_range(end=now - timedelta(minutes=1), periods=40, freq="min")
        df = pd.DataFrame({
            "Open": [100.0] * 40,
            "High": [101.0] * 40,
            "Low": [99.0] * 40,
            "Close": [100.5] * 40,
            "Volume": [1000] * 40,
        }, index=idx)

        ok, error = validate_m1_data_freshness(df, "2330.TW", now=now)
        assert ok is True
        assert error is None

    def test_validate_m1_freshness_fails_when_stale(self):
        now = datetime(2026, 4, 30, 14, 35)
        idx = pd.date_range(end=now - timedelta(minutes=10), periods=40, freq="min")
        df = pd.DataFrame({
            "Open": [100.0] * 40,
            "High": [101.0] * 40,
            "Low": [99.0] * 40,
            "Close": [100.5] * 40,
            "Volume": [1000] * 40,
        }, index=idx)

        ok, error = validate_m1_data_freshness(df, "2330.TW", now=now)
        assert ok is False
        assert "延遲" in error

    def test_validate_m1_completeness_passes(self):
        start = datetime(2026, 4, 30, 14, 0)
        idx = pd.date_range(start=start, periods=40, freq="min")
        df = pd.DataFrame({
            "Open": [100.0] * 40,
            "High": [101.0] * 40,
            "Low": [99.0] * 40,
            "Close": [100.5] * 40,
            "Volume": [1000] * 40,
        }, index=idx)

        ok, error = validate_m1_data_completeness(df, "2330.TW")
        assert ok is True
        assert error is None

    def test_validate_m1_completeness_fails_without_cutoff_bars(self):
        start = datetime(2026, 4, 30, 13, 0)
        idx = pd.date_range(start=start, periods=40, freq="min")
        df = pd.DataFrame({
            "Open": [100.0] * 40,
            "High": [101.0] * 40,
            "Low": [99.0] * 40,
            "Close": [100.5] * 40,
            "Volume": [1000] * 40,
        }, index=idx)

        ok, error = validate_m1_data_completeness(df, "2330.TW")
        assert ok is False
        assert "14:30 後" in error


    def test_validate_m1_snapshot_warns_when_cutoff_bars_missing(self):
        ok, errors, warnings = validate_m1_snapshot(
            {
                "snapshot_time": "2026-04-30T14:35:00",
                "last_price": 100,
                "prev_close": 99,
                "volume_cum": 100000,
                "avg_volume_20d": 90000,
                "post_cutoff_bar_count": 0,
            },
            "2330.TW",
        )
        assert ok is True
        assert errors == []
        assert any("cutoff 後無 M1 bars" in warning for warning in warnings)
