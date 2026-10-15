# -*- coding: utf-8 -*-
"""
Agent 績效指標收集器
====================
收集各 Agent 執行時間、成功率、API 費用等指標
輸出到 logs/agent_metrics.jsonl，供長期優化分析
"""

import json
import logging
import time
import functools
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@dataclass
class AgentMetrics:
    """單次執行指標"""
    timestamp: str
    agent_name: str
    mode: str  # morning / afternoon / evening / etc.
    success: bool
    duration_sec: float
    error_type: Optional[str] = None

    # AI 成本（估算）
    ai_input_tokens: int = 0
    ai_output_tokens: int = 0
    ai_cost_usd: float = 0.0

    # 數據源狀態
    data_sources: Dict[str, str] = None  # {"yfinance": "ok", "finmind": "error"}

    # 產出品質
    output_items: int = 0  # 產出的項目數（如新聞數、警報數）
    output_quality_score: Optional[float] = None  # 0-100

    # 通知狀態
    notification_sent: bool = False
    notification_channels: list = None  # ["feishu", "wechat"]

    def __post_init__(self):
        if self.data_sources is None:
            self.data_sources = {}
        if self.notification_channels is None:
            self.notification_channels = []


class AgentMetricsCollector:
    """
    Agent 績效指標收集器

    檔案：logs/agent_metrics.jsonl
    """

    # Claude API 費用（每 1K tokens）
    CLAUDE_PRICING = {
        "input": 0.003,   # $0.003 per 1K tokens
        "output": 0.015,  # $0.015 per 1K tokens
    }

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        self.base_dir = Path(base_dir)
        self.metrics_dir = self.base_dir / "logs"
        self.metrics_file = self.metrics_dir / "agent_metrics.jsonl"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        # 當前執行的指標（用於累積）
        self._current: Optional[AgentMetrics] = None
        self._start_time: Optional[float] = None

    def start_run(self, agent_name: str, mode: str = ""):
        """開始記錄一次執行"""
        self._current = AgentMetrics(
            timestamp=datetime.now().isoformat(),
            agent_name=agent_name,
            mode=mode,
            success=False,
            duration_sec=0.0,
        )
        self._start_time = time.time()
        return self

    def set_ai_usage(self, input_tokens: int = 0, output_tokens: int = 0):
        """記錄 AI 使用量"""
        if self._current:
            self._current.ai_input_tokens = input_tokens
            self._current.ai_output_tokens = output_tokens
            # 估算費用
            input_cost = (input_tokens / 1000) * self.CLAUDE_PRICING["input"]
            output_cost = (output_tokens / 1000) * self.CLAUDE_PRICING["output"]
            self._current.ai_cost_usd = round(input_cost + output_cost, 4)

    def set_data_source_status(self, source: str, status: str):
        """記錄數據源狀態"""
        if self._current:
            self._current.data_sources[source] = status

    def set_output_metrics(self, items: int = 0, quality_score: Optional[float] = None):
        """記錄產出指標"""
        if self._current:
            self._current.output_items = items
            if quality_score is not None:
                self._current.output_quality_score = quality_score

    def set_notification_status(self, sent: bool, channels: list = None):
        """記錄通知狀態"""
        if self._current:
            self._current.notification_sent = sent
            if channels:
                self._current.notification_channels = channels

    def end_run(self, success: bool = True, error: Optional[str] = None):
        """結束記錄並保存"""
        if self._current and self._start_time:
            self._current.duration_sec = round(time.time() - self._start_time, 2)
            self._current.success = success
            if error:
                self._current.error_type = error[:100]  # 限制長度

            # 寫入檔案
            try:
                with open(self.metrics_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(self._current), ensure_ascii=False) + "\n")
                logger.debug(f"[Metrics] 已記錄 {self._current.agent_name} 執行指標")
            except Exception as e:
                logger.debug(f"[Metrics] 記錄失敗: {e}")

        self._current = None
        self._start_time = None

    @contextmanager
    def measure(self, agent_name: str, mode: str = ""):
        """上下文管理器，方便使用 with 語句"""
        self.start_run(agent_name, mode)
        try:
            yield self
            self.end_run(success=True)
        except Exception as e:
            self.end_run(success=False, error=str(e))
            raise


