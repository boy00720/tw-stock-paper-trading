"""automation/sim/advisor/auto_trail.py — 階梯式 trailing stop 自動上移

每輪 dispatch / run_advisor_once 撮合後呼叫 maybe_raise_trailing_stops()：
- 讀取持倉 avg_cost + 當前 quote
- 若浮盈達到某階梯門檻，且既有 auto-stop 低於該階梯的新 stop 價，自動上移

階梯設計：
  浮盈 +5%  → stop 上移到成本價 (0%)
  浮盈 +8%  → stop 上移到 +3%
  浮盈 +12% → stop 上移到 +7%
  浮盈 +15% → stop 上移到 +10%

只升不降：如果既有 auto-stop 已高於階梯計算的新 stop，不做任何改動。
"""

from __future__ import annotations

import logging
from typing import Any

from sim.engine import MatchEngine
from sim.models import OrderSide, OrderType

logger = logging.getLogger(__name__)

AUTO_STOP_NOTE_PREFIX = "[auto-stop]"

# 階梯定義：(浮盈門檻 pct, stop 上移到 avg_cost * (1 + target_pct))
_TRAIL_STEPS: list[tuple[float, float]] = [
    (0.05, 0.00),   # +5% → stop at cost
    (0.08, 0.03),   # +8% → stop at +3%
    (0.12, 0.07),   # +12% → stop at +7%
    (0.15, 0.10),   # +15% → stop at +10%
]


def _find_auto_stop_for(engine: MatchEngine, symbol: str):
    """找到該 symbol 的 auto-stop pending order（應只有一筆）。"""
    for oid, o in engine.pending_orders.items():
        if (
            o.symbol == symbol
            and o.side == OrderSide.SELL
            and o.type == OrderType.STOP
            and (o.notes or "").startswith(AUTO_STOP_NOTE_PREFIX)
        ):
            return oid, o
    return None, None


def _calc_trail_stop(avg_cost: float, current_price: float) -> float | None:
    """根據浮盈幅度，回傳階梯式 trailing stop 價格（若未達任何門檻回 None）。"""
    if avg_cost <= 0 or current_price <= avg_cost:
        return None
    gain_pct = (current_price - avg_cost) / avg_cost
    new_stop = None
    for threshold, target in _TRAIL_STEPS:
        if gain_pct >= threshold:
            new_stop = avg_cost * (1.0 + target)
    return new_stop


def maybe_raise_trailing_stops(
    engine: MatchEngine,
    quotes: dict[str, float],
) -> list[dict[str, Any]]:
    """檢查所有持倉，若浮盈達門檻且 auto-stop 可上移，自動替換。

    Returns:
        audit 紀錄列表，每筆包含 symbol / old_stop / new_stop / gain_pct。
    """
    if not engine or not getattr(engine, "account", None):
        return []
    positions = getattr(engine.account, "positions", None) or {}
    if not positions:
        return []

    records: list[dict[str, Any]] = []

    for sym, pos in positions.items():
        qty = getattr(pos, "qty", 0)
        if qty <= 0:
            continue
        avg_cost = float(getattr(pos, "avg_cost", 0))
        if avg_cost <= 0:
            continue
        current_price = quotes.get(sym)
        if current_price is None or current_price <= 0:
            continue

        new_stop = _calc_trail_stop(avg_cost, current_price)
        if new_stop is None:
            continue

        # 找既有 auto-stop
        oid, existing = _find_auto_stop_for(engine, sym)
        if existing is None:
            continue  # 沒有 auto-stop 的不動（可能已被 LLM 手動管理）

        old_stop = existing.price
        rounded_new_stop = round(new_stop, 2)
        # 只升不降：用實際送單價比較，避免 raw 值略高但四捨五入後不變時反覆重掛。
        if rounded_new_stop <= old_stop:
            continue

        # 上移：cancel 舊的 + submit 新的
        engine.cancel_order(oid)
        from sim.models import Order
        new_order = Order(
            symbol=sym,
            side=OrderSide.SELL,
            type=OrderType.STOP,
            qty=qty,
            price=rounded_new_stop,
            notes=f"{AUTO_STOP_NOTE_PREFIX} trail raised from {old_stop:.2f}",
        )
        engine.submit_order(new_order)

        gain_pct = (current_price - avg_cost) / avg_cost
        records.append({
            "symbol": sym,
            "avg_cost": avg_cost,
            "current_price": current_price,
            "gain_pct": round(gain_pct * 100, 2),
            "old_stop": old_stop,
            "new_stop": rounded_new_stop,
            "new_order_id": new_order.order_id,
        })
        logger.info(
            "[auto_trail] %s trail raised: stop %.2f → %.2f (gain=+%.1f%%)",
            sym, old_stop, new_stop, gain_pct * 100,
        )

    return records
