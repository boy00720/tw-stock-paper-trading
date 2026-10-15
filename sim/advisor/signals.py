"""automation/sim/advisor/signals.py — 訊號組裝（M6 區塊 B）

對外兩個函數：
- load_watchlist_pools(path=None) -> dict
- build_decision_context(engine, feed, *, fills_path, recent_fills_n, baseline_symbol, watchlist_path) -> dict

build_decision_context 組成 Claude 用的 user_payload，六大欄位：
- account：現金 / 權益 / 持倉 / 累積已實現損益（從 engine.account.to_dict）
- pools：short_term / medium_term / long_term / intraday + cross_signals（從 logs/watchlist/current.json）
- quotes：持倉檔 + 池內候選檔 + baseline_symbol 的當前報價
- recent_fills：近 N 筆成交（fills.jsonl tail）
- market_context：baseline_symbol（預設 0050.TW）+ 當前報價（M6 stage1）
- risk_state：today_realized_pnl / circuit_threshold / circuit_remaining /
  circuit_active_today（讓 LLM 不再把累計 PnL 誤判為日內熔斷）

token 壓縮：char 估值超過 _CHAR_BUDGET（80K tokens × 4 chars）→ 去掉
short_term 的 breakdown 細節，保留 score / entry_zone / stop_loss / take_profit /
time_stop_date / inst_signal。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_WATCHLIST_PATH = _BASE_DIR / "logs" / "watchlist" / "current.json"
DEFAULT_FILLS_PATH = _BASE_DIR / "logs" / "sim" / "fills.jsonl"
DEFAULT_ORDERS_PATH = _BASE_DIR / "logs" / "sim" / "orders.jsonl"

# 估算 token：粗略 1 token ≈ 4 chars（CJK 略多但夠用做門檻）
_TOKEN_BUDGET = 80_000
_CHAR_BUDGET = _TOKEN_BUDGET * 4


def load_watchlist_pools(path: Optional[Path] = None) -> dict[str, Any]:
    """讀 logs/watchlist/current.json 的四池 + cross_signals。

    回傳 dict：
    - short_term, medium_term, long_term, intraday: list[dict]
    - cross_signals: dict[symbol, signal]
    - updated_at, summary（透傳）

    讀失敗（檔不存在 / 格式錯）回所有池為空的 dict，**不 raise**（caller 拿到空池也能跑）。
    """
    path = Path(path) if path else DEFAULT_WATCHLIST_PATH
    if not path.exists():
        logger.warning("[advisor.signals] watchlist 不存在: %s", path)
        return _empty_pools()

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.exception("[advisor.signals] watchlist 讀取失敗: %s", path)
        return _empty_pools()

    formal = data.get("formal_pools") or {}
    aux = data.get("auxiliary_signals") or {}
    return {
        "short_term": list(formal.get("short_term") or []),
        "medium_term": list(formal.get("medium_term") or []),
        "long_term": list(formal.get("long_term") or []),
        "intraday": list(aux.get("intraday") or []),
        "leveraged_etf": list(aux.get("leveraged_etf") or data.get("leveraged_etf") or []),
        "cross_signals": dict(data.get("cross_signals") or {}),
        "updated_at": data.get("updated_at"),
        "summary": dict(data.get("summary") or {}),
    }


def _empty_pools() -> dict[str, Any]:
    return {
        "short_term": [],
        "medium_term": [],
        "long_term": [],
        "intraday": [],
        "leveraged_etf": [],
        "cross_signals": {},
        "updated_at": None,
        "summary": {},
    }


def build_decision_context(
    engine,
    feed,
    *,
    fills_path: Optional[Path] = None,
    recent_fills_n: int = 20,
    baseline_symbol: str = "0050.TW",
    watchlist_path: Optional[Path] = None,
    orders_log_path: Optional[Path] = None,
    circuit_pct: float = 0.03,
    circuit_active_today: bool = False,
) -> dict[str, Any]:
    """組成給 Claude 的 user_payload。

    engine 可為 None（測試時），對應 account / holdings 為空。
    feed 必須提供 get_quotes(list[str]) -> dict[str, float]。

    pending_orders 採「以 order_id 取最後狀態」的 replay 方式從 orders.jsonl 讀，
    僅注入只讀上下文供模型避免重複下單；advisor decide 本身不會 cancel/replace
    它們，模型若要避開只能透過 hold 或調整 qty。

    risk_state 用 today_realized_pnl 與 initial_cash × circuit_pct 比對，
    讓 LLM 不再把 account.total_realized_pnl（累計）當熔斷依據。
    circuit_active_today 由 caller（runner）從 RiskGuard 真實狀態傳入。
    """
    pools = load_watchlist_pools(watchlist_path)

    # 候選 symbols：池內 + 持倉中 + baseline
    candidates: set[str] = set()
    candidates.update(pools["cross_signals"].keys())
    for item in pools["short_term"]:
        if isinstance(item, dict) and item.get("symbol"):
            candidates.add(item["symbol"])
    for item in pools["medium_term"]:
        if isinstance(item, dict) and item.get("symbol"):
            candidates.add(item["symbol"])
    for item in pools["long_term"]:
        if isinstance(item, dict) and item.get("symbol"):
            candidates.add(item["symbol"])
    for item in pools["intraday"]:
        if isinstance(item, dict) and item.get("symbol"):
            candidates.add(item["symbol"])
    for item in pools["leveraged_etf"]:
        if isinstance(item, dict) and item.get("symbol"):
            candidates.add(item["symbol"])

    holdings: list[str] = []
    if engine is not None and getattr(engine, "account", None) is not None:
        holdings = list(engine.account.positions.keys())
    candidates.update(holdings)
    candidates.add(baseline_symbol)

    quotes: dict[str, float] = {}
    if feed is not None and candidates:
        try:
            quotes = feed.get_quotes(list(candidates)) or {}
        except Exception:
            logger.exception("[advisor.signals] feed.get_quotes failed")
            quotes = {}

    # account snapshot；給 holdings 對應的 quotes 算 mark-to-market
    account_snapshot: dict[str, Any] = {}
    if engine is not None and getattr(engine, "account", None) is not None:
        holding_quotes = {s: q for s, q in quotes.items() if s in holdings}
        try:
            account_snapshot = engine.account.to_dict(holding_quotes)
        except Exception:
            logger.exception("[advisor.signals] account.to_dict failed")

    recent_fills = _tail_fills(fills_path or DEFAULT_FILLS_PATH, recent_fills_n)
    pending_orders = _load_pending_orders_summary(orders_log_path or DEFAULT_ORDERS_PATH)
    risk_state = _build_risk_state(
        engine,
        fills_path or DEFAULT_FILLS_PATH,
        circuit_pct=circuit_pct,
        circuit_active_today=circuit_active_today,
    )

    payload: dict[str, Any] = {
        "account": account_snapshot,
        "pools": pools,
        "quotes": quotes,
        "recent_fills": recent_fills,
        "pending_orders": pending_orders,
        "market_context": {
            "baseline_symbol": baseline_symbol,
            "baseline_price": quotes.get(baseline_symbol),
        },
        "risk_state": risk_state,
    }

    # 估 token budget；超 budget 走壓縮
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if len(serialized) > _CHAR_BUDGET:
        before_chars = len(serialized)
        payload = _compress_payload(payload)
        after = json.dumps(payload, ensure_ascii=False, default=str)
        logger.info(
            "[advisor.signals] payload 壓縮: %s → %s chars（budget=%s）",
            before_chars, len(after), _CHAR_BUDGET,
        )

    return payload


def _count_today_new_buys(engine, fills_path: Path) -> int:
    """計算今日新開倉數（symbol 不在目前持倉的 BUY fill）。"""
    held: set[str] = set()
    if engine and getattr(engine, "account", None):
        for sym, pos in (getattr(engine.account, "positions", None) or {}).items():
            if getattr(pos, "qty", 0) > 0:
                held.add(sym)

    count = 0
    p = Path(fills_path)
    if not p.exists():
        return 0
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        for line in p.read_text(encoding="utf-8").strip().splitlines():
            f = json.loads(line)
            if f["side"] == "BUY" and f.get("filled_at", "")[:10] == today_str:
                if f["symbol"] not in held:
                    count += 1
                    held.add(f["symbol"])
    except Exception:
        pass
    return count


def _build_risk_state(
    engine,
    fills_path: Path,
    *,
    circuit_pct: float,
    circuit_active_today: bool,
) -> dict[str, Any]:
    """建構 risk_state 區塊（讓 LLM 用真實「今日 PnL vs 熔斷門檻」判斷）。

    initial_cash 取自 engine.account.initial_cash；engine 為 None 時回最小欄位
    供 LLM 知道熔斷規則，但 today/threshold/remaining 為 None。
    today_realized_pnl 計算失敗（例如 fills 不存在）→ 0.0，不 raise。
    """
    initial_cash: Optional[float] = None
    if engine is not None and getattr(engine, "account", None) is not None:
        try:
            initial_cash = float(engine.account.initial_cash)
        except Exception:
            logger.exception("[advisor.signals] read initial_cash failed")

    today_pnl: Optional[float] = None
    if Path(fills_path).exists():
        try:
            from sim.reports import today_realized_pnl
            today_pnl = float(today_realized_pnl(fills_path))
        except Exception:
            logger.exception("[advisor.signals] today_realized_pnl failed")
            today_pnl = 0.0
    else:
        today_pnl = 0.0

    circuit_threshold: Optional[float] = None
    circuit_remaining: Optional[float] = None
    if initial_cash is not None and initial_cash > 0:
        circuit_threshold = -initial_cash * float(circuit_pct)
        if today_pnl is not None:
            circuit_remaining = today_pnl - circuit_threshold

    # 今日新開倉數（供 LLM 知道還剩幾筆額度）
    today_new_buys = _count_today_new_buys(engine, fills_path)

    return {
        "today_realized_pnl": today_pnl,
        "circuit_threshold": circuit_threshold,
        "circuit_remaining": circuit_remaining,
        "circuit_active_today": bool(circuit_active_today),
        "circuit_pct": float(circuit_pct),
        "today_new_buys": today_new_buys,
        "max_new_buys_per_day": 3,
    }


def _tail_fills(path: Path, n: int) -> list[dict[str, Any]]:
    """讀 fills.jsonl 最後 n 行；損壞行略過。"""
    p = Path(path)
    if not p.exists():
        return []
    fills: list[dict[str, Any]] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    fills.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        logger.exception("[advisor.signals] tail fills failed: %s", path)
        return []
    return fills[-n:] if len(fills) > n else fills


_PENDING_SUMMARY_FIELDS = (
    "order_id", "symbol", "side", "type", "qty", "price",
    "created_at", "notes", "oco_group_id", "parent_order_id",
)


def _load_pending_orders_summary(path: Path) -> list[dict[str, Any]]:
    """讀 orders.jsonl，以 order_id 取最後狀態，回傳當前仍 PENDING 的精簡 dict 列表。

    僅供 decision payload 注入只讀上下文用，不重建 Order 物件。
    """
    p = Path(path)
    if not p.exists():
        return []
    last_by_id: dict[str, dict[str, Any]] = {}
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                oid = rec.get("order_id")
                if not oid:
                    continue
                last_by_id[oid] = rec
    except Exception:
        logger.exception("[advisor.signals] read orders failed: %s", path)
        return []
    summary: list[dict[str, Any]] = []
    for rec in last_by_id.values():
        if rec.get("status") != "PENDING":
            continue
        summary.append({k: rec.get(k) for k in _PENDING_SUMMARY_FIELDS})
    return summary


def _compress_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """超 token budget 時的壓縮策略：去掉 short_term 的 breakdown 細節。

    保留：score, entry_zone, stop_loss, take_profit, time_stop_date, inst_signal,
    pick_date, symbol, name, engine, entry_price, price。
    """
    pools = payload.get("pools") or {}
    short = pools.get("short_term") or []
    compressed: list[Any] = []
    for item in short:
        if not isinstance(item, dict):
            compressed.append(item)
            continue
        c = {k: v for k, v in item.items() if k != "breakdown"}
        compressed.append(c)
    new_pools = dict(pools)
    new_pools["short_term"] = compressed
    new_payload = dict(payload)
    new_payload["pools"] = new_pools
    return new_payload