def metrics_decorator(agent_name: str, mode: str = ""):
    """裝飾器，自動收集函數執行指標"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            collector = AgentMetricsCollector()
            with collector.measure(agent_name, mode or kwargs.get("mode", "")):
                result = func(*args, **kwargs)
                # 嘗試從結果提取指標
                if isinstance(result, dict):
                    if "items" in result:
                        collector.set_output_metrics(items=result["items"])
                    if "ai_tokens" in result:
                        collector.set_ai_usage(**result["ai_tokens"])
                return result
        return wrapper
    return decorator


class AgentMetricsReporter:
    """生成績效報告"""

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        self.base_dir = Path(base_dir)
        self.metrics_file = self.base_dir / "logs" / "agent_metrics.jsonl"

    def load_metrics(self, days: int = 7) -> list:
        """載入最近 N 天的指標"""
        if not self.metrics_file.exists():
            return []

        cutoff = datetime.now().timestamp() - (days * 24 * 3600)
        metrics = []

        try:
            with open(self.metrics_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        ts = datetime.fromisoformat(record["timestamp"])
                        if ts.timestamp() > cutoff:
                            metrics.append(record)
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"載入指標失敗: {e}")

        return metrics

    def generate_summary(self, days: int = 7) -> Dict[str, Any]:
        """生成摘要報告"""
        metrics = self.load_metrics(days)

        if not metrics:
            return {"error": "無可用數據"}

        # 按 Agent 分組統計
        by_agent: Dict[str, list] = {}
        for m in metrics:
            name = m["agent_name"]
            if name not in by_agent:
                by_agent[name] = []
            by_agent[name].append(m)

        summary = {
            "period_days": days,
            "total_runs": len(metrics),
            "by_agent": {},
            "overall": {
                "success_rate": 0,
                "avg_duration": 0,
                "total_cost_usd": 0,
            }
        }

        total_success = 0
        total_duration = 0
        total_cost = 0

        for agent_name, records in by_agent.items():
            success_count = sum(1 for r in records if r["success"])
            durations = [r["duration_sec"] for r in records]
            costs = [r.get("ai_cost_usd", 0) for r in records]

            agent_summary = {
                "runs": len(records),
                "successes": success_count,
                "failures": len(records) - success_count,
                "success_rate": round(success_count / len(records) * 100, 1),
                "avg_duration_sec": round(sum(durations) / len(durations), 2),
                "max_duration_sec": round(max(durations), 2),
                "total_cost_usd": round(sum(costs), 4),
            }

            summary["by_agent"][agent_name] = agent_summary

            total_success += success_count
            total_duration += sum(durations)
            total_cost += sum(costs)

        # 整體統計
        if metrics:
            summary["overall"]["success_rate"] = round(total_success / len(metrics) * 100, 1)
            summary["overall"]["avg_duration"] = round(total_duration / len(metrics), 2)
            summary["overall"]["total_cost_usd"] = round(total_cost, 4)

        return summary

    def format_report(self, summary: Dict[str, Any]) -> str:
        """格式化報告為可讀文字"""
        if "error" in summary:
            return f"# Agent 績效報告\n\n{summary['error']}"

        lines = [
            f"# 🤖 Agent 績效報告（最近 {summary['period_days']} 天）",
            "",
            f"**總執行次數**: {summary['total_runs']}",
            f"**整體成功率**: {summary['overall']['success_rate']}%",
            f"**平均執行時間**: {summary['overall']['avg_duration']} 秒",
            f"**AI API 總費用**: ${summary['overall']['total_cost_usd']}",
            "",
            "## 各 Agent 表現",
            "",
        ]

        for agent_name, stats in summary["by_agent"].items():
            icon = "✅" if stats["success_rate"] >= 90 else "⚠️" if stats["success_rate"] >= 70 else "❌"
            lines.extend([
                f"### {icon} {agent_name}",
                f"- 執行: {stats['runs']} 次 | 成功: {stats['successes']} | 失敗: {stats['failures']}",
                f"- 成功率: {stats['success_rate']}%",
                f"- 平均耗時: {stats['avg_duration_sec']} 秒 (最長: {stats['max_duration_sec']} 秒)",
                f"- AI 費用: ${stats['total_cost_usd']}",
                "",
            ])

        return "\n".join(lines)

    def generate_weekly_report(self) -> str:
        """生成週報告"""
        summary = self.generate_summary(days=7)
        return self.format_report(summary)


# 全局實例
_metrics_collector: Optional[AgentMetricsCollector] = None
_metrics_reporter: Optional[AgentMetricsReporter] = None


def get_metrics_collector() -> AgentMetricsCollector:
    """取得指標收集器實例"""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = AgentMetricsCollector()
    return _metrics_collector


def get_metrics_reporter() -> AgentMetricsReporter:
    """取得指標報告器實例"""
    global _metrics_reporter
    if _metrics_reporter is None:
        _metrics_reporter = AgentMetricsReporter()
    return _metrics_reporter
