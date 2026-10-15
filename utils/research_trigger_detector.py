# -*- coding: utf-8 -*-
"""
Research W 擴充觸發偵測器
========================
擴充 researcher.py 的觸發條件，包含：
1. 成交量異常（> 3σ）
2. 法人連續買賣（3日累積）
3. 技術指標突破（RSI 超買超賣、MACD 交叉）
4. 新聞情緒急遽轉向
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ResearchTrigger:
    """研究觸發事件"""
    symbol: str
    name: str
    trigger_type: str  # volume / institution / technical / sentiment
    severity: str  # critical / warning / info
    description: str
    metrics: Dict[str, Any]  # 觸發指標詳情
    suggested_topic: str  # 建議研究主題
    timestamp: str


def format_name_with_code(name: str, symbol: str) -> str:
    suffix = f"（{symbol.replace('.TW', '').replace('.TWO', '').lstrip('^')}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


class ResearchTriggerDetector:
    """
    研究觸發偵測器

    偵測市場異常事件，自動觸發 Research W 進行深度分析
    """

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        self.base_dir = Path(base_dir)

        # 觸發門檻設定
        self.thresholds = {
            "volume_zscore": 3.0,  # 成交量 Z-score 門檻
            "volume_ratio": 3.0,   # 量比門檻
            "inst_consecutive_days": 3,  # 法人連續買賣天數
            "inst_net_threshold": 1000,  # 法人門檻（張）
            "rsi_overbought": 75,  # RSI 超買
            "rsi_oversold": 25,    # RSI 超賣
            "price_change_pct": 4.5,  # 價格變動門檻
        }

    def detect_volume_anomaly(
        self,
        symbol: str,
        name: str,
        df: pd.DataFrame,
        current_price: float
    ) -> Optional[ResearchTrigger]:
        """
        偵測成交量異常

        條件：
        - 成交量 Z-score > 3
        - 量比 > 3
        """
        if len(df) < 20:
            return None

        try:
            volume = df["volume"].iloc[-1]
            volume_ma20 = df["volume"].rolling(20).mean().iloc[-1]
            volume_std = df["volume"].rolling(20).std().iloc[-1]

            if volume_std == 0:
                return None

            z_score = (volume - volume_ma20) / volume_std
            volume_ratio = volume / volume_ma20 if volume_ma20 > 0 else 0

            if z_score >= self.thresholds["volume_zscore"] or \
               volume_ratio >= self.thresholds["volume_ratio"]:

                severity = "critical" if z_score >= 4 or volume_ratio >= 5 else "warning"

                return ResearchTrigger(
                    symbol=symbol,
                    name=name,
                    trigger_type="volume",
                    severity=severity,
                    description=f"成交量異常：Z-score {z_score:.2f}，量比 {volume_ratio:.2f}x",
                    metrics={
                        "z_score": round(z_score, 2),
                        "volume_ratio": round(volume_ratio, 2),
                        "volume": int(volume),
                        "volume_ma20": int(volume_ma20),
                    },
                    suggested_topic=f"{format_name_with_code(name, symbol)}成交量異常 {volume_ratio:.1f}x 原因分析",
                    timestamp=datetime.now().isoformat(),
                )
        except Exception as e:
            logger.debug(f"成交量異常偵測失敗 {symbol}: {e}")

        return None

    def detect_institution_accumulation(
        self,
        symbol: str,
        name: str,
        inst_data: List[Dict],  # FinMind institutional data
    ) -> Optional[ResearchTrigger]:
        """
        偵測法人連續買賣

        條件：
        - 連續 3 日同向買賣超
        - 累積買賣超 > 1000 張
        """
        if not inst_data or len(inst_data) < 3:
            return None

        try:
            # 合併外資、投信、自營商
            daily_net = {}
            for record in inst_data[-3:]:  # 最近 3 天
                date = record.get("date")
                if date not in daily_net:
                    daily_net[date] = 0
                net = (record.get("buy", 0) - record.get("sell", 0)) / 1000  # 轉張
                daily_net[date] += net

            if len(daily_net) < 3:
                return None

            nets = list(daily_net.values())
            total_net = sum(nets)

            # 檢查是否連續同向
            all_positive = all(n > 0 for n in nets)
            all_negative = all(n < 0 for n in nets)

            if (all_positive or all_negative) and abs(total_net) >= self.thresholds["inst_net_threshold"]:
                direction = "買超" if all_positive else "賣超"
                severity = "critical" if abs(total_net) >= 3000 else "warning"

                return ResearchTrigger(
                    symbol=symbol,
                    name=name,
                    trigger_type="institution",
                    severity=severity,
                    description=f"法人連續{self.thresholds['inst_consecutive_days']}日{direction}，累積 {total_net:+.0f} 張",
                    metrics={
                        "consecutive_days": len(nets),
                        "total_net": round(total_net, 0),
                        "daily_nets": [round(n, 0) for n in nets],
                        "direction": direction,
                    },
                    suggested_topic=f"{format_name_with_code(name, symbol)}法人連續{direction}原因分析",
                    timestamp=datetime.now().isoformat(),
                )
        except Exception as e:
            logger.debug(f"法人偵測失敗 {symbol}: {e}")

        return None

    def detect_technical_break(
        self,
        symbol: str,
        name: str,
        df: pd.DataFrame,
    ) -> Optional[ResearchTrigger]:
        """
        偵測技術指標突破

        條件：
        - RSI > 75（超買）或 RSI < 25（超賣）
        - MACD 交叉
        """
        if len(df) < 26:
            return None

        try:
            close = df["close"]

            # 計算 RSI
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            current_rsi = rsi.iloc[-1]

            triggers = []

            # RSI 超買超賣
            if current_rsi >= self.thresholds["rsi_overbought"]:
                triggers.append(f"RSI 超買 ({current_rsi:.1f})")
            elif current_rsi <= self.thresholds["rsi_oversold"]:
                triggers.append(f"RSI 超賣 ({current_rsi:.1f})")

            # MACD 交叉
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd = ema12 - ema26
            signal = macd.ewm(span=9, adjust=False).mean()

            if len(macd) >= 2:
                if macd.iloc[-2] <= signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]:
                    triggers.append("MACD 金叉")
                elif macd.iloc[-2] >= signal.iloc[-2] and macd.iloc[-1] < signal.iloc[-1]:
                    triggers.append("MACD 死叉")

            if triggers:
                return ResearchTrigger(
                    symbol=symbol,
                    name=name,
                    trigger_type="technical",
                    severity="warning",
                    description=f"技術指標訊號：{' + '.join(triggers)}",
                    metrics={
                        "rsi": round(current_rsi, 1),
                        "macd": round(macd.iloc[-1], 2),
                        "macd_signal": round(signal.iloc[-1], 2),
                        "triggers": triggers,
                    },
                    suggested_topic=f"{name}（{symbol}）技術指標突破分析",
                    timestamp=datetime.now().isoformat(),
                )

        except Exception as e:
            logger.debug(f"技術指標偵測失敗 {symbol}: {e}")

        return None

    def detect_news_sentiment_shift(
        self,
        symbol: str,
        name: str,
        news_items: List[str],
    ) -> Optional[ResearchTrigger]:
        """
        偵測新聞情緒轉向

        條件：
        - 今日新聞出現明顯正負面轉折
        - 或出現關鍵詞（重大利好/利空）

        簡化版：檢查關鍵詞
        """
        if not news_items:
            return None

        positive_keywords = ["大漲", "利多", "突破", "看好", "買超", "升評", "目標價"]
        negative_keywords = ["大跌", "利空", "跌破", "看壞", "賣超", "降評", "裁員", "虧損"]

        today_text = " ".join(news_items)

        pos_count = sum(1 for kw in positive_keywords if kw in today_text)
        neg_count = sum(1 for kw in negative_keywords if kw in today_text)

        if pos_count >= 3 or neg_count >= 3:
            sentiment = "正面" if pos_count > neg_count else "負面"
            return ResearchTrigger(
                symbol=symbol,
                name=name,
                trigger_type="sentiment",
                severity="warning",
                description=f"新聞情緒偏向{sentiment}（正:{pos_count} / 負:{neg_count}）",
                metrics={
                    "positive_keywords": pos_count,
                    "negative_keywords": neg_count,
                    "sentiment": sentiment,
                },
                suggested_topic=f"{name}（{symbol}）新聞情緒{sentiment}轉向分析",
                timestamp=datetime.now().isoformat(),
            )

        return None

    def scan_all_triggers(
        self,
        quotes: List[Dict],
        inst_data: Dict[str, List[Dict]],
        price_data: Dict[str, pd.DataFrame],
    ) -> List[ResearchTrigger]:
        """
        掃描所有觸發條件

        Returns:
            List[ResearchTrigger] 按嚴重程度排序
        """
        triggers: List[ResearchTrigger] = []

        for q in quotes:
            symbol = q.get("symbol")
            name = q.get("name")

            if not symbol or not name:
                continue

            df = price_data.get(symbol)
            if df is not None:
                # 成交量異常
                t = self.detect_volume_anomaly(symbol, name, df, q.get("price", 0))
                if t:
                    triggers.append(t)

                # 技術指標
                t = self.detect_technical_break(symbol, name, df)
                if t:
                    triggers.append(t)

            # 法人連續買賣
            inst = inst_data.get(symbol)
            if inst:
                t = self.detect_institution_accumulation(symbol, name, inst)
                if t:
                    triggers.append(t)

        # 按嚴重程度排序
        severity_order = {"critical": 0, "warning": 1, "info": 2}
        triggers.sort(key=lambda x: severity_order.get(x.severity, 99))

        return triggers

    def format_triggers_for_report(self, triggers: List[ResearchTrigger]) -> str:
        """格式化觸發事件供報告使用"""
        if not triggers:
            return ""

        lines = [f"\n🚨 【自動偵測異常】共 {len(triggers)} 件"]

        type_icons = {
            "volume": "📊",
            "institution": "🏦",
            "technical": "📈",
            "sentiment": "📰",
        }

        for t in triggers[:5]:  # 只顯示前 5
            icon = type_icons.get(t.trigger_type, "⚠️")
            lines.append(
                f"  {icon} {t.name}（{t.symbol}）| {t.severity.upper()} | {t.description}"
            )

        return "\n".join(lines)


# 全局實例
_trigger_detector: Optional[ResearchTriggerDetector] = None


def get_trigger_detector(base_dir: Optional[Path] = None) -> ResearchTriggerDetector:
    """取得 ResearchTriggerDetector 實例"""
    global _trigger_detector
    if _trigger_detector is None:
        _trigger_detector = ResearchTriggerDetector(base_dir)
    return _trigger_detector
