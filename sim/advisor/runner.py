"""automation/sim/advisor/runner.py — 執行循環（M6 區塊 E）

對外四個函數：
- init_engine_from_fills() -> MatchEngine：advisor 進程自己 init MatchEngine + 從
  fills.jsonl replay 重建持倉/現金/累積已實現損益（advisor 跟 bot 是兩個進程，
  記憶體分離，replay 是唯一同步狀態的方式 — PLAN 風險點 9）
- should_run_now(now=None, calendar_path=None) -> bool：盤中 09:00–13:30 + 非台股假日 + 非週末
- run_advisor_once(engine, feed, client, *, dry_run, watchlist_path, fills_path, audit_path) -> dict
- _decision_to_order(d) -> Optional[Order]：Decision → sim.models.Order

run_advisor_once 流程：
  1. RiskGuard.check_and_maybe_trigger_circuit()（讀 fills 算今日 PnL，可能觸發熔斷）
  2. decide_once() → (decisions, raw_response_dict)
  3. 對每筆 decision call risk_guard.evaluate()
     - hold → 計入 hold（不 submit、不寫 reject audit）
     - reject → blocked（risk_guard 內部已寫 audit）
     - pass + dry_run → 計 passed 但不 submit
     - pass + live → submit_order
  4. append decision_round 到 advisor_audit.jsonl（含 provider_id + model）
  5. 回傳統計 dict（status / decided / passed / submitted / blocked / hold / decisions_log）
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

from sim.engine import MatchEngine
from sim.feed import QuoteFeed
from sim.models import Order, OrderSide, OrderStatus, OrderType
from sim.advisor._audit import append_jsonl
from sim.advisor._types import Decision
from sim.advisor.auto_stop import sync_auto_stops_after_fills
from sim.advisor.client import AdvisorClient
from sim.advisor.decision import decide_once
from sim.advisor.risk_guard import DEFAULT_AUDIT_PATH, RiskGuard
from sim.advisor.signals import load_watchlist_pools

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_TRADING_CALENDAR = _BASE_DIR / "config" / "trading_calendar.yaml"

# 台股盤中時段（保留供 runner 內部引用；canonical 定義在 sim.calendar）
_TRADING_OPEN = dtime(9, 0)
_TRADING_CLOSE = dtime(13, 30)


def init_engine_from_fills() -> MatchEngine:
    """init MatchEngine + 從 fills.jsonl replay 重建 account 狀態。

    advisor 不接管 bot 的 pending_orders（那是 bot 進程記憶體的）；
    advisor 出單寫進同一份 orders.jsonl + 自己進程的 pending_orders，
    bot 進程不會看到 — PLAN 風險點 9 說明這個 cross-process state 限制。
    階段一只 dry-run 不 submit、不踩這個問題；階段二上線時的策略：
    advisor 出 MKT 立即撮合 + 寫 fills.jsonl；bot 端的 reports / portfolio
    讀 fills.jsonl 自然看得到。LMT/STOP 留待階段二再評估是否支援。
    """
    e = MatchEngine()
    if not e.fills_log_path.exists():
        return e

    try:
        from sim.models import Position
        from sim.reports import replay_positions_from_fills
        replay = replay_positions_from_fills(e.fills_log_path)
    except Exception:
        logger.exception("[advisor.runner] replay fills 失敗，使用空帳戶")
        return e

    for sym, p in (replay.get("positions") or {}).items():
        try:
            e.account.positions[sym] = Position(
                symbol=sym,
                qty=int(p.get("qty", 0)),
                avg_cost=float(p.get("avg_cost", 0)),
                realized_pnl=float(p.get("realized_pnl", 0)),
            )
        except Exception:
            logger.warning(
                "[advisor.runner] 跳過無法解析的 replay position: %s=%r", sym, p,
            )

    try:
        e.account.cash = e.account.initial_cash + float(replay.get("cash_delta", 0))
        e.account.total_realized_pnl = float(replay.get("total_realized_pnl", 0))
    except Exception:
        logger.exception("[advisor.runner] cash/pnl replay 套用失敗")

    return e


# ─────────────────────────────────────────────────────
# M6.1：跨進程 PENDING orders replay
# ─────────────────────────────────────────────────────


def _order_from_dict(d: dict) -> Optional[Order]:
    """逆向 Order.to_dict — 把 orders.jsonl 一筆 dict 重建為 Order 物件。

    無法解析的欄位回 None；caller 自行 skip + warn。
    """
    try:
        return Order(
            symbol=d["symbol"],
            side=OrderSide(d["side"]),
            type=OrderType(d["type"]),
            qty=int(d["qty"]),
            price=d.get("price"),
            order_id=d["order_id"],
            status=OrderStatus(d["status"]),
            created_at=d.get("created_at", ""),
            notes=d.get("notes", "") or "",
            oco_group_id=d.get("oco_group_id"),
            parent_order_id=d.get("parent_order_id"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def load_pending_orders(orders_log_path: Path) -> dict[str, Order]:
    """從 orders.jsonl 讀全檔，按 order_id 取最後狀態，回傳 status=PENDING 的 dict。

    一個 order_id 可能寫多次（PENDING 一次、後續 FILLED/CANCELLED/REJECTED 各一次）；
    只看 *最後一筆* 狀態，避免 replay 已成交/取消的訂單。

    回傳 {order_id: Order}，可直接餵進 engine.pending_orders。
    """
    orders_log_path = Path(orders_log_path)
    if not orders_log_path.exists():
        return {}

    last_state: dict[str, dict] = {}
    with open(orders_log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            oid = d.get("order_id")
            if oid:
                last_state[oid] = d

    pending: dict[str, Order] = {}
    for oid, d in last_state.items():
        if d.get("status") != OrderStatus.PENDING.value:
            continue
        order = _order_from_dict(d)
        if order is None:
            logger.warning("[advisor.runner] 跳過無法解析的 PENDING order: %s", oid)
            continue
        pending[oid] = order
    return pending


def init_engine_with_pending() -> MatchEngine:
    """init MatchEngine + replay fills（重建 account）+ replay PENDING orders（重建 pending_orders）。

    給 dispatch / cancel-pending 子命令用；advisor decide 路徑仍用
    init_engine_from_fills（不需要看到 PENDING orders，submit 只關心 account）。

    M6.2：replay 時為 PENDING BUY 重算 reserved_cash（orders.jsonl 不持久化此欄位，
    每次起進程都從 PENDING list 重新建構，避免跨重啟漏算）。
    """
    from sim.models import OrderSide as _OS
    e = init_engine_from_fills()
    pending = load_pending_orders(e.orders_log_path)
    e.pending_orders.update(pending)
    if pending:
        logger.info("[advisor.runner] replay %d 筆 PENDING orders", len(pending))
        # 重算 reserved_cash：對每筆 PENDING BUY 用 helper 估值
        for oid, o in pending.items():
            if o.side != _OS.BUY:
                continue
            reserve = e._estimate_buy_reserve(o.symbol, o.qty, o.price)
            if reserve is not None and reserve > 0:
                e.account.reserved_cash += reserve
                e._reserved_by_order[oid] = reserve
    return e


def should_run_now(
    now: Optional[datetime] = None,
    *,
    calendar_path: Optional[Path] = None,
) -> bool:
    """盤中 + 非假日 + 非週末才 run。

    委派給 sim.calendar.should_run_now()（canonical 實作）。
    dry-run CLI **不該調這個**（dry-run 是給 W 隨時看，不應被時段卡住）；
    `python3 -m sim.advisor decide` 在沒 --force / --dry-run 時會檢查此函數。
    """
    from sim.calendar import should_run_now as _should_run_now
    return _should_run_now(now, calendar_path=calendar_path)


def run_advisor_once(
    engine: MatchEngine,
    feed: QuoteFeed,
    client: AdvisorClient,
    *,
    dry_run: bool = False,
    watchlist_path: Optional[Path] = None,
    fills_path: Optional[Path] = None,
    audit_path: Optional[Path] = None,
) -> dict[str, Any]:
    """跑一輪 advisor 決策。

    回傳統計 dict：
        {
            "status": "ok" | "decide_failed",
            "decided": int, "passed": int, "submitted": int,
            "blocked": int, "hold": int,
            "circuit_triggered_this_round": bool,
            "summary": str | None, "metrics_self_eval": str | None,
            "decisions_log": list[dict],
            "error": str (僅 decide_failed),
        }
    """
    ts = datetime.now().isoformat(timespec="seconds")
    audit_p = Path(audit_path) if audit_path else DEFAULT_AUDIT_PATH
    rg = RiskGuard(engine, audit_path=audit_p)

    # 0) 熔斷檢查（可能觸發）
    circuit_triggered = rg.check_and_maybe_trigger_circuit()

    # 1) decide（把 RiskGuard 真實熔斷狀態與門檻傳進 payload.risk_state）
    try:
        decisions, raw = decide_once(
            client, engine, feed,
            watchlist_path=watchlist_path,
            fills_path=fills_path,
            orders_log_path=engine.orders_log_path,
            circuit_pct=rg.circuit_pct,
            circuit_active_today=rg.is_circuit_broken_today(),
            new_position_mode=_new_position_mode(),
        )
    except Exception as exc:
        logger.exception("[advisor.runner] decide_once 失敗，本輪 skip")
        append_jsonl(audit_p, {
            "type": "decision_round",
            "time": ts,
            "provider_id": client.provider_id,
            "model": client.model,
            "status": "decide_failed",
            "dry_run": dry_run,
            "error": str(exc)[:300],
        })
        return {
            "status": "decide_failed",
            "decided": 0, "passed": 0, "submitted": 0,
            "blocked": 0, "hold": 0,
            "circuit_triggered_this_round": circuit_triggered,
            "summary": None, "metrics_self_eval": None,
            "decisions_log": [],
            "error": str(exc)[:300],
        }

    # 1.39) 保守觀察期：先暫停自主新開倉，避免後續 gate 做無效計算
    decisions, no_new_items = _gate_no_new_positions_mode(decisions, engine)
    if no_new_items:
        append_jsonl(audit_p, {
            "type": "no_new_positions_gate",
            "time": ts,
            "downgraded": len(no_new_items),
            "items": no_new_items,
        })

    # 1.4) 低質量 buy 降級 hold（P0-2：cross_signals 或 short_term score ≥ 80 才放行）
    decisions, gated_items = _gate_low_quality_buys(decisions, watchlist_path)
    if gated_items:
        append_jsonl(audit_p, {
            "type": "buy_quality_gate",
            "time": ts,
            "downgraded": len(gated_items),
            "items": gated_items,
        })

    # 1.45) stop 距離硬下限（P0-4：< 5% 的 buy 降級 hold，防盤中震盪掃止損）
    quote_cache: dict[str, float] = {}
    # pre-fetch quotes for MKT buys (no price field) so stop gate can calculate distance
    _buy_syms_need_quote = [
        d.symbol for d in decisions
        if d.action == "buy" and d.symbol and not d.price and d.stop_loss
    ]
    if _buy_syms_need_quote and feed is not None:
        try:
            quote_cache.update(feed.get_quotes(_buy_syms_need_quote) or {})
        except Exception:
            logger.exception("[advisor.runner] pre-fetch quotes for stop gate failed")
    decisions, stop_gate_items = _gate_stop_too_tight(decisions, quote_cache)
    if stop_gate_items:
        append_jsonl(audit_p, {
            "type": "stop_distance_gate",
            "time": ts,
            "downgraded": len(stop_gate_items),
            "items": stop_gate_items,
        })

    # 1.451) 槓反 ETF 正反互斥：同 underlying 不同方向不可同時持有或同輪買入
    decisions, leveraged_conflict_items = _gate_leveraged_etf_exposure(decisions, engine, watchlist_path)
    if leveraged_conflict_items:
        append_jsonl(audit_p, {
            "type": "leveraged_etf_exposure_gate",
            "time": ts,
            "downgraded": len(leveraged_conflict_items),
            "items": leveraged_conflict_items,
        })

    # 1.46) 每日新開倉上限（P0-5：避免過度分散，已有持倉的加倉不受限；probe 模式走動態額度）
    decisions, daily_cap_items = _gate_daily_new_position_cap(
        decisions, engine, fills_path,
        watchlist_path=watchlist_path, feed=feed, quote_cache=quote_cache,
    )
    if daily_cap_items:
        append_jsonl(audit_p, {
            "type": "daily_new_position_cap",
            "time": ts,
            "downgraded": len(daily_cap_items),
            "items": daily_cap_items,
        })

    # 1.461) 早盤冷靜期：開盤前 45 分鐘不開新 symbol，避免假突破快速反轉
    decisions, morning_items = _gate_morning_new_buys(decisions, engine)
    if morning_items:
        append_jsonl(audit_p, {
            "type": "morning_cooldown_gate",
            "time": ts,
            "downgraded": len(morning_items),
            "items": morning_items,
        })

    # 1.462) 近期虧損標的 probation：剛虧過的 symbol 暫停新買，避免連續試錯
    decisions, probation_items = _gate_recent_loss_probation(decisions, engine, fills_path)
    if probation_items:
        append_jsonl(audit_p, {
            "type": "loss_probation_gate",
            "time": ts,
            "downgraded": len(probation_items),
            "items": probation_items,
        })

    # 1.47) 倉位管理：固定分數法 clamp qty（P0-6：不擋掉、只縮小到合規值）
    decisions, qty_clamp_items = _clamp_qty_by_risk(decisions, quote_cache, engine, watchlist_path=watchlist_path)
    if qty_clamp_items:
        append_jsonl(audit_p, {
            "type": "qty_clamped_by_risk",
            "time": ts,
            "adjusted": len(qty_clamp_items),
            "items": qty_clamp_items,
        })

    # 1.5) 退池浮虧持倉強制 sell sweep（P0-3：不依 LLM 主觀判斷）
    forced_sells, force_sell_log = _build_forced_sells_for_stale_losers(
        engine, feed, watchlist_path, decisions, quote_cache,
    )
    if forced_sells:
        decisions = list(decisions) + forced_sells
        append_jsonl(audit_p, {
            "type": "forced_sell_sweep",
            "time": ts,
            "count": len(forced_sells),
            "items": force_sell_log,
        })

    # 1.6) 過度交易防火牆：同檔 cooldown gate（2026-05-14：擋砍完回補 / 連續加倉吃摩擦）
    decisions, cooldown_items = _gate_recent_cooldown(decisions, fills_path)
    if cooldown_items:
        append_jsonl(audit_p, {
            "type": "cooldown_gate",
            "time": ts,
            "downgraded": len(cooldown_items),
            "items": cooldown_items,
        })

    # 2) 對每筆 evaluate + submit
    submitted = blocked = passed = hold = 0
    per_decision_log: list[dict[str, Any]] = []

    for d in decisions:
        if d.action == "hold":
            hold += 1
            per_decision_log.append({
                "action": "hold", "symbol": d.symbol, "reason": d.reason,
            })
            continue

        ref_q: Optional[float] = None
        if d.symbol:
            if d.symbol not in quote_cache:
                try:
                    quote_cache.update(feed.get_quotes([d.symbol]) or {})
                except Exception:
                    logger.exception("[advisor.runner] feed.get_quotes(%s) 失敗", d.symbol)
            ref_q = quote_cache.get(d.symbol)

        gr = rg.evaluate(d, current_quote=ref_q)
        if not gr.passed:
            blocked += 1
            per_decision_log.append({
                "action": d.action, "symbol": d.symbol, "qty": d.qty,
                "result": "blocked", "reason": gr.reason,
            })
            continue

        passed += 1
        if dry_run:
            per_decision_log.append({
                "action": d.action, "symbol": d.symbol, "qty": d.qty,
                "type": d.type, "price": d.price, "stop_loss": d.stop_loss,
                "result": "dry_run_pass", "reason": d.reason,
                "source_pools": list(d.source_pools or []),
            })
            continue

        # live submit
        order = _decision_to_order(d)
        if order is None:
            per_decision_log.append({
                "action": d.action, "symbol": d.symbol,
                "result": "convert_to_order_failed",
                "reason": f"qty={d.qty!r} type={d.type!r}",
            })
            continue
        try:
            engine.submit_order(order)
            submitted += 1
            per_decision_log.append({
                "action": d.action, "symbol": d.symbol, "qty": d.qty,
                "result": "submitted",
                "order_id": order.order_id,
                "order_status": order.status.value,
                "reason": d.reason,
            })
        except Exception as exc:
            logger.exception("[advisor.runner] submit_order 失敗 %s", d.symbol)
            per_decision_log.append({
                "action": d.action, "symbol": d.symbol,
                "result": "submit_exception", "error": str(exc)[:200],
            })

    # 3) M6.1：advisor 進程內 submit 後立即撮合一輪
    # advisor 跟 feishu_bot 是兩個獨立 process（PLAN 風險點 9）；feishu_bot 內
    # MatchEngine 看不到 advisor 寫進 orders.jsonl 的訂單，advisor 自己 init 時
    # replay fills，pending_orders 起始為空。本輪 submit 完直接 match() 一次吃掉
    # MKT + 偶發符合 quote 的 LMT；LMT/STOP/TP 跨進程撮合靠 dispatch 子命令
    # （M6.1-B：每 30 秒 replay PENDING + match）。
    matched_immediately = 0
    auto_stop_records: list[dict[str, Any]] = []
    if not dry_run and submitted > 0:
        try:
            sym_set = sorted({d.symbol for d in decisions if d.symbol})
            quotes_for_match: dict[str, float] = {
                s: q for s, q in quote_cache.items() if s in sym_set
            }
            missing = [s for s in sym_set if s not in quotes_for_match]
            if missing:
                try:
                    quotes_for_match.update(feed.get_quotes(missing) or {})
                except Exception:
                    logger.exception("[advisor.runner] post-submit feed.get_quotes 失敗")
            if quotes_for_match:
                fills = engine.match(quotes_for_match)
                if fills:
                    engine.apply_fills(fills)
                    matched_immediately = len(fills)
                    logger.info(
                        "[advisor.runner] post-submit 立即撮合 %d 筆", matched_immediately,
                    )

                    # M6 階段二 P1：BUY 成交後同步 auto-stop SELL（含部分減倉重對齊 qty）
                    try:
                        stop_loss_by_symbol: dict[str, float] = {}
                        for d in decisions:
                            if d.action == "buy" and d.symbol and d.stop_loss:
                                stop_loss_by_symbol[d.symbol] = float(d.stop_loss)
                        auto_stop_records = sync_auto_stops_after_fills(
                            engine, fills, stop_loss_by_symbol,
                        )
                        if auto_stop_records:
                            logger.info(
                                "[advisor.runner] auto-stop sync %d 檔",
                                len(auto_stop_records),
                            )
                    except Exception:
                        logger.exception("[advisor.runner] sync_auto_stops 失敗（不阻塞撮合）")

                    try:
                        from sim._notify import notify_fill
                        snapshot = engine.snapshot(quotes_for_match)
                        for fill in fills:
                            notify_fill(fill.to_dict(), snapshot)
                    except Exception:
                        logger.exception("[advisor.runner] notify_fill 失敗（不阻塞撮合）")

                    # trailing stop 階梯上移
                    try:
                        from sim.advisor.auto_trail import maybe_raise_trailing_stops
                        trail_records = maybe_raise_trailing_stops(engine, quotes_for_match)
                        if trail_records:
                            logger.info("[advisor.runner] trail raised %d 檔", len(trail_records))
                    except Exception:
                        logger.exception("[advisor.runner] trailing stop 失敗（不阻塞）")
        except Exception:
            logger.exception("[advisor.runner] post-submit match 失敗（不阻擋 audit）")

    # trailing stop 也在每輪 run（即使沒有新 submit，quote 更新也可能觸發上移）
    if not dry_run and engine is not None:
        try:
            from sim.advisor.auto_trail import maybe_raise_trailing_stops
            # 取所有持倉 symbol 的 quote
            all_held = sorted({
                s for s, p in (engine.account.positions or {}).items()
                if getattr(p, "qty", 0) > 0
            })
            trail_quotes = dict(quote_cache)
            missing_trail = [s for s in all_held if s not in trail_quotes]
            if missing_trail and feed is not None:
                try:
                    trail_quotes.update(feed.get_quotes(missing_trail) or {})
                except Exception:
                    pass
            trail_records_all = maybe_raise_trailing_stops(engine, trail_quotes)
            if trail_records_all:
                logger.info("[advisor.runner] trail raised (full check) %d 檔", len(trail_records_all))
        except Exception:
            logger.exception("[advisor.runner] trailing stop full check 失敗（不阻塞）")

    # 4) audit decision_round
    summary_text = raw.get("summary") if isinstance(raw, dict) else None
    self_eval = raw.get("metrics_self_eval") if isinstance(raw, dict) else None
    append_jsonl(audit_p, {
        "type": "decision_round",
        "time": ts,
        "provider_id": client.provider_id,
        "model": client.model,
        "status": "ok",
        "dry_run": dry_run,
        "circuit_triggered_this_round": circuit_triggered,
        "decided": len(decisions),
        "passed": passed,
        "submitted": submitted,
        "blocked": blocked,
        "hold": hold,
        "matched_immediately": matched_immediately,
        "auto_stop_sync": auto_stop_records,
        "summary": summary_text,
        "metrics_self_eval": self_eval,
        "decisions_log": per_decision_log,
    })

    return {
        "status": "ok",
        "decided": len(decisions),
        "passed": passed,
        "submitted": submitted,
        "blocked": blocked,
        "hold": hold,
        "matched_immediately": matched_immediately,
        "auto_stop_sync": auto_stop_records,
        "circuit_triggered_this_round": circuit_triggered,
        "summary": summary_text,
        "metrics_self_eval": self_eval,
        "decisions_log": per_decision_log,
    }


# P0-2：short_term 入場分數門檻；TODO 未來移到 sim_config.yaml advisor.signal_quality
_MIN_SHORT_TERM_SCORE_FOR_BUY = 85
_LEVERAGED_ETF_POOL = "leveraged_etf"
_DEFAULT_LEVERAGED_ETF_RISK_MULTIPLIER = 0.5


def _load_leveraged_etf_map(watchlist_path: Optional[Path]) -> dict[str, dict[str, Any]]:
    """讀 watchlist auxiliary_signals.leveraged_etf，回傳 symbol → ETF metadata。"""
    try:
        pools = load_watchlist_pools(watchlist_path)
    except Exception:
        logger.exception("[advisor.runner] load_watchlist_pools failed in leveraged ETF map")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in pools.get(_LEVERAGED_ETF_POOL) or []:
        if isinstance(item, dict) and item.get("symbol"):
            out[str(item["symbol"])] = item
    return out

# 過度交易防火牆（2026-05-14：對應同檔砍完回補 / 短時間連續加倉摩擦問題）
# 被 advisor 主動 sell（含 forced_sell / auto-stop 觸發）後，同檔 N 分鐘內禁止再 buy
_COOLDOWN_AFTER_SELL_MINUTES = 60
# 同檔同向 buy 之間至少間隔 M 分鐘（擋三輪連續加倉式吃摩擦）
_COOLDOWN_BETWEEN_BUYS_MINUTES = 30


def _gate_low_quality_buys(
    decisions: list[Decision],
    watchlist_path: Optional[Path],
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """P0-2：過濾低質量 buy decision。

    放行條件（任一即可）：
      - 該檔在 cross_signals（多池命中，高信心）
      - 該檔在 short_term 池且 score ≥ 80（單池但分數夠高）

    擋下條件：
      - 不在 cross 且（不在 short_term 池 / short_term score < 80 / score 缺失）
      - 該筆 buy 替換為 hold + reason=buy_signal_below_threshold
    其他 action（sell / hold）原樣回傳。
    """
    try:
        pools = load_watchlist_pools(watchlist_path)
    except Exception:
        logger.exception("[advisor.runner] load_watchlist_pools failed in buy gate")
        return list(decisions), []

    cross_set: set[str] = set((pools.get("cross_signals") or {}).keys())
    leveraged_map: dict[str, dict[str, Any]] = {}
    for item in pools.get(_LEVERAGED_ETF_POOL) or []:
        if isinstance(item, dict) and item.get("symbol"):
            leveraged_map[str(item["symbol"])] = item
    short_term_score: dict[str, Optional[int]] = {}
    for item in pools.get("short_term") or []:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if not sym:
            continue
        sc = item.get("score")
        try:
            short_term_score[sym] = int(sc) if sc is not None else None
        except (TypeError, ValueError):
            short_term_score[sym] = None

    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if d.action != "buy" or not d.symbol:
            out.append(d)
            continue
        sym = d.symbol
        if sym in cross_set:
            out.append(d)
            continue
        score = short_term_score.get(sym)
        if score is not None and score >= _MIN_SHORT_TERM_SCORE_FOR_BUY:
            out.append(d)
            continue
        leveraged = leveraged_map.get(sym)
        if leveraged:
            try:
                leveraged_score = int(leveraged.get("score") or 0)
                min_score = int(leveraged.get("min_score") or _MIN_SHORT_TERM_SCORE_FOR_BUY)
            except (TypeError, ValueError):
                leveraged_score = 0
                min_score = _MIN_SHORT_TERM_SCORE_FOR_BUY
            if leveraged_score >= min_score:
                out.append(d)
                continue
        gated.append({
            "symbol": sym,
            "qty": d.qty,
            "original_reason": d.reason,
            "short_term_score": score,
            "leveraged_etf_score": (leveraged or {}).get("score"),
            "in_leveraged_etf_pool": bool(leveraged),
            "in_cross_signals": False,
            "threshold": _MIN_SHORT_TERM_SCORE_FOR_BUY,
        })
        out.append(Decision(
            action="hold", symbol=sym,
            reason=f"buy_signal_below_threshold(score={score} cross=no min={_MIN_SHORT_TERM_SCORE_FOR_BUY})",
            source_pools=list(d.source_pools or []),
        ))
    return out, gated


def _gate_leveraged_etf_exposure(
    decisions: list[Decision],
    engine: Optional[MatchEngine],
    watchlist_path: Optional[Path],
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """槓反 ETF 硬風控：同 underlying 正反方向不可同時持有/買入。

    只擋 buy；sell/hold 原樣放行。已有同 underlying 同方向持倉可加倉（仍受集中度/risk guard）。
    """
    etf_map = _load_leveraged_etf_map(watchlist_path)
    if not etf_map:
        return list(decisions), []

    held_symbols = _held_symbols(engine)
    held_by_underlying: dict[str, set[str]] = {}
    for sym in held_symbols:
        meta = etf_map.get(sym)
        if not meta:
            continue
        underlying = str(meta.get("underlying") or sym)
        direction = str(meta.get("direction") or "")
        held_by_underlying.setdefault(underlying, set()).add(direction)

    pending_direction_by_underlying: dict[str, set[str]] = {}
    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if d.action != "buy" or not d.symbol:
            out.append(d)
            continue
        meta = etf_map.get(d.symbol)
        if not meta:
            out.append(d)
            continue
        underlying = str(meta.get("underlying") or d.symbol)
        direction = str(meta.get("direction") or "")
        opposite = "inverse" if direction == "long" else "long" if direction == "inverse" else ""
        held_dirs = held_by_underlying.get(underlying, set())
        pending_dirs = pending_direction_by_underlying.get(underlying, set())
        if opposite and (opposite in held_dirs or opposite in pending_dirs):
            gated.append({
                "symbol": d.symbol,
                "qty": d.qty,
                "underlying": underlying,
                "direction": direction,
                "blocked_by_direction": opposite,
                "held_directions": sorted(held_dirs),
                "round_pending_directions": sorted(pending_dirs),
                "original_reason": d.reason,
            })
            out.append(Decision(
                action="hold", symbol=d.symbol,
                reason=f"leveraged_etf_direction_conflict(underlying={underlying} direction={direction})",
                source_pools=list(d.source_pools or []),
            ))
            continue
        pending_direction_by_underlying.setdefault(underlying, set()).add(direction)
        out.append(d)
    return out, gated


_MIN_STOP_DISTANCE_PCT = 0.05  # 硬下限 5%：stop 距離 < 5% 一律降級 hold


def _gate_stop_too_tight(
    decisions: list[Decision],
    quote_cache: dict[str, float],
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """P0-4：stop_loss 距離 < 5% 的 buy 降級為 hold。

    stop 距離 = (entry_price - stop_loss) / entry_price
    entry_price 用 quote_cache 的即時價（MKT 沒有 price 欄位）或 decision.price。
    """
    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if d.action != "buy" or not d.symbol:
            out.append(d)
            continue
        stop = float(d.stop_loss or 0)
        if stop <= 0:
            out.append(d)  # 沒 stop 的會被 decision 層擋，這裡不重複擋
            continue
        ref = float(d.price or 0) or quote_cache.get(d.symbol, 0)
        if ref <= 0:
            out.append(d)
            continue
        distance = (ref - stop) / ref
        if distance < _MIN_STOP_DISTANCE_PCT:
            gated.append({
                "symbol": d.symbol,
                "qty": d.qty,
                "ref_price": ref,
                "stop_loss": stop,
                "distance_pct": round(distance * 100, 2),
                "min_required_pct": round(_MIN_STOP_DISTANCE_PCT * 100, 1),
                "original_reason": d.reason,
            })
            out.append(Decision(
                action="hold", symbol=d.symbol,
                reason=f"stop_too_tight(distance={distance*100:.1f}% < min={_MIN_STOP_DISTANCE_PCT*100:.0f}%)",
                source_pools=list(d.source_pools or []),
            ))
        else:
            out.append(d)
    return out, gated


_BASE_NEW_POSITIONS_PER_DAY = 1  # probe 基礎額度：每日先放行第 1 筆新倉
_MAX_NEW_POSITIONS_PER_DAY = 3  # 動態/flat 新開倉硬上限（已有持倉的加倉不計）
_MAX_NEW_POSITIONS_PER_ROUND = 1  # probe：每個 5 分鐘決策輪最多送出 1 筆新 symbol buy
_UNLOCK_SECOND_MIN_SCORE = 90  # probe 解鎖第 2 筆：需 cross 或 short_term score ≥ 90
_UNLOCK_THIRD_MIN_SCORE = 95   # probe 解鎖第 3 筆：需 cross 或 short_term score ≥ 95（更嚴）
# 第 3 筆額外風控門檻（皆可調）：條件全達成才放行，否則保守擋下。
_BASELINE_WEAK_PCT = -0.003            # 0050 當日 < -0.3% 視為大盤偏弱，不給第 3 筆
_THIRD_HEALTH_MAX_LOSS_PCT = 0.03      # 當日任一新倉浮虧 > 3% → 持倉不健康，不給第 3 筆
_MAX_CONCURRENT_POSITIONS = 8          # 持倉檔數 ≥ 8 → 分散度已滿，不給第 3 筆
_MIN_CASH_RATIO_FOR_THIRD = 0.10       # 現金/權益 < 10% → 曝險已高，不給第 3 筆
_BASELINE_SYMBOL = "0050.TW"           # 大盤趨勢基準
_NEW_POSITION_MODE_ENV = "SIM_ADVISOR_NEW_POSITION_MODE"  # defensive(預設)|probe；未設時沿用 NO_NEW_POSITIONS
_MORNING_NEW_BUY_BLOCK_UNTIL = dtime(9, 45)  # 早盤冷靜期：09:00–09:45 不開新 symbol
_LOSS_PROBATION_DAYS = 3  # 近 N 個自然日有 realized loss 的 symbol 暫停新買
_NO_NEW_POSITIONS_MODE = True  # 預設防守：等同 NEW_POSITION_MODE=defensive


def _held_symbols(engine: Optional[MatchEngine]) -> set[str]:
    """回傳目前帳戶內 qty > 0 的 symbol。"""
    held: set[str] = set()
    if engine and getattr(engine, "account", None):
        for sym, pos in (engine.account.positions or {}).items():
            if getattr(pos, "qty", 0) > 0:
                held.add(sym)
    return held


def _is_new_position_buy(d: Decision, held_symbols: set[str]) -> bool:
    """Decision 是否為新 symbol buy；已有持倉的加倉不算新開倉。"""
    return d.action == "buy" and bool(d.symbol) and d.symbol not in held_symbols


def _start_of_day_held_symbols(
    fills_path: Optional[Path],
    *,
    day: datetime,
) -> set[str]:
    """用 fills replay 到 day 00:00 前，回傳當日開盤前已持有的 symbols。

    用途：daily_new_position_cap 計算今日新 symbol 時，排除「昨天已持有、今天只是加倉」
    的標的，避免加倉誤消耗新倉額度。fills 壞檔/缺檔時 fail-open 回空集合。
    """
    if not fills_path:
        return set()
    p = Path(fills_path)
    if not p.exists():
        return set()
    cutoff = day.replace(hour=0, minute=0, second=0, microsecond=0)
    rows: list[dict[str, Any]] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    fill = json.loads(line)
                except Exception:
                    continue
                raw_ts = fill.get("filled_at") or fill.get("time")
                if not raw_ts:
                    continue
                try:
                    ts = datetime.fromisoformat(str(raw_ts))
                except Exception:
                    continue
                if ts < cutoff:
                    rows.append(fill)
    except Exception:
        logger.exception("[advisor.runner] start_of_day replay 讀 fills 失敗")
        return set()
    try:
        from sim._position_math import replay_positions_from_fills
        replay = replay_positions_from_fills(rows)
    except Exception:
        logger.exception("[advisor.runner] start_of_day replay positions 失敗")
        return set()
    positions = replay.get("positions") if isinstance(replay, dict) else {}
    return {
        sym for sym, info in (positions or {}).items()
        if isinstance(info, dict) and int(info.get("qty", 0) or 0) > 0
    }


def _load_score_cross(
    watchlist_path: Optional[Path],
) -> tuple[set[str], dict[str, Optional[int]]]:
    """讀 watchlist 四池，回傳 (cross_signals symbol 集合, short_term {symbol: score})。

    供 probe 動態額度判高信心用。讀取失敗回空集合 —— 對 probe 而言「讀不到品質資料」
    應導致「不解鎖第 2 筆以上」(fail-closed)，與 buy 品質 gate 的 fail-open 語義不同，
    因此這裡不與 `_gate_low_quality_buys` 共用回傳，刻意各自保留失敗行為。
    """
    try:
        pools = load_watchlist_pools(watchlist_path)
    except Exception:
        logger.exception("[advisor.runner] load_watchlist_pools 失敗（probe 額度）")
        return set(), {}
    cross_set: set[str] = set((pools.get("cross_signals") or {}).keys())
    short_term_score: dict[str, Optional[int]] = {}
    for item in pools.get("short_term") or []:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if not sym:
            continue
        sc = item.get("score")
        try:
            short_term_score[sym] = int(sc) if sc is not None else None
        except (TypeError, ValueError):
            short_term_score[sym] = None
    for item in pools.get(_LEVERAGED_ETF_POOL) or []:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if not sym:
            continue
        sc = item.get("score")
        try:
            short_term_score[str(sym)] = int(sc) if sc is not None else None
        except (TypeError, ValueError):
            short_term_score[str(sym)] = None
    return cross_set, short_term_score


_SECTOR_UNIVERSE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "stock_universe.yaml"
)


def _load_sector_map(
    universe_path: Optional[Path] = None,
    watchlist_path: Optional[Path] = None,
) -> dict[str, str]:
    """symbol → 產業別對照（probe 第 2/3 筆「產業不重疊」判定用）。

    主源 config/stock_universe.yaml 的 universe[].{symbol,sector}（涵蓋 screener 掃描範圍）；
    再用 watchlist long_term 池內帶 sector 的標的補充（價值股池不一定落在 screener universe）。
    讀不到 / 缺 sector 的 symbol 不進 map —— 呼叫端遇「未知產業」一律不據此擋單（fail-open），
    只有「候選與既有持倉產業皆已知且相同」才算重疊。universe 為主，watchlist 只補空缺。
    """
    sector_map: dict[str, str] = {}
    path = Path(universe_path) if universe_path else _SECTOR_UNIVERSE_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for item in data.get("universe") or []:
            if not isinstance(item, dict):
                continue
            sym, sec = item.get("symbol"), item.get("sector")
            if sym and sec:
                sector_map.setdefault(str(sym), str(sec))
    except Exception:
        logger.exception("[advisor.runner] 讀 stock_universe sector 失敗 %s", path)
    try:
        pools = load_watchlist_pools(watchlist_path)
        for key in ("long_term", _LEVERAGED_ETF_POOL):
            for item in pools.get(key) or []:
                if not isinstance(item, dict):
                    continue
                sym, sec = item.get("symbol"), item.get("sector")
                if sym and sec:
                    sector_map.setdefault(str(sym), str(sec))
    except Exception:
        logger.exception("[advisor.runner] watchlist long_term sector 補充失敗")
    return sector_map


def _held_sectors(held_symbols: set[str], sector_map: dict[str, str]) -> set[str]:
    """既有持倉中「產業別已知」的集合（未知產業不納入，避免誤擋新倉）。"""
    return {sector_map[s] for s in held_symbols if s in sector_map}


def _sector_overlap(
    symbol: str,
    sector_map: dict[str, str],
    held_sectors: set[str],
) -> Optional[bool]:
    """候選 symbol 是否與既有持倉產業重疊。

    True=重疊、False=不重疊、None=候選產業未知（無法判定，呼叫端不據此擋單）。
    """
    sec = sector_map.get(symbol)
    if not sec:
        return None
    return sec in held_sectors


def _today_realized_pnl(
    fills_path: Optional[Path],
    *,
    now: datetime,
) -> Optional[float]:
    """當日（now 日期）已實現 PnL 合計；realized 以 fills replay 為真源。

    當日無平倉 → 0.0；無 fills / 讀取失敗 → None（未知，呼叫端需保守處理）。
    口徑與 reports._per_fill_realized 一致（fee 攤入 avg_cost、SELL 用既有 avg_cost）。
    """
    if not fills_path or not Path(fills_path).exists():
        return None
    today = now.strftime("%Y-%m-%d")
    try:
        from sim.reports import _per_fill_realized
        realized_by_fill = _per_fill_realized(fills_path)
    except Exception:
        logger.exception("[advisor.runner] today realized 讀取失敗")
        return None
    total = 0.0
    seen = False
    try:
        for line in Path(fills_path).read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            f = json.loads(line)
            if str(f.get("filled_at") or f.get("time") or "")[:10] != today:
                continue
            r = realized_by_fill.get(f.get("fill_id"))
            if r is not None:
                total += float(r)
                seen = True
    except Exception:
        logger.exception("[advisor.runner] today realized 解析失敗")
        return None
    return total if seen else 0.0


def _baseline_today_trend(symbol: str = _BASELINE_SYMBOL) -> Optional[float]:
    """大盤基準（0050）今日相對前一交易日收盤的漲跌幅（小數，0.01=+1%）。

    用 yfinance 近 5 日日線，取最後一根（盤中為暫定值）相對前一根收盤；
    網路/rate limit/資料不足 → None（呼叫端保守處理）。
    僅在 probe 評估「第 3 筆」時呼叫（罕見路徑），故同步網路成本可接受；
    參數化為獨立函數，未來可換本地 intraday baseline 源、測試可注入避免打網路。
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="5d", interval="1d")
    except Exception:
        logger.exception("[advisor.runner] baseline 今日趨勢拉取失敗 %s", symbol)
        return None
    try:
        if hist is None or len(hist) < 2:
            return None
        prev_close = float(hist["Close"].iloc[-2])
        last = float(hist["Close"].iloc[-1])
    except (KeyError, IndexError, ValueError):
        logger.exception("[advisor.runner] baseline close 欄位異常 %s", symbol)
        return None
    if prev_close <= 0:
        return None
    return (last - prev_close) / prev_close


