# -*- coding: utf-8 -*-
"""
資料品質監控儀表板 — 統一呈現各數據源健康狀況

功能：
1. 收集每日數據品質指標
2. 生成品質摘要（JSON + Markdown）
3. 整合進每日日報
4. 異常時發送警報
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class DataSourceMetrics:
    """單一數據源指標"""
    source_name: str  # yfinance, finmind, twse
    total_requests: int = 0
    success_count: int = 0
    fail_count: int = 0
    avg_delay_ms: float = 0.0
    max_delay_ms: float = 0.0
    last_success_time: Optional[str] = None
    last_error: Optional[str] = None
    error_types: Dict[str, int] = None

    def __post_init__(self):
        if self.error_types is None:
            self.error_types = {}

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 100.0
        return (self.success_count / self.total_requests) * 100

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "total_requests": self.total_requests,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "success_rate": round(self.success_rate, 2),
            "avg_delay_ms": round(self.avg_delay_ms, 2),
            "max_delay_ms": round(self.max_delay_ms, 2),
            "last_success_time": self.last_success_time,
            "last_error": self.last_error,
            "error_types": self.error_types,
        }


@dataclass
class DailyQualitySummary:
    """每日品質摘要"""
    date: str
    created_at: str
    overall_health: str  # green, yellow, red
    data_sources: List[DataSourceMetrics]
    fallback_usage: List[str]  # 哪些股票用到備援
    alerts: List[str]  # 品質警報訊息
    recommendations: List[str]  # 改善建議

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "created_at": self.created_at,
            "overall_health": self.overall_health,
            "data_sources": [ds.to_dict() for ds in self.data_sources],
            "fallback_usage": self.fallback_usage,
            "alerts": self.alerts,
            "recommendations": self.recommendations,
        }


class DataQualityDashboard:
    """
    資料品質監控儀表板

    檔案結構：
    logs/data_quality/
      YYYY-MM-DD.json       — 每日品質摘要
      latest.json           — 最新摘要（符號連結）
      trends.jsonl          — 歷史趨勢
    """

    # 健康度門檻
    HEALTH_THRESHOLDS = {
        "green": {"min_success_rate": 95, "max_avg_delay_ms": 5000},
        "yellow": {"min_success_rate": 80, "max_avg_delay_ms": 10000},
        "red": {"min_success_rate": 0, "max_avg_delay_ms": float("inf")},
    }

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent / "logs" / "data_quality"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._today_str = datetime.now().strftime("%Y-%m-%d")
        self._summary_file = self.base_dir / f"{self._today_str}.json"
        self._latest_link = self.base_dir / "latest.json"
        self._trends_file = self.base_dir / "trends.jsonl"

        # 今日的累積指標
        self._today_metrics: Dict[str, DataSourceMetrics] = {}
        self._fallback_usage: List[str] = []
        self._alerts: List[str] = []

    def _get_or_create_metrics(self, source_name: str) -> DataSourceMetrics:
        """取得或建立數據源指標"""
        if source_name not in self._today_metrics:
            self._today_metrics[source_name] = DataSourceMetrics(source_name=source_name)
        return self._today_metrics[source_name]

    def record_request(
        self,
        source_name: str,
        success: bool,
        delay_ms: float = 0.0,
        error: Optional[str] = None,
        symbol: Optional[str] = None,
    ):
        """
        記錄單次請求結果

        Args:
            source_name: 數據源名稱 (yfinance, finmind, twse)
            success: 是否成功
            delay_ms: 延遲（毫秒）
            error: 錯誤訊息
            symbol: 相關股票代號
        """
        metrics = self._get_or_create_metrics(source_name)
        metrics.total_requests += 1

        if success:
            metrics.success_count += 1
            metrics.last_success_time = datetime.now().isoformat()

            # 更新平均延遲
            old_avg = metrics.avg_delay_ms
            n = metrics.success_count
            metrics.avg_delay_ms = (old_avg * (n - 1) + delay_ms) / n
            metrics.max_delay_ms = max(metrics.max_delay_ms, delay_ms)
        else:
            metrics.fail_count += 1
            metrics.last_error = error

            # 記錄錯誤類型
            error_type = self._classify_error(error)
            metrics.error_types[error_type] = metrics.error_types.get(error_type, 0) + 1

            # 記錄警報
            if symbol:
                self._alerts.append(f"{source_name}: {symbol} 請求失敗 - {error}")
            else:
                self._alerts.append(f"{source_name}: 請求失敗 - {error}")

    def record_fallback_usage(self, symbol: str, primary_source: str, fallback_source: str):
        """
        記錄備援數據源使用

        Args:
            symbol: 股票代號
            primary_source: 主要數據源（失敗）
            fallback_source: 備援數據源
        """
        self._fallback_usage.append(f"{symbol} ({primary_source} → {fallback_source})")
        logger.info(f"🔄 備援機制啟動: {symbol} {primary_source} → {fallback_source}")

    def _classify_error(self, error: Optional[str]) -> str:
        """分類錯誤類型"""
        if not error:
            return "unknown"

        error_lower = error.lower()
        if "timeout" in error_lower or "timed out" in error_lower:
            return "timeout"
        elif "connection" in error_lower or "connect" in error_lower:
            return "connection"
        elif "rate limit" in error_lower or "429" in error_lower:
            return "rate_limit"
        elif "parse" in error_lower or "json" in error_lower:
            return "parse_error"
        elif "empty" in error_lower or "no data" in error_lower:
            return "no_data"
        else:
            return "other"

    def _calculate_health(self) -> str:
        """計算整體健康度"""
        if not self._today_metrics:
            return "green"

        # 檢查每個數據源
        for metrics in self._today_metrics.values():
            # 成功率檢查
            if metrics.success_rate < self.HEALTH_THRESHOLDS["yellow"]["min_success_rate"]:
                return "red"
            elif metrics.success_rate < self.HEALTH_THRESHOLDS["green"]["min_success_rate"]:
                health = "yellow"

            # 延遲檢查
            if metrics.avg_delay_ms > self.HEALTH_THRESHOLDS["yellow"]["max_avg_delay_ms"]:
                return "red"
            elif metrics.avg_delay_ms > self.HEALTH_THRESHOLDS["green"]["max_avg_delay_ms"]:
                health = "yellow"

        return health if "health" in dir() else "green"

    def _generate_recommendations(self) -> List[str]:
        """生成改善建議"""
        recommendations = []

        for metrics in self._today_metrics.values():
            # 成功率偏低
            if metrics.success_rate < 90:
                recommendations.append(
                    f"{metrics.source_name}: 成功率僅 {metrics.success_rate:.1f}%，建議檢查 API 狀態"
                )

            # 延遲偏高
            if metrics.avg_delay_ms > 5000:
                recommendations.append(
                    f"{metrics.source_name}: 平均延遲 {metrics.avg_delay_ms:.0f}ms，建議優化請求策略"
                )

            # 特定錯誤類型集中
            for error_type, count in metrics.error_types.items():
                if count > 5:
                    recommendations.append(
                        f"{metrics.source_name}: {error_type} 錯誤頻繁 ({count} 次)，建議調查原因"
                    )

        # 備援使用頻繁
        if len(self._fallback_usage) > 10:
            recommendations.append(
                f"備援機制頻繁啟動 ({len(self._fallback_usage)} 次)，建議檢查主要數據源穩定性"
            )

        return recommendations

    def generate_summary(self) -> DailyQualitySummary:
        """生成每日品質摘要"""
        health = self._calculate_health()
        recommendations = self._generate_recommendations()

        summary = DailyQualitySummary(
            date=self._today_str,
            created_at=datetime.now().isoformat(),
            overall_health=health,
            data_sources=list(self._today_metrics.values()),
            fallback_usage=self._fallback_usage,
            alerts=self._alerts,
            recommendations=recommendations,
        )

        return summary

    def save_summary(self, summary: Optional[DailyQualitySummary] = None) -> Path:
        """
        保存品質摘要

        Args:
            summary: 品質摘要（None 則自動生成）

        Returns:
            保存的檔案路徑
        """
        if summary is None:
            summary = self.generate_summary()

        # 保存每日摘要
        self._summary_file.write_text(
            json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # 更新 latest.json 符號連結
        if self._latest_link.exists():
            self._latest_link.unlink()
        self._latest_link.symlink_to(self._summary_file.name)

        # 追加到趨勢檔
        with open(self._trends_file, "a", encoding="utf-8") as f:
            trend_record = {
                "date": summary.date,
                "health": summary.overall_health,
                "sources": {ds.source_name: ds.success_rate for ds in summary.data_sources},
                "fallback_count": len(summary.fallback_usage),
                "alert_count": len(summary.alerts),
            }
            f.write(json.dumps(trend_record, ensure_ascii=False) + "\n")

        logger.info(f"📊 資料品質摘要已保存: {self._summary_file}")
        return self._summary_file

    def format_markdown_report(self, summary: Optional[DailyQualitySummary] = None) -> str:
        """
        格式化 Markdown 報告

        Args:
            summary: 品質摘要（None 則自動生成）

        Returns:
            Markdown 格式報告
        """
        if summary is None:
            summary = self.generate_summary()

        lines = [
            "## 📊 資料品質監控報告",
            f"**日期**: {summary.date}",
            f"**整體健康度**: {self._health_emoji(summary.overall_health)} {summary.overall_health.upper()}",
            "",
            "### 數據源狀態",
            "",
            "| 數據源 | 請求數 | 成功率 | 平均延遲 | 狀態 |",
            "|--------|--------|--------|----------|------|",
        ]

        for ds in summary.data_sources:
            status = self._health_emoji("green" if ds.success_rate >= 95 else "yellow" if ds.success_rate >= 80 else "red")
            lines.append(
                f"| {ds.source_name} | {ds.total_requests} | {ds.success_rate:.1f}% | {ds.avg_delay_ms:.0f}ms | {status} |"
            )

        if summary.fallback_usage:
            lines.extend([
                "",
                "### 🔄 備援機制使用",
                "",
            ])
            for item in summary.fallback_usage[:10]:  # 最多顯示 10 筆
                lines.append(f"- {item}")
            if len(summary.fallback_usage) > 10:
                lines.append(f"- ... 還有 {len(summary.fallback_usage) - 10} 筆")

        if summary.alerts:
            lines.extend([
                "",
                "### ⚠️ 品質警報",
                "",
            ])
            for alert in summary.alerts[:5]:  # 最多顯示 5 筆
                lines.append(f"- {alert}")
            if len(summary.alerts) > 5:
                lines.append(f"- ... 還有 {len(summary.alerts) - 5} 筆")

        if summary.recommendations:
            lines.extend([
                "",
                "### 💡 改善建議",
                "",
            ])
            for rec in summary.recommendations:
                lines.append(f"- {rec}")

        return "\n".join(lines)

    def _health_emoji(self, health: str) -> str:
        """健康度表情符號"""
        return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(health, "⚪")

    def get_historical_trends(self, days: int = 7) -> List[Dict[str, Any]]:
        """
        取得歷史趨勢

        Args:
            days: 查詢天數

        Returns:
            趨勢記錄列表
        """
        if not self._trends_file.exists():
            return []

        cutoff = datetime.now() - timedelta(days=days)
        results = []

        try:
            with open(self._trends_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        record_date = datetime.strptime(record["date"], "%Y-%m-%d")
                        if record_date >= cutoff:
                            results.append(record)
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"讀取趨勢檔失敗: {e}")

        return results


# 全局實例
_quality_dashboard: Optional[DataQualityDashboard] = None


def get_quality_dashboard(base_dir: Optional[Path] = None) -> DataQualityDashboard:
    """取得 DataQualityDashboard 單例"""
    global _quality_dashboard
    if _quality_dashboard is None:
        _quality_dashboard = DataQualityDashboard(base_dir)
    return _quality_dashboard
