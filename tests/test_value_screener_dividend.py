from __future__ import annotations

import pytest

from scripts import value_screener


class _FakeTicker:
    info = {
        "currentPrice": 100,
        "dividendRate": 0.34,
        "dividendYield": 99,
    }


def test_yfinance_dividend_yield_uses_shared_rate_price_normalization(monkeypatch):
    monkeypatch.setattr(value_screener.yf, "Ticker", lambda _symbol: _FakeTicker())

    data = value_screener.fetch_value_data("ABC", fm=None)

    assert data["dividend_yield"] == pytest.approx(0.0034)


def test_value_check_treats_dividend_yield_as_decimal_ratio():
    result = value_screener.check_value(
        "ABC",
        "ABC",
        "test",
        {"dividend_yield": 0.0034},
        [2026, 2025, 2024, 2023, 2022],
        None,
    )

    assert result.checks["③ 股息率>3% + 年年發放"] is False
    assert result.values["股息率"] == "0.34%"