def _build_third_position_context(
    engine: Optional[MatchEngine],
    today_new_symbols: set[str],
    *,
    quote_map: Optional[dict[str, float]] = None,
    baseline_trend: Optional[float] = None,
    feed: Any = None,
) -> dict[str, Any]:
    """組第 3 筆新倉的風控 context：大盤趨勢 / 集中度·現金 / 當日新倉健康度。

    baseline_trend 為 None 時才打 yfinance（測試可直接注入）；quote 缺時用成本基礎退化。
    回傳含 *_ok 旗標與診斷欄位；任一 ok 為 False/None → 呼叫端擋第 3 筆並標明原因。
    """
    bt = baseline_trend if baseline_trend is not None else _baseline_today_trend()
    baseline_ok = bt is not None and bt >= _BASELINE_WEAK_PCT

    account = getattr(engine, "account", None) if engine else None
    positions = dict(getattr(account, "positions", {}) or {})
    cash = _safe_account_cash(account)

    held = {s: p for s, p in positions.items() if getattr(p, "qty", 0) > 0}
    qmap = dict(quote_map or {})
    missing = [s for s in held if s not in qmap]
    if missing and feed is not None:
        try:
            qmap.update(feed.get_quotes(missing) or {})
        except Exception:
            logger.exception("[advisor.runner] 第3筆 context 補 quote 失敗")

    # 權益 / 現金比（quote 缺退回成本基礎）
    pos_value = 0.0
    for s, p in held.items():
        qty = getattr(p, "qty", 0)
        q = qmap.get(s)
        pos_value += qty * (q if (q and q > 0) else getattr(p, "avg_cost", 0.0))
    equity = cash + pos_value
    cash_ratio = (cash / equity) if equity > 0 else 0.0
    n_positions = len(held)
    concentration_ok = n_positions < _MAX_CONCURRENT_POSITIONS and cash_ratio >= _MIN_CASH_RATIO_FOR_THIRD

    # 當日新倉健康度：每檔浮虧不得 > 門檻；quote 缺 → 無法確認 → 視為不健康（保守）
    health_ok = True
    unhealthy: list[str] = []
    for s in sorted(today_new_symbols):
        p = held.get(s)
        if p is None:  # 今日新倉已被平掉 → 不影響第 3 筆
            continue
        avg = getattr(p, "avg_cost", 0.0)
        q = qmap.get(s)
        if (q is None or q <= 0) and feed is not None:
            try:
                q = (feed.get_quotes([s]) or {}).get(s)
            except Exception:
                q = None
        if q is None or q <= 0 or avg <= 0:
            health_ok = False
            unhealthy.append(f"{s}:noquote")
            continue
        chg = (q - avg) / avg
        if chg < -_THIRD_HEALTH_MAX_LOSS_PCT:
            health_ok = False
            unhealthy.append(f"{s}:{chg * 100:.1f}%")

    return {
        "baseline_trend": None if bt is None else round(bt, 4),
        "baseline_ok": baseline_ok,
        "cash_ratio": round(cash_ratio, 3),
        "n_positions": n_positions,
        "concentration_ok": concentration_ok,
        "health_ok": health_ok,
        "unhealthy": unhealthy,
    }


