"""Controlled dispatch for high-priority US equity events."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable

import yaml

from utils.us_event_router import score_event


_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "us_equity_alerts.yaml"
_DEFAULT_STATE = Path(__file__).resolve().parent.parent / "logs" / "us_equity_alert_state.json"


def load_alert_config(path: str | Path = _DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _load_state(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _expand_watch_symbols(config: dict[str, Any]) -> set[str]:
    explicit = {str(s).upper() for s in (config.get("watch_symbols") or []) if str(s).strip()}
    aliases = config.get("watch_aliases") or {}
    expanded = set(explicit)
    for name in explicit:
        values = aliases.get(name) if isinstance(aliases, dict) else None
        if isinstance(values, (list, tuple, set)):
            expanded.update(str(value).upper() for value in values if str(value).strip())
    return expanded


def format_alert(event: dict[str, Any]) -> tuple[str, str]:
    symbol = str(event.get("symbol") or "?")
    title = str(event.get("title") or "重大事件")
    severity = str(event.get("severity") or "high").upper()
    direction = str(event.get("direction") or "unknown")
    source = str(event.get("source") or "unknown")
    url = str(event.get("url") or "")
    body = [
        f"{symbol} 美股事件告警｜{severity}",
        f"方向：{direction}",
        f"事件：{title}",
        f"來源：{source}",
    ]
    if url:
        body.append(f"連結：{url}")
    body.append("僅供研究與風險判讀，非交易指令。")
    return f"🚨 {symbol} 重大事件", "\n".join(body)


def dispatch_events(
    events: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    state_path: str | Path = _DEFAULT_STATE,
    sender: Callable[[str, str], bool] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Filter, cooldown and optionally send events; disabled is side-effect free."""
    cfg = config or load_alert_config()
    enabled = bool(cfg.get("enabled", False)) and os.getenv(
        "US_EQUITY_ALERT_PUSH_ENABLED", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if enabled and sender is None:
        from sim._notify import send_dm
        sender = send_dm
    min_severity = str(cfg.get("min_severity", "high")).lower()
    min_rank = _SEVERITY_RANK.get(min_severity, 3)
    watch = _expand_watch_symbols(cfg)
    cooldown = max(0.0, float(cfg.get("cooldown_seconds", 1800)))
    current = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    path = Path(state_path)
    state = _load_state(path)
    candidates: list[dict[str, Any]] = []
    sent: list[str] = []
    skipped: list[str] = []

    for raw in events:
        event = score_event(raw)
        symbol = str(event.get("symbol") or "").upper()
        if not symbol or (watch and symbol not in watch):
            continue
        if _SEVERITY_RANK.get(str(event.get("severity")), 0) < min_rank:
            continue
        key = "|".join((symbol, str(event.get("title")), str(event.get("published_at"))))
        candidates.append(event)
        if current - state.get(key, 0) < cooldown:
            skipped.append(key)
            continue
        if not enabled:
            continue
        title, content = format_alert(event)
        ok = bool(sender(title, content)) if sender else False
        if ok:
            state[key] = current
            sent.append(key)

    if enabled and sent:
        _save_state(path, state)
    return {
        "enabled": enabled,
        "candidate_count": len(candidates),
        "sent_count": len(sent),
        "cooldown_skipped_count": len(skipped),
        "sent_keys": sent,
    }
