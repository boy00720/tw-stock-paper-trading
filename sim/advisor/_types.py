"""automation/sim/advisor/_types.py — advisor 共用資料型別

只有 Decision dataclass。risk_guard 與 decision 都 import 此檔。

Decision 是 advisor → engine 之間的中介資料結構：
- advisor.decision.decide_once() 產出 list[Decision]
- advisor.risk_guard.RiskGuard.evaluate() 輸入 Decision、輸出 GuardResult
- advisor.runner.run_advisor_once() 把通過的 Decision 轉 sim.models.Order 餵給 engine.submit_order
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Decision:
    """advisor 對單一 symbol 的決策。

    action="hold" 時其他欄位可全省（advisor 表達「該檔此輪不動作」）。
    action="buy"/"sell" 時 symbol/qty/type 必填；price 對 LMT/STOP/TP 必填。
    stop_loss 是給風控閘算單筆風險用的「advisor 預期止損價」，不一定要轉成
    sim.engine 的 STOP order——是否同時掛 STOP 子單由 runner 決定（M6 階段一不掛）。
    """

    action: str  # "buy" | "sell" | "hold"
    symbol: Optional[str] = None
    qty: Optional[int] = None
    type: Optional[str] = None  # "MKT" | "LMT" | "STOP" | "TP"
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    reason: str = ""
    source_pools: list[str] = field(default_factory=list)