def _safe_account_cash(account: Any) -> float:
    try:
        return float(getattr(account, "cash", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _new_position_mode() -> str:
    """回傳新倉模式：'defensive'（預設防守）| 'probe'（條件式試單）。

    優先讀 SIM_ADVISOR_NEW_POSITION_MODE；未設時沿用既有 NO_NEW_POSITIONS 開關
    （保守開啟→defensive，關閉→probe），避免新增多個不可追蹤的平行開關。
    未知值一律回退 defensive。
    """
    raw = os.environ.get(_NEW_POSITION_MODE_ENV)
    if raw is not None:
        val = raw.strip().lower()
        return val if val in {"probe", "defensive"} else "defensive"
    no_new = _env_flag("SIM_ADVISOR_NO_NEW_POSITIONS_MODE", _NO_NEW_POSITIONS_MODE)
    return "defensive" if no_new else "probe"


def _evaluate_new_position_slot(
    *,
    symbol: str,
    slot: int,
    probe: bool,
    accepted_this_round: int,
    cross_set: set[str],
    short_term_score: dict[str, Optional[int]],
    today_realized: Optional[float],
    third_ctx: Optional[dict[str, Any]] = None,
    sector_map: Optional[dict[str, str]] = None,
    held_sectors: Optional[set[str]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """判斷「今日第 slot 筆新倉」是否放行；回傳 (allow, block_reason, detail)。

    - 硬上限：任何模式都不可超過 _MAX_NEW_POSITIONS_PER_DAY。
    - defensive/flat：slot ≤ 上限即放行（維持既有行為；正式防守由 no_new gate 統一擋新倉）。
    - probe：基礎 1 筆；單輪最多 1 筆；
      第 2 筆需 cross 或 score≥90、當日 realized≥0、且與既有持倉產業不重疊；
      第 3 筆更嚴（再加 0050 不弱 + 當日新倉健康 + 集中度/現金充足），同樣要求產業不重疊。
      產業未知（candidate 或持倉查不到 sector）一律不據此擋單（fail-open）。
    """
    sector_map = sector_map or {}
    held_sectors = held_sectors or set()
    if slot > _MAX_NEW_POSITIONS_PER_DAY:
        return False, "hard_cap", {}
    if not probe:
        return True, "", {}

    # probe：單輪最多 1 筆新 symbol（避免同一個 5 分鐘決策輪一次追多）
    if accepted_this_round >= _MAX_NEW_POSITIONS_PER_ROUND:
        return False, "round_cap", {"max_per_round": _MAX_NEW_POSITIONS_PER_ROUND}

    score = short_term_score.get(symbol)
    in_cross = symbol in cross_set

    # 第 1 筆：基礎額度（已過前面 quality / stop / probation gate）
    if slot <= _BASE_NEW_POSITIONS_PER_DAY:
        return True, "", {"unlock": "base", "in_cross": in_cross, "short_term_score": score}

    # 第 2 筆：高信心（cross 或 score≥90）+ 當日 realized 未轉負 + 與既有持倉產業不重疊
    if slot == 2:
        high_conf = in_cross or (score is not None and score >= _UNLOCK_SECOND_MIN_SCORE)
        realized_ok = today_realized is not None and today_realized >= 0
        overlap = _sector_overlap(symbol, sector_map, held_sectors)
        detail = {
            "in_cross": in_cross,
            "short_term_score": score,
            "today_realized": None if today_realized is None else round(today_realized, 2),
            "unlock_second_min_score": _UNLOCK_SECOND_MIN_SCORE,
            "candidate_sector": sector_map.get(symbol),
            "sector_overlap": overlap,
        }
        if not high_conf:
            return False, "second_locked_low_conf", detail
        if not realized_ok:
            return False, "second_locked_realized_negative", detail
        if overlap:
            return False, "second_locked_sector_overlap", detail
        return True, "", {**detail, "unlock": "second"}

    # 第 3 筆：條件式解鎖（每多一筆條件更嚴）。需全部達成才放行：
    #   高信心(cross/score≥95) + 當日 realized≥0 + 大盤不弱 + 當日新倉健康 + 集中度/現金充足。
    # 任一條件不成立（含資料不可得）一律保守擋下，並標明卡在哪一關。
    if slot == 3:
        high_conf = in_cross or (score is not None and score >= _UNLOCK_THIRD_MIN_SCORE)
        realized_ok = today_realized is not None and today_realized >= 0
        overlap = _sector_overlap(symbol, sector_map, held_sectors)
        ctx = third_ctx or {}
        detail = {
            "in_cross": in_cross,
            "short_term_score": score,
            "today_realized": None if today_realized is None else round(today_realized, 2),
            "unlock_third_min_score": _UNLOCK_THIRD_MIN_SCORE,
            "baseline_trend": ctx.get("baseline_trend"),
            "baseline_ok": ctx.get("baseline_ok"),
            "cash_ratio": ctx.get("cash_ratio"),
            "n_positions": ctx.get("n_positions"),
            "concentration_ok": ctx.get("concentration_ok"),
            "health_ok": ctx.get("health_ok"),
            "unhealthy": ctx.get("unhealthy"),
            "candidate_sector": sector_map.get(symbol),
            "sector_overlap": overlap,
        }
        if not high_conf:
            return False, "third_locked_low_conf", detail
        if not realized_ok:
            return False, "third_locked_realized_negative", detail
        if not ctx.get("baseline_ok"):
            return False, "third_locked_market_weak", detail
        if not ctx.get("health_ok"):
            return False, "third_locked_position_unhealthy", detail
        if not ctx.get("concentration_ok"):
            return False, "third_locked_concentration", detail
        if overlap:
            return False, "third_locked_sector_overlap", detail
        return True, "", {**detail, "unlock": "third"}

    # slot > 3 已於開頭 hard_cap 擋下，不會到這
    return False, "hard_cap", {}


def _gate_daily_new_position_cap(
    decisions: list[Decision],
    engine: Optional[MatchEngine],
    fills_path: Optional[Path],
    *,
    watchlist_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    mode: Optional[str] = None,
    feed: Any = None,
    quote_cache: Optional[dict[str, float]] = None,
    baseline_trend: Optional[float] = None,
    sector_map: Optional[dict[str, str]] = None,
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """P0-5 / 漸進放寬：每日新開倉（本輪開始未持有的新 symbol BUY）動態額度 gate。

    今日新倉計數口徑（不變）：
      - 今日第一次買入的新 symbol → 計 1
      - 昨日已持有、今日只是加倉 → 不計
      - 今日買入後賣出又買回同 symbol → 仍只計 1，回補節奏交給 cooldown / probation 管
    額度口徑：
      - defensive / flat（預設）：維持原本固定上限 _MAX_NEW_POSITIONS_PER_DAY。
      - probe：基礎 1 筆；第 2 筆需高信心(cross/score≥90)、當日 realized≥0、且與既有持倉產業不重疊；
        單輪最多 1 筆；第 3 筆條件式解鎖（cross/score≥95 + 當日 realized≥0 + 大盤不弱 + 當日新倉健康
        + 集中度/現金充足 + 產業不重疊）。產業資料源自 stock_universe.yaml（未知產業不擋）。
    """
    now = now or datetime.now()
    mode = mode or _new_position_mode()
    probe = mode == "probe"
    initial_held = _held_symbols(engine)
    start_of_day_held = _start_of_day_held_symbols(fills_path, day=now)

    # 1) 計算今日已成交的新開倉 symbol：排除今日 00:00 前已持有的加倉標的。
    today_new_symbols: set[str] = set()
    if fills_path and Path(fills_path).exists():
        today_str = now.strftime("%Y-%m-%d")
        try:
            for line in Path(fills_path).read_text(encoding="utf-8").strip().splitlines():
                if not line.strip():
                    continue
                f = json.loads(line)
                if str(f.get("side") or "").upper() != "BUY":
                    continue
                if str(f.get("filled_at") or "")[:10] != today_str:
                    continue
                sym = f.get("symbol")
                if sym and sym not in start_of_day_held:
                    today_new_symbols.add(sym)
        except Exception:
            pass  # 讀取失敗不阻擋

    today_new_buys = len(today_new_symbols)

    # 2) probe 才載入 score/cross 與當日 realized 做動態解鎖；defensive 維持 flat 上限。
    cross_set: set[str] = set()
    short_term_score: dict[str, Optional[int]] = {}
    today_realized: Optional[float] = None
    third_ctx: Optional[dict[str, Any]] = None
    sector_map_resolved: dict[str, str] = {}
    held_sectors: set[str] = set()
    if probe:
        cross_set, short_term_score = _load_score_cross(watchlist_path)
        today_realized = _today_realized_pnl(fills_path, now=now)
        sector_map_resolved = (
            sector_map if sector_map is not None
            else _load_sector_map(watchlist_path=watchlist_path)
        )
        held_sectors = _held_sectors(initial_held, sector_map_resolved)
        # 第 3 筆風控 context 只在「今日已 2 筆新倉」時才建（含罕見的 yfinance 大盤呼叫）
        if today_new_buys == _MAX_NEW_POSITIONS_PER_DAY - 1:
            third_ctx = _build_third_position_context(
                engine, today_new_symbols,
                quote_map=quote_cache, baseline_trend=baseline_trend, feed=feed,
            )

    accepted_new = 0  # 本輪已放行的新 symbol 數（probe round cap 用）
    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if d.action != "buy" or not d.symbol:
            out.append(d)
            continue
        # 本輪開始前已有持倉 → 加倉，不受每日新 symbol 上限限制。
        if d.symbol in initial_held:
            out.append(d)
            continue
        # 今日已買過同 symbol，不重複消耗額度；交給 cooldown / 其他 gate 管。
        if d.symbol in today_new_symbols:
            out.append(d)
            continue

        slot = today_new_buys + accepted_new + 1
        allow, block_reason, detail = _evaluate_new_position_slot(
            symbol=d.symbol,
            slot=slot,
            probe=probe,
            accepted_this_round=accepted_new,
            cross_set=cross_set,
            short_term_score=short_term_score,
            today_realized=today_realized,
            third_ctx=third_ctx,
            sector_map=sector_map_resolved,
            held_sectors=held_sectors,
        )
        if allow:
            accepted_new += 1
            out.append(d)
            continue

        gated.append({
            "symbol": d.symbol,
            "qty": d.qty,
            "mode": mode,
            "slot": slot,
            "block_reason": block_reason,
            "today_new_buys": today_new_buys,
            "today_new_symbols": sorted(today_new_symbols),
            "start_of_day_held_symbols": sorted(start_of_day_held),
            "max_per_day": _MAX_NEW_POSITIONS_PER_DAY,
            "held_sectors": sorted(held_sectors),
            "original_reason": d.reason,
            **detail,
        })
        out.append(Decision(
            action="hold", symbol=d.symbol,
            reason=f"daily_new_position_cap({block_reason} slot={slot} mode={mode})",
            source_pools=list(d.source_pools or []),
        ))
    return out, gated


def _gate_morning_new_buys(
    decisions: list[Decision],
    engine: Optional[MatchEngine],
    *,
    now: Optional[datetime] = None,
    block_until: dtime = _MORNING_NEW_BUY_BLOCK_UNTIL,
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """早盤冷靜期：09:00–block_until 不允許新 symbol buy。"""
    now = now or datetime.now()
    held = _held_symbols(engine)
    if not (_TRADING_OPEN <= now.time() < block_until):
        return list(decisions), []

    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if not _is_new_position_buy(d, held):
            out.append(d)
            continue
        gated.append({
            "symbol": d.symbol,
            "qty": d.qty,
            "now": now.isoformat(timespec="seconds"),
            "block_until": block_until.strftime("%H:%M"),
            "original_reason": d.reason,
        })
        out.append(Decision(
            action="hold", symbol=d.symbol,
            reason=f"morning_cooldown(new_buy_blocked_until={block_until.strftime('%H:%M')})",
            source_pools=list(d.source_pools or []),
        ))
    return out, gated


def _gate_recent_loss_probation(
    decisions: list[Decision],
    engine: Optional[MatchEngine],
    fills_path: Optional[Path],
    *,
    now: Optional[datetime] = None,
    probation_days: int = _LOSS_PROBATION_DAYS,
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """近期已實現虧損標的暫停新買，避免 2303/2881 式連續試錯。"""
    now = now or datetime.now()
    held = _held_symbols(engine)
    loss_by_symbol: dict[str, dict[str, Any]] = {}
    if fills_path and Path(fills_path).exists():
        try:
            from sim.reports import _per_fill_realized
            realized_by_fill = _per_fill_realized(fills_path)
            cutoff = (now - timedelta(days=probation_days)).date()
            with open(fills_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        fill = json.loads(line)
                    except Exception:
                        continue
                    if str(fill.get("side") or "").upper() != "SELL":
                        continue
                    fid = fill.get("fill_id")
                    realized = realized_by_fill.get(fid)
                    if realized is None or realized >= 0:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(fill.get("filled_at")))
                    except Exception:
                        continue
                    if ts.date() < cutoff or ts > now:
                        continue
                    sym = fill.get("symbol")
                    if not sym:
                        continue
                    prev = loss_by_symbol.get(sym)
                    if prev is None or ts > prev["last_loss_at"]:
                        loss_by_symbol[sym] = {
                            "last_loss_at": ts,
                            "realized_pnl": float(realized),
                            "fill_id": fid,
                        }
        except Exception:
            logger.exception("[advisor.runner] loss probation 讀取 realized 失敗")
            loss_by_symbol = {}

    if not loss_by_symbol:
        return list(decisions), []

    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if not _is_new_position_buy(d, held):
            out.append(d)
            continue
        loss = loss_by_symbol.get(d.symbol or "")
        if not loss:
            out.append(d)
            continue
        gated.append({
            "symbol": d.symbol,
            "qty": d.qty,
            "probation_days": probation_days,
            "last_loss_at": loss["last_loss_at"].isoformat(timespec="seconds"),
            "last_loss_pnl": round(loss["realized_pnl"], 2),
            "fill_id": loss.get("fill_id"),
            "original_reason": d.reason,
        })
        out.append(Decision(
            action="hold", symbol=d.symbol,
            reason=f"loss_probation(last_loss={loss['last_loss_at'].isoformat(timespec='minutes')} pnl={loss['realized_pnl']:.0f})",
            source_pools=list(d.source_pools or []),
        ))
    return out, gated


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _gate_no_new_positions_mode(
    decisions: list[Decision],
    engine: Optional[MatchEngine],
    *,
    enabled: Optional[bool] = None,
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """保守觀察期：禁止新 symbol buy；sell/hold 與既有持倉管理不受影響。

    正式預設開啟；測試或臨時回滾可設 `SIM_ADVISOR_NO_NEW_POSITIONS_MODE=0`，
    或改用 `SIM_ADVISOR_NEW_POSITION_MODE=probe` 進入條件式試單。
    """
    if enabled is None:
        enabled = _new_position_mode() == "defensive"
    if not enabled:
        return list(decisions), []
    held = _held_symbols(engine)
    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if not _is_new_position_buy(d, held):
            out.append(d)
            continue
        gated.append({
            "symbol": d.symbol,
            "qty": d.qty,
            "mode": "no_new_positions",
            "original_reason": d.reason,
        })
        out.append(Decision(
            action="hold", symbol=d.symbol,
            reason="no_new_positions_mode(active; manage_existing_positions_only)",
            source_pools=list(d.source_pools or []),
        ))
    return out, gated


# P0-6：固定分數倉位管理
_RISK_BUDGET_NORMAL = 0.02   # 普通信號：每筆最大虧損 = equity × 2%
_RISK_BUDGET_CROSS = 0.025   # cross_signals：每筆最大虧損 = equity × 2.5%


def _clamp_qty_by_risk(
    decisions: list[Decision],
    quote_cache: dict[str, float],
    engine: Optional[MatchEngine],
    *,
    watchlist_path: Optional[Path] = None,
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """P0-6：按 fixed-fractional 公式 clamp qty。

    公式：max_qty = (equity × risk_budget) / (entry_price - stop_loss)
    若 LLM 給的 qty > max_qty，自動縮到 max_qty（不擋掉、不降級）。
    台股最小單位考量：< 1000 股用零股，≥ 1000 股向下取整到百位。
    """
    equity = 0.0
    if engine and getattr(engine, "account", None):
        # equity() 需要 quotes，這裡用 cash + 持倉 cost_basis 作近似
        acct = engine.account
        equity = float(getattr(acct, "cash", 0) or 0)
        for pos in (getattr(acct, "positions", None) or {}).values():
            equity += float(getattr(pos, "cost_basis", 0) or 0)
    if equity <= 0:
        return decisions, []

    out: list[Decision] = []
    adjusted: list[dict[str, Any]] = []
    leveraged_map = _load_leveraged_etf_map(watchlist_path) if watchlist_path is not None else {}

    for d in decisions:
        if d.action != "buy" or not d.symbol or not d.stop_loss:
            out.append(d)
            continue

        stop = float(d.stop_loss)
        ref = float(d.price or 0) or quote_cache.get(d.symbol, 0)
        if ref <= 0 or stop <= 0 or stop >= ref:
            out.append(d)
            continue

        # 決定 risk budget（cross_signals 加碼；槓反 ETF 用較小風險預算）
        pools = set(d.source_pools or [])
        risk_budget = _RISK_BUDGET_CROSS if "cross_signals" in pools else _RISK_BUDGET_NORMAL
        leveraged_meta = leveraged_map.get(d.symbol)
        risk_multiplier = 1.0
        if leveraged_meta:
            try:
                risk_multiplier = float(leveraged_meta.get("risk_budget_multiplier") or _DEFAULT_LEVERAGED_ETF_RISK_MULTIPLIER)
            except (TypeError, ValueError):
                risk_multiplier = _DEFAULT_LEVERAGED_ETF_RISK_MULTIPLIER
            risk_budget *= max(0.0, min(risk_multiplier, 1.0))

        per_share_risk = ref - stop
        max_qty = int((equity * risk_budget) / per_share_risk)

        # 台股整股 round down to 百位（零股 < 1000 不做 round）
        if max_qty >= 1000:
            max_qty = (max_qty // 100) * 100
        if max_qty <= 0:
            max_qty = 1  # 至少 1 股（零股）

        orig_qty = int(d.qty or 0)
        if orig_qty <= 0:
            out.append(d)
            continue

        if orig_qty <= max_qty:
            out.append(d)
            continue

        # clamp
        adjusted.append({
            "symbol": d.symbol,
            "original_qty": orig_qty,
            "clamped_qty": max_qty,
            "ref_price": ref,
            "stop_loss": stop,
            "per_share_risk": round(per_share_risk, 2),
            "risk_budget_pct": round(risk_budget * 100, 2),
            "risk_budget_multiplier": round(risk_multiplier, 2),
            "is_leveraged_etf": bool(leveraged_meta),
            "equity": round(equity, 0),
            "reason": d.reason,
        })
        # 建新 Decision 只改 qty
        out.append(Decision(
            action=d.action,
            symbol=d.symbol,
            qty=max_qty,
            type=d.type,
            price=d.price,
            stop_loss=d.stop_loss,
            reason=d.reason,
            source_pools=list(d.source_pools or []),
        ))
    return out, adjusted


def _build_forced_sells_for_stale_losers(
    engine: Optional[MatchEngine],
    feed: Optional[QuoteFeed],
    watchlist_path: Optional[Path],
    decisions: list[Decision],
    quote_cache: dict[str, float],
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """P0-3：對「不在 short/medium/long 池且浮虧」的持倉強制 inject sell（MKT）。

    避免 LLM 拖延 → 等到強制停損才出（2345 場景虧 -5%）。
    winner（current >= avg_cost）即便退池也讓 LLM 自決，不強制砍。
    LLM 本輪已主動 sell 的標的不重複 inject。
    auto-stop PENDING SELL 不影響此邏輯：force sell 是 MKT 立即成交，
    比 STOP 觸發更早出場，整體損失更小。
    """
    if engine is None or getattr(engine, "account", None) is None:
        return [], []
    positions = getattr(engine.account, "positions", {}) or {}
    if not positions:
        return [], []

    try:
        pools = load_watchlist_pools(watchlist_path)
    except Exception:
        logger.exception("[advisor.runner] load_watchlist_pools failed in forced-sell sweep")
        return [], []

    pool_symbols: set[str] = set()
    for key in ("short_term", "medium_term", "long_term", _LEVERAGED_ETF_POOL):
        for item in pools.get(key) or []:
            if isinstance(item, dict) and item.get("symbol"):
                pool_symbols.add(item["symbol"])
    pool_symbols.update((pools.get("cross_signals") or {}).keys())

    already_sell: set[str] = {
        d.symbol for d in decisions if d.action == "sell" and d.symbol
    }

    forced: list[Decision] = []
    log_items: list[dict[str, Any]] = []
    for sym, pos in positions.items():
        qty = int(getattr(pos, "qty", 0) or 0)
        if qty <= 0:
            continue
        if sym in pool_symbols:
            continue
        if sym in already_sell:
            continue
        avg_cost = float(getattr(pos, "avg_cost", 0.0) or 0.0)
        if avg_cost <= 0:
            continue
        cq = quote_cache.get(sym)
        if cq is None and feed is not None:
            try:
                cq = (feed.get_quotes([sym]) or {}).get(sym)
                if cq is not None:
                    quote_cache[sym] = cq
            except Exception:
                logger.exception("[advisor.runner] feed.get_quotes(%s) failed in sweep", sym)
                continue
        if cq is None or cq >= avg_cost:
            continue
        forced.append(Decision(
            action="sell", symbol=sym, qty=qty, type="MKT", price=None,
            stop_loss=None,
            reason="forced_sell_out_of_pool_loser",
            source_pools=[],
        ))
        log_items.append({
            "symbol": sym, "qty": qty,
            "avg_cost": avg_cost, "current_quote": cq,
            "loss_pct": (cq / avg_cost - 1.0) if avg_cost else None,
        })
    return forced, log_items


def _recent_same_symbol_fills(
    fills_path: Optional[Path],
    *,
    now: datetime,
    within_minutes: int,
) -> dict[str, list[dict[str, Any]]]:
    """讀 fills.jsonl，回傳 {symbol: [fills...]}（過濾 now 起回溯 within_minutes 內）。

    fills 內時間欄位採 `filled_at`（ISO 字串）；缺欄位 / 解析失敗的列跳過。
    """
    if not fills_path:
        return {}
    p = Path(fills_path)
    if not p.exists():
        return {}
    cutoff = now - timedelta(minutes=within_minutes)
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts_s = r.get("filled_at") or r.get("timestamp") or ""
                if not ts_s:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ts_s))
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                sym = r.get("symbol")
                if not sym:
                    continue
                out.setdefault(sym, []).append({**r, "_parsed_ts": ts})
    except Exception:
        logger.exception("[advisor.runner] 讀 fills 失敗 path=%s", fills_path)
        return {}
    return out


def _gate_recent_cooldown(
    decisions: list[Decision],
    fills_path: Optional[Path],
    *,
    now: Optional[datetime] = None,
    cooldown_after_sell_min: int = _COOLDOWN_AFTER_SELL_MINUTES,
    cooldown_between_buys_min: int = _COOLDOWN_BETWEEN_BUYS_MINUTES,
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """過度交易防火牆：對每筆 buy 檢查最近 fills，命中 cooldown 即降級 hold。

    兩道規則：
      - 同 symbol 在 `cooldown_after_sell_min` 內出現過 SELL → 降級 hold
        （reason=cooldown_after_sell）— 擋「砍完馬上回補」式白交摩擦
      - 同 symbol 在 `cooldown_between_buys_min` 內出現過 BUY → 降級 hold
        （reason=cooldown_between_buys）— 擋連續加倉式攤平摩擦

    sell / hold 一律不動。
    fills 讀失敗、缺欄位、時間解析失敗 → fail-open（不擋）。
    """
    now = now or datetime.now()
    window = max(cooldown_after_sell_min, cooldown_between_buys_min)
    recent = _recent_same_symbol_fills(fills_path, now=now, within_minutes=window)
    if not recent:
        return list(decisions), []

    out: list[Decision] = []
    gated: list[dict[str, Any]] = []
    for d in decisions:
        if d.action != "buy" or not d.symbol:
            out.append(d)
            continue
        fills = recent.get(d.symbol) or []
        latest_sell: Optional[datetime] = None
        latest_buy: Optional[datetime] = None
        for r in fills:
            ts = r.get("_parsed_ts")
            if not isinstance(ts, datetime):
                continue
            side = str(r.get("side") or "").upper()
            if side == "SELL" and (latest_sell is None or ts > latest_sell):
                latest_sell = ts
            elif side == "BUY" and (latest_buy is None or ts > latest_buy):
                latest_buy = ts

        if latest_sell is not None:
            elapsed = (now - latest_sell).total_seconds() / 60
            if elapsed < cooldown_after_sell_min:
                gated.append({
                    "symbol": d.symbol,
                    "qty": d.qty,
                    "rule": "cooldown_after_sell",
                    "last_sell_at": latest_sell.isoformat(timespec="seconds"),
                    "elapsed_minutes": round(elapsed, 1),
                    "cooldown_minutes": cooldown_after_sell_min,
                    "original_reason": d.reason,
                })
                out.append(Decision(
                    action="hold", symbol=d.symbol,
                    reason=(
                        f"cooldown_after_sell(last_sell={latest_sell.isoformat(timespec='minutes')} "
                        f"elapsed={elapsed:.1f}min<{cooldown_after_sell_min}min)"
                    ),
                    source_pools=list(d.source_pools or []),
                ))
                continue

        if latest_buy is not None:
            elapsed = (now - latest_buy).total_seconds() / 60
            if elapsed < cooldown_between_buys_min:
                gated.append({
                    "symbol": d.symbol,
                    "qty": d.qty,
                    "rule": "cooldown_between_buys",
                    "last_buy_at": latest_buy.isoformat(timespec="seconds"),
                    "elapsed_minutes": round(elapsed, 1),
                    "cooldown_minutes": cooldown_between_buys_min,
                    "original_reason": d.reason,
                })
                out.append(Decision(
                    action="hold", symbol=d.symbol,
                    reason=(
                        f"cooldown_between_buys(last_buy={latest_buy.isoformat(timespec='minutes')} "
                        f"elapsed={elapsed:.1f}min<{cooldown_between_buys_min}min)"
                    ),
                    source_pools=list(d.source_pools or []),
                ))
                continue

        out.append(d)
    return out, gated


def _decision_to_order(d: Decision) -> Optional[Order]:
    """Decision → sim.models.Order；不合法回 None。

    advisor 不直接出 STOP/TP（一律寫進 stop_loss 欄位）；BUY 成交後 runner 透過
    sync_auto_stops_after_fills 自動掛 STOP SELL，跌破即觸發。
    """
    if d.action not in ("buy", "sell"):
        return None
    if not d.symbol or not d.qty or int(d.qty) <= 0:
        return None
    side = OrderSide.BUY if d.action == "buy" else OrderSide.SELL
    type_str = (d.type or "MKT").upper()
    type_map = {
        "MKT": OrderType.MKT,
        "LMT": OrderType.LMT,
        "STOP": OrderType.STOP,
        "TP": OrderType.TP,
    }
    otype = type_map.get(type_str)
    if otype is None:
        return None
    if otype in (OrderType.LMT, OrderType.STOP, OrderType.TP) and not d.price:
        return None
    note_payload = {
        "reason": str(d.reason or "")[:500],
        "source_pools": list(d.source_pools or []),
        "stop_loss": d.stop_loss,
    }
    return Order(
        symbol=d.symbol,
        side=side,
        type=otype,
        qty=int(d.qty),
        price=d.price,
        notes="[advisor] " + json.dumps(note_payload, ensure_ascii=False, separators=(",", ":")),
    )
