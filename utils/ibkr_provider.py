#!/usr/bin/env python3
"""
IBKR read-only provider for US market data.

Scope:
- US stock / ETF quote read-only access
- Account snapshot summary with masked account ids
- Explicit delayed/live/permission metadata
- No production integration by default

Security boundary:
- Default host is 127.0.0.1
- Always connects with readonly=True
- No credential handling
- No trading action helpers
- No full account id output
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import logging
import math
import os
from pathlib import Path
import time
import fcntl
from typing import Any, Dict, List, Optional

from ib_insync import IB, ContFuture, ScannerSubscription, Stock


_IBKR_LOGGER_NAMES = (
    "ib_insync",
    "ib_insync.client",
    "ib_insync.ib",
    "ib_insync.wrapper",
)


class _ScannerNoiseFilter(logging.Filter):
    """Drop the benign Error 162 ib_insync logs when a one-shot scanner
    snapshot ends ("scanner subscription cancelled"). It is not a real error
    and otherwise spams the CLI on every screener run. Genuine Error 162
    cases (e.g. historical pacing violations) carry different text and pass.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "scanner subscription cancelled" not in record.getMessage()


def _silence_ib_insync_info_logs() -> None:
    """Keep ib_insync INFO logs from leaking account/position details.

    ib_insync emits position/portfolio synchronization lines at INFO level;
    those can include full account ids. The provider exposes only masked
    account ids, so the dependency logger must stay quieter than INFO.
    """
    for name in _IBKR_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)
    wrapper_logger = logging.getLogger("ib_insync.wrapper")
    if not any(isinstance(f, _ScannerNoiseFilter) for f in wrapper_logger.filters):
        wrapper_logger.addFilter(_ScannerNoiseFilter())


_silence_ib_insync_info_logs()


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _ibkr_lock_path(port: int) -> Path:
    """回傳每個 Gateway 對應的本機程序鎖路徑。

    多個短生命週期研究程序共用 Gateway 時，可能重疊握手、掃描與斷線；
    此鎖只限制本機自動化程序，不影響手動 IBKR Desktop session。
    """
    root = Path(os.getenv("IBKR_SESSION_LOCK_DIR", "/tmp"))
    return root / f"ibkr-gateway-{port}.lock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mask_account(account: str) -> str:
    if not account:
        return "***"
    if len(account) >= 4:
        return f"{account[:2]}***{account[-2:]}"
    return "***"


def _quote_has_price(payload: Dict[str, Any]) -> bool:
    return any(
        payload.get(k) is not None
        for k in ("bid", "ask", "last", "close", "market_price")
    )


def _compact_error(req_id: int, code: int, message: str) -> Dict[str, Any]:
    return {
        "req_id": req_id,
        "code": code,
        "message": message,
    }


@dataclass
class IBKRQuote:
    market: str
    symbol: str
    provider: str
    asof: str
    session: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    close: Optional[float]
    market_price: Optional[float]
    currency: str
    is_realtime: bool
    is_fallback: bool
    market_data_type: str
    permission_status: str
    warnings: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# 隔夜期貨領先指標標的（read-only delayed；美股盤中/開盤/收盤卡與台股盤前定調用）
OVERNIGHT_FUTURE_SPECS = (
    ("ES", "CME", "S&P500 期貨"),
    ("NQ", "CME", "Nasdaq 期貨"),
)


