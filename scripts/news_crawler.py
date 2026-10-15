#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞資訊爬蟲與自動推送
"""

import os
import sys

# 清除代理變數，避免 403 CONNECT tunnel failed
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

# 添加項目根目錄到 Python 路徑
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.claude_helper import ClaudeHelper
from utils.config_loader import load_config
from utils.news_fetcher import NewsFetcher
from utils.news_processor import NewsProcessor
from utils.lark_notifier import LarkNotifier
from utils.master_agent_control import get_current_control, write_payload

_BASE_DIR = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(_BASE_DIR / 'logs' / 'news_crawler.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class NewsCrawler:
    """新聞爬蟲主類"""

    def __init__(self, config_path: str = "config/news_sources.yaml"):
        self.config = load_config(config_path)
        self.fetcher = NewsFetcher(self.config)
        self.processor = NewsProcessor(self.config)
        self.notifier = LarkNotifier("config/lark_config.yaml")

        os.environ['TZ'] = self.config.get('output', {}).get('timezone', 'Asia/Taipei')
        logger.info(f" News Crawler initialized with timezone: {os.environ['TZ']}")

    def run(self) -> dict:
        """執行爬蟲流程，回傳結構化結果供 Orchestrator 使用"""
        master_control = get_current_control()
        try:
            logger.info("Starting news crawling process")

            logger.info("Fetching news from sources...")
            raw_news = self.fetcher.fetch_all_news()
            if not raw_news:
                logger.warning("No news fetched, skipping processing")
                return {"success": False, "error": "無法抓取新聞"}

            logger.info(f"Fetched {len(raw_news)} raw news items")

            logger.info("Processing news data...")
            processed_news = self.processor.process_news(raw_news)
            logger.info(f"Processed {len(processed_news)} news items")

            # 分類新聞
            categorized_news = self._categorize_news(processed_news)

            # 步驟 3: 保存新聞供其他 Agent 聯動
            logger.info("Saving daily news...")
            news_file = self._save_daily_news(categorized_news)

            # 步驟 3.5: 抽取風險雷達供 stock_screener 使用
            logger.info("Extracting risk radar...")
            risk_radar = self._extract_risk_radar(categorized_news)

            # 步驟 4: 主控模式下改為輸出 payload，由 orchestrator 統一 dispatch
            if master_control is not None:
                logger.info("Preparing news payloads for master agent...")
                payload_steps = self._emit_master_payloads(categorized_news, news_file, master_control.step_name)
            else:
                logger.info("Preparing news notifications...")
                self._send_notifications(categorized_news)
                payload_steps = []

            economy_count = len(categorized_news.get("economy", []))
            logger.info("News crawling completed successfully")
            return {
                "success": True,
                "news_file": news_file,
                "total": len(processed_news),
                "economy_count": economy_count,
                "payload_steps": payload_steps,
            }

        except Exception as e:
            logger.error(f"News crawling failed: {str(e)}", exc_info=True)
            if master_control is None:
                self.notifier.send_error_notification("News Crawler Error", str(e))
            return {"success": False, "error": str(e)}

    def _save_daily_news(self, categorized_news: Dict[str, List[Dict]]) -> str:
        """保存今日新聞到 logs/daily_news/YYYY-MM-DD.md (供股市 W 聯動使用)，回傳檔案路徑"""
        log_dir = "logs/daily_news"
        os.makedirs(log_dir, exist_ok=True)

        today = datetime.now().strftime("%Y-%m-%d")
        filepath = os.path.join(log_dir, f"{today}.md")

        lines = [f"# {today} 金融與市場新聞摘要\n"]

        if 'economy' in categorized_news and categorized_news['economy']:
            for i, news in enumerate(categorized_news['economy'], 1):
                lines.append(f"## {i}. {news.get('title', '')}")
                lines.append(f"**來源**: {news.get('source', '')}")
                lines.append(f"**摘要**: {news.get('summary', '')}\n")

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
        logger.info(f"Daily news saved to {filepath}")
        return filepath

    def _extract_risk_radar(self, categorized_news: Dict[str, List[Dict]]) -> Dict[str, Any]:
        """用 Claude 從當日新聞抽取結構化風險訊號，存入 logs/risk_radar.json"""
        economy_news = categorized_news.get("economy", [])
        if not economy_news:
            logger.info("無財經新聞，跳過風險雷達抽取")
            return {}

        # 組合新聞摘要（限制長度）
        news_text = ""
        for i, n in enumerate(economy_news[:20], 1):
            title = n.get("title", "")
            summary = n.get("summary", "")[:200]
            news_text += f"{i}. {title}\n   {summary}\n\n"

        # 載入風險關鍵字（若存在）
        keywords_file = _BASE_DIR / "config" / "risk_keywords.yaml"
        keywords_hint = ""
        if keywords_file.exists():
            try:
                import yaml
                kw_cfg = yaml.safe_load(keywords_file.read_text(encoding="utf-8"))
                intl = kw_cfg.get("international", {})
                indiv = kw_cfg.get("individual", {})
                keywords_hint = f"""
