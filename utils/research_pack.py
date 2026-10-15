from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Callable, Dict, List, Optional

from utils.us_research_metrics import info_sections, technical_metrics


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ResearchPack:
    market: str
    symbol: str
    asof: str
    provider_map: Dict[str, Any]
    quote: Optional[Dict[str, Any]]
    historical: Optional[Dict[str, Any]] = None
    technical: Optional[Dict[str, Any]] = None
    valuation: Optional[Dict[str, Any]] = None
    fundamentals: Optional[Dict[str, Any]] = None
    capital: Optional[Dict[str, Any]] = None
    news: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    macro: Optional[Dict[str, Any]] = None
    freshness: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _technical_snapshot(bars: Any, spy_bars: Any) -> Dict[str, Any]:
    metrics = technical_metrics(
        list(getattr(bars, "rows", None) or []),
        list(getattr(spy_bars, "rows", None) or []),
    )
    warnings = [*(getattr(bars, "warnings", None) or []), *metrics.pop("warnings")]
    available = bool(metrics.pop("available"))
    last_bar = metrics.pop("last_bar")
    rounded = {
        key: round(value, 4 if key in {"last_close", "ma20", "ma50"} else 3)
        if isinstance(value, float) else value
        for key, value in metrics.items()
    }
    return {
        "available": available,
        "provider": "IBKR",
        "asof": last_bar,
        "retrieved_at": getattr(bars, "asof", ""),
        "metrics": rounded if available else {},
        "warnings": warnings,
    }


def _historical_snapshot(bars: Any) -> Dict[str, Any]:
    rows = list(getattr(bars, "rows", None) or [])
    first = rows[0].get("date") if rows else None
    last = rows[-1].get("date") if rows else None
    return {
        "available": bool(rows),
        "provider": "IBKR",
        "asof": str(last) if last is not None else "",
        "retrieved_at": getattr(bars, "asof", ""),
        "bar_size": getattr(bars, "bar_size", ""),
        "row_count": len(rows),
        "first_bar": str(first) if first is not None else None,
        "last_bar": str(last) if last is not None else None,
        "warnings": list(getattr(bars, "warnings", None) or []),
    }


def _info_sections(info: dict[str, Any], retrieved_at: str) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    normalized = info_sections(info)

    def section(name: str) -> Dict[str, Any]:
        item = normalized[name]
        available = bool(item["available"])
        return {
            "available": available,
            "provider": "yfinance",
            "asof": None,
            "retrieved_at": retrieved_at,
            "metrics": item["metrics"],
            "warnings": [] if available else ["provider returned no usable fields"],
        }

    return section("valuation"), section("fundamental"), section("capital")


def _macro_snapshot() -> Dict[str, Any]:
    from utils.macro_calendar import fetch_fed_rss_items, flatten_watch_events, load_macro_config

    asof = _now_iso()
    warnings: List[str] = []
    try:
        config = load_macro_config()
        fed_items = fetch_fed_rss_items(config)
        watch_events = flatten_watch_events(config)
    except Exception as exc:
        fed_items = []
        watch_events = []
        warnings.append(f"macro snapshot unavailable: {type(exc).__name__}")
    if not fed_items:
        warnings.append("Fed RSS returned no current items; configured watchlist is reference-only")
    return {
        "available": bool(fed_items),
        "provider": ["Federal Reserve RSS", "configured macro watchlist"],
        "asof": asof,
        "scope": "official Fed RSS and configured event watchlist; dynamic web macro news not requested",
        "fed_items": [item.to_dict() for item in fed_items],
        "watch_events": [item.to_dict() for item in watch_events],
        "warnings": warnings,
    }


