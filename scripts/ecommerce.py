#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨境電商 W — 每週市場研究週報
每週一 09:00 自動執行，推送本週電商情報到飛書
"""

import os
import sys

# 清除代理變數，避免 403 CONNECT tunnel failed
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Dict
from xml.etree import ElementTree as ET

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.claude_helper import ClaudeHelper
from utils.lark_notifier import LarkNotifier
from utils.master_agent_control import get_current_control, write_payload

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/ecommerce.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
MAX_PER_SOURCE = 5

# 已驗證可用的跨境電商 RSS 來源
RSS_SOURCES = [
    {
        "name": "億恩網（中文電商）",
        "url": "https://www.ennews.com/rss.xml",
        "lang": "zh",
        "focus": "亞馬遜/速賣通 中文實戰資訊",
    },
    {
        "name": "Google 新聞 跨境電商",
        "url": "https://news.google.com/rss/search?q=%E8%B7%A8%E5%A2%83%E9%9B%BB%E5%95%86&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "lang": "zh",
        "focus": "台灣跨境電商最新動態",
    },
    {
        "name": "Jungle Scout",
        "url": "https://www.junglescout.com/blog/feed/",
        "lang": "en",
        "focus": "Amazon 選品與市場趨勢",
    },
    {
        "name": "Practical Ecommerce",
        "url": "https://www.practicalecommerce.com/feed",
        "lang": "en",
        "focus": "獨立站 / Shopify 操作技巧",
    },
    {
        "name": "Google News Amazon Seller",
        "url": "https://news.google.com/rss/search?q=amazon+seller+tips&hl=en&gl=US&ceid=US:en",
        "lang": "en",
        "focus": "Amazon 賣家政策與最新消息",
    },
]

# 每週新手操盤固定檢查清單（市場研究階段）
WEEKLY_CHECKLIST = [
    "📦 選品調研：在 Amazon 搜尋 3 個感興趣的品類，記錄 BSR 前 10 名",
    "💰 成本試算：計算 1 個潛在商品的頭程 + FBA 費用 + 利潤空間",
    "🔍 競品分析：查看目標品類 Top 賣家的評論數、定價策略",
    "📊 趨勢確認：用 Google Trends 確認品類是否有季節性需求",
    "🏭 供應鏈：在 1688 / 阿里巴巴找 3 家同類供應商，比較報價",
    "📝 記錄本週研究結論：哪個品類值得繼續深挖？原因是什麼？",
]


def fetch_rss(source: dict) -> List[Dict]:
    """抓取單一 RSS 來源"""
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = root.findall(".//item")

        results = []
        for item in items[:MAX_PER_SOURCE]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if not title:
                continue
            summary = desc[:150] + "..." if len(desc) > 150 else desc
            results.append({
                "title": title,
                "url": link,
                "summary": summary,
                "source": source["name"],
                "focus": source["focus"],
                "lang": source["lang"],
            })
        return results
    except Exception as e:
        logger.error(f"[{source['name']}] 抓取失敗: {e}")
        return []


def fetch_all_news() -> Dict[str, List[Dict]]:
    """分語言抓取所有來源新聞"""
    zh_news, en_news = [], []

    for source in RSS_SOURCES:
        items = fetch_rss(source)
        logger.info(f"[{source['name']}] 抓取 {len(items)} 則")
        if source["lang"] == "zh":
            zh_news.extend(items)
        else:
            en_news.extend(items)

    return {"zh": zh_news, "en": en_news}



def generate_ecommerce_analysis(news: Dict[str, List[Dict]], now: datetime) -> str:
    """用 Claude 消化中立資訊，產出精華分析"""
    try:
        # 整理餵給 Claude 的素材
        materials = []
        for lang, items in news.items():
            for item in items[:10]:  # 每種語言最多取前10則防爆
                materials.append(f"【{item['source']}】{item['title']}\n摘要: {item['summary']}")
                
        material_str = "\n---\n".join(materials)
        
        prompt = f"""你是一位擁有 8 年經驗的亞馬遜/速賣通跨境電商資深操盤手。
目前是 {now.strftime('%Y-%m-%d')}，請閱讀以下最新抓取到的跨境電商產業新聞（包含中英文來源）：

{material_str}

