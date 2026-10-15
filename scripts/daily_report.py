#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_report.py — 日報 W
每日晚間 21:00 自動執行（由 orchestrator.py 呼叫）

流程：
  1. 讀取今日爬取的新聞存檔（logs/daily_news/YYYY-MM-DD.md）
  2. 讀取 W 的晨間回覆（logs/plan_today.md）→ 作為今日計畫
  3. 詢問 W 今日三件事：做了什麼、最大的收穫、明天最重要的事
  4. 生成 Markdown 日誌 → 飛書推送 + 存檔到 logs/daily_reports/
"""

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHAT_ID = os.getenv("FEISHU_DEFAULT_CHAT_ID", "oc_your_feishu_chat_id")

# ── 清除 proxy ──────────────────────────────────────────────────────────────
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

from dotenv import load_dotenv
load_dotenv(".env")

import logging
from utils.claude_helper import ClaudeHelper
from utils.master_agent_control import (
    get_current_control,
    record_interactive_exception,
    write_payload,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/daily_report.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)



def format_name_with_code(name: str, symbol: str) -> str:
    code = symbol.replace('.TW', '').replace('.TWO', '').lstrip('^')
    suffix = f"（{code}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


def log_claude_route_plan(purpose: str, helper: ClaudeHelper) -> None:
    try:
        routes = helper.resolve_routes()
        if not routes:
            logger.warning("%s Claude route plan: no routes resolved", purpose)
            return
        summaries = [helper.route_debug_summary(route) for route in routes]
        logger.info("%s Claude route plan: %s", purpose, " | ".join(summaries))
    except Exception as exc:
        logger.warning("%s Claude route plan unavailable: %s", purpose, exc)


def _safe_str(value) -> str:
    return str(value or "").strip()


def _is_retryable_route_error(error: Exception) -> bool:
    return ClaudeHelper.should_try_route_fallback(error)


def _extract_last_route_summary(exc: Exception) -> str:
    text = str(exc)
    match = next((line.strip() for line in text.splitlines() if "label=" in line and "base_host=" in line), "")
    return match


def _log_daily_report_fallback(from_model: str, to_model: str, exc: Exception) -> None:
    route_summary = _extract_last_route_summary(exc)
    if route_summary:
        logger.warning(
            "Daily report Claude fallback from=%s to=%s route=%s error=%s",
            from_model,
            to_model,
            route_summary,
            exc,
        )
        return
    logger.warning(
        "Daily report Claude fallback from=%s to=%s error=%s",
        from_model,
        to_model,
        exc,
    )


def _generate_journal_with_claude(prompt: str) -> str:
    helper = ClaudeHelper(max_retries=0)
    log_claude_route_plan("Daily report", helper)
    text = helper.chat(
        prompt=prompt,
        max_tokens=1500,
        on_fallback=_log_daily_report_fallback,
    )
    logger.info("Daily report Claude call completed")
    return text


# ─────────────────────────────────────────────────────────────────────────────
# 資料讀取
# ─────────────────────────────────────────────────────────────────────────────


def read_today_news() -> str:
    """讀取今日新聞存檔"""
    today = datetime.now().strftime("%Y-%m-%d")
    path = Path("logs/daily_news") / f"{today}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def read_today_plan() -> str:
    """讀取 W 早晨回覆的今日計畫"""
    path = Path("logs/plan_today.md")
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def read_stock_summary() -> str:
    """讀取今日股市晨報（從 orchestrator 日誌或直接讀 news feed 存檔）"""
    # 從 stock_monitor.log 讀取當天的摘要
    log_path = Path("logs/stock_monitor.log")
    if not log_path.exists():
        return ""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    in_today = False
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if today in line and ("晨報" in line or "brief" in line.lower() or "sent" in line.lower()):
            in_today = True
            lines.append(line.strip())
        elif in_today and line.startswith("20") and today not in line:
            break
    return "\n".join(lines[-5:]) if lines else ""


# ─────────────────────────────────────────────────────────────────────────────
# 詢問 W 三件事
# ─────────────────────────────────────────────────────────────────────────────


def ask_w_reflection() -> dict:
    """互動例外：發飛書訊息問 W 三個問題，回傳（同步模式直接讀取回覆檔）"""
    try:
        from utils.lark_notifier import LarkNotifier
        notifier = LarkNotifier("config/lark_config.yaml")
        title = "📋 日報 W · 今天回顧"
        msg = (
            "📋 日報 W 請你回顧今天（花 2 分鐘簡短回覆）：\n\n"
            "1️⃣ 今天做了什麼最重要的事？\n"
            "回覆：\n\n"
            "2️⃣ 今天最大的收穫或感想？\n"
            "回覆：\n\n"
            "3️⃣ 明天最重要的一件事？\n"
            "回覆：\n\n"
            "_直接回覆在這條訊息底下，我會自動收集整理進日誌_"
        )
        sent = notifier.send_message(CHAT_ID, title, msg)
        record_interactive_exception(
            "ask_w_reflection",
            channel="feishu",
            title=title,
            success=bool(sent),
            details="" if sent else "send_failed",
            metadata={"question_count": 3},
        )
        if sent:
            logger.info("已發送晚間回顧詢問到飛書")
        else:
            logger.warning("晚間回顧詢問未成功送出")
    except Exception as e:
        record_interactive_exception(
            "ask_w_reflection",
            channel="feishu",
            title="📋 日報 W · 今天回顧",
            success=False,
            details=str(e),
            metadata={"phase": "exception"},
        )
        logger.warning(f"發送回顧詢問失敗: {e}")
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# 日誌生成（Claude 輔助）
# ─────────────────────────────────────────────────────────────────────────────


def generate_journal(plan: str, news: str, reflection: dict, alerts: list = None) -> str:
    """用 Claude 整合所有資料生成格式化日誌"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        weekday = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][datetime.now().weekday()]

        alerts_section = ""
        if alerts:
            lines = ["【今日股市異常警報】"]
            for a in alerts:
                sign = "+" if a["change_pct"] > 0 else ""
                lines.append(f"- {format_name_with_code(a['name'], a['symbol'])}：{a['price']}｜{sign}{a['change_pct']}%  [{a['alert']}]")
            alerts_section = "\n".join(lines)

        prompt = f"""你是一位簡潔有力的個人日誌教練。以下是 W 今天的資料：

【早晨計畫】
{plan or "（無）"}

【今日新聞摘要】
{news[:500] if news else "（無）"}

{alerts_section}

【晚間回顧】
1. 今天最重要的事：{reflection.get('q1', '（未回覆）')}
2. 最大收穫或感想：{reflection.get('q2', '（未回覆）')}
3. 明日最重要的事：{reflection.get('q3', '（未回覆）')}

請幫我生成一份 Markdown 格式的個人日誌，格式如下：

# {today} {weekday} 日誌

## 🌅 晨間計畫
（整理成 2~4 點）

## 📰 今日重點新聞
（摘要 2~3 點）

## 💼 今日回顧
### 最重要的事
### 最大收穫

## 🎯 明日重點
（整理成 1~3 點）

## 📊 今日狀態（1-10）
能量：___
專注度：___
身體感覺：___

---
直接輸出 Markdown，不要有任何開場白。"""

        return _generate_journal_with_claude(prompt)
    except Exception as e:
        logger.error(f"Claude 日誌生成失敗: {e}")
        return build_simple_journal(plan, news, reflection)


