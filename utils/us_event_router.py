"""路由並評分正規化後的美股事件。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable


_HIGH_NEGATIVE = re.compile(
    r"(?:\b(?:restatement|fraud|investigation|bankruptcy|dilution|misses|downgrade|layoff|recall)\b|"
    r"\b(?:guidance[ -]cut|cuts[ -]guidance|missed[ -]estimates)\b|"
    r"\b(?:secondary|equity|stock|share|public)[ -]offering\b|"
    r"訴訟|調查|破產|稀釋|下修|造假)",
    re.IGNORECASE,
)
_HIGH_POSITIVE = re.compile(
    r"(?:\b(?:raises[ -]guidance|beats[ -]estimates|approved|contract[ -]award|acquisition)\b|"
    r"上修|超越預期|核准|重大合約|收購)",
    re.IGNORECASE,
)
_MEDIUM = re.compile(
    r"(?:\b(?:earnings|quarterly[ -]results|analyst|analyst[ -]rating|credit[ -]rating|"
    r"rating[ -](?:upgrade|downgrade)|target[ -]price|merger|lawsuit)\b|"
    r"財報|評級|目標價|併購|訴訟)",
    re.IGNORECASE,
)
_NEGATIVE_DIRECTION = re.compile(r"\b(?:downgrade|sell|underperform)\b|下修", re.IGNORECASE)
_POSITIVE_DIRECTION = re.compile(r"\b(?:upgrade|buy|outperform)\b|上修", re.IGNORECASE)


def score_event(event: dict[str, Any]) -> dict[str, Any]:
    """為事件加入可重現的嚴重度與方向標籤。"""
    text = " ".join(str(event.get(key) or "") for key in ("title", "summary"))
    event_type = str(event.get("event_type") or "")
    severity = "low"
    direction = "unknown"
    if event_type == "sec_filing" and str((event.get("data") or {}).get("form")) == "8-K":
        severity = "high"
    elif _HIGH_NEGATIVE.search(text):
        severity, direction = "high", "negative"
    elif _HIGH_POSITIVE.search(text):
        severity, direction = "high", "positive"
    elif _MEDIUM.search(text) or event_type in {"earnings", "analyst_rating"}:
        severity = "medium"
    if direction == "unknown" and severity == "medium":
        if _NEGATIVE_DIRECTION.search(text):
            direction = "negative"
        elif _POSITIVE_DIRECTION.search(text):
            direction = "positive"

    result = dict(event)
    result["severity"] = severity
    result["direction"] = direction
    result["scoring_basis"] = "keyword_rules_v1"
    return result


def route_events_by_symbol(events: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    routed: dict[str, list[dict[str, Any]]] = {}
    for raw in events:
        scored = score_event(raw)
        symbol = str(scored.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        routed.setdefault(symbol, []).append(scored)
    for values in routed.values():
        values.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    return routed


def save_event_routes(
    events: Iterable[dict[str, Any]],
    *,
    root: str | Path = "logs/us_equity_events",
    asof: str | None = None,
) -> dict[str, str]:
    """每個標的保存一份 JSON，並回傳標的與路徑對照。"""
    date_key = (asof or datetime.now().astimezone().isoformat())[:10]
    base = Path(root) / date_key
    base.mkdir(parents=True, exist_ok=True)
    output: dict[str, str] = {}
    for symbol, routed in route_events_by_symbol(events).items():
        path = base / f"{symbol.replace('/', '_')}.json"
        path.write_text(json.dumps(routed, ensure_ascii=False, indent=2), encoding="utf-8")
        output[symbol] = str(path)
    return output


def high_priority_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    routed = route_events_by_symbol(events)
    return [event for items in routed.values() for event in items if event["severity"] == "high"]


def save_alert_queue(
    events: Iterable[dict[str, Any]],
    *,
    root: str | Path = "logs/us_equity_alerts",
    asof: str | None = None,
) -> str | None:
    """保存去重後的高優先候選，不進行發送。"""
    alerts = high_priority_events(events)
    if not alerts:
        return None
    date_key = (asof or datetime.now().astimezone().isoformat())[:10]
    path = Path(root) / f"{date_key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            existing = []
    seen = {(str(item.get("symbol")), str(item.get("title")), str(item.get("published_at"))) for item in existing}
    for alert in alerts:
        key = (str(alert.get("symbol")), str(alert.get("title")), str(alert.get("published_at")))
        if key not in seen:
            existing.append(alert)
            seen.add(key)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
