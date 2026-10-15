# -*- coding: utf-8 -*-
"""
Agent 間契約定義 — 明確定義各 Agent 輸入輸出 Interface
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum


class AgentStatus(Enum):
    """Agent 執行狀態"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotificationChannel(Enum):
    """通知渠道"""
    FEISHU = "feishu"
    WECHAT = "wechat"
    EMAIL = "email"


@dataclass
class AgentOutput:
    """Agent 基礎輸出"""
    agent_name: str
    status: AgentStatus
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


@dataclass
class NewsOutput(AgentOutput):
    """新聞資訊 W 輸出"""
    file_path: str = ""
    economy_count: int = 0
    tech_count: int = 0
    risk_keywords: List[str] = field(default_factory=list)
    payload_steps: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.agent_name = "新聞資訊 W"


@dataclass
class StockBriefOutput(AgentOutput):
    """股市 W 輸出"""
    mode: str = "morning"  # morning / afternoon
    alerts_count: int = 0
    alerts_file: Optional[str] = None
    watch_list_summary: str = ""

    def __post_init__(self):
        self.agent_name = f"股市 W ({self.mode})"


@dataclass
class ScreenerPick:
    """選股結果單一標的"""
    symbol: str
    name: str
    engine: str  # momentum / rebound
    price: float
    score: int
    stop_loss: float
    take_profit: float
    time_stop: str
    inst_signal: str


@dataclass
class ScreenerOutput(AgentOutput):
    """短線選股 W 輸出"""
    date: str = ""
    momentum_picks: List[ScreenerPick] = field(default_factory=list)
    rebound_picks: List[ScreenerPick] = field(default_factory=list)
    sentiment_score: int = 50
    narrative_file: Optional[str] = None

    def __post_init__(self):
        self.agent_name = "短線選股 W"


@dataclass
class DailyReportOutput(AgentOutput):
    """日報 W 輸出"""
    report_file: str = ""
    sections_included: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.agent_name = "日報 W"


@dataclass
class ResearchOutput(AgentOutput):
    """深度研究 W 輸出"""
    symbol: str = ""
    trigger_type: str = ""
    report_path: Optional[str] = None
    report_summary: str = ""

    def __post_init__(self):
        self.agent_name = "深度研究 W"


@dataclass
class MemoryIngestOutput(AgentOutput):
    """記憶管家 W 輸出"""
    chunks_added: int = 0
    sources_processed: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.agent_name = "記憶管家 W"


@dataclass
class DailyStockReportOutput(AgentOutput):
    """台股每日專業報告 W 輸出"""
    report_file: str = ""
    pdf_file: Optional[str] = None
    payload_file: Optional[str] = None

    def __post_init__(self):
        self.agent_name = "台股每日專業報告 W"


@dataclass
class MarketReviewOutput(AgentOutput):
    """大盤盤後復盤 W 輸出"""
    review_file: str = ""
    summary: str = ""

    def __post_init__(self):
        self.agent_name = "大盤盤後復盤 W"


@dataclass
class SepaScreenerOutput(AgentOutput):
    """成長選股 W 輸出"""
    week: str = ""
    picks_count: int = 0
    report_file: Optional[str] = None

    def __post_init__(self):
        self.agent_name = "成長選股 W"


@dataclass
class ValueScreenerOutput(AgentOutput):
    """價值選股 W 輸出"""
    week: str = ""
    picks_count: int = 0
    report_file: Optional[str] = None

    def __post_init__(self):
        self.agent_name = "價值選股 W"


@dataclass
class IntradayScreenerOutput(AgentOutput):
    """日內選股 W 輸出"""
    date: str = ""
    picks_count: int = 0
    snapshot_file: Optional[str] = None

    def __post_init__(self):
        self.agent_name = "日內選股 W"


@dataclass
class EcommerceOutput(AgentOutput):
    """跨境電商 W 輸出"""
    report_file: str = ""
    opportunities_count: int = 0

    def __post_init__(self):
        self.agent_name = "跨境電商 W"


@dataclass
class ReflectionOutput(AgentOutput):
    """反思 W 輸出"""
    reflection_file: str = ""
    next_week_focus_file: Optional[str] = None

    def __post_init__(self):
        self.agent_name = "反思 W"


class AgentValidator:
    """Agent 輸入輸出驗證器"""

    @staticmethod
    def validate_news_output(data: dict) -> tuple[bool, Optional[str]]:
        """驗證新聞輸出"""
        required = ["file_path", "economy_count"]
        for field in required:
            if field not in data:
                return False, f"缺少必要欄位: {field}"
        return True, None

    @staticmethod
    def validate_stock_output(data: dict) -> tuple[bool, Optional[str]]:
        """驗證股市輸出"""
        required = ["mode", "alerts_count"]
        for field in required:
            if field not in data:
                return False, f"缺少必要欄位: {field}"
        if data.get("mode") not in ["morning", "afternoon"]:
            return False, "mode 必須是 morning 或 afternoon"
        return True, None

    @staticmethod
    def validate_screener_output(data: dict) -> tuple[bool, Optional[str]]:
        """驗證選股輸出"""
        required = ["date", "momentum_picks", "rebound_picks"]
        for field in required:
            if field not in data:
                return False, f"缺少必要欄位: {field}"
        return True, None


def create_agent_output(agent_type: str, **kwargs) -> AgentOutput:
    """
    工廠函數：建立對應的 Agent 輸出

    Args:
        agent_type: Agent 類型
        **kwargs: 額外參數

    Returns:
        AgentOutput 子類實例
    """
    mapping = {
        "news": NewsOutput,
        "stock": StockBriefOutput,
        "screener": ScreenerOutput,
        "daily_report": DailyReportOutput,
        "daily_stock_report": DailyStockReportOutput,
        "market_review": MarketReviewOutput,
        "research": ResearchOutput,
        "memory": MemoryIngestOutput,
        "sepa_screener": SepaScreenerOutput,
        "value_screener": ValueScreenerOutput,
        "intraday_screener": IntradayScreenerOutput,
        "ecommerce": EcommerceOutput,
        "reflection": ReflectionOutput,
    }

    cls = mapping.get(agent_type, AgentOutput)
    return cls(**kwargs)
