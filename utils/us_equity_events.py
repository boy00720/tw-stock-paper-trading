"""供研究使用的美股事件聚合。

所有函式皆為唯讀並回傳正規化記錄。資料源異常時不得讓 Research Pack
假裝資料存在，因此同時回傳記錄與明確警告。
"""

from __future__ import annotations

from datetime import datetime, timezone
from numbers import Number
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)
_EVENT_CACHE_TTL = max(0, int(os.getenv("US_EQUITY_EVENTS_CACHE_TTL_SECONDS", "900")))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _event(symbol: str, event_type: str, *, title: str = "", source: str,
           provider: str = "", published_at: str = "", scheduled_at: str = "",
           retrieved_at: str = "", url: str = "", summary: str = "",
           data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "event_type": event_type,
        "title": title,
        "summary": summary,
        "source": source,
        "provider": provider or source,
        "url": url,
        "published_at": published_at,
        "scheduled_at": scheduled_at,
        "retrieved_at": retrieved_at or _now(),
        "data": data or {},
    }


def _published_at(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, Number) or str(value).strip().isdigit():
        try:
            timestamp = float(value)
            return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, TypeError, ValueError):
            return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


def _frame_row_and_timestamp(frame: Any, position: int) -> tuple[Any, str]:
    row = frame.iloc[position]
    index_value = None
    try:
        index_value = frame.index[position]
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    return row, _published_at(index_value)


def _provider_name(item: dict[str, Any], content: dict[str, Any]) -> str:
    raw = content.get("provider") or item.get("publisher") or "Yahoo Finance"
    if isinstance(raw, dict):
        return str(raw.get("displayName") or raw.get("name") or "Yahoo Finance")
    return str(raw)


def _related_symbols(item: dict[str, Any], content: dict[str, Any]) -> set[str]:
    raw = content.get("relatedTickers") or item.get("relatedTickers") or []
    symbols: set[str] = set()
    for value in raw:
        if isinstance(value, dict):
            value = value.get("symbol") or value.get("ticker")
        if value:
            symbols.add(str(value).strip().upper())
    return symbols


def _company_aliases(symbol: str, ticker: Any) -> set[str]:
    aliases: set[str] = set()
    normalized_symbol = symbol.strip().upper()
    if len(normalized_symbol) >= 3:
        aliases.add(normalized_symbol)
    try:
        info = getattr(ticker, "info", None) or {}
    except Exception:
        info = {}
    for key in ("shortName", "longName", "displayName"):
        name = str(info.get(key) or "").strip()
        if not name:
            continue
        aliases.add(name)
        base = re.sub(
            r",?\s+(?:incorporated|inc\.?|corporation|corp\.?|company|co\.?|limited|ltd\.?)$",
            "", name, flags=re.IGNORECASE,
        ).strip()
        if len(base) >= 3:
            aliases.add(base)
    return aliases


def _is_relevant_news(
    symbol: str,
    item: dict[str, Any],
    content: dict[str, Any],
    aliases: set[str],
) -> tuple[bool, str]:
    related = _related_symbols(item, content)
    normalized = symbol.strip().upper()
    if related:
        return normalized in related, "relatedTickers"

    text = " ".join(str(content.get(key) or item.get(key) or "") for key in ("title", "summary"))
    for alias in aliases:
        if alias == normalized:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.IGNORECASE):
                return True, "symbol_text"
        elif re.search(
            rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        ):
            return True, "company_text"
    return False, "no_ticker_or_company_match"