def build_us_research_pack(
    symbol: str,
    *,
    market_data_type: str = "delayed",
    client_id: int = 401,
    include_account_snapshot: bool = False,
    include_equity_events: bool = True,
    include_macro: bool = True,
    persist_equity_events: bool = False,
    dispatch_equity_alerts: bool = False,
    cik: str | None = None,
    provider_factory: Callable[..., Any] | None = None,
    ticker_factory: Callable[[str], Any] | None = None,
) -> ResearchPack:
    """建立唯讀美股 Research Pack，預設不交易、不通知。"""
    from utils.ibkr_provider import IBKRReadOnlyProvider

    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    provider_factory = provider_factory or IBKRReadOnlyProvider
    warnings: List[str] = []
    events: List[Dict[str, Any]] = []
    provider_map: Dict[str, Any] = {
        "quote": "IBKR",
        "historical": "IBKR",
        "technical": "IBKR",
        "valuation": "yfinance",
        "fundamentals": "yfinance",
        "capital": "yfinance",
        "news": [],
        "events": [],
        "macro": [],
    }

    provider_kwargs: Dict[str, Any] = {"client_id": client_id}
    if include_account_snapshot:
        provider_kwargs["light"] = False
    provider = provider_factory(**provider_kwargs)
    with provider as connected_provider:
        quote = connected_provider.fetch_us_stock_quote(
            normalized_symbol,
            market_data_type=market_data_type,
        ).to_dict()
        provider_map["quote"] = quote.get("provider") or "unknown"
        warnings.extend(quote.get("warnings") or [])
        if quote.get("permission_status") != "ok":
            warnings.append(f"quote permission_status={quote.get('permission_status')}")

        try:
            bars = connected_provider.fetch_historical_bars(
                normalized_symbol, duration="70 D", bar_size="1 day",
            )
            spy_bars = bars if normalized_symbol == "SPY" else connected_provider.fetch_historical_bars(
                "SPY", duration="70 D", bar_size="1 day",
            )
            historical = _historical_snapshot(bars)
            technical = _technical_snapshot(bars, spy_bars)
        except Exception as exc:
            warning = f"IBKR historical/technical unavailable: {type(exc).__name__}"
            warnings.append(warning)
            retrieved_at = _now_iso()
            historical = {
                "available": False, "provider": "IBKR", "asof": "",
                "retrieved_at": retrieved_at, "warnings": [warning],
            }
            technical = {
                "available": False, "provider": "IBKR", "asof": "",
                "retrieved_at": retrieved_at, "metrics": {}, "warnings": [warning],
            }

        if include_account_snapshot:
            account = connected_provider.account_snapshot().to_dict()
            provider_map["account"] = account.get("provider")
            warnings.append("account snapshot is available but not included in Research Pack payload")

    ticker = None
    info: dict[str, Any] = {}
    info_asof = _now_iso()
    ticker_failed = False
    ticker_factory_injected = ticker_factory is not None
    try:
        if ticker_factory is None:
            import yfinance as yf
            ticker_factory = yf.Ticker
        ticker = ticker_factory(normalized_symbol)
        info = getattr(ticker, "info", None) or {}
    except Exception as exc:
        ticker_failed = True
        warnings.append(f"yfinance fundamentals unavailable: {type(exc).__name__}")
    valuation, fundamentals, capital = _info_sections(info, info_asof)

    if include_equity_events:
        from utils.us_equity_events import collect_us_equity_events
        if ticker_failed:
            events = []
            event_warnings = [f"yfinance events unavailable for {normalized_symbol}: ticker initialization failed"]
        else:
            events, event_warnings = collect_us_equity_events(
                normalized_symbol,
                cik=cik,
                ticker_factory=ticker_factory,
                ticker=ticker,
                cache_enabled=not ticker_factory_injected,
            )
        warnings.extend(event_warnings)
        news_events = [event for event in events if event.get("event_type") == "news"]
        provider_map["news"] = sorted({
            str(event.get("provider") or "unknown") for event in news_events
        })
        provider_map["events"] = sorted({
            str(event.get("provider") or "unknown") for event in events
        })
        if persist_equity_events:
            try:
                from utils.us_event_router import high_priority_events, save_alert_queue, save_event_routes
                save_event_routes(events)
                alert_path = save_alert_queue(events)
                provider_map["high_priority_event_count"] = len(high_priority_events(events))
                if alert_path:
                    provider_map["alert_queue"] = alert_path
            except Exception as exc:
                warnings.append(f"event route persistence failed: {type(exc).__name__}")
        if dispatch_equity_alerts:
            from utils.us_alert_dispatcher import dispatch_events
            provider_map["alert_dispatch"] = dispatch_events(events)

    macro = _macro_snapshot() if include_macro else None
    if macro:
        provider_map["macro"] = macro.get("provider") or []
        warnings.extend(macro.get("warnings") or [])

    freshness = {
        "quote": quote.get("asof"),
        "historical": historical.get("asof"),
        "technical": technical.get("asof"),
        "valuation": valuation.get("asof"),
        "fundamentals": fundamentals.get("asof"),
        "capital": capital.get("asof"),
        "events": max((str(event.get("retrieved_at") or "") for event in events), default=""),
        "macro": macro.get("asof") if macro else None,
    }
    return ResearchPack(
        market="US",
        symbol=normalized_symbol,
        asof=_now_iso(),
        provider_map=provider_map,
        quote=quote,
        historical=historical,
        technical=technical,
        valuation=valuation,
        fundamentals=fundamentals,
        capital=capital,
        news=[event for event in events if event.get("event_type") == "news"],
        events=events,
        macro=macro,
        freshness=freshness,
        warnings=warnings,
    )