# ─────────────────────────────────────────────────────────────────────────────
# 備援日誌生成
# ─────────────────────────────────────────────────────────────────────────────


def build_simple_journal(plan: str, news: str, reflection: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    weekday = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][datetime.now().weekday()]
    return f"""# {today} {weekday} 日誌

## 🌅 晨間計畫
{plan or "（未填寫）"}

## 📰 今日重點新聞
{news[:300] if news else "（無新聞資料）"}

## 💼 今日回顧
### 最重要的事
{reflection.get('q1', '（未回覆）')}
### 最大的收穫
{reflection.get('q2', '（未回覆）')}

## 🎯 明日重點
{reflection.get('q3', '（未回覆）')}

## 📊 今日狀態（1-10）
能量：___ / 10
專注度：___ / 10
身體感覺：___

---
由 日報 W 自動生成 {today}
"""


# ─────────────────────────────────────────────────────────────────────────────
# 存檔 + 推送
# ─────────────────────────────────────────────────────────────────────────────

def archive(content: str) -> str:
    """日誌存檔"""
    archive_dir = Path("logs/daily_reports")
    archive_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = archive_dir / f"{today}.md"
    path.write_text(content, encoding="utf-8")
    logger.info(f"日誌已存檔：{path}")
    return str(path)


# ─────────────────────────────────────────────────────────────────────────────
# 主類（供 orchestrator 呼叫）
# ─────────────────────────────────────────────────────────────────────────────

class DailyReporter:
    """日報 W 主類"""

    def run(self, plan_content: str = "", alerts: list = None) -> bool:
        """
        執行完整晚間日誌流程。
        plan_content：由 orchestrator 傳入的 W 晨間回覆（可選）
        alerts：由 orchestrator 傳入的今日股市警報清單（可選）
        """
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        logger.info(f"─── 日報 W 啟動｜{today_str} ───")

        # 1. 讀取資料
        news = read_today_news()
        plan = plan_content or read_today_plan()
        alerts = alerts or []
        logger.info(f"已讀取新聞（{len(news)} 字）、晨間計畫（{len(plan)} 字）、股市警報（{len(alerts)} 筆）")

        # 2. 發送晚間回顧詢問
        ask_w_reflection()

        # 3. 生成日誌（不等待 W 回覆，先產生草稿）
        reflection = {}   # W 若有回覆，日後從 plan_today.md 的回覆 section 讀取
        journal = generate_journal(plan, news, reflection, alerts=alerts)

        # 4. 存檔
        filepath = archive(journal)
        logger.info(f"日誌已存檔：{filepath}")

        master_control = get_current_control()
        if master_control is not None:
            payload_path = write_payload(
                {
                    "agent": "daily_report",
                    "artifacts": {
                        "journal_file": filepath,
                    },
                    "notification": {
                        "feishu": {
                            "title": f"📋 日報 W · {today_str}",
                            "content": journal,
                        },
                        "wechat": {
                            "text": journal,
                            "typing_seconds": 3,
                        },
                    },
                },
                control=master_control,
            )
            logger.info(
                "Master agent 接管通知：已輸出 payload=%s，略過日報 W 直接推播",
                payload_path,
            )
            return True

        # 5. 推送飛書
        try:
            from utils.lark_notifier import LarkNotifier
            notifier = LarkNotifier("config/lark_config.yaml")
            success = notifier.send_message(
                CHAT_ID,
                f"📋 日報 W · {today_str}",
                journal,
            )
            if success:
                logger.info("日誌已推送飛書")
            return success
        except Exception as e:
            logger.error(f"推送飛書失敗: {e}")
            return False


def main():
    reporter = DailyReporter()
    reporter.run()
    sys.exit(0)


if __name__ == "__main__":
    main()