請為 W（一位準備入局的跨境電商探索者）撰寫一份精煉的【本週跨境電商市場週報】。
不需要長篇大論，請保持繁體中文，語氣實戰、接地氣且一針見血。

輸出格式如下：
# 🛒 本週跨境趨勢洞察
（基於上述新聞，總結出 2-3 個最值得關注的市場趨勢或平台政策變化，用點列式摘要）

# 💡 本週選品靈感或避坑指南
（結合新聞內容或你作為資深賣家的經驗，給出 1-2 個近期的選品方向建議，或是操作上的避坑提醒）

# ✅ 本週新手推進建議
（給出 3 個具體可以落地的行動步驟，如：用xx工具搜集xx資料、計算特定的成本結構等）

直接輸出 Markdown 格式，不要廢話。"""

        logger.info("呼叫 Claude 進行電商週報分析...")
        helper = ClaudeHelper(max_retries=0)
        analysis = helper.chat(
            prompt=prompt,
            max_tokens=1000,
        )
        logger.info("Ecommerce Claude call completed")
        return analysis
    except Exception as e:
        logger.error(f"Claude 分析電商情報失敗: {e}")
        return ""


def build_weekly_brief(news: Dict[str, List[Dict]], now: datetime) -> str:
    """組合每週電商週報"""
    week_num = now.isocalendar()[1]
    date_str = now.strftime("%Y-%m-%d")
    
    analysis = generate_ecommerce_analysis(news, now)
    
    if analysis:
        lines = [
            f"🛒 跨境電商 W · 第 {week_num} 週情報",
            f"📅 {date_str}",
            "",
            analysis,
            "",
            "─────────────────────",
            "📌 實操乾貨 · 低成本起號 · 不走彎路",
            "由 跨境電商 W 結合 AI 深度消化後發送",
        ]
        return "\n".join(lines)
    else:
        # Fallback 到簡單列表
        lines = [f"🛒 跨境電商 W · 第 {week_num} 週情報（AI 輔助失敗，顯示原始資料）\n"]
        for item in news["zh"][:5] + news["en"][:5]:
            lines.append(f"▸ 【{item['source']}】{item['title']}")
        return "\n".join(lines)

def send_to_lark(notifier: LarkNotifier, content: str) -> bool:
    """發送到飛書"""
    chat_id = "oc_your_feishu_chat_id"
    return notifier.send_message(chat_id, "🛒 跨境電商 W 週報", content)


def archive_brief(content: str, now: datetime) -> str:
    archive_dir = Path("logs/ecommerce_reports")
    archive_dir.mkdir(parents=True, exist_ok=True)
    filename = archive_dir / f"{now.strftime('%Y')}-W{now.isocalendar()[1]:02d}.md"
    filename.write_text(content, encoding="utf-8")
    logger.info(f"跨境電商週報已存檔：{filename}")
    return str(filename)


def main():
    now = datetime.now()
    notifier = LarkNotifier("config/lark_config.yaml")

    logger.info(f"Starting ecommerce weekly brief for week {now.isocalendar()[1]}")

    news = fetch_all_news()
    total = len(news["zh"]) + len(news["en"])
    logger.info(f"Total fetched: {total} articles (zh={len(news['zh'])}, en={len(news['en'])})")

    brief = build_weekly_brief(news, now)
    archive_path = archive_brief(brief, now)
    master_control = get_current_control()

    if master_control is not None:
        payload_path = write_payload(
            {
                "agent": "ecommerce",
                "meta": {
                    "zh_count": len(news["zh"]),
                    "en_count": len(news["en"]),
                    "total_count": total,
                },
                "artifacts": {
                    "report_file": archive_path,
                },
                "notification": {
                    "feishu": {
                        "title": "🛒 跨境電商 W 週報",
                        "content": brief,
                    },
                    "wechat": {
                        "text": brief,
                        "typing_seconds": 3,
                    },
                },
            },
            control=master_control,
        )
        logger.info(
            "Master agent 接管通知：已輸出 payload=%s，略過跨境電商 W 直接推播",
            payload_path,
        )
        sys.exit(0)

    logger.info("Weekly brief built, sending to Feishu...")

    success = send_to_lark(notifier, brief)
    if success:
        logger.info("Ecommerce weekly brief sent successfully")
    else:
        logger.error("Failed to send ecommerce brief")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
