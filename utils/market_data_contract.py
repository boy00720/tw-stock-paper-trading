"""Shared market-data semantics for multi-provider research outputs.

The system may combine IBKR delayed quotes, Yahoo daily bars and other feeds.
Every change value must therefore declare what it is measured against.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(frozen=True)
class ChangeMetric:
    value_pct: Optional[float]
    basis: str
    source: str
    asof: str
    is_realtime: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def percent_change(
    current: Optional[float],
    reference: Optional[float],
    *,
    basis: str,
    source: str,
    is_realtime: bool = False,
    asof: Optional[str] = None,
) -> ChangeMetric:
    """Calculate a labelled percentage change without silently mixing bases."""
    value = None
    if current is not None and reference not in (None, 0):
        value = (float(current) - float(reference)) / float(reference) * 100.0
    return ChangeMetric(
        value_pct=value,
        basis=basis,
        source=source,
        asof=asof or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        is_realtime=is_realtime,
    )


def attach_change_metric(
    payload: dict[str, Any],
    *,
    current_key: str,
    reference_key: str,
    basis: str,
    source: str,
    is_realtime: bool = False,
) -> dict[str, Any]:
    """Return a copy with an explicit ``change_metric`` object."""
    out = dict(payload)
    metric = percent_change(
        out.get(current_key),
        out.get(reference_key),
        basis=basis,
        source=source,
        is_realtime=is_realtime,
        asof=out.get("asof"),
    )
    out["change_metric"] = metric.to_dict()
    return out
