"""automation/sim/advisor/auto_stop.py — BUY 成交後自動掛 STOP SELL（M6 階段二 P1）

對外一個函數：
- sync_auto_stops_after_fills(engine, fills, stop_loss_by_symbol) -> list[dict]

設計：
- 每個 fill 涉及的 symbol：先 cancel 該 symbol 既有 auto-stop，再依「新持倉量 +
  最新 stop_loss」重新 submit 一筆 STOP SELL；持倉歸零則只 cancel。
- 識別 auto-stop 靠 Order.notes 開頭的 `[auto-stop]`，避免誤 cancel 使用者手動掛的 STOP。
- BUY/SELL 都觸發同步，可同時處理「加倉換 stop_loss」與「部分減倉對齊 qty」。
- 新 stop_loss 取自本輪 BUY decision；若該 symbol 本輪沒有 BUY decision（例如純 SELL），
  則回退到被取消的舊 auto-stop 的觸發價，避免減倉時意外卸下保護。

範圍限制（M6 階段二 P1）：
- 只覆蓋 advisor 進程「即時撮合」這條路徑（run_advisor_once 內 match() 之後）。
- LMT 跨進程 dispatch 撮合路徑暫不接（advisor 已退出、stop_loss 拿不到）。
  P2 階段考慮把 stop_loss 寫進 Order.notes 讓 dispatch 也能還原。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sim.engine import MatchEngine
from sim.models import Fill, Order, OrderSide, OrderType

logger = logging.getLogger(__name__)

AUTO_STOP_NOTE_PREFIX = "[auto-stop]"


def _is_auto_stop(order: Order) -> bool:
    return (
        order.side == OrderSide.SELL
        and order.type == OrderType.STOP
        and (order.notes or "").startswith(AUTO_STOP_NOTE_PREFIX)
    )


def _cancel_auto_stops_for(engine: MatchEngine, symbol: str) -> Optional[float]:
    """取消該 symbol 既有的 auto-stop；回傳被取消的觸發價（多筆取最後一筆）。"""
    cancelled_price: Optional[float] = None
    for oid in list(engine.pending_orders.keys()):
        o = engine.pending_orders.get(oid)
        if o is None or o.symbol != symbol or not _is_auto_stop(o):
            continue
        cancelled_price = o.price
        engine.cancel_order(oid)
    return cancelled_price


def _submit_auto_stop(
    engine: MatchEngine,
    symbol: str,
    qty: int,
    stop_price: float,
    *,
    reason: str = "",
) -> Order:
    note = f"{AUTO_STOP_NOTE_PREFIX} {reason}".strip()
    order = Order(
        symbol=symbol,
        side=OrderSide.SELL,
        type=OrderType.STOP,
        qty=qty,
        price=stop_price,
        notes=note,
    )
    return engine.submit_order(order)


def sync_auto_stops_after_fills(
    engine: MatchEngine,
    fills: list[Fill],
    stop_loss_by_symbol: dict[str, float],
) -> list[dict[str, Any]]:
    """每個 fill 涉及的 symbol 都重算 auto-stop。

    Args:
        engine: 已執行 apply_fills 的 MatchEngine（讀 account.positions 拿最新持倉）
        fills: 本輪的 fills（用來決定哪些 symbol 需要同步）
        stop_loss_by_symbol: 本輪 BUY decision 的 stop_loss（同檔多筆取最後一筆）

    Returns:
        每個 symbol 一筆 audit 紀錄：
            {
              "symbol": str,
              "old_stop": float | None,       # 被取消的舊觸發價
              "qty_after_fill": int,           # apply_fills 後該 symbol 持倉量
              "new_stop": float | None,        # 新掛 auto-stop 的觸發價（None = 沒重掛）
              "new_order_id": str | None,
              "new_order_status": str | None,
              "skip_reason": str | None,       # 沒重掛的原因
              "error": str | None,
            }
    """
    audit_records: list[dict[str, Any]] = []
    symbols = sorted({f.symbol for f in fills})

    for sym in symbols:
        old_price = _cancel_auto_stops_for(engine, sym)
        pos = engine.account.positions.get(sym)
        qty = pos.qty if pos else 0

        # 新 stop_loss 來源優先序：本輪 BUY decision > 舊 auto-stop 價
        new_stop = stop_loss_by_symbol.get(sym)
        if new_stop is None:
            new_stop = old_price

        record: dict[str, Any] = {
            "symbol": sym,
            "old_stop": old_price,
            "qty_after_fill": qty,
            "new_stop": None,
            "new_order_id": None,
            "new_order_status": None,
            "skip_reason": None,
            "error": None,
        }

        if qty <= 0:
            record["skip_reason"] = "position_closed"
            audit_records.append(record)
            continue

        if not new_stop or new_stop <= 0:
            record["skip_reason"] = "no_stop_loss_available"
            audit_records.append(record)
            logger.warning(
                "[auto_stop] %s 持倉 %d 但無 stop_loss 可用（既無新 decision 也無舊 auto-stop）",
                sym, qty,
            )
            continue

        try:
            reason = f"buy_synced sl={new_stop}" if sym in stop_loss_by_symbol else f"sell_resync sl={new_stop}"
            o = _submit_auto_stop(engine, sym, qty, float(new_stop), reason=reason)
            record["new_stop"] = float(new_stop)
            record["new_order_id"] = o.order_id
            record["new_order_status"] = o.status.value
        except Exception as exc:
            logger.exception("[auto_stop] submit_auto_stop 失敗 %s", sym)
            record["error"] = str(exc)[:200]

        audit_records.append(record)

    return audit_records
