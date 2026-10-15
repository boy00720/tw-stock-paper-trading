"""
automation/sim/engine.py — 撮合引擎

核心類別 MatchEngine：
- submit_order(order)：下單驗證 + 寫 orders.jsonl + 加入 pending
- cancel_order(order_id)：取消委託
- match(quotes)：對 pending 用當前報價撮合，回傳 fills
- apply_fills(fills)：套用到 Account（更新 cash、Position、total_realized_pnl）+ 寫 fills.jsonl
- snapshot(quotes)：回傳當前狀態（給 /portfolio、日報用）

設計原則（M1 Day 2）：
- 純記憶體 state，restore from jsonl 留待後續
- 撮合與套用解耦：match() 只算 fills 不改帳戶；apply_fills() 才更新
- 手續費攤入 avg_cost（會計上「成本基」），簡化平倉計算
- 賣出在 submit_order 階段就檢查持倉，避免進撮合後才發現超賣
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import yaml

from sim.models import (
    Account,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)


_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = _BASE_DIR / "config" / "sim_config.yaml"
_LOGS_SIM = _BASE_DIR / "logs" / "sim"
_DEFAULT_ORDERS_JSONL = _LOGS_SIM / "orders.jsonl"
_DEFAULT_FILLS_JSONL = _LOGS_SIM / "fills.jsonl"


class MatchEngine:
    """模擬盤撮合引擎。"""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path else _DEFAULT_CONFIG
        with open(self.config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        initial_cash = float(self.config.get("initial_cash", 1_000_000))
        self.account = Account(
            cash=initial_cash,
            initial_cash=initial_cash,
        )
        self.pending_orders: dict[str, Order] = {}
        # M2：IFD 子單暫存區（parent_order_id -> child Order）
        # 父單成交後 child 才會被「升級」進 pending_orders 撮合
        self._waiting_children: dict[str, Order] = {}
        # M2：本輪 match() 已成交、待 apply_fills() 觸發 OCO/IFD 後處理的 orders
        # 移到 apply_fills 才處理是因為：IFD child 持倉檢查必須在 _apply_buy/_apply_sell 之後
        self._just_filled_orders: list[Order] = []
        # M3：thread-safe lock。
        # B 案下 feishu_bot 起 background runner thread（30 秒撮合一次），主 thread
        # 也會處理用戶下單，兩條 thread 都修改 pending_orders / account / 暫存區，
        # 必須加鎖才安全。RLock 允許同一 thread 重入（例 submit_oco 內呼叫兩次
        # submit_order）。所有修改型公開方法都用 with self._lock 包起來。
        self._lock = threading.RLock()

        # log 路徑（測試時可被 override）
        self.orders_log_path: Path = _DEFAULT_ORDERS_JSONL
        self.fills_log_path: Path = _DEFAULT_FILLS_JSONL

        # M6.2：BUY 預扣資金的 per-order 記帳；cancel / FILL / OCO 取消都從這釋放
        # （Account.reserved_cash 是聚合值，這個 dict 記每筆 PENDING BUY 預扣多少。）
        self._reserved_by_order: dict[str, float] = {}

    # ─────────────────────────────────────────────────────
    # 配置查詢 helpers
    # ─────────────────────────────────────────────────────

    def _get_symbol_type(self, symbol: str) -> str:
        """查 symbol 屬於 stock/etf/futures，找不到當 stock"""
        types_map = self.config.get("symbol_types") or {}
        return types_map.get(symbol, "stock")

    def _slippage_pct(self) -> float:
        return float(self.config.get("slippage_pct", 0.001))

    def watchlist(self) -> list[str]:
        """從 config 讀 watchlist。runner 撮合迴圈會用這個決定要拉哪些報價。"""
        return list(self.config.get("watchlist") or [])

    def match_interval_sec(self) -> int:
        """從 config 讀撮合間隔秒數。"""
        return int(self.config.get("match_interval_sec", 30))

    # ─────────────────────────────────────────────────────
    # 手續費 / 稅金
    # ─────────────────────────────────────────────────────

    def calc_fee(
        self, symbol: str, side: OrderSide, price: float, qty: int
    ) -> tuple[float, float]:
        """
        計算手續費與稅。回傳 (fee, tax)。
        - fee：券商手續費（雙邊都收，最低 20 元）
        - tax：台股/ETF 證交稅（賣方收）；期貨期交稅（雙邊收）
        """
        symbol_type = self._get_symbol_type(symbol)
        fees_cfg = self.config.get("fees", {})
        type_cfg = fees_cfg.get(symbol_type, fees_cfg.get("stock", {}))

        gross = price * qty

        # 期貨（v2 才完整支援；v1 算佔位）
        if symbol_type == "futures":
            fee = float(type_cfg.get("commission_per_lot", 50)) * qty
            tax = gross * float(type_cfg.get("tax_pct", 0.00002))
            return fee, tax

        # 台股、ETF
        commission_pct = float(type_cfg.get("commission_pct", 0.001425))
        commission_min = float(type_cfg.get("commission_min", 20))
        fee_raw = gross * commission_pct
        fee = max(fee_raw, commission_min)

        if side == OrderSide.SELL:
            tax_pct = float(type_cfg.get("tax_pct", 0.003))
            tax = gross * tax_pct
        else:
            tax = 0.0

        return fee, tax

    def _estimate_buy_reserve(
        self, symbol: str, qty: int, price: Optional[float],
    ) -> Optional[float]:
        """估算 BUY 訂單應預扣金額 = gross + commission。

        price=None（MKT）→ 回 None，表示無法估計、跳過預扣（依賴撮合輪立即吃掉）。
        非 BUY 用途請呼叫者自己過濾；本 helper 只負責算數。
        """
        if price is None or price <= 0:
            return None
        fee, _tax = self.calc_fee(symbol, OrderSide.BUY, price, qty)
        return price * qty + fee

    def _release_reservation(self, order_id: str) -> float:
        """釋放某 order 的預扣（cancel / FILL / OCO 取消都用）。回傳釋放金額。"""
        amount = self._reserved_by_order.pop(order_id, 0.0)
        if amount > 0:
            self.account.reserved_cash -= amount
            # 浮點 drift 防呆：若理論該 0 卻負一點點，夾回 0
            if -1e-6 < self.account.reserved_cash < 0:
                self.account.reserved_cash = 0.0
        return amount

    # ─────────────────────────────────────────────────────
    # 訂單管理
    # ─────────────────────────────────────────────────────

    def submit_order(self, order: Order) -> Order:
        """
        下單。回傳同一個 Order（可能是 PENDING 或 REJECTED）。
        - 數量必須 > 0
        - LMT/STOP/TP 必須有 price
        - 賣單必須持有足夠數量
        - 通過驗證 → 寫 orders.jsonl + 加 pending_orders
        """
        with self._lock:
            if order.qty <= 0:
                order.status = OrderStatus.REJECTED
                order.notes = "qty must be > 0"
                self._write_jsonl(self.orders_log_path, order.to_dict())
                return order

            if order.type in (OrderType.LMT, OrderType.STOP, OrderType.TP) and order.price is None:
                order.status = OrderStatus.REJECTED
                order.notes = f"{order.type.value} order requires price"
                self._write_jsonl(self.orders_log_path, order.to_dict())
                return order

            if order.side == OrderSide.SELL:
                held = self.account.positions.get(order.symbol)
                held_qty = held.qty if held else 0
                if held_qty < order.qty:
                    order.status = OrderStatus.REJECTED
                    order.notes = f"insufficient holding: held={held_qty} requested={order.qty}"
                    self._write_jsonl(self.orders_log_path, order.to_dict())
                    return order

            # M6.2：BUY 預扣資金（LMT 用 price 精確估、MKT 不預扣靠立即撮合）
            reserve_amount: Optional[float] = None
            if order.side == OrderSide.BUY:
                reserve_amount = self._estimate_buy_reserve(
                    order.symbol, order.qty, order.price,
                )
                if reserve_amount is not None:
                    if reserve_amount > self.account.available_cash + 1e-6:
                        order.status = OrderStatus.REJECTED
                        order.notes = (
                            f"insufficient available cash: "
                            f"need={reserve_amount:.2f} "
                            f"available={self.account.available_cash:.2f}"
                        )
                        self._write_jsonl(self.orders_log_path, order.to_dict())
                        return order

            self.pending_orders[order.order_id] = order
            if reserve_amount is not None and reserve_amount > 0:
                self.account.reserved_cash += reserve_amount
                self._reserved_by_order[order.order_id] = reserve_amount
            self._write_jsonl(self.orders_log_path, order.to_dict())
            return order

    def cancel_order(self, order_id: str) -> bool:
        """取消委託。"""
        with self._lock:
            if order_id not in self.pending_orders:
                return False
            order = self.pending_orders.pop(order_id)
            order.status = OrderStatus.CANCELLED
            self._release_reservation(order_id)
            self._write_jsonl(self.orders_log_path, order.to_dict())
            return True

    def close_all_positions(self) -> list[Order]:
        """
        對 account.positions 內每檔持倉下市價 SELL，回傳 submit 出去的 Order list。

        - 不負責撮合（撮合靠 runner 或下次 match()）
        - 任何單獨 reject（理論上不該發生，因為剛拿到的持倉量就是 SELL qty）
          仍會被回傳，呼叫端可檢查 status
        - 用 RLock 包整段是因為 submit_order 內部也會拿 lock（RLock 可重入）；
          這樣外層的「掃 positions」也是 atomic 的快照
        """
        with self._lock:
            submitted: list[Order] = []
            # snapshot positions 避免遍歷時修改 dict（理論上 submit MKT SELL 不會
            # 立即動 positions，要等 match+apply_fills；但保險起見還是先 list 化）
            for sym, pos in list(self.account.positions.items()):
                if pos.qty <= 0:
                    continue
                order = Order(
                    symbol=sym,
                    side=OrderSide.SELL,
                    type=OrderType.MKT,
                    qty=pos.qty,
                )
                self.submit_order(order)
                submitted.append(order)
            return submitted

    # ─────────────────────────────────────────────────────
    # M2：複合訂單（OCO / IFD）
    # ─────────────────────────────────────────────────────

    def submit_oco(
        self, stop_order: Order, tp_order: Order
    ) -> tuple[Order, Order]:
        """
        提交一組 OCO（One-Cancels-Other），通常是「停損 + 停利」配對。
        - 兩張子單共享一個 oco_group_id
        - 任一成交後 match() 會自動取消同組另一張
        - 兩張子單個別走 submit_order 完整驗證流程
        """
        import uuid
        with self._lock:
            group_id = f"oco-{uuid.uuid4().hex[:12]}"
            stop_order.oco_group_id = group_id
            tp_order.oco_group_id = group_id
            return self.submit_order(stop_order), self.submit_order(tp_order)

    def submit_ifd(
        self, parent_order: Order, child_order: Order
    ) -> tuple[Order, Order]:
        """
        提交 IFD（If-Done）：parent 成交後 child 才生效。
        - parent 走完整 submit_order 驗證 → 進 pending_orders
        - child 標 PENDING_PARENT、放進 _waiting_children；parent FILLED 後 match() 會升級
        - 若 parent 直接被 REJECTED，child 不會被建立到 _waiting_children
        """
        with self._lock:
            submitted_parent = self.submit_order(parent_order)
            if submitted_parent.status == OrderStatus.REJECTED:
                child_order.status = OrderStatus.REJECTED
                child_order.notes = f"parent rejected: {parent_order.order_id}"
                self._write_jsonl(self.orders_log_path, child_order.to_dict())
                return submitted_parent, child_order

            child_order.parent_order_id = parent_order.order_id
            child_order.status = OrderStatus.PENDING_PARENT
            self._waiting_children[parent_order.order_id] = child_order
            self._write_jsonl(self.orders_log_path, child_order.to_dict())
            return submitted_parent, child_order

    def _cancel_oco_siblings(self, filled_order: Order) -> int:
        """
        某 order 成交後，取消同 OCO 組的所有其他 pending order。
        回傳被取消的筆數。
        """
        if not filled_order.oco_group_id:
            return 0
        n = 0
        for oid in list(self.pending_orders.keys()):
            peer = self.pending_orders[oid]
            if peer.order_id == filled_order.order_id:
                continue
            if peer.oco_group_id != filled_order.oco_group_id:
                continue
            # 模仿 cancel_order，但要記錄是 OCO 觸發的取消
            self.pending_orders.pop(oid)
            peer.status = OrderStatus.CANCELLED
            peer.notes = f"OCO sibling filled: {filled_order.order_id}"
            self._release_reservation(oid)
            self._write_jsonl(self.orders_log_path, peer.to_dict())
            n += 1
        return n

    def _activate_children(self, parent_order: Order) -> None:
        """
        parent 成交後，把 IFD child 從 _waiting_children 升級為 pending_orders。
        升級時重新驗證（例如 SELL 必須持倉足夠）；驗證失敗則 child REJECTED。
        """
        child = self._waiting_children.pop(parent_order.order_id, None)
        if child is None:
            return

        # 重新驗證 child（複用 submit_order 的關鍵檢查，但避免 order_id 重複進 pending）
        if child.qty <= 0:
            child.status = OrderStatus.REJECTED
            child.notes = "IFD child reject: qty must be > 0"
            self._write_jsonl(self.orders_log_path, child.to_dict())
            return

        if child.type in (OrderType.LMT, OrderType.STOP, OrderType.TP) and child.price is None:
            child.status = OrderStatus.REJECTED
            child.notes = f"IFD child reject: {child.type.value} requires price"
            self._write_jsonl(self.orders_log_path, child.to_dict())
            return

        if child.side == OrderSide.SELL:
            held = self.account.positions.get(child.symbol)
            held_qty = held.qty if held else 0
            if held_qty < child.qty:
                child.status = OrderStatus.REJECTED
                child.notes = (
                    f"IFD child reject: insufficient holding "
                    f"(held={held_qty} requested={child.qty})"
                )
                self._write_jsonl(self.orders_log_path, child.to_dict())
                return

        # M6.2：BUY child 升級為 PENDING 時補預扣
        reserve_amount: Optional[float] = None
        if child.side == OrderSide.BUY:
            reserve_amount = self._estimate_buy_reserve(
                child.symbol, child.qty, child.price,
            )
            if reserve_amount is not None and reserve_amount > self.account.available_cash + 1e-6:
                child.status = OrderStatus.REJECTED
                child.notes = (
                    f"IFD child reject: insufficient available cash "
                    f"(need={reserve_amount:.2f} available={self.account.available_cash:.2f})"
                )
                self._write_jsonl(self.orders_log_path, child.to_dict())
                return

        # 通過驗證 → 升級為 PENDING、加進 pending_orders
        child.status = OrderStatus.PENDING
        self.pending_orders[child.order_id] = child
        if reserve_amount is not None and reserve_amount > 0:
            self.account.reserved_cash += reserve_amount
            self._reserved_by_order[child.order_id] = reserve_amount
        self._write_jsonl(self.orders_log_path, child.to_dict())
        return True

    # ─────────────────────────────────────────────────────
    # 撮合
    # ─────────────────────────────────────────────────────

    def match(self, quotes: dict[str, float]) -> list[Fill]:
        """
        對 pending_orders 用當前 quotes 撮合，回傳本輪產生的 Fills。

        本方法**只計算 fills**，不更新 Account。要等 apply_fills() 才寫入帳戶狀態。
        但會把已成交的 Order 從 pending_orders 移除、status 改 FILLED 並寫 jsonl。
        """
        with self._lock:
            fills: list[Fill] = []
            slippage = self._slippage_pct()
            sell_available: dict[str, int] = {}

            # 用 list(keys()) 拷貝，避免遍歷時修改 dict
            for order_id in list(self.pending_orders.keys()):
                order = self.pending_orders.get(order_id)
                if order is None:
                    # 防禦：若未來 match() 內出現連鎖刪除（例如本輪先觸發 OCO），仍可安全跳過
                    continue
                quote = quotes.get(order.symbol)
                if quote is None:
                    continue  # 沒拿到報價，跳過本輪

                fill_price = self._compute_fill_price(order, quote, slippage)
                if fill_price is None:
                    continue  # 不滿足成交條件

                if order.side == OrderSide.SELL:
                    available_qty = sell_available.get(order.symbol)
                    if available_qty is None:
                        held = self.account.positions.get(order.symbol)
                        available_qty = held.qty if held else 0
                    if available_qty < order.qty:
                        order.status = OrderStatus.REJECTED
                        order.notes = (
                            f"insufficient holding at match: "
                            f"available={available_qty} requested={order.qty}"
                        )
                        self.pending_orders.pop(order_id)
                        self._write_jsonl(self.orders_log_path, order.to_dict())
                        continue
                    sell_available[order.symbol] = available_qty - order.qty

                fee, tax = self.calc_fee(order.symbol, order.side, fill_price, order.qty)

                fill = Fill(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    price=fill_price,
                    fee=fee,
                    tax=tax,
                )
                fills.append(fill)

                # Order 標 FILLED + 從 pending 移除 + 補一筆 status update 到 orders.jsonl
                order.status = OrderStatus.FILLED
                self.pending_orders.pop(order_id)
                # M6.2：BUY 成交即釋放預扣（apply_fills 會把實際成本從 cash 扣掉）
                self._release_reservation(order_id)
                self._write_jsonl(self.orders_log_path, order.to_dict())

                # M2：暫存到 _just_filled_orders，等 apply_fills 更新帳戶後再觸發 OCO/IFD 後處理
                self._just_filled_orders.append(order)

            return fills

    def _compute_fill_price(
        self, order: Order, quote: float, slippage: float
    ) -> Optional[float]:
        """判斷訂單是否能用當前 quote 成交，回傳成交價；不滿足則 None。"""
        if order.type == OrderType.MKT:
            # 市價：立即成交在 quote ± slippage
            return quote * (1 + slippage) if order.side == OrderSide.BUY else quote * (1 - slippage)

        if order.type == OrderType.LMT:
            # 限價：BUY quote ≤ limit、SELL quote ≥ limit 才成交
            # 簡化：成交在限價（不取更優報價，避免回測過度樂觀）
            if order.side == OrderSide.BUY and quote <= order.price:
                return order.price
            if order.side == OrderSide.SELL and quote >= order.price:
                return order.price
            return None

        if order.type == OrderType.STOP:
            # STOP 停損 / 突破：報價穿越觸發價 → 轉市價成交（含滑價）
            # BUY STOP（多頭突破買進）：quote ≥ trigger
            # SELL STOP（持多頭跌破停損賣出）：quote ≤ trigger
            if order.side == OrderSide.BUY and quote >= order.price:
                return quote * (1 + slippage)
            if order.side == OrderSide.SELL and quote <= order.price:
                return quote * (1 - slippage)
            return None

        if order.type == OrderType.TP:
            # TP 停利 / 達標：報價達到目標 → 轉市價獲利出場
            # SELL TP（持多頭漲到目標賣）：quote ≥ target
            # BUY TP（跌到目標撿便宜，少見）：quote ≤ target
            if order.side == OrderSide.SELL and quote >= order.price:
                return quote * (1 - slippage)
            if order.side == OrderSide.BUY and quote <= order.price:
                return quote * (1 + slippage)
            return None

        return None

    # ─────────────────────────────────────────────────────
    # 套用成交（更新帳戶）
    # ─────────────────────────────────────────────────────

    def apply_fills(self, fills: list[Fill]) -> None:
        """套用 fills 到 Account：更新 cash、Position、total_realized_pnl，寫 fills.jsonl"""
        with self._lock:
            for fill in fills:
                if fill.side == OrderSide.BUY:
                    self._apply_buy(fill)
                else:
                    self._apply_sell(fill)
                self._write_jsonl(self.fills_log_path, fill.to_dict())

            # M2：帳戶更新完才觸發 OCO 取消 / IFD 升級
            # IFD child 在 _activate_children 內要查持倉，必須等 _apply_buy/_apply_sell 跑完
            for order in self._just_filled_orders:
                self._cancel_oco_siblings(order)
                self._activate_children(order)
            self._just_filled_orders = []

    def _apply_buy(self, fill: Fill) -> None:
        """買入：扣現金、加倉（均價加權，攤入手續費到 avg_cost）"""
        # 買單：cash -= gross + fee（無稅）
        self.account.cash -= fill.gross_value + fill.fee

        sym = fill.symbol
        existing = self.account.positions.get(sym)
        if existing is None:
            avg_cost = (fill.gross_value + fill.fee) / fill.qty
            self.account.positions[sym] = Position(
                symbol=sym, qty=fill.qty, avg_cost=avg_cost
            )
        else:
            old_total_cost = existing.cost_basis  # qty × avg_cost
            new_total_cost = old_total_cost + fill.gross_value + fill.fee
            new_qty = existing.qty + fill.qty
            existing.qty = new_qty
            existing.avg_cost = new_total_cost / new_qty

    def _apply_sell(self, fill: Fill) -> None:
        """賣出：加現金（扣費扣稅）、減倉、計算已實現損益"""
        sym = fill.symbol
        existing = self.account.positions.get(sym)
        if existing is None or existing.qty < fill.qty:
            # 防呆：submit_order 已攔住，這裡再保護一次
            return

        # 賣單：cash += gross - fee - tax
        self.account.cash += fill.gross_value - fill.fee - fill.tax

        # 已實現損益 = net_proceeds - 賣出量 × avg_cost
        cost_of_sold = fill.qty * existing.avg_cost
        realized = fill.net_proceeds - cost_of_sold

        existing.realized_pnl += realized
        self.account.total_realized_pnl += realized

        existing.qty -= fill.qty
        if existing.qty == 0:
            # 全平倉：刪除 position（realized_pnl 已累積到 account）
            del self.account.positions[sym]
        # 部分平倉：avg_cost 不變

    # ─────────────────────────────────────────────────────
    # 快照
    # ─────────────────────────────────────────────────────

    def snapshot(self, quotes: Optional[dict[str, float]] = None) -> dict:
        """回傳當前狀態 dict（用於 /portfolio、日報、週報）"""
        return {
            "account": self.account.to_dict(quotes),
            "pending_orders": [o.to_dict() for o in self.pending_orders.values()],
        }

    # ─────────────────────────────────────────────────────
    # jsonl 寫入
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _write_jsonl(path: Path, obj: dict) -> None:
        """append-only 寫一行 JSON。OS 層保證 append 原子性。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
