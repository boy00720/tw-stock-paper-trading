import pandas as pd
from datetime import datetime

from scripts.intraday_screener import (
    INTRADAY_ENGINE_VERSION,
    build_m1_snapshot,
    check_intraday,
    recompute_result_flags,
)


def _build_daily_df():
    idx = pd.date_range(end=datetime(2026, 4, 29), periods=30, freq="D")
    return pd.DataFrame(
        {
            "Open": [100 + i * 0.5 for i in range(30)],
            "High": [101 + i * 0.5 for i in range(30)],
            "Low": [99 + i * 0.5 for i in range(30)],
            "Close": [100 + i * 0.6 for i in range(30)],
            "Volume": [1_000_000 + i * 10_000 for i in range(30)],
        },
        index=idx,
    )


def _build_m1_df():
    idx = pd.date_range(start=datetime(2026, 4, 30, 14, 0), periods=40, freq="min")
    close_values = [118 + i * 0.1 for i in range(40)]
    return pd.DataFrame(
        {
            "Open": close_values,
            "High": [value + 0.5 for value in close_values],
            "Low": [value - 0.3 for value in close_values],
            "Close": close_values,
            "Volume": [50_000] * 40,
        },
        index=idx,
    )


def _build_equal_high_df():
    idx = pd.date_range(start=datetime(2026, 4, 30, 14, 0), periods=40, freq="min")
    close_values = [118 + i * 0.05 for i in range(40)]
    highs = [120.5] * 40
    highs[30:] = [120.4] * 10
    return pd.DataFrame(
        {
            "Open": close_values,
            "High": highs,
            "Low": [value - 0.2 for value in close_values],
            "Close": close_values,
            "Volume": [50_000] * 40,
        },
        index=idx,
    )


def _build_breakout_then_break_df():
    idx = pd.date_range(start=datetime(2026, 4, 30, 14, 0), periods=40, freq="min")
    close_values = [118 + i * 0.08 for i in range(40)]
    highs = [120 + i * 0.05 for i in range(40)]
    lows = [value - 0.2 for value in close_values]
    highs[31] = 122.0
    lows[33] = 117.0
    return pd.DataFrame(
        {
            "Open": close_values,
            "High": highs,
            "Low": lows,
            "Close": close_values,
            "Volume": [50_000] * 40,
        },
        index=idx,
    )


def test_build_m1_snapshot_contains_core_fields():
    daily = _build_daily_df()
    minute = _build_m1_df()
    snapshot = build_m1_snapshot(
        "2330.TW",
        minute,
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 40_000_000},
    )

    assert snapshot is not None
    assert snapshot["source"] == "yfinance_m1"
    assert snapshot["bar_count"] == 40
    assert snapshot["post_cutoff_bar_count"] > 0
    assert "snapshot_time" in snapshot
    assert "volume_ratio" in snapshot
    assert "turnover" in snapshot


def test_check_intraday_uses_m1_snapshot_without_breaking_contract():
    daily = _build_daily_df()
    minute = _build_m1_df()
    snapshot = build_m1_snapshot(
        "2330.TW",
        minute,
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
    )

    result = check_intraday(
        "2330.TW",
        "台積電",
        "半導體",
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
        m1_snapshot=snapshot,
    )

    assert set(["symbol", "name", "sector", "passed", "strict_passed", "checks", "values", "missing", "notes", "error"]).issubset(result.__dict__.keys())
    assert "① 漲幅 3%-7%" in result.checks
    assert "④ 量比>1.5" in result.checks
    assert "⑤ 換手率 3%-10%" in result.checks
    assert "⑥ 收在開盤上+近5日新高 (代理)" in result.checks
    assert result.values["引擎版本"] == INTRADAY_ENGINE_VERSION
    assert "m1_snapshot" in result.values
    assert "通過數" in result.values
    assert "strict_hard_fails" in result.values


def test_recompute_result_flags_keeps_m1_snapshot_payload():
    daily = _build_daily_df()
    minute = _build_m1_df()
    snapshot = build_m1_snapshot(
        "2330.TW",
        minute,
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
    )
    result = check_intraday(
        "2330.TW",
        "台積電",
        "半導體",
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
        m1_snapshot=snapshot,
    )

    recomputed = recompute_result_flags(result)
    assert "m1_snapshot" in recomputed.values
    assert recomputed.values["m1_snapshot"]["source"] == "yfinance_m1"


def test_m1_signal_requires_strict_breakout_not_equal_high():
    daily = _build_daily_df()
    minute = _build_equal_high_df()
    snapshot = build_m1_snapshot(
        "2330.TW",
        minute,
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
    )
    assert snapshot is not None
    assert snapshot["breakout_time"] is None
    assert snapshot["intraday_signal_ok"] is False


def test_m1_signal_requires_pullback_after_breakout_to_hold_support():
    daily = _build_daily_df()
    minute = _build_breakout_then_break_df()
    snapshot = build_m1_snapshot(
        "2330.TW",
        minute,
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
    )
    assert snapshot is not None
    assert snapshot["breakout_time"] is not None
    assert snapshot["pullback_low_after_breakout"] is not None
    assert snapshot["intraday_signal_ok"] is False


def test_legacy_check_key_is_preserved():
    daily = _build_daily_df()
    minute = _build_m1_df()
    snapshot = build_m1_snapshot(
        "2330.TW",
        minute,
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
    )
    result = check_intraday(
        "2330.TW",
        "台積電",
        "半導體",
        daily,
        {"market_cap": 2_000e8, "shares_outstanding": 4_000_000},
        m1_snapshot=snapshot,
    )
    assert "⑥ 收在開盤上+近5日新高 (代理)" in result.checks
