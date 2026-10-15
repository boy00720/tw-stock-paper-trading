"""automation/sim/advisor/risk_guard.py — 三層風控閘（M6 區塊 D）

對每個 advisor 出的 Decision 做三層審查：
1. **單筆風險**：(entry - stop_loss) × qty ≤ equity × max_risk_pct（預設 2%）
   - 沒帶 stop_loss → 視同最大風險 = ref_price × qty × max_risk_no_stop_pct（預設 5%）
2. **持倉集中度**：(現有持倉 cost_basis + 本次 ref_price × qty) ≤ equity × max_concentration_pct（預設 20%）
3. **日內熔斷**：今日已實現 PnL ≤ -initial_cash × circuit_pct（預設 3%）→ paper_only 至次日

action="hold" 一律放行；action="sell" 走簡化路徑（熔斷判斷後放行，
持倉檢查由 sim.engine.submit_order 自己做）；action="buy" 走完整三層。

熔斷狀態 sticky 持久化在 logs/sim/advisor_state.json，跨 advisor 進程生效。
所有 reject 寫 logs/sim/advisor_audit.jsonl。

參數從 config/sim_config.yaml 的 advisor 區塊讀；缺項用內建預設。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from sim.advisor._audit import append_jsonl, read_state, write_state_atomic
from sim.advisor._types import Decision

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_AUDIT_PATH = _BASE_DIR / "logs" / "sim" / "advisor_audit.jsonl"
DEFAULT_STATE_PATH = _BASE_DIR / "logs" / "sim" / "advisor_state.json"
DEFAULT_SIM_CONFIG = _BASE_DIR / "config" / "sim_config.yaml"

# 預設風控參數（sim_config.yaml advisor 區塊缺項時 fallback）
DEFAULT_MAX_RISK_PCT = 0.02
DEFAULT_MAX_RISK_NO_STOP_PCT = 0.05
DEFAULT_MAX_CONCENTRATION_PCT = 0.20
DEFAULT_CIRCUIT_PCT = 0.03


@dataclass
class GuardResult:
    """單筆 decision 的審查結果。"""
    passed: bool
    reason: str  # "passed" / "passed_hold" / "passed_sell" / "circuit_breaker_active_today" / "single_risk_too_high(...)" / "concentration_too_high(...)" / ...
    details: dict[str, Any] = field(default_factory=dict)


class RiskGuard:
    def __init__(
        self,
        engine,
        *,
        sim_config_path: Optional[Path] = None,
        audit_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
    ):
        self.engine = engine
        self.audit_path = Path(audit_path) if audit_path else DEFAULT_AUDIT_PATH
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._load_params(Path(sim_config_path) if sim_config_path else DEFAULT_SIM_CONFIG)

    def _load_params(self, config_path: Path) -> None:
        cfg: dict[str, Any] = {}
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:
                logger.exception("[advisor.risk_guard] 讀取 %s 失敗", config_path)
        adv = cfg.get("advisor") or {}
        self.max_risk_pct = float(adv.get("max_risk_pct", DEFAULT_MAX_RISK_PCT))
        self.max_risk_no_stop_pct = float(
            adv.get("max_risk_no_stop_pct", DEFAULT_MAX_RISK_NO_STOP_PCT)
        )
        self.max_concentration_pct = float(
            adv.get("max_concentration_pct", DEFAULT_MAX_CONCENTRATION_PCT)
        )
        self.circuit_pct = float(adv.get("circuit_pct", DEFAULT_CIRCUIT_PCT))

    # ─────────────────────────────────────────────────────
    # 主審查入口
    # ─────────────────────────────────────────────────────

    def evaluate(
        self,
        decision: Decision,
        *,
        current_quote: Optional[float] = None,
    ) -> GuardResult:
        """對單一 decision 做三層審查。

        current_quote：advisor 出 BUY 時的當前報價，用於估持倉值。
        沒給時用 decision.price 推估。沒 price 也沒 quote → equity 估算 0、
        必然 reject（避免風控閘漏估）。
        """
        # 0) 熔斷
        if self.is_circuit_broken_today():
            r = GuardResult(False, "circuit_breaker_active_today", {})
            self._write_reject(decision, r)
            return r

        # hold 直接放行（不寫 audit）
        if decision.action == "hold":
            return GuardResult(True, "passed_hold", {"action": "hold"})

        # SELL：熔斷已過 → 放行（持倉足夠由 engine.submit_order 自己擋）
        if decision.action == "sell":
            return GuardResult(True, "passed_sell", {})

        if decision.action != "buy":
            r = GuardResult(False, f"unknown_action({decision.action!r})", {})
            self._write_reject(decision, r)
            return r

        # ── BUY 走完整三層 ──
        equity = self._current_equity()
        if equity <= 0:
            r = GuardResult(False, f"equity_non_positive({equity})", {"equity": equity})
            self._write_reject(decision, r)
            return r

        qty = int(decision.qty or 0)
        if qty <= 0:
            r = GuardResult(False, f"qty_non_positive({qty})", {})
            self._write_reject(decision, r)
            return r

        ref_price = float(current_quote or decision.price or 0)
        stop = float(decision.stop_loss or 0)

        # 1) 單筆風險
        if stop > 0 and ref_price > 0 and stop < ref_price:
            per_share_risk = ref_price - stop
            risk_amount = per_share_risk * qty
        elif ref_price > 0:
            # 沒 stop 或 stop ≥ ref_price（資料異常）：視為最大風險
            risk_amount = ref_price * qty * self.max_risk_no_stop_pct
        else:
            r = GuardResult(False, "no_ref_price_for_risk_calc", {})
            self._write_reject(decision, r)
            return r

        risk_limit = equity * self.max_risk_pct
        if risk_amount > risk_limit:
            r = GuardResult(
                False,
                f"single_risk_too_high(risk={risk_amount:.0f} > limit={risk_limit:.0f})",
                {
                    "equity": equity, "risk_amount": risk_amount,
                    "risk_limit": risk_limit, "ref_price": ref_price, "qty": qty,
                    "stop_loss": stop,
                },
            )
            self._write_reject(decision, r)
            return r

        # 2) 集中度
        if not decision.symbol:
            r = GuardResult(False, "buy_without_symbol", {})
            self._write_reject(decision, r)
            return r

        existing_cost = 0.0
        if self.engine and getattr(self.engine, "account", None):
            pos = self.engine.account.positions.get(decision.symbol)
            if pos:
                existing_cost = pos.cost_basis
        new_position_value = existing_cost + ref_price * qty
        conc_limit = equity * self.max_concentration_pct
        if new_position_value > conc_limit:
            r = GuardResult(
                False,
                f"concentration_too_high(value={new_position_value:.0f} > limit={conc_limit:.0f})",
                {
                    "equity": equity, "new_value": new_position_value,
                    "limit": conc_limit, "symbol": decision.symbol,
                    "existing_cost": existing_cost,
                },
            )
            self._write_reject(decision, r)
            return r

        return GuardResult(
            True, "passed",
            {"equity": equity, "risk_amount": risk_amount, "new_value": new_position_value},
        )

    # ─────────────────────────────────────────────────────
    # 熔斷管理
    # ─────────────────────────────────────────────────────

    def is_circuit_broken_today(self) -> bool:
        state = read_state(self.state_path)
        return state.get("circuit_until_date") == _today_iso()

    def trigger_circuit_breaker(self) -> None:
        """手動觸發熔斷（runner 偵測 today_pnl 跌破門檻時呼叫）。"""
        state = read_state(self.state_path)
        state["circuit_until_date"] = _today_iso()
        state["circuit_triggered_at"] = datetime.now().isoformat(timespec="seconds")
        write_state_atomic(self.state_path, state)
        logger.warning(
            "[advisor.risk_guard] 熔斷觸發 until=%s",
            state["circuit_until_date"],
        )
        append_jsonl(
            self.audit_path,
            {
                "type": "circuit_breaker_triggered",
                "time": state["circuit_triggered_at"],
                "until_date": state["circuit_until_date"],
            },
        )

    def check_and_maybe_trigger_circuit(self) -> bool:
        """讀 fills.jsonl 算今日 PnL；若 ≤ -initial_cash × circuit_pct → 觸發熔斷。

        回傳是否觸發（True = 本次觸發；False = 未達門檻或已觸發過）。
        """
        if self.is_circuit_broken_today():
            return False  # 已觸發
        if not self.engine or not getattr(self.engine, "account", None):
            return False
        try:
            from sim.reports import today_realized_pnl
        except ImportError:
            logger.warning("[advisor.risk_guard] sim.reports 不可用，跳過 circuit check")
            return False
        try:
            today_pnl = today_realized_pnl(self.engine.fills_log_path)
        except Exception:
            logger.exception("[advisor.risk_guard] today_realized_pnl 計算失敗")
            return False
        threshold = -float(self.engine.account.initial_cash) * self.circuit_pct
        if today_pnl <= threshold:
            self.trigger_circuit_breaker()
            return True
        return False

    # ─────────────────────────────────────────────────────
    # 內部
    # ─────────────────────────────────────────────────────

    def _current_equity(self) -> float:
        if not self.engine or not getattr(self.engine, "account", None):
            return 0.0
        try:
            return float(self.engine.account.equity())
        except Exception:
            logger.exception("[advisor.risk_guard] equity 計算失敗")
            return 0.0

    def _write_reject(self, decision: Decision, result: GuardResult) -> None:
        append_jsonl(
            self.audit_path,
            {
                "type": "risk_reject",
                "time": datetime.now().isoformat(timespec="seconds"),
                "decision": _decision_to_dict(decision),
                "reason": result.reason,
                "details": result.details,
            },
        )


def _today_iso() -> str:
    return date.today().isoformat()


def _decision_to_dict(d: Decision) -> dict[str, Any]:
    return {
        "action": d.action,
        "symbol": d.symbol,
        "qty": d.qty,
        "type": d.type,
        "price": d.price,
        "stop_loss": d.stop_loss,
        "reason": d.reason,
        "source_pools": list(d.source_pools or []),
    }