def _pack_to_dict(pack: ResearchPack | Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(pack, ResearchPack):
        return pack.to_dict()
    if hasattr(pack, "to_dict"):
        return dict(pack.to_dict())
    return dict(pack or {})


def _format_scalar(value: Any) -> str:
    if value is None:
        return "None"
    if value == "":
        return "（空）"
    return str(value)


def _section_lines(name: str, section: Any) -> List[str]:
    data = section if isinstance(section, dict) else {}
    metrics = data.get("metrics") or {}
    lines = [
        f"【{name}】",
        f"- available: {bool(data) and bool(data.get('available'))}",
        f"- provider: {_format_scalar(data.get('provider'))}",
        f"- asof: {_format_scalar(data.get('asof'))}",
        f"- retrieved_at: {_format_scalar(data.get('retrieved_at'))}",
    ]
    for key, value in metrics.items():
        lines.append(f"- {key}: {_format_scalar(value)}")
    return lines


def format_research_pack_for_prompt(pack: ResearchPack | Dict[str, Any]) -> str:
    """格式化來源、新鮮度與延遲／即時邊界，供 Research W 使用。"""
    data = _pack_to_dict(pack)
    quote = data.get("quote") or {}
    provider_map = data.get("provider_map") or {}
    news = data.get("news") or []
    events = data.get("events") or []
    historical = data.get("historical") or {}
    technical = data.get("technical") or {}
    valuation = data.get("valuation") or {}
    fundamentals = data.get("fundamentals") or {}
    capital = data.get("capital") or {}
    macro = data.get("macro") or {}
    freshness = data.get("freshness") or {}
    warnings = data.get("warnings") or []

    quote_warnings = quote.get("warnings") or []
    section_warnings: List[str] = []
    for section in (historical, technical, valuation, fundamentals, capital, macro):
        if isinstance(section, dict):
            section_warnings.extend(section.get("warnings") or [])
    all_warnings = list(dict.fromkeys(str(w) for w in [*warnings, *quote_warnings, *section_warnings] if w))

    lines = [
        "【Research Pack（結構化資料，供 Research W 使用）】",
        f"- market: {_format_scalar(data.get('market'))}",
        f"- symbol: {_format_scalar(data.get('symbol'))}",
        f"- asof: {_format_scalar(data.get('asof'))}",
        f"- provider_map.quote: {_format_scalar(provider_map.get('quote'))}",
        f"- provider_map.technical: {_format_scalar(provider_map.get('technical'))}",
        f"- provider_map.fundamentals: {_format_scalar(provider_map.get('fundamentals'))}",
        f"- provider_map.news: {_format_scalar(provider_map.get('news'))}",
        f"- provider_map.macro: {_format_scalar(provider_map.get('macro'))}",
        "",
        "【quote】",
        f"- provider: {_format_scalar(quote.get('provider'))}",
        f"- asof: {_format_scalar(quote.get('asof'))}",
        f"- market_data_type: {_format_scalar(quote.get('market_data_type'))}",
        f"- session: {_format_scalar(quote.get('session'))}",
        f"- is_realtime: {_format_scalar(quote.get('is_realtime'))}",
        f"- permission_status: {_format_scalar(quote.get('permission_status'))}",
        f"- bid: {_format_scalar(quote.get('bid'))}",
        f"- ask: {_format_scalar(quote.get('ask'))}",
        f"- last: {_format_scalar(quote.get('last'))}",
        f"- close: {_format_scalar(quote.get('close'))}",
        f"- market_price: {_format_scalar(quote.get('market_price'))}",
        "",
        "【historical】",
        f"- available: {bool(historical.get('available'))}",
        f"- provider: {_format_scalar(historical.get('provider'))}",
        f"- asof: {_format_scalar(historical.get('asof'))}",
        f"- retrieved_at: {_format_scalar(historical.get('retrieved_at'))}",
        f"- row_count: {_format_scalar(historical.get('row_count'))}",
        f"- first_bar: {_format_scalar(historical.get('first_bar'))}",
        f"- last_bar: {_format_scalar(historical.get('last_bar'))}",
        "",
    ]
    for name, section in (
        ("technical", technical),
        ("valuation", valuation),
        ("fundamentals", fundamentals),
        ("capital", capital),
    ):
        lines.extend(_section_lines(name, section))
        lines.append("")
    lines.extend([
        "【news / events / macro】",
        f"- news_count: {len(news)}",
        f"- fundamentals_available: {bool(fundamentals.get('available'))}",
        f"- event_count: {len(events)}",
    ])
    for index, event in enumerate(events[:12], start=1):
        lines.append(
            f"- event_{index}: type={_format_scalar(event.get('event_type'))}; "
            f"severity={_format_scalar(event.get('severity'))}; "
            f"direction={_format_scalar(event.get('direction'))}; "
            f"source={_format_scalar(event.get('source'))}; "
            f"provider={_format_scalar(event.get('provider'))}; "
            f"published_at={_format_scalar(event.get('published_at'))}; "
            f"scheduled_at={_format_scalar(event.get('scheduled_at'))}; "
            f"title={_format_scalar(event.get('title'))}; "
            f"summary={_format_scalar(event.get('summary'))}; "
            f"url={_format_scalar(event.get('url'))}; "
            f"data={_format_scalar(event.get('data'))}"
        )
    lines.extend([
        f"- macro_available: {bool(macro.get('available'))}",
        f"- macro_asof: {_format_scalar(macro.get('asof'))}",
        f"- macro_scope: {_format_scalar(macro.get('scope'))}",
        f"- fed_item_count: {len(macro.get('fed_items') or [])}",
    ])
    for index, item in enumerate((macro.get("fed_items") or [])[:8], start=1):
        lines.append(
            f"- fed_{index}: published_at={_format_scalar(item.get('published_at'))}; "
            f"title={_format_scalar(item.get('title'))}"
        )
    lines.append(f"- macro_watch_event_count: {len(macro.get('watch_events') or [])}")
    for index, event in enumerate((macro.get("watch_events") or [])[:8], start=1):
        lines.append(
            f"- macro_watch_{index}: importance={_format_scalar(event.get('importance'))}; "
            f"label={_format_scalar(event.get('label'))}"
        )
    lines.extend(["", "【freshness】"])
    lines.extend(f"- {key}: {_format_scalar(value)}" for key, value in freshness.items())
    lines.extend([
        "",
        "【Research W 使用規則】",
        "- delayed 或 is_realtime=False 的行情不得寫成即時行情。",
        "- permission_status=no_live_subscription 代表目前沒有 IBKR API 即時行情訂閱，不可用模型自行補即時價。",
        "- available=False 或 count=0 的 section 不得補寫成已有事實；只能改用明確標示的其他來源或標資料不足。",
        "- 各 section 的 asof/freshness 不同，不得以 Research Pack 外層時間冒充所有資料時間。",
        "- 本資料包只供研究判讀，不是自動交易或下單指令。",
    ])
    if all_warnings:
        lines.extend(["", "【warnings】"])
        lines.extend(f"- {warning}" for warning in all_warnings)
    return "\n".join(lines)


def _cli_summary(pack: ResearchPack) -> Dict[str, Any]:
    data = pack.to_dict()
    section_warnings: List[str] = []
    for key in ("historical", "technical", "valuation", "fundamentals", "capital", "macro"):
        section_warnings.extend((data.get(key) or {}).get("warnings") or [])
    all_warnings = list(dict.fromkeys(str(w) for w in [*(data.get("warnings") or []), *section_warnings] if w))
    return {
        "market": data["market"],
        "symbol": data["symbol"],
        "asof": data["asof"],
        "readonly": True,
        "quote": {
            "provider": (data.get("quote") or {}).get("provider"),
            "market_data_type": (data.get("quote") or {}).get("market_data_type"),
            "is_realtime": (data.get("quote") or {}).get("is_realtime"),
            "permission_status": (data.get("quote") or {}).get("permission_status"),
            "last": (data.get("quote") or {}).get("last"),
            "close": (data.get("quote") or {}).get("close"),
        },
        "sections": {
            key: data.get(key)
            for key in ("historical", "technical", "valuation", "fundamentals", "capital", "macro")
        },
        "news": data.get("news") or [],
        "events": data.get("events") or [],
        "news_count": len(data.get("news") or []),
        "event_count": len(data.get("events") or []),
        "freshness": data.get("freshness") or {},
        "warnings": all_warnings,
        "side_effects": {
            "trading": False,
            "llm": False,
            "notification": False,
            "event_persistence": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only US Research Pack data snapshot")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market-data-type", choices=("delayed", "live"), default="delayed")
    parser.add_argument("--client-id", type=int, default=491)
    parser.add_argument("--no-events", action="store_true")
    parser.add_argument("--no-macro", action="store_true")
    args = parser.parse_args()
    pack = build_us_research_pack(
        args.symbol,
        market_data_type=args.market_data_type,
        client_id=args.client_id,
        include_equity_events=not args.no_events,
        include_macro=not args.no_macro,
        persist_equity_events=False,
        dispatch_equity_alerts=False,
    )
    print(json.dumps(_cli_summary(pack), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
