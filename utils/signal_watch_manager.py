# -*- coding: utf-8 -*-
"""
策略信號監控管理器 — 自動將策略信號加入短期監控
================================================
當策略掃描產生信號時，自動將該標的加入臨時監查清單
並在盤後回填結果，評估是否保留或移除
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class SignalWatchEntry:
    """臨時監控項目"""
    symbol: str
    name: str
    source: str           # 哪個策略產生的信號
    added_date: str       # ISO 日期格式
    expiry_date: str      # 過期日期
    signal_price: float
    stop_loss: Optional[float]
    strength: str         # 強/中/弱
    status: str = "active"  # active / confirmed / rejected
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SignalWatchEntry":
        return cls(**d)


class SignalWatchManager:
    """
    管理策略信號產生的臨時監控清單

    檔案位置: logs/signal_watch/active_signals.json
    """

    DEFAULT_DURATION_DAYS = 5  # 預設監控 5 個交易日

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        self.base_dir = Path(base_dir)
        self.watch_dir = self.base_dir / "logs" / "signal_watch"
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.active_file = self.watch_dir / "active_signals.json"
        self.history_file = self.watch_dir / "signal_history.jsonl"

    def _load_active(self) -> List[SignalWatchEntry]:
        """載入當前活躍的監控項目"""
        if not self.active_file.exists():
            return []
        try:
            data = json.loads(self.active_file.read_text(encoding="utf-8"))
            return [SignalWatchEntry.from_dict(item) for item in data.get("signals", [])]
        except Exception as e:
            logger.error(f"載入活躍監控清單失敗: {e}")
            return []

    def _save_active(self, signals: List[SignalWatchEntry]):
        """保存活躍監控項目"""
        try:
            data = {
                "updated_at": datetime.now().isoformat(),
                "count": len(signals),
                "signals": [s.to_dict() for s in signals],
            }
            self.active_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存活躍監控清單失敗: {e}")

    def _add_to_history(self, entry: SignalWatchEntry, outcome: str):
        """將結案項目加入歷史記錄"""
        try:
            record = {
                "timestamp": datetime.now().isoformat(),
                "entry": entry.to_dict(),
                "final_outcome": outcome,
            }
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"加入歷史記錄失敗: {e}")

    def add_signals(self, signals_data: List[Dict[str, Any]], duration_days: int = None):
        """
        將策略信號加入臨時監控清單

        Args:
            signals_data: 來自 strategy_scanner 的信號字典列表
            duration_days: 監控天數，預設 5 天
        """
        if duration_days is None:
            duration_days = self.DEFAULT_DURATION_DAYS

        active = self._load_active()
        existing_symbols = {s.symbol for s in active}

        today = datetime.now().date()
        added_count = 0

        for sig in signals_data:
            symbol = sig.get("symbol", "")
            if not symbol or symbol in existing_symbols:
                continue

            # 計算過期日（排除週末的簡化版）
            expiry = today + timedelta(days=duration_days)
            # 簡單處理：如果過期日是週六日，延到週一
            while expiry.weekday() >= 5:  # 5=週六, 6=週日
                expiry += timedelta(days=1)

            entry = SignalWatchEntry(
                symbol=symbol,
                name=sig.get("name", symbol),
                source=sig.get("strategy", "unknown"),
                added_date=today.isoformat(),
                expiry_date=expiry.isoformat(),
                signal_price=sig.get("price", 0.0),
                stop_loss=sig.get("stop_loss"),
                strength=sig.get("strength", "中"),
                status="active",
                notes=f"信號強度: {sig.get('strength', '中')}, 標籤: {', '.join(sig.get('tags', []))}",
            )
            active.append(entry)
            added_count += 1
            logger.info(f"[SignalWatch] 新增監控: {symbol} ({entry.source}, 至 {expiry})")

        if added_count > 0:
            self._save_active(active)
            logger.info(f"[SignalWatch] 已新增 {added_count} 個臨時監控項目")

        return added_count

    def get_active_symbols(self) -> List[str]:
        """取得當前活躍監控的股票代號列表"""
        active = self._load_active()
        today = datetime.now().date()

        # 過濾已過期的
        valid = []
        expired = []
        for entry in active:
            expiry = datetime.fromisoformat(entry.expiry_date).date()
            if expiry >= today:
                valid.append(entry)
            else:
                expired.append(entry)

        # 保存清理後的清單
        if len(valid) != len(active):
            self._save_active(valid)
            for e in expired:
                self._add_to_history(e, "expired")
            logger.info(f"[SignalWatch] 清理 {len(expired)} 個過期監控項目")

        return [e.symbol for e in valid]

    def get_active_entries(self) -> List[SignalWatchEntry]:
        """取得完整的活躍監控項目詳情"""
        active = self._load_active()
        today = datetime.now().date()
        return [
            e for e in active
            if datetime.fromisoformat(e.expiry_date).date() >= today
        ]

    def update_outcome(self, symbol: str, close_price: float, outcome_note: str = ""):
        """
        更新監控項目的盤後結果

        Returns:
            是否達到確認條件（可轉為長期監控）
        """
        active = self._load_active()

        for entry in active:
            if entry.symbol == symbol:
                chg_pct = (close_price - entry.signal_price) / entry.signal_price * 100

                # 判斷結果
                if entry.stop_loss and close_price < entry.stop_loss:
                    entry.status = "rejected"
                    entry.notes += f" | 盤後: 觸及止損 ({chg_pct:+.1f}%)"
                    self._add_to_history(entry, "stop_loss_hit")
                elif chg_pct > 2.0:  # 漲超過 2% 視為確認
                    entry.status = "confirmed"
                    entry.notes += f" | 盤後: 走勢確認 (+{chg_pct:.1f}%)"
                    self._add_to_history(entry, "confirmed")
                elif chg_pct < -2.0:  # 跌超過 2% 視為失效
                    entry.status = "rejected"
                    entry.notes += f" | 盤後: 走勢失效 ({chg_pct:.1f}%)"
                    self._add_to_history(entry, "failed")
                else:
                    entry.notes += f" | 盤後: 持平觀察 ({chg_pct:+.1f}%)"
                    continue  # 保留在活躍清單

                self._save_active(active)
                logger.info(f"[SignalWatch] {symbol} 更新為 {entry.status}: {chg_pct:+.1f}%")
                return entry.status == "confirmed"

        return False

    def get_watch_list_additions(self) -> List[Dict[str, Any]]:
        """
        取得應該加入主監查清單的項目（status=confirmed）
        回傳格式兼容 stocks.yaml
        """
        active = self._load_active()
        additions = []

        for entry in active:
            if entry.status == "confirmed":
                additions.append({
                    "symbol": entry.symbol,
                    "name": entry.name,
                    "category": "momentum",  # 動能股
                    "lower": round(entry.signal_price * 0.92, 1),  # 止損區間
                    "upper": round(entry.signal_price * 1.15, 1),  # 目標區間
                    "alert_pct": 4.0,  # 波動較大
                    "source": f"strategy_{entry.source}",
                    "note": f"由 {entry.source} 信號確認轉入，建議價 {entry.signal_price}",
                })

        return additions

    def generate_daily_summary(self) -> str:
        """生成每日監控摘要供報告使用"""
        active = self.get_active_entries()

        if not active:
            return ""

        lines = [f"\n📡 【策略信號追蹤中】共 {len(active)} 檔"]

        for e in active:
            days_left = (datetime.fromisoformat(e.expiry_date).date() - datetime.now().date()).days
            lines.append(
                f"  • {e.name}（{e.symbol}）| {e.source} | "
                f"建議價 {e.signal_price:.1f} | 剩餘 {days_left} 天"
            )

        return "\n".join(lines)


# 全局實例（Singleton 模式）
_signal_watch_manager: Optional[SignalWatchManager] = None


def get_signal_watch_manager(base_dir: Optional[Path] = None) -> SignalWatchManager:
    """取得 SignalWatchManager 實例"""
    global _signal_watch_manager
    if _signal_watch_manager is None:
        _signal_watch_manager = SignalWatchManager(base_dir)
    return _signal_watch_manager
