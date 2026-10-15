# -*- coding: utf-8 -*-
"""
選股績效追蹤器 — 記錄並評估選股結果
==================================
追蹤 stock_screener 選出的標的，比對實際走勢，計算勝率與盈虧比
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any

import pandas as pd

from utils.watchlist_schema import index_pool_by_symbol, load_watchlist

logger = logging.getLogger(__name__)

def _pick_entry_from_record(record: Dict[str, Any]) -> "PickEntry":
    allowed = {k: record.get(k) for k in PickEntry.__dataclass_fields__.keys()}
    return PickEntry(**allowed)


@dataclass
class PickEntry:
    """單一選股項目"""
    symbol: str
    name: str
    engine: str  # momentum / rebound
    pick_date: str  # ISO date
    entry_price: float
    stop_loss: float
    take_profit: float
    time_stop_date: str
    score: int
    inst_signal: str
    price: Optional[float] = None
    breakdown: Optional[Dict[str, Any]] = None
    entry_zone: Optional[List[float]] = None

    # 追蹤結果（盤後回填）
    outcome: str = "pending"  # pending / win / loss / timeout / open
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None  # stop_loss / take_profit / time_stop / manual
    pnl_pct: Optional[float] = None
    holding_days: int = 0


@dataclass
class WeeklyPerformance:
    """週績效統計"""
    week_start: str
    total_picks: int
    completed_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_gain_pct: float
    avg_loss_pct: float
    profit_factor: float  # 盈虧比
    best_pick: Optional[str] = None
    worst_pick: Optional[str] = None

    # Alpha 計算（新增）
    alpha_pct: float = 0.0  # 超額報酬（相對大盤）
    actual_return_pct: float = 0.0  # 考慮滑價/手續費後的實際報酬
    sharpe_ratio: float = 0.0  # 夏普比率
    max_drawdown_pct: float = 0.0  # 最大回撤
    benchmark_return_pct: float = 0.0  # 大盤同期報酬
    total_trades_cost: float = 0.0  # 總交易成本（滑價 + 手續費）


class ScreenerTracker:
    """
    選股績效追蹤器

    檔案結構:
    logs/screener_tracker/
      active_picks.json       — 當前追蹤中的選股
      history/YYYY-MM.jsonl   — 歷史記錄（按月）
      weekly/YYYY-WXX.json    — 週績效報告
    """

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        self.base_dir = Path(base_dir)
        self.tracker_dir = self.base_dir / "logs" / "screener_tracker"
        self.active_file = self.tracker_dir / "active_picks.json"
        self.history_dir = self.tracker_dir / "history"
        self.weekly_dir = self.tracker_dir / "weekly"

        for d in [self.tracker_dir, self.history_dir, self.weekly_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def _load_active(self) -> List[PickEntry]:
        """載入當前追蹤中的選股"""
        if not self.active_file.exists():
            return []
        try:
            data = json.loads(self.active_file.read_text(encoding="utf-8"))
            picks = [_pick_entry_from_record(item) for item in data.get("picks", [])]
            for pick in picks:
                if pick.breakdown and pick.entry_zone:
                    continue
                recovered = self._recover_pick_metadata(pick.symbol, pick.pick_date)
                if not pick.breakdown and recovered.get("breakdown"):
                    pick.breakdown = recovered.get("breakdown")
                if not pick.entry_zone and recovered.get("entry_zone"):
                    pick.entry_zone = recovered.get("entry_zone")
            return picks
        except Exception as e:
            logger.error(f"載入追蹤清單失敗: {e}")
            return []


    def _save_active(self, picks: List[PickEntry]):
        """保存當前追蹤中的選股"""
        payload = {
            "updated_at": datetime.now().isoformat(),
            "count": len(picks),
            "picks": [asdict(p) for p in picks],
        }
        self.active_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _recover_pick_metadata(self, symbol: str, pick_date: str) -> Dict[str, Any]:
        screener_file = self.base_dir / "logs" / "screener" / f"{pick_date}.json"
        if not screener_file.exists():
            return {}
        try:
            payload = json.loads(screener_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug(f"讀取 screener 檔失敗 {screener_file.name}: {e}")
            return {}

        for group_key in ["momentum_picks", "rebound_picks"]:
            for item in payload.get(group_key) or []:
                if item.get("symbol") != symbol:
                    continue
                return {
                    "breakdown": item.get("breakdown") or {},
                    "entry_zone": item.get("entry_zone") or [],
                }
        return {}

    def _add_to_history(self, pick: PickEntry):
        """將結案項目加入歷史記錄"""
        try:
            month_str = pick.pick_date[:7]  # YYYY-MM
            history_file = self.history_dir / f"{month_str}.jsonl"

            record = {
                "timestamp": datetime.now().isoformat(),
                **asdict(pick),
            }
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"加入歷史記錄失敗: {e}")

    def add_daily_picks(self, screener_state: Any):
        """
        將 stock_screener 的結果加入追蹤

        Args:
            screener_state: ScreenerState dataclass
        """
        active = self._load_active()
        existing_symbols = {p.symbol for p in active}

        new_count = 0
        for pick in screener_state.momentum_picks + screener_state.rebound_picks:
            if pick.symbol in existing_symbols:
                continue

            entry = PickEntry(
                symbol=pick.symbol,
                name=pick.name,
                engine=pick.engine,
                pick_date=screener_state.date,
                entry_price=pick.price,
                stop_loss=pick.stop_loss,
                take_profit=pick.take_profit,
                time_stop_date=pick.time_stop,
                score=pick.score,
                inst_signal=pick.inst_signal,
                price=pick.price,
                breakdown=getattr(pick, "breakdown", None),
                entry_zone=getattr(pick, "entry_zone", None),
            )
            active.append(entry)
            new_count += 1

        if new_count > 0:
            self._save_active(active)
            logger.info(f"[ScreenerTracker] 新增 {new_count} 個選股追蹤")

    def update_daily_outcomes(self, quotes: Dict[str, float], today: Optional[datetime] = None):
        """
        每日盤後更新追蹤結果

        Args:
            quotes: {symbol: close_price} 的字典
            today: 當前日期，預設今天
        """
        if today is None:
            today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")

        active = self._load_active()
        updated = []
        completed = []

        for pick in active:
            if pick.outcome != "pending":
                updated.append(pick)
                continue

            current_price = quotes.get(pick.symbol)
            if current_price is None:
                updated.append(pick)
                continue

            pick_date = datetime.fromisoformat(pick.pick_date)
            holding_days = (today - pick_date).days
            pick.holding_days = holding_days

            # 檢查停損
            if current_price <= pick.stop_loss:
                pick.outcome = "loss"
                pick.exit_price = current_price
                pick.exit_date = today_str
                pick.exit_reason = "stop_loss"
                pick.pnl_pct = (current_price - pick.entry_price) / pick.entry_price * 100
                completed.append(pick)
                logger.info(f"[ScreenerTracker] {pick.symbol} 觸及停損: {pick.pnl_pct:+.2f}%")
                continue

            # 檢查停利
            if current_price >= pick.take_profit:
                pick.outcome = "win"
                pick.exit_price = current_price
                pick.exit_date = today_str
                pick.exit_reason = "take_profit"
                pick.pnl_pct = (current_price - pick.entry_price) / pick.entry_price * 100
                completed.append(pick)
                logger.info(f"[ScreenerTracker] {pick.symbol} 觸及停利: {pick.pnl_pct:+.2f}%")
                continue

            # 檢查時間停損
            if today_str >= pick.time_stop_date:
                pick.outcome = "timeout"
                pick.exit_price = current_price
                pick.exit_date = today_str
                pick.exit_reason = "time_stop"
                pick.pnl_pct = (current_price - pick.entry_price) / pick.entry_price * 100
                # 根據最終盈虧標記為 win/loss
                pick.outcome = "win" if pick.pnl_pct > 0 else "loss"
                completed.append(pick)
                logger.info(f"[ScreenerTracker] {pick.symbol} 時間停損: {pick.pnl_pct:+.2f}%")
                continue

            # 仍持有中
            updated.append(pick)

        # 保存更新後的清單
        self._save_active(updated)

        # 將完成的加入歷史
        for pick in completed:
            self._add_to_history(pick)

        return len(completed)

    def get_active_summary(self) -> str:
        """生成當前追蹤摘要"""
        active = self._load_active()
        if not active:
            return ""

        momentum = [p for p in active if p.engine == "momentum"]
        rebound = [p for p in active if p.engine == "rebound"]

        lines = [f"\n📊 【選股追蹤中】共 {len(active)} 檔"]
        if momentum:
            lines.append(f"  量價突破: {', '.join([p.name for p in momentum[:3]])}{'...' if len(momentum) > 3 else ''}")
        if rebound:
            lines.append(f"  均值回歸: {', '.join([p.name for p in rebound[:3]])}{'...' if len(rebound) > 3 else ''}")

        return "\n".join(lines)

    def generate_weekly_report(self, week_start: Optional[datetime] = None) -> WeeklyPerformance:
        """
        生成週績效報告
        """
        if week_start is None:
            today = datetime.now()
            week_start = today - timedelta(days=today.weekday())

        week_start_str = week_start.strftime("%Y-%m-%d")
        week_end = week_start + timedelta(days=6)

        # 載入歷史記錄
        month_str = week_start.strftime("%Y-%m")
        history_file = self.history_dir / f"{month_str}.jsonl"

        week_picks: List[PickEntry] = []

        if history_file.exists():
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        record = json.loads(line.strip())
                        exit_date = record.get("exit_date")
                        if not exit_date:
                            continue
                        exit_dt = datetime.fromisoformat(exit_date)
                        if week_start <= exit_dt <= week_end:
                            week_picks.append(_pick_entry_from_record(record))
            except Exception as e:
                logger.error(f"讀取歷史記錄失敗: {e}")

        # 計算績效
        completed = [p for p in week_picks if p.outcome in ("win", "loss")]
        wins = [p for p in completed if p.outcome == "win"]
        losses = [p for p in completed if p.outcome == "loss"]

        avg_gain = sum(p.pnl_pct for p in wins) / len(wins) if wins else 0
        avg_loss = sum(p.pnl_pct for p in losses) / len(losses) if losses else 0

        # 盈虧比
        total_gain = sum(p.pnl_pct for p in wins)
        total_loss = abs(sum(p.pnl_pct for p in losses))
        profit_factor = total_gain / total_loss if total_loss > 0 else float('inf')

        # 找出最佳/最差
        best = max(completed, key=lambda p: p.pnl_pct or 0) if completed else None
        worst = min(completed, key=lambda p: p.pnl_pct or 0) if completed else None

        # === Alpha 計算（新增）===
        alpha_metrics = self._calculate_alpha_metrics(completed, week_start, week_end)

        perf = WeeklyPerformance(
            week_start=week_start_str,
            total_picks=len(week_picks),
            completed_trades=len(completed),
            wins=len(wins),
            losses=len(losses),
            win_rate=len(wins) / len(completed) * 100 if completed else 0,
            avg_gain_pct=avg_gain,
            avg_loss_pct=avg_loss,
            profit_factor=profit_factor,
            best_pick=f"{best.name} ({best.pnl_pct:+.1f}%)" if best else None,
            worst_pick=f"{worst.name} ({worst.pnl_pct:+.1f}%)" if worst else None,
            # Alpha 指標
            alpha_pct=alpha_metrics.get("alpha_pct", 0.0),
            actual_return_pct=alpha_metrics.get("actual_return_pct", 0.0),
            sharpe_ratio=alpha_metrics.get("sharpe_ratio", 0.0),
            max_drawdown_pct=alpha_metrics.get("max_drawdown_pct", 0.0),
            benchmark_return_pct=alpha_metrics.get("benchmark_return_pct", 0.0),
            total_trades_cost=alpha_metrics.get("total_trades_cost", 0.0),
        )

        # 保存週報告
        week_str = week_start.strftime("%Y-W%W")
        report_file = self.weekly_dir / f"{week_str}.json"
        report_file.write_text(
            json.dumps(asdict(perf), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        return perf

    def _calculate_alpha_metrics(
        self,
        completed_picks: List[PickEntry],
        week_start: datetime,
        week_end: datetime,
    ) -> Dict[str, float]:
        """
        計算 Alpha 指標

        Args:
            completed_picks: 已完成交易列表
            week_start: 週起始日
            week_end: 週結束日

        Returns:
            Alpha 指標字典
        """
        if not completed_picks:
            return {
                "alpha_pct": 0.0,
                "actual_return_pct": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown_pct": 0.0,
                "benchmark_return_pct": 0.0,
                "total_trades_cost": 0.0,
            }

        # 交易成本設定（台股典型值）
        COMMISSION_RATE = 0.001425  # 手續費率 0.1425%
        SLIPPAGE_RATE = 0.001  # 滑價率 0.1%
        TAX_RATE = 0.003  # 證交稅 0.3%（賣出時）

        # 計算實際報酬（考慮成本）
        actual_returns = []
        total_cost = 0.0

        for pick in completed_picks:
            if pick.pnl_pct is None or pick.entry_price is None:
                continue

            # 買入成本
            buy_cost = pick.entry_price * (COMMISSION_RATE + SLIPPAGE_RATE)
            # 賣出成本
            sell_price = pick.exit_price or pick.entry_price
            sell_cost = sell_price * (COMMISSION_RATE + SLIPPAGE_RATE + TAX_RATE)

            total_trade_cost = buy_cost + sell_cost
            total_cost += total_trade_cost

            # 實際淨報酬
            gross_return = pick.pnl_pct / 100  # 轉換為小數
            cost_impact = total_trade_cost / pick.entry_price
            net_return = gross_return - cost_impact
            actual_returns.append(net_return * 100)  # 轉回百分比

        # 實際平均報酬
        actual_return_pct = sum(actual_returns) / len(actual_returns) if actual_returns else 0

        # 夏普比率計算（簡化版，假設無風險利率 2%）
        if len(actual_returns) > 1:
            returns_array = pd.Series(actual_returns)
            excess_returns = returns_array - 2.0  # 超額報酬
            sharpe_ratio = excess_returns.mean() / returns_array.std() if returns_array.std() != 0 else 0
        else:
            sharpe_ratio = 0

        # 最大回撤計算
        max_drawdown = 0
        peak = 0
        cumulative = 0
        for ret in actual_returns:
            cumulative += ret
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # 取得大盤同期報酬（簡化版，使用 ^TWII）
        benchmark_return_pct = self._get_benchmark_return(week_start, week_end)

        # Alpha = 實際報酬 - 大盤報酬
        alpha_pct = actual_return_pct - benchmark_return_pct

        return {
            "alpha_pct": round(alpha_pct, 2),
            "actual_return_pct": round(actual_return_pct, 2),
            "sharpe_ratio": round(sharpe_ratio, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "benchmark_return_pct": round(benchmark_return_pct, 2),
            "total_trades_cost": round(total_cost, 2),
        }

    def _get_benchmark_return(self, start_date: datetime, end_date: datetime) -> float:
        """
        取得大盤基準報酬（簡化版）

        Args:
            start_date: 起始日
            end_date: 結束日

        Returns:
            大盤報酬百分比
        """
        try:
            import yfinance as yf

            # 使用台股加權指數 ^TWII
            ticker = "^TWII"
            data = yf.download(ticker, start=start_date, end=end_date + timedelta(days=1), progress=False)

            if len(data) >= 2:
                close_series = data["Close"]
                if hasattr(close_series, "squeeze"):
                    close_series = close_series.squeeze()
                start_price = float(close_series.iloc[0])
                end_price = float(close_series.iloc[-1])
                return (end_price - start_price) / start_price * 100

        except Exception as e:
            logger.debug(f"取得大盤報酬失敗: {e}")

        # 預設值：假設大盤當週漲跌 0%
        return 0.0


    def _format_pick_reason(self, pick: PickEntry) -> str:
        breakdown = pick.breakdown or {}
        entry_zone = pick.entry_zone or []
        if breakdown:
            top_parts = sorted(breakdown.items(), key=lambda pair: pair[1], reverse=True)[:3]
            reason = "、".join(f"{key}={value}" for key, value in top_parts)
        else:
            reason = "進池理由欄位未提供"
        if isinstance(entry_zone, list) and len(entry_zone) >= 2:
            reason += f"｜進場區 {entry_zone[0]:.2f}-{entry_zone[1]:.2f}"
        return reason

    def _load_watchlist_short_term(self) -> Dict[str, Dict[str, Any]]:
        watchlist_path = self.base_dir / "logs" / "watchlist" / "current.json"
        watchlist = load_watchlist(watchlist_path)
        return index_pool_by_symbol(watchlist, "short_term")

    def _load_stock_institutional(self) -> Dict[str, Dict[str, Any]]:
        snapshot_dir = self.base_dir / "logs" / "institutional_snapshots"
        for path in [snapshot_dir / f"{datetime.now().strftime('%Y-%m-%d')}_morning.json", snapshot_dir / "current.json"]:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return payload.get("stocks") or {}
            except Exception as e:
                logger.debug(f"讀取 institutional snapshot 失敗 {path.name}: {e}")
        return {}

    def _summarize_institutional_stats(self, stats: Dict[str, Any]) -> str:
        if not stats:
            return "法人明細缺失"
        parts = []
        for label in ["外資", "投信", "自營商"]:
            item = stats.get(label) or {}
            today = item.get("today")
            month = item.get("month")
            if today is None and month is None:
                continue
            today_text = f"今{today:+,.0f}張" if today is not None else "今缺"
            month_text = f"月{month:+,.0f}張" if month is not None else "月缺"
            parts.append(f"{label}{today_text}／{month_text}")
        return "；".join(parts) if parts else "法人明細缺失"

    def format_weekly_report(self, perf: WeeklyPerformance) -> str:
        """格式化週報告為可讀文字"""
        lines = [
            f"# 📈 選股績效週報 · {perf.week_start}",
            "",
            f"**選股總數**: {perf.total_picks} 檔",
            f"**完成交易**: {perf.completed_trades} 筆（{perf.wins} 勝 / {perf.losses} 敗）",
            f"**勝率**: {perf.win_rate:.1f}%",
            f"**平均獲利**: {perf.avg_gain_pct:+.1f}%",
            f"**平均虧損**: {perf.avg_loss_pct:+.1f}%",
            f"**盈虧比**: {perf.profit_factor:.2f}",
            "",
            "### 📊 Alpha 分析",
            f"**Alpha（超額報酬）**: {perf.alpha_pct:+.2f}%",
            f"**實際報酬（含成本）**: {perf.actual_return_pct:+.2f}%",
            f"**大盤同期報酬**: {perf.benchmark_return_pct:+.2f}%",
            f"**夏普比率**: {perf.sharpe_ratio:.2f}",
            f"**最大回撤**: {perf.max_drawdown_pct:.2f}%",
            f"**總交易成本**: ${perf.total_trades_cost:,.0f}",
        ]

        if perf.best_pick:
            lines.append(f"**最佳標的**: {perf.best_pick}")
        if perf.worst_pick:
            lines.append(f"**最差標的**: {perf.worst_pick}")

        active = self._load_active()
        if active:
            watchlist_short = self._load_watchlist_short_term()
            stock_institutional = self._load_stock_institutional()
            lines.extend(["", "### 📋 逐檔觀察池"])
            engine_order = {"momentum": 0, "rebound": 1}
            ordered_active = sorted(
                active,
                key=lambda p: (
                    engine_order.get(p.engine, 99),
                    -(p.score or 0),
                    p.name,
                ),
            )
            current_engine = None
            for pick in ordered_active[:12]:
                if pick.engine != current_engine:
                    current_engine = pick.engine
                    header = "#### 量價突破" if current_engine == "momentum" else ("#### 均值回歸" if current_engine == "rebound" else f"#### {current_engine}")
                    lines.append(header)
                code = pick.symbol.replace('.TW', '').replace('.TWO', '')
                watch_item = watchlist_short.get(pick.symbol) or {}
                price = watch_item.get("price") if watch_item.get("price") is not None else (pick.price if pick.price is not None else pick.entry_price)
                stop_loss = watch_item.get("stop_loss") if watch_item.get("stop_loss") is not None else pick.stop_loss
                breakdown = watch_item.get("breakdown") if watch_item.get("breakdown") is not None else pick.breakdown
                entry_zone = watch_item.get("entry_zone") if watch_item.get("entry_zone") is not None else pick.entry_zone
                temp_pick = PickEntry(**{**asdict(pick), "price": price, "breakdown": breakdown, "entry_zone": entry_zone})
                institutional_note = self._summarize_institutional_stats(stock_institutional.get(pick.symbol) or {})
                engine_label = "量價突破" if pick.engine == "momentum" else ("均值回歸" if pick.engine == "rebound" else pick.engine)
                lines.append(
                    f"- {pick.name}（{code}）｜{engine_label}｜score={pick.score}｜價格 {float(price):.2f}｜停損 {float(stop_loss):.2f}｜{self._format_pick_reason(temp_pick)}｜{institutional_note}"
                )

        if perf.win_rate >= 60 and perf.profit_factor >= 2.0 and perf.alpha_pct > 0:
            lines.append("\n✅ **評語**: 本週策略表現優異，Alpha 為正且風險調整後報酬良好，建議維持當前參數。")
        elif perf.win_rate >= 50 and perf.profit_factor >= 1.5 and perf.alpha_pct > 0:
            lines.append("\n⚠️ **評語**: 本週策略表現良好，有打敗大盤（正 Alpha），可微調停損/停利比例。")
        elif perf.alpha_pct < -2:
            lines.append(f"\n❌ **評語**: 本週策略大幅落後大盤（Alpha {perf.alpha_pct:+.1f}%），建議檢視選股條件或暫停使用。")
        elif perf.win_rate < 40:
            lines.append("\n❌ **評語**: 本週尚無選股數據或勝率過低，建議檢視選股條件。")
        else:
            lines.append("\n📝 **評語**: 表現持平，建議持續觀察。")

        if perf.sharpe_ratio > 1.5:
            lines.append("📈 夏普比率高於 1.5，風險調整後報酬優秀。")
        elif perf.max_drawdown_pct > 10:
            lines.append(f"⚠️ 最大回撤達 {perf.max_drawdown_pct:.1f}%，注意風險控制。")

        return "\n".join(lines)


# 全局實例
_screener_tracker: Optional[ScreenerTracker] = None


def get_screener_tracker(base_dir: Optional[Path] = None) -> ScreenerTracker:
    """取得 ScreenerTracker 實例"""
    global _screener_tracker
    if _screener_tracker is None:
        _screener_tracker = ScreenerTracker(base_dir)
    return _screener_tracker
