"""
automation/sim/models.py — 模擬盤資料模型

四個核心 dataclass：
- Order：訂單（下單時建立）
- Fill：成交（撮合後產生）
- Position：持倉（單一標的的當前部位）
- Account：帳戶（cash + positions + 已實現損益）

設計原則（M1 Day 1）：
- 純 dataclass + Enum，零外部依賴（不用 Pydantic）
- 所有 model 提供 to_dict() 確保 JSON 序列化兼容（寫 jsonl 用）
- 時間戳用 ISO8601 字串，避免 datetime 序列化的歧義
- v1 不支援放空（Position.qty 永遠 >= 0；期貨多空在 v2 處理）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Dict


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MKT = "MKT"     # 市價：立即成交在當下報價 ± 滑價
    LMT = "LMT"     # 限價：報價優於限價才成交
    STOP = "STOP"   # 停損：觸發價達到後轉市價
    TP = "TP"       # 停利：達標自動賣出


class OrderStatus(str, Enum):
    PENDING = "PENDING"               # 等待撮合
    PENDING_PARENT = "PENDING_PARENT" # IFD 子單：等待父單成交後才生效（M2 新增）
    FILLED = "FILLED"                 # 已完全成交
    CANCELLED = "CANCELLED"           # 已取消
    REJECTED = "REJECTED"             # 已拒絕（漲跌停外、資金不足等）


def _new_order_id() -> str:
    return f"ord-{uuid.uuid4().hex[:12]}"


def _new_fill_id() -> str:
    return f"fill-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Order:
    """訂單。由使用者下單建立，後續被撮合引擎處理。"""

    symbol: str
    side: OrderSide
    type: OrderType
    qty: int
    price: Optional[float] = None  # 限價、停損的觸發價；市價時為 None
    order_id: str = field(default_factory=_new_order_id)
    status: OrderStatus = OrderStatus.PENDING
    created_at: str = field(default_factory=_now_iso)
    notes: str = ""  # 備註欄（reject 原因、取消原因等）
    # M2 新增：複合訂單關聯
    oco_group_id: Optional[str] = None      # OCO 組 ID（同組任一成交，其他自動取消）
    parent_order_id: Optional[str] = None   # IFD 父單 ID（child 等 parent 成交才生效）

    def to_dict(self) -> dict:
        d = asdict(self)
        # Enum -> str 確保 JSON 序列化
        d["side"] = self.side.value if isinstance(self.side, OrderSide) else self.side
        d["type"] = self.type.value if isinstance(self.type, OrderType) else self.type
        d["status"] = self.status.value if isinstance(self.status, OrderStatus) else self.status
        return d


@dataclass
class Fill:
    """成交回報。撮合引擎產生，不可變紀錄。"""

    order_id: str
    symbol: str
    side: OrderSide
    qty: int
    price: float        # 實際成交價（含滑價）
    fee: float          # 券商手續費（NTD）
    tax: float          # 證交稅 / 期交稅（NTD）
    fill_id: str = field(default_factory=_new_fill_id)
    filled_at: str = field(default_factory=_now_iso)

    @property
    def gross_value(self) -> float:
        """毛交易值（不含費用）"""
        return self.price * self.qty

    @property
    def net_cost(self) -> float:
        """買單實際扣款（含手續費）"""
        return self.gross_value + self.fee

    @property
    def net_proceeds(self) -> float:
        """賣單實際入帳（扣掉手續費與證交稅）"""
        return self.gross_value - self.fee - self.tax

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value if isinstance(self.side, OrderSide) else self.side
        return d


@dataclass
class Position:
    """單一標的的持倉。"""

    symbol: str
    qty: int                  # 持有股數（v1 永遠 >= 0，不放空）
    avg_cost: float           # 加權平均成本（含買入手續費攤提）
    realized_pnl: float = 0.0  # 此標的累計已實現損益（部分平倉累積）

    @property
    def cost_basis(self) -> float:
        """當前持倉總成本"""
        return self.qty * self.avg_cost

    def market_value(self, current_price: float) -> float:
        """以給定報價計算市值"""
        return self.qty * current_price

    def unrealized_pnl(self, current_price: float) -> float:
        """以給定報價計算未實現損益（不考慮平倉時的賣出費用）"""
        return self.market_value(current_price) - self.cost_basis

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Account:
    """模擬帳戶。M1 內僅存記憶體，M3+ 接入 jsonl 持久化。"""

    cash: float                                       # 總現金（已結算，不含 PENDING 預扣）
    positions: Dict[str, Position] = field(default_factory=dict)
    total_realized_pnl: float = 0.0                   # 帳戶累計已實現損益
    initial_cash: float = 1_000_000.0                 # 起始資金，用於計算總報酬率
    reserved_cash: float = 0.0                        # PENDING BUY 預扣（M6.2 新增）

    @property
    def available_cash(self) -> float:
        """可動用現金 = cash - reserved_cash。submit_order 風控檢查用此值。"""
        return self.cash - self.reserved_cash

    @property
    def cost_value(self) -> float:
        """所有持倉的總成本（不含未實現損益）"""
        return sum(p.cost_basis for p in self.positions.values())

    def equity(self, quotes: Optional[Dict[str, float]] = None) -> float:
        """
        權益（總資產）：cash + 持倉市值。
        - 若提供 quotes，計算 mark-to-market 市值
        - 若未提供 quotes，回退用 cost_basis（保守估算）
        - reserved_cash 不從 equity 扣（reserved 仍是自己的錢，只是被佔用）
        """
        if quotes:
            position_value = sum(
                p.market_value(quotes[p.symbol])
                for p in self.positions.values()
                if p.symbol in quotes
            )
            # 沒拿到報價的標的退用 cost_basis
            position_value += sum(
                p.cost_basis
                for p in self.positions.values()
                if p.symbol not in quotes
            )
        else:
            position_value = self.cost_value
        return self.cash + position_value

    def total_return_pct(self, quotes: Optional[Dict[str, float]] = None) -> float:
        """總報酬率（%）= (current_equity + total_realized_pnl - initial_cash) / initial_cash * 100"""
        if self.initial_cash <= 0:
            return 0.0
        current_equity = self.equity(quotes)
        return (current_equity + self.total_realized_pnl - self.initial_cash) / self.initial_cash * 100.0

    def to_dict(self, quotes: Optional[Dict[str, float]] = None) -> dict:
        positions: Dict[str, dict] = {}
        for sym, pos in self.positions.items():
            row = pos.to_dict()
            if quotes and sym in quotes:
                quote = quotes[sym]
                row["quote"] = quote
                row["market_value"] = pos.market_value(quote)
                row["unrealized_pnl"] = pos.unrealized_pnl(quote)
                row["unrealized_pct"] = (
                    row["unrealized_pnl"] / pos.cost_basis * 100.0
                    if pos.cost_basis else 0.0
                )
            positions[sym] = row
        return {
            "cash": self.cash,
            "reserved_cash": self.reserved_cash,
            "available_cash": self.available_cash,
            "initial_cash": self.initial_cash,
            "total_realized_pnl": self.total_realized_pnl,
            "positions": positions,
            "equity": self.equity(quotes),
            "total_return_pct": self.total_return_pct(quotes),
        }