參考關鍵字（國際）：{', '.join(sum(intl.values(), []))}
參考關鍵字（個股）：{', '.join(sum(indiv.values(), []))}
"""
            except Exception:
                pass

        prompt = f"""你是財經風險分析師。以下是今日新聞摘要，請抽取結構化風險訊號。
{keywords_hint}
【新聞】
{news_text}

請以 JSON 格式回傳（只輸出 JSON，不要其他文字）：
{{
  "international": ["關稅"|"地緣"|"Fed"|...],  // 命中的國際風險類型
  "sector_risks": {{"半導體": "利空描述", ...}},  // 受影響類股
  "blacklist": ["2317.TW", ...],  // 有明確重大利空的個股代碼
  "sentiment_hint": "fear"|"neutral"|"greed",  // 整體情緒傾向
  "summary": "一句話總結今日風險"
}}
"""
        try:
            claude = ClaudeHelper()
            raw = claude.chat(prompt=prompt, max_tokens=1000)

            # 嘗試解析 JSON（可能包含 markdown code block）
            json_match = re.search(r'\{[\s\S]*\}', raw)
            if json_match:
                radar = json.loads(json_match.group())
            else:
                radar = {"international": [], "sector_risks": {}, "blacklist": [], "sentiment_hint": "neutral", "summary": "無法解析"}

            # 儲存
            radar_file = _BASE_DIR / "logs" / "risk_radar.json"
            radar_file.write_text(json.dumps(radar, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("風險雷達已存至 %s: %s", radar_file, radar.get("summary", ""))
            return radar

        except Exception as e:
            logger.warning("風險雷達抽取失敗: %s", e)
            return {}

    def _send_notifications(self, categorized_news: Dict[str, List[Dict]]) -> None:
        """發送新聞通知"""
        for category, news_list, start, end, message, sequence_index, sequence_total in self._iter_notification_batches(categorized_news):
            try:
                self.notifier.send_news_notification(
                    category=f"{category} {start}-{end}",
                    message=message,
                    news_count=len(news_list)
                )

                # 同步推送到微信
                from utils.wechat_notifier import WechatNotifier
                wx_notifier = WechatNotifier()
                wx_notifier.send_message(
                    self._build_wechat_text(category, start, end, len(news_list), message, sequence_index, sequence_total),
                    metadata={
                        "category": category,
                        "sequence_index": sequence_index,
                        "sequence_total": sequence_total,
                        "range_start": start,
                        "range_end": end,
                        "news_count": len(news_list),
                    },
                )
            except Exception as e:
                logger.error(f"Failed to send {category} notification: {str(e)}", exc_info=True)

    @staticmethod
    def _safe_step_component(value: str) -> str:
        return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "")).strip("_") or "news"

    @staticmethod
    def _build_wechat_text(
        category: str,
        start: int,
        end: int,
        total: int,
        message: str,
        sequence_index: int,
        sequence_total: int,
    ) -> str:
        header = f"📰 新聞 W [{sequence_index}/{sequence_total}] {category} {start}-{end}/{total}"
        return f"{header}\n\n{message}".strip()

    def _iter_notification_batches(self, categorized_news: Dict[str, List[Dict]]):
        for category, news_list in categorized_news.items():
            if category != 'economy' or not news_list:
                continue

            batch_size = 10
            sequence_total = max(1, (len(news_list) + batch_size - 1) // batch_size)
            for batch_offset, i in enumerate(range(0, len(news_list), batch_size), start=1):
                batch = news_list[i:i + batch_size]
                start = i + 1
                end = i + len(batch)
                message = self.processor.format_category_news(batch, category, offset=i)
                yield category, news_list, start, end, message, batch_offset, sequence_total

    def _emit_master_payloads(
        self,
        categorized_news: Dict[str, List[Dict]],
        news_file: str,
        base_step_name: str,
    ) -> List[str]:
        payload_steps: List[str] = []

        for category, news_list, start, end, message, sequence_index, sequence_total in self._iter_notification_batches(categorized_news):
            step_name = f"{base_step_name}_{self._safe_step_component(category)}_{start}_{end}"
            payload_path = write_payload(
                {
                    "agent": "news_crawler",
                    "category": category,
                    "meta": {
                        "category": category,
                        "news_count": len(news_list),
                        "range_start": start,
                        "range_end": end,
                        "sequence_index": sequence_index,
                        "sequence_total": sequence_total,
                    },
                    "artifacts": {
                        "news_file": news_file,
                    },
                    "notification": {
                        "feishu": {
                            "title": f"📰 {category} 新聞（{start}-{end}/{len(news_list)}）",
                            "content": message,
                        },
                        "wechat": {
                            "text": self._build_wechat_text(category, start, end, len(news_list), message, sequence_index, sequence_total),
                            "typing_seconds": 6,
                        },
                    },
                },
                step_name=step_name,
            )
            payload_steps.append(step_name)
            logger.info(
                "Master agent 接管通知：已輸出 payload=%s，略過新聞子 agent 直接推播",
                payload_path,
            )

        return payload_steps

    def _categorize_news(self, news_items: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
        """按關鍵字分類新聞"""
        KEYWORDS = {
            'economy': [
                '股市', '股票', '股價', '指數', '漲跌', '大盤', '台積電', '聯發科', '輝達', 'NVIDIA', 'ETF',
                '財經', '通膨', '通貨膨脹', '利率', '升息', '降息', 'Fed', '聯準會', '央行', 'GDP',
                '匯率', '美元', '外匯', '關稅', '貿易戰', '貿易逆差',
                '油價', '原油', '石油', '黃金', '貴金屬', '銅價',
                '比特幣', '加密貨幣', 'Bitcoin', 'crypto',
                'inflation', 'interest rate', 'tariff', 'Federal Reserve', 'earnings', 'GDP',
                'S&P', 'Nasdaq', 'Dow Jones', 'bond yield', 'recession', 'gold price', 'oil price',
            ],
        }

        categorized = {
            'politics': [], 'economy': [], 'technology': [], 'society': [],
        }

        for news in news_items:
            title = news.get('title', '') + ' ' + news.get('summary', '')
            matched = False
            if 'economy' in KEYWORDS:
                if any(kw.lower() in title.lower() for kw in KEYWORDS['economy']):
                    categorized['economy'].append(news)
                    matched = True
            if not matched:
                categorized['society'].append(news)

        return categorized

def main():
    crawler = NewsCrawler()
    result = crawler.run()
    sys.exit(0 if result.get("success") else 1)

if __name__ == "__main__":
    main()
