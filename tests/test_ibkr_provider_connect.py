from __future__ import annotations

import time

from utils.ibkr_provider import IBKRReadOnlyProvider


def test_light_connect_suppresses_account_requests():
    """light=True（預設）應把 connectAsync 會發的 positions/account/executions
    請求換成 no-op，避免它們在 Gateway 不穩窗口 timeout 拖崩連線。建構即驗、不連網。"""
    p = IBKRReadOnlyProvider(client_id=499, light=True)
    assert p.light is True
    for name in ("reqPositionsAsync", "reqAccountUpdatesAsync",
                 "reqAccountUpdatesMultiAsync", "reqExecutionsAsync"):
        assert getattr(p._ib, name).__name__ == "_empty", f"{name} 未被抑制"


def test_non_light_keeps_original_requests():
    p = IBKRReadOnlyProvider(client_id=499, light=False)
    assert p.light is False
    # 非 light：保留 ib_insync 原生方法（非我們的 _empty）
    assert p._ib.reqPositionsAsync.__name__ != "_empty"


def test_default_is_light():
    p = IBKRReadOnlyProvider(client_id=499)
    assert p.light is True


def test_blocking_requests_use_provider_timeout():
    p = IBKRReadOnlyProvider(client_id=499, timeout=7.0)
    assert p._ib.RequestTimeout == 7.0


class _FakeTicker:
    def __init__(self, last=None, close=None):
        self.last = last
        self.close = close

    def marketPrice(self):
        return self.last


class _FakeFutureIB:
    def __init__(self, tickers, *, permission_error=False):
        self._tickers = list(tickers)
        self._permission_error = permission_error
        self.requests = 0
        self.RequestTimeout = 0

    def isConnected(self):
        return True

    def reqMarketDataType(self, _value):
        return None

    def qualifyContracts(self, contract):
        contract.localSymbol = "ES_TEST"
        return [contract]

    def reqMktData(self, _contract, *_args):
        self.requests += 1
        if self._permission_error:
            provider._errors.append({"code": 10089, "message": "subscription required"})
        return self._tickers[min(self.requests - 1, len(self._tickers) - 1)]

    def sleep(self, _seconds):
        return None

    def cancelMktData(self, _contract):
        return None


def test_future_quote_retries_once_after_recent_farm_event():
    global provider
    provider = IBKRReadOnlyProvider(client_id=499)
    provider._ib = _FakeFutureIB([_FakeTicker(), _FakeTicker(5000, 4950)])
    provider._farm_events.append({"code": 2104, "message": "farm is OK", "monotonic": time.monotonic()})

    quote = provider.fetch_overnight_futures(specs=(("ES", "CME", "ES"),), sleep_seconds=0)[0]

    assert provider._ib.requests == 2
    assert quote.permission_status == "ok"
    assert quote.last == 5000
    assert "retried once" in quote.warnings[0]


def test_future_quote_rejects_negative_market_price_sentinel():
    global provider
    provider = IBKRReadOnlyProvider(client_id=499)
    provider._ib = _FakeFutureIB([_FakeTicker(-1, 4950)])

    quote = provider.fetch_overnight_futures(specs=(("ES", "CME", "ES"),), sleep_seconds=0)[0]

    assert quote.last is None
    assert quote.close == 4950
    assert quote.change_pct is None
    assert quote.permission_status == "ok"


def test_live_future_close_only_is_not_realtime():
    global provider
    provider = IBKRReadOnlyProvider(client_id=499)
    provider._ib = _FakeFutureIB([_FakeTicker(None, 4950)])

    quote = provider.fetch_overnight_futures(
        specs=(("ES", "CME", "ES"),), market_data_type="live", sleep_seconds=0,
    )[0]

    assert quote.permission_status == "ok"
    assert quote.last is None
    assert quote.close == 4950
    assert quote.is_realtime is False


def test_future_quote_does_not_retry_broken_farm_event():
    global provider
    provider = IBKRReadOnlyProvider(client_id=499)
    provider._ib = _FakeFutureIB([_FakeTicker()])
    provider._farm_events.append({"code": 2103, "message": "farm connection broken", "monotonic": time.monotonic()})

    provider.fetch_overnight_futures(specs=(("ES", "CME", "ES"),), sleep_seconds=0)

    assert provider._ib.requests == 1


def test_future_quote_does_not_retry_permission_error():
    global provider
    provider = IBKRReadOnlyProvider(client_id=499)
    provider._ib = _FakeFutureIB([_FakeTicker()], permission_error=True)
    provider._farm_events.append({"code": 2104, "message": "farm is OK", "monotonic": time.monotonic()})

    quote = provider.fetch_overnight_futures(specs=(("ES", "CME", "ES"),), sleep_seconds=0)[0]

    assert provider._ib.requests == 1
    assert quote.permission_status == "no_live_subscription"