@dataclass
class IBKRFutureQuote:
    symbol: str
    local_symbol: str
    name: str
    provider: str
    asof: str
    last: Optional[float]
    close: Optional[float]
    change_pct: Optional[float]
    market_data_type: str
    is_realtime: bool
    permission_status: str
    warnings: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IBKRAccountSnapshot:
    provider: str
    asof: str
    connected: bool
    readonly: bool
    account_values_count: int
    accounts_masked: List[str]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IBKRScanRow:
    rank: int
    symbol: str
    con_id: int
    exchange: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IBKRHistoricalBars:
    symbol: str
    bar_size: str
    asof: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class IBKRReadOnlyProvider:
    """
    Minimal IBKR read-only provider.

    This provider is intentionally US-only at this stage.
    It does not integrate into TW market data line.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 301,
        timeout: float = 10.0,
        allow_non_local: bool = False,
        light: bool = True,
    ) -> None:
        if host not in LOCAL_HOSTS and not allow_non_local:
            raise ValueError(
                "Refuse non-local IBKR host by default. "
                "Use allow_non_local only after explicit security review."
            )

        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self.readonly = True
        self.light = light
        self.session_lock_timeout = max(
            0.0, float(os.getenv("IBKR_SESSION_LOCK_TIMEOUT", "20"))
        )
        self._session_lock_handle = None

        self._ib = IB()
        # ib_insync 阻塞請求預設 RequestTimeout=0（永久等待）。
        # 排程市場卡在 Gateway 接受連線但不回覆合約、掃描或歷史請求時，
        # 必須降級而不是持續卡住。
        self._ib.RequestTimeout = timeout
        self._errors: List[Dict[str, Any]] = []
        self._farm_events: List[Dict[str, Any]] = []

        def on_error(req_id, code, message, contract):
            self._errors.append(_compact_error(req_id, code, message))
            if code in {2104, 2108, 2119} and "farm" in str(message).lower():
                self._farm_events.append({
                    "code": code,
                    "message": str(message),
                    "monotonic": time.monotonic(),
                })
                self._farm_events[:] = self._farm_events[-20:]

        self._ib.errorEvent += on_error

        # 連線輕量化：ib_insync connectAsync 連線後會強制發 reqPositions /
        # reqAccountUpdates / reqExecutions。read-only 行情/掃描/歷史用不到這些，
        # 而它們在 Gateway 不穩窗口會 timeout 並把連線拖崩（2026-06-19 05:1x 事故）。
        # 將其改為 no-op，讓連線只做 socket 握手＋行情請求，不觸發崩潰。
        # 注意：light=True 時 account_snapshot()/accountValues() 會空（read-only
        # enrichment 不用；研究 include_account_snapshot 預設 False）。需帳戶資料
        # 時以 light=False 建構。
        if light:
            self._make_connect_light()

    def _make_connect_light(self) -> None:
        async def _empty(*_a, **_k):
            return []

        self._ib.reqPositionsAsync = _empty
        self._ib.reqAccountUpdatesAsync = _empty
        self._ib.reqAccountUpdatesMultiAsync = _empty
        self._ib.reqExecutionsAsync = _empty

    def connect(self) -> bool:
        if self._ib.isConnected():
            return True

        lock_path = _ibkr_lock_path(self.port)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        deadline = time.monotonic() + self.session_lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"IBKR Gateway session lock busy for port {self.port}"
                        )
                    time.sleep(0.25)

            self._session_lock_handle = handle
            self._ib.connect(
                self.host,
                self.port,
                clientId=self.client_id,
                readonly=True,
                timeout=self.timeout,
            )
        except Exception:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                self._session_lock_handle = None
            raise
        return bool(self._ib.isConnected())

    def disconnect(self) -> None:
        try:
            if self._ib.isConnected():
                self._ib.disconnect()
        finally:
            if self._session_lock_handle is not None:
                try:
                    fcntl.flock(
                        self._session_lock_handle.fileno(), fcntl.LOCK_UN
                    )
                finally:
                    self._session_lock_handle.close()
                    self._session_lock_handle = None

    def __enter__(self) -> "IBKRReadOnlyProvider":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._ib.isConnected())

    def account_snapshot(self) -> IBKRAccountSnapshot:
        """
        Return non-sensitive account summary.

        Full account ids and cash details are intentionally not exposed here.
        """
        if not self.connected:
            self.connect()

        warnings: List[str] = []
        values = self._ib.accountValues()
        accounts = sorted({v.account for v in values if getattr(v, "account", None)})

        if not values:
            warnings.append("accountValues is empty")

        return IBKRAccountSnapshot(
            provider="IBKR",
            asof=_now_iso(),
            connected=self.connected,
            readonly=True,
            account_values_count=len(values),
            accounts_masked=[_mask_account(a) for a in accounts],
            warnings=warnings,
        )

    def fetch_us_stock_quote(
        self,
        symbol: str,
        *,
        market_data_type: str = "delayed",
        exchange: str = "SMART",
        currency: str = "USD",
        sleep_seconds: float = 6.0,
    ) -> IBKRQuote:
        """
        Fetch US stock/ETF quote.

        market_data_type:
        - delayed: IBKR marketDataType=3
        - live: IBKR marketDataType=1

        If live data is not subscribed, permission_status will be
        no_live_subscription and is_realtime will remain False.
        """
        if not self.connected:
            self.connect()

        normalized_type = market_data_type.lower().strip()
        if normalized_type not in {"delayed", "live"}:
            raise ValueError("market_data_type must be 'delayed' or 'live'")

        ibkr_type = 3 if normalized_type == "delayed" else 1
        errors_start = len(self._errors)

        self._ib.reqMarketDataType(ibkr_type)

        contract = Stock(symbol, exchange, currency)
        qualified = self._ib.qualifyContracts(contract)

        if not qualified:
            return IBKRQuote(
                market="US",
                symbol=symbol,
                provider="IBKR",
                asof=_now_iso(),
                session="unknown",
                bid=None,
                ask=None,
                last=None,
                close=None,
                market_price=None,
                currency=currency,
                is_realtime=False,
                is_fallback=False,
                market_data_type=normalized_type,
                permission_status="contract_not_qualified",
                warnings=["contract not qualified"],
                errors=self._errors[errors_start:],
            )

        ticker = self._ib.reqMktData(contract, "", False, False)
        self._ib.sleep(sleep_seconds)

        payload = {
            "bid": _clean_number(ticker.bid),
            "ask": _clean_number(ticker.ask),
            "last": _clean_number(ticker.last),
            "close": _clean_number(ticker.close),
            "market_price": _clean_number(ticker.marketPrice()),
        }

        stage_errors = self._errors[errors_start:]
        warnings: List[str] = []

        error_codes = {e.get("code") for e in stage_errors}

        if 10089 in error_codes:
            permission_status = "no_live_subscription"
            warnings.append("Error 10089: live market data subscription required")
        elif _quote_has_price(payload):
            permission_status = "ok"
        else:
            permission_status = "unavailable"
            warnings.append("quote fields are empty")

        if 10167 in error_codes:
            warnings.append("IBKR is showing delayed market data because live data is not subscribed")

        is_realtime = normalized_type == "live" and permission_status == "ok"

        if normalized_type == "delayed":
            session = "delayed"
        elif is_realtime:
            session = "live"
        else:
            session = "unknown"

        try:
            self._ib.cancelMktData(contract)
        except Exception:
            warnings.append("cancel market data failed")

        return IBKRQuote(
            market="US",
            symbol=symbol,
            provider="IBKR",
            asof=_now_iso(),
            session=session,
            bid=payload["bid"],
            ask=payload["ask"],
            last=payload["last"],
            close=payload["close"],
            market_price=payload["market_price"],
            currency=currency,
            is_realtime=is_realtime,
            is_fallback=False,
            market_data_type=normalized_type,
            permission_status=permission_status,
            warnings=warnings,
            errors=stage_errors,
        )

    def fetch_overnight_futures(
        self,
        specs: tuple = OVERNIGHT_FUTURE_SPECS,
        *,
        market_data_type: str = "delayed",
        sleep_seconds: float = 6.0,
    ) -> List[IBKRFutureQuote]:
        """
        Fetch continuous front-month index futures (e.g. ES, NQ).

        Read-only, delayed by default; used as overnight leading indicators
        for the US market cards. Live type stays subject to subscription
        (Error 10089 -> no_live_subscription, is_realtime stays False).

        change_pct is computed against the prior settlement (close), so it is
        a different convention from yfinance daily-bar change and must be
        labelled delayed, not treated as realtime.
        """
        if not self.connected:
            self.connect()

        normalized_type = market_data_type.lower().strip()
        if normalized_type not in {"delayed", "live"}:
            raise ValueError("market_data_type must be 'delayed' or 'live'")

        ibkr_type = 3 if normalized_type == "delayed" else 1
        self._ib.reqMarketDataType(ibkr_type)

        results: List[IBKRFutureQuote] = []
        for symbol, exchange, name in specs:
            errors_start = len(self._errors)
            contract = ContFuture(symbol, exchange)
            qualified = self._ib.qualifyContracts(contract)

            if not qualified:
                results.append(IBKRFutureQuote(
                    symbol=symbol,
                    local_symbol="",
                    name=name,
                    provider="IBKR",
                    asof=_now_iso(),
                    last=None,
                    close=None,
                    change_pct=None,
                    market_data_type=normalized_type,
                    is_realtime=False,
                    permission_status="contract_not_qualified",
                    warnings=["contract not qualified"],
                    errors=self._errors[errors_start:],
                ))
                continue

            farm_events_start = len(self._farm_events)
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._ib.sleep(sleep_seconds)

            last = _clean_number(ticker.last)
            close = _clean_number(ticker.close)
            market_price = _clean_number(ticker.marketPrice())
            last = last if last is not None and last > 0 else None
            close = close if close is not None and close > 0 else None
            market_price = market_price if market_price is not None and market_price > 0 else None
            if last is None:
                last = market_price

            retry_count = 0
            stage_codes = {e.get("code") for e in self._errors[errors_start:]}
            non_transient_codes = {200, 321, 354, 10089, 10168}
            recent_farm_event = any(
                event.get("code") in {2104, 2108, 2119}
                and time.monotonic() - float(event.get("monotonic") or 0) <= max(30.0, sleep_seconds * 3)
                for event in self._farm_events[max(0, farm_events_start - 5):]
            )
            if (
                last is None
                and close is None
                and not (stage_codes & non_transient_codes)
                and recent_farm_event
            ):
                retry_count = 1
                try:
                    self._ib.cancelMktData(contract)
                except Exception:
                    pass
                self._ib.sleep(min(3.0, max(0.5, sleep_seconds / 2)))
                ticker = self._ib.reqMktData(contract, "", False, False)
                self._ib.sleep(sleep_seconds)
                last = _clean_number(ticker.last)
                close = _clean_number(ticker.close)
                market_price = _clean_number(ticker.marketPrice())
                last = last if last is not None and last > 0 else None
                close = close if close is not None and close > 0 else None
                market_price = market_price if market_price is not None and market_price > 0 else None
                if last is None:
                    last = market_price

            change_pct = None
            if last is not None and close not in (None, 0):
                change_pct = (last - close) / close * 100.0

            stage_errors = self._errors[errors_start:]
            error_codes = {e.get("code") for e in stage_errors}
            warnings: List[str] = []
            if retry_count:
                warnings.append("future quote retried once after market data farm warm-up")

            if 10089 in error_codes or 10168 in error_codes:
                permission_status = "no_live_subscription"
                warnings.append("live market data subscription required")
            elif last is not None or close is not None:
                permission_status = "ok"
            else:
                permission_status = "unavailable"
                warnings.append("future quote fields are empty")

            if 10167 in error_codes:
                warnings.append("IBKR is showing delayed market data because live data is not subscribed")

            is_realtime = normalized_type == "live" and permission_status == "ok" and last is not None

            try:
                self._ib.cancelMktData(contract)
            except Exception:
                warnings.append("cancel market data failed")

            results.append(IBKRFutureQuote(
                symbol=symbol,
                local_symbol=getattr(contract, "localSymbol", "") or "",
                name=name,
                provider="IBKR",
                asof=_now_iso(),
                last=last,
                close=close,
                change_pct=change_pct,
                market_data_type=normalized_type,
                is_realtime=is_realtime,
                permission_status=permission_status,
                warnings=warnings,
                errors=stage_errors,
            ))

        return results

    def fetch_scanner(
        self,
        scan_code: str,
        *,
        location_code: str = "STK.US.MAJOR",
        instrument: str = "STK",
        above_price: Optional[float] = None,
        below_price: Optional[float] = None,
        above_volume: Optional[int] = None,
        market_cap_above: Optional[float] = None,
        number_of_rows: int = 25,
    ) -> List[IBKRScanRow]:
        """
        Run an IBKR market scanner (read-only) and return ranked symbols.

        Scanner needs no market-data subscription. ib_insync's reqScannerData
        is a one-shot snapshot; IBKR emits an informational "scanner
        subscription cancelled" (code 162) when the snapshot ends -- data is
        still returned normally.
        """
        if not self.connected:
            self.connect()

        sub = ScannerSubscription(
            instrument=instrument,
            locationCode=location_code,
            scanCode=scan_code,
            numberOfRows=number_of_rows,
        )
        if above_price is not None:
            sub.abovePrice = above_price
        if below_price is not None:
            sub.belowPrice = below_price
        if above_volume is not None:
            sub.aboveVolume = above_volume
        if market_cap_above is not None:
            sub.marketCapAbove = market_cap_above

        data = self._ib.reqScannerData(sub)

        rows: List[IBKRScanRow] = []
        for i, item in enumerate(data or []):
            details = getattr(item, "contractDetails", None)
            contract = getattr(details, "contract", None)
            if contract is None:
                continue
            rows.append(IBKRScanRow(
                rank=int(getattr(item, "rank", i) or i),
                symbol=contract.symbol,
                con_id=int(getattr(contract, "conId", 0) or 0),
                exchange=(getattr(contract, "primaryExchange", "")
                          or getattr(contract, "exchange", "") or ""),
            ))
        return rows

    def fetch_historical_bars(
        self,
        symbol: str,
        *,
        duration: str = "70 D",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> IBKRHistoricalBars:
        """
        Fetch historical OHLCV bars (read-only). Defaults to daily bars.

        Used for US screener technical filtering (MA / momentum / RS / volume).
        Historical data does not require the live quote subscription.
        """
        if not self.connected:
            self.connect()

        contract = Stock(symbol, exchange, currency)
        qualified = self._ib.qualifyContracts(contract)
        if not qualified:
            return IBKRHistoricalBars(
                symbol=symbol, bar_size=bar_size, asof=_now_iso(),
                rows=[], warnings=["contract not qualified"],
            )

        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            timeout=self.timeout,
        )

        rows = [{
            "date": str(getattr(b, "date", "")),
            "open": _clean_number(b.open),
            "high": _clean_number(b.high),
            "low": _clean_number(b.low),
            "close": _clean_number(b.close),
            "volume": _clean_number(b.volume),
        } for b in (bars or [])]

        warnings: List[str] = []
        if not rows:
            warnings.append("no historical bars returned")

        return IBKRHistoricalBars(
            symbol=symbol, bar_size=bar_size, asof=_now_iso(),
            rows=rows, warnings=warnings,
        )


def fetch_us_stock_quote(
    symbol: str,
    *,
    market_data_type: str = "delayed",
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 301,
    timeout: float = 10.0,
    sleep_seconds: float = 6.0,
) -> IBKRQuote:
    """
    Convenience one-shot US quote fetch.

    This is still read-only and does not route TW symbols.
    """
    with IBKRReadOnlyProvider(
        host=host,
        port=port,
        client_id=client_id,
        timeout=timeout,
    ) as provider:
        return provider.fetch_us_stock_quote(
            symbol,
            market_data_type=market_data_type,
            sleep_seconds=sleep_seconds,
        )


def fetch_overnight_futures(
    specs: tuple = OVERNIGHT_FUTURE_SPECS,
    *,
    market_data_type: str = "delayed",
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 311,
    timeout: float = 10.0,
    sleep_seconds: float = 6.0,
) -> List[IBKRFutureQuote]:
    """
    Convenience one-shot overnight futures fetch (read-only, delayed).

    Still read-only; does not route TW symbols or place orders.
    """
    with IBKRReadOnlyProvider(
        host=host,
        port=port,
        client_id=client_id,
        timeout=timeout,
    ) as provider:
        return provider.fetch_overnight_futures(
            specs,
            market_data_type=market_data_type,
            sleep_seconds=sleep_seconds,
        )