def fetch_yfinance_events(
    symbol: str,
    *,
    ticker_factory: Callable[[str], Any] | None = None,
    ticker: Any = None,
    cache_enabled: bool | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """取得個股新聞、財報日期及分析師建議快照。

    測試可注入 ``ticker_factory``；yfinance 仍是補充資料源，缺失或限流只記錄警告，
    不阻斷整體流程。
    """
    injected = ticker_factory is not None
    use_cache = (not injected) if cache_enabled is None else cache_enabled
    if ticker_factory is None and ticker is None:
        import yfinance as yf
        ticker_factory = yf.Ticker

    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    cache_key = "us_equity_events_" + re.sub(r"[^A-Za-z0-9_.-]", "_", symbol.upper())
    if use_cache:
        cached = None
        try:
            from utils.ttl_cache import read_json
            cached = read_json(cache_key, ttl_seconds=_EVENT_CACHE_TTL)
        except Exception:
            cached = None
        if isinstance(cached, dict):
            return list(cached.get("events") or []), list(cached.get("warnings") or [])
    try:
        ticker = ticker if ticker is not None else ticker_factory(symbol)
        aliases = _company_aliases(symbol, ticker)
        filtered_news = 0
        for item in (getattr(ticker, "news", None) or [])[:20]:
            content = item.get("content") or item
            title = str(content.get("title") or item.get("title") or "").strip()
            if not title:
                continue
            relevant, relevance_basis = _is_relevant_news(symbol, item, content, aliases)
            if not relevant:
                filtered_news += 1
                continue
            provider = _provider_name(item, content)
            url = str((content.get("canonicalUrl") or {}).get("url") or item.get("link") or "")
            events.append(_event(
                symbol, "news", title=title, source=provider, provider="yfinance", url=url,
                published_at=_published_at(content.get("pubDate") or item.get("providerPublishTime")),
                summary=str(content.get("summary") or item.get("summary") or ""),
                data={"relevance_basis": relevance_basis},
            ))
        if filtered_news:
            warnings.append(f"yfinance news filtered for {symbol}: {filtered_news} unrelated item(s)")

        earnings_dates = getattr(ticker, "earnings_dates", None)
        if earnings_dates is not None and not getattr(earnings_dates, "empty", True):
            first, event_time = _frame_row_and_timestamp(earnings_dates, 0)
            events.append(_event(
                symbol, "earnings", source="yfinance", provider="yfinance",
                title=f"{symbol} earnings calendar",
                scheduled_at=event_time,
                data={str(k): (None if v is None else str(v)) for k, v in first.to_dict().items()},
            ))

        recommendations = getattr(ticker, "recommendations", None)
        if recommendations is not None and not getattr(recommendations, "empty", True):
            first, event_time = _frame_row_and_timestamp(recommendations, -1)
            events.append(_event(
                symbol, "analyst_rating", source="yfinance", provider="yfinance",
                title=f"{symbol} latest analyst recommendation",
                published_at=event_time,
                data={str(k): (None if v is None else str(v)) for k, v in first.to_dict().items()},
            ))
    except Exception as exc:
        warning = f"yfinance events unavailable for {symbol}: {type(exc).__name__}"
        warnings.append(warning)
        logger.warning("%s: %s", warning, exc)
    if use_cache and not any("unavailable" in warning for warning in warnings):
        try:
            from utils.ttl_cache import write_json
            write_json(cache_key, {"events": events, "warnings": warnings})
        except Exception:
            logger.debug("event cache write failed", exc_info=True)
    return events, warnings


def fetch_sec_filings(
    symbol: str,
    *,
    cik: str | None = None,
    session: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """設定合規 User-Agent 時取得近期 SEC 申報。

    SEC 要求可識別的 User-Agent；未設定時直接略過，不發送匿名請求。
    """
    user_agent = os.getenv("SEC_USER_AGENT", "").strip()
    if not user_agent:
        return [], ["SEC filings skipped: SEC_USER_AGENT is not configured"]
    if not cik:
        from utils.us_equity_identity import resolve_cik
        cik = resolve_cik(symbol)
    if not cik:
        return [], [f"SEC filings skipped for {symbol}: CIK mapping unavailable"]

    import requests
    http = session or requests.Session()
    try:
        normalized = str(cik).zfill(10)
        resp = http.get(
            f"https://data.sec.gov/submissions/CIK{normalized}.json",
            headers={"User-Agent": user_agent}, timeout=15,
        )
        resp.raise_for_status()
        recent = (resp.json().get("filings") or {}).get("recent") or {}
        records: list[dict[str, Any]] = []
        forms = {"10-K", "10-Q", "8-K", "20-F", "6-K"}
        for form, date, accession, primary in zip(
            recent.get("form", []), recent.get("filingDate", []),
            recent.get("accessionNumber", []), recent.get("primaryDocument", []),
        ):
            if form not in forms:
                continue
            accession_path = str(accession).replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{int(normalized)}/{accession_path}/{primary}"
            records.append(_event(
                symbol, "sec_filing", title=f"{symbol} {form}", source="SEC", provider="SEC",
                published_at=str(date), url=url,
                data={"form": form, "accession_number": accession},
            ))
            if len(records) >= 20:
                break
        return records, []
    except Exception as exc:
        warning = f"SEC filings unavailable for {symbol}: {type(exc).__name__}"
        logger.warning("%s: %s", warning, exc)
        return [], [warning]


def collect_us_equity_events(
    symbol: str,
    *,
    cik: str | None = None,
    ticker_factory: Callable[[str], Any] | None = None,
    ticker: Any = None,
    cache_enabled: bool | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    events, warnings = fetch_yfinance_events(
        symbol,
        ticker_factory=ticker_factory,
        ticker=ticker,
        cache_enabled=cache_enabled,
    )
    sec_events, sec_warnings = fetch_sec_filings(symbol, cik=cik)
    events.extend(sec_events)
    warnings.extend(sec_warnings)
    from utils.us_event_router import score_event
    events = [score_event(event) for event in events]
    events.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    return events, warnings
