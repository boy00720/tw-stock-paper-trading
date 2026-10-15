"""
automation/sim/_position_math.py — 持倉計算共用邏輯（M5）

設計：與 `engine.MatchEngine._apply_buy` / `_apply_sell` 完全一致的「加權
平均成本（買入手續費攤入 avg_cost；賣出按 avg_cost 算實現損益、avg_cost
不變）」算法，抽出純函數版本給 reports CLI 從 fills.jsonl replay 用。

**規則一致性是 M5_PLAN 風險點 2 的核心**：兩處實作必須行為一致，否則日報
產出的「今日已實現損益」會跟 engine 的 `account.total_realized_pnl` 飄移。
test_reports.py 內含 cross-validation 測試（同一組 fills 餵 engine 與
replay_positions_from_fills，positions/cash_delta/total_realized 必須一致）。

對外只有一個函數：`replay_positions_from_fills(fills) -> dict`。
"""

from __future__ import annotations

from typing import Any, Iterable


def replay_positions_from_fills(fills: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """從 fills 序列重建持倉、現金 delta、累計已實現損益。

    輸入：fills dict 序列（fills.jsonl 解析後的 dict，欄位含 symbol/side/qty/
    price/fee/tax；side 為 "BUY"/"SELL"）。

    回傳 dict：
    - positions: {symbol: {qty, avg_cost, realized_pnl}}
    - cash_delta: float — 從 0 算起的現金變化（買入扣 gross+fee；賣出加
      gross-fee-tax）。Reports CLI 需自己加 initial_cash 才得到當前 cash。
    - total_realized_pnl: float — 累計已實現損益。

    防呆：
    - 缺欄位 / 型別錯誤 / qty<=0 / price<=0 → 該 fill 略過
    - 賣超（持倉不足）→ 該 fill 略過（M3 設計：bot 重啟時若 fills.jsonl 漏
      初始 buy，replay 不應炸；engine 已防呆，這裡照做）
    - side 非 BUY/SELL → 略過
    """
    positions: dict[str, dict[str, Any]] = {}
    cash_delta = 0.0
    total_realized = 0.0

    for fill in fills:
        if not isinstance(fill, dict):
            continue
        symbol = fill.get("symbol")
        side = (fill.get("side") or "").upper()
        if not symbol or side not in ("BUY", "SELL"):
            continue
        try:
            price = float(fill.get("price", 0))
            qty = int(fill.get("qty", 0))
            fee = float(fill.get("fee", 0) or 0)
            tax = float(fill.get("tax", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        gross = price * qty

        if side == "BUY":
            # 買單：cash -= gross + fee（無稅）；avg_cost 加權平均（含手續費）
            cash_delta -= (gross + fee)
            existing = positions.get(symbol)
            if existing is None:
                positions[symbol] = {
                    "qty": qty,
                    "avg_cost": (gross + fee) / qty,
                    "realized_pnl": 0.0,
                }
            else:
                old_total_cost = existing["qty"] * existing["avg_cost"]
                new_total_cost = old_total_cost + gross + fee
                new_qty = existing["qty"] + qty
                existing["qty"] = new_qty
                existing["avg_cost"] = new_total_cost / new_qty
        else:  # SELL
            existing = positions.get(symbol)
            if existing is None or existing["qty"] < qty:
                continue  # 防呆：賣超略過
            # 賣單：cash += gross - fee - tax
            cash_delta += (gross - fee - tax)
            net_proceeds = gross - fee - tax
            cost_of_sold = qty * existing["avg_cost"]
            realized = net_proceeds - cost_of_sold

            existing["realized_pnl"] += realized
            total_realized += realized

            existing["qty"] -= qty
            if existing["qty"] == 0:
                # 全平倉：刪除 position（與 engine 一致；realized_pnl 已累積到 total）
                del positions[symbol]
            # 部分平倉：avg_cost 不變（與 engine 一致）

    return {
        "positions": positions,
        "cash_delta": cash_delta,
        "total_realized_pnl": total_realized,
    }
