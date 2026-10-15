# -*- coding: utf-8 -*-
"""
研究觸發抑振機制 — 防止同一標的連續多天重複觸發研究

核心功能：
1. 記錄最近觸發過的標的
2. 檢查是否需要重新觸發（價格變動 > 5% 或有新聞事件）
3. 自動清理過期記錄
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class TriggerHistory:
    """觸發歷史記錄"""
    symbol: str
    trigger_types: List[str]  # 觸發類型列表
    triggered_at: str
    price_at_trigger: float
    severity: str
    research_completed: bool = False
    research_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "trigger_types": self.trigger_types,
            "triggered_at": self.triggered_at,
            "price_at_trigger": self.price_at_trigger,
            "severity": self.severity,
            "research_completed": self.research_completed,
            "research_path": self.research_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerHistory":
        return cls(
            symbol=data.get("symbol", ""),
            trigger_types=data.get("trigger_types", []),
            triggered_at=data.get("triggered_at", ""),
            price_at_trigger=data.get("price_at_trigger", 0.0),
            severity=data.get("severity", "info"),
            research_completed=data.get("research_completed", False),
            research_path=data.get("research_path"),
        )


class ResearchTriggerSuppressor:
    """
    研究觸發抑振器

    檔案結構：
    logs/research_triggers/
      history.jsonl         — 觸發歷史（最近 7 天）
      suppressed.jsonl      — 被抑制的觸發記錄
      daily_summary.json    — 每日摘要
    """

    # 抑制設定
    DEFAULT_SUPPRESS_DAYS = 7  # 預設抑制 7 天
    PRICE_CHANGE_THRESHOLD = 5.0  # 價格變動 > 5% 才重新觸發
    MAX_SUPPRESS_DAYS = 30  # 最大抑制天數

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent / "logs" / "research_triggers"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._history_file = self.base_dir / "history.jsonl"
        self._suppressed_file = self.base_dir / "suppressed.jsonl"
        self._summary_file = self.base_dir / "daily_summary.json"

        # 記憶體快取
        self._cached_history: Dict[str, TriggerHistory] = {}
        self._load_cache()

    def _load_cache(self):
        """載入最近的歷史記錄到快取"""
        cutoff = datetime.now() - timedelta(days=self.MAX_SUPPRESS_DAYS)

        if not self._history_file.exists():
            return

        try:
            with open(self._history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        history = TriggerHistory.from_dict(data)
                        # 只載入最近記錄
                        triggered_time = datetime.fromisoformat(history.triggered_at)
                        if triggered_time >= cutoff:
                            # 同一標的保留最新的
                            if history.symbol in self._cached_history:
                                existing = self._cached_history[history.symbol]
                                if history.triggered_at > existing.triggered_at:
                                    self._cached_history[history.symbol] = history
                            else:
                                self._cached_history[history.symbol] = history
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"載入觸發歷史失敗: {e}")

    def should_trigger(
        self,
        symbol: str,
        current_price: float,
        trigger_type: str,
        news_hash: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        檢查是否應該觸發研究

        Args:
            symbol: 股票代號
            current_price: 當前價格
            trigger_type: 觸發類型
            news_hash: 新聞雜湊（用於檢測新聞變化）

        Returns:
            (should_trigger, reason)
            - should_trigger: True 表示應該觸發
            - reason: 原因說明
        """
        # 清理舊記錄
        self._cleanup_old_records()

        # 檢查是否有歷史記錄
        history = self._cached_history.get(symbol)
        if not history:
            return True, "無近期觸發記錄"

        # 檢查時間間隔
        try:
            last_triggered = datetime.fromisoformat(history.triggered_at)
            days_since = (datetime.now() - last_triggered).days

            if days_since >= self.MAX_SUPPRESS_DAYS:
                return True, f"已超過最大抑制天數 ({days_since} 天)"

            if days_since < self.DEFAULT_SUPPRESS_DAYS:
                # 在抑制期內，檢查例外條件

                # 例外 1: 價格變動 > 5%
                if current_price > 0 and history.price_at_trigger > 0:
                    price_change_pct = abs(current_price - history.price_at_trigger) / history.price_at_trigger * 100
                    if price_change_pct >= self.PRICE_CHANGE_THRESHOLD:
                        return True, f"價格變動 {price_change_pct:.1f}% > 門檻 {self.PRICE_CHANGE_THRESHOLD}%"

                # 例外 2: 嚴重程度升級
                severity_order = {"info": 0, "warning": 1, "critical": 2}
                current_severity_value = severity_order.get(self._estimate_severity(trigger_type), 0)
                history_severity_value = severity_order.get(history.severity, 0)

                if current_severity_value > history_severity_value:
                    return True, f"嚴重程度升級 ({history.severity} -> {self._estimate_severity(trigger_type)})"

                # 例外 3: 新聞事件變化
                if news_hash and news_hash != history.trigger_types[0] if history.trigger_types else True:
                    return True, "新聞事件變化"

                # 抑制觸發
                reason = f"距上次觸發僅 {days_since} 天，且未達重新觸發條件"
                self._record_suppressed(symbol, trigger_type, current_price, reason)
                return False, reason

            # 超過預設抑制期，但還在最大抑制期內
            return True, f"已超過預設抑制期 ({days_since} 天)"

        except Exception as e:
            logger.warning(f"檢查抑制條件失敗 {symbol}: {e}")
            return True, "檢查異常，允許觸發"

    def _estimate_severity(self, trigger_type: str) -> str:
        """根據觸發類型估計嚴重程度"""
        severity_map = {
            "volume": "warning",
            "institution": "warning",
            "technical": "info",
            "sentiment": "info",
            "critical": "critical",
        }
        return severity_map.get(trigger_type, "info")

    def record_trigger(
        self,
        symbol: str,
        trigger_types: List[str],
        price: float,
        severity: str = "warning",
        research_path: Optional[str] = None,
    ):
        """
        記錄觸發事件

        Args:
            symbol: 股票代號
            trigger_types: 觸發類型列表
            price: 觸發時價格
            severity: 嚴重程度
            research_path: 研究報告路徑
        """
        history = TriggerHistory(
            symbol=symbol,
            trigger_types=trigger_types,
            triggered_at=datetime.now().isoformat(),
            price_at_trigger=price,
            severity=severity,
            research_completed=research_path is not None,
            research_path=research_path,
        )

        # 更新快取
        self._cached_history[symbol] = history

        # 追加到歷史檔
        with open(self._history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(history.to_dict(), ensure_ascii=False) + "\n")

        logger.info(f"📝 [{symbol}] 研究觸發已記錄，類型: {', '.join(trigger_types)}")

    def _record_suppressed(
        self,
        symbol: str,
        trigger_type: str,
        price: float,
        reason: str,
    ):
        """記錄被抑制的觸發"""
        suppressed_record = {
            "symbol": symbol,
            "trigger_type": trigger_type,
            "price": price,
            "suppressed_at": datetime.now().isoformat(),
            "reason": reason,
        }

        with open(self._suppressed_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(suppressed_record, ensure_ascii=False) + "\n")

        logger.info(f"⏭️ [{symbol}] 研究觸發被抑制: {reason}")

    def _cleanup_old_records(self):
        """清理過期記錄"""
        cutoff = datetime.now() - timedelta(days=self.MAX_SUPPRESS_DAYS)
        cleaned = 0

        # 清理快取
        symbols_to_remove = []
        for symbol, history in self._cached_history.items():
            try:
                triggered_time = datetime.fromisoformat(history.triggered_at)
                if triggered_time < cutoff:
                    symbols_to_remove.append(symbol)
            except Exception:
                continue

        for symbol in symbols_to_remove:
            del self._cached_history[symbol]
            cleaned += 1

        if cleaned > 0:
            logger.debug(f"清理 {cleaned} 筆過期觸發記錄")

    def get_recent_triggers(self, days: int = 7) -> List[TriggerHistory]:
        """
        取得最近觸發記錄

        Args:
            days: 最近幾天

        Returns:
            觸發歷史列表
        """
        cutoff = datetime.now() - timedelta(days=days)
        results = []

        for history in self._cached_history.values():
            try:
                triggered_time = datetime.fromisoformat(history.triggered_at)
                if triggered_time >= cutoff:
                    results.append(history)
            except Exception:
                continue

        # 按時間排序
        results.sort(key=lambda x: x.triggered_at, reverse=True)
        return results

    def get_suppression_stats(self) -> Dict[str, Any]:
        """取得抑制統計"""
        total_triggered = len(self._cached_history)
        total_suppressed = 0

        if self._suppressed_file.exists():
            try:
                with open(self._suppressed_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            total_suppressed += 1
            except Exception:
                pass

        today = datetime.now().strftime("%Y-%m-%d")
        today_triggered = sum(
            1 for h in self._cached_history.values()
            if h.triggered_at.startswith(today)
        )

        return {
            "total_triggered": total_triggered,
            "total_suppressed": total_suppressed,
            "today_triggered": today_triggered,
            "suppression_rate": total_suppressed / (total_triggered + total_suppressed) if (total_triggered + total_suppressed) > 0 else 0,
        }

    def reset_symbol(self, symbol: str):
        """
        重置特定標的的抑制狀態

        Args:
            symbol: 股票代號
        """
        if symbol in self._cached_history:
            del self._cached_history[symbol]
            logger.info(f"🗑️ [{symbol}] 抑制狀態已重置")


# 全局實例
_trigger_suppressor: Optional[ResearchTriggerSuppressor] = None


def get_trigger_suppressor(base_dir: Optional[Path] = None) -> ResearchTriggerSuppressor:
    """取得 ResearchTriggerSuppressor 實例"""
    global _trigger_suppressor
    if _trigger_suppressor is None:
        _trigger_suppressor = ResearchTriggerSuppressor(base_dir)
    return _trigger_suppressor
