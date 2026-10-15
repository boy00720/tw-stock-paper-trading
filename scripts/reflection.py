#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
反思 W — 每週深度反思推送
每週日 21:30 自動執行，推送本週反思模板 + 靈魂提問到飛書
"""

import os
import sys

# 清除代理變數，避免 403 CONNECT tunnel failed
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import logging
import random
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from utils.claude_helper import ClaudeHelper
from utils.lark_notifier import LarkNotifier
from utils.master_agent_control import get_current_control, write_payload

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOGS_DIR / 'reflection.log'),
        logging.StreamHandler()
    ]
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


def _extract_last_route_summary(exc: Exception) -> str:
    text = str(exc)
    return next((line.strip() for line in text.splitlines() if "label=" in line and "base_host=" in line), "")


def _log_reflection_fallback(from_model: str, to_model: str, exc: Exception) -> None:
    route_summary = _extract_last_route_summary(exc)
    if route_summary:
        logger.warning(
            "Reflection Claude fallback from=%s to=%s route=%s error=%s",
            from_model,
            to_model,
            route_summary,
            exc,
        )
        return
    logger.warning(
        "Reflection Claude fallback from=%s to=%s error=%s",
        from_model,
        to_model,
        exc,
    )


def _generate_focus_with_claude(prompt: str) -> str:
    helper = ClaudeHelper(max_retries=0)
    log_claude_route_plan("Reflection", helper)
    text = helper.chat(
        prompt=prompt,
        max_tokens=600,
        on_fallback=_log_reflection_fallback,
    )
    logger.info("Reflection Claude call completed")
    return text


SOUL_QUESTIONS = [
    "這週你做了什麼，是 6 個月後的自己會感謝你的？",
    "你今天的行動，和你真正想要的生活，有多大距離？",
    "如果你只剩三個月，你還會在乎這週讓你焦慮的事嗎？",
    "你這週逃避了什麼？那個東西在等你去面對。",
    "你最近一次全心投入一件事是什麼時候？",
    "你給別人的建議，有沒有也對自己說過？",
    "你有沒有在用忙碌掩蓋某種空洞？",
    "如果你的行動被公開播出，你會為自己感到驕傲嗎？",
    "這週你有沒有做一件讓自己害怕的事？",
    "五年後的你，會希望現在的你做什麼決定？",
    "你最後一次真正休息是什麼時候？那是什麼感覺？",
    "你有沒有在等某個「對的時機」？那個時機真的會來嗎？",
]

# ── 毒雞湯庫 ─────────────────────────────────────────
BITTER_QUOTES = [
    "努力不一定成功，但不努力一定很舒服。",
    "堅持夢想很重要，但夢想不付房租。",
    "人生最大的敵人，是昨晚說要早睡的那個自己。",
    "成功的人都說要堅持，放棄的人也說自己堅持過。",
    "自律給你自由——前提是你先撐過不自律的那段痛苦。",
    "每天進步 1%，一年後進步 37 倍。但你得先活過今天。",
    "不是沒有時間，是你選擇了把時間花在別的地方。",
    "想改變卻不行動，叫做幻想。想改變並且行動，叫做成長。",
    "你沒有失敗，你只是還沒完成。（但要小心這不是在合理化拖延）",
]

# ── 週反思維度 ────────────────────────────────────────
REFLECTION_DIMENSIONS = [
    ("🎯 目標與執行", [
        "這週你最接近自己目標的一個行動是什麼？",
        "哪件事你一直想做，卻還在拖？",
        "下週最重要的一個目標是什麼？",
    ]),
    ("🧠 情緒與能量", [
        "這週哪個時刻你最有能量？哪個時刻最耗損？",
        "有哪個情緒反覆出現？它在提醒你什麼？",
        "你有沒有好好照顧自己的身體與睡眠？",
    ]),
    ("💰 工作與財務", [
        "這週最有產出的工作是什麼？",
        "有哪個決策其實可以更果斷？",
        "下週最值得投入資源的事情是什麼？",
    ]),
    ("📈 投資與市場", [
        "這週市場給你最大的提醒是什麼？",
        "你的投資決策是依照紀律，還是被情緒推著走？",
        "下週最需要追蹤的市場變化是什麼？",
    ]),
    ("🤝 人際關係", [
        "這週最有意義的一次對話是什麼？",
        "有沒有一句話說出口後後悔了？",
        "有沒有想感謝某人，但還沒說出口？",
    ]),
    ("📚 學習與成長", [
        "這週學到最意外的一件事是什麼？",
        "有沒有改變了某個舊有的想法？",
        "下週想深入學習的一個主題是什麼？",
    ]),
    ("🔮 下週意圖", [
        "下週你最重要的一件事是什麼？",
        "有什麼事需要提前準備或安排？",
        "你想在下週培養的一個習慣或狀態？",
    ]),
]


def load_weekly_content(now: datetime) -> dict:
    """讀取本週所有 Agent 的輸出內容"""
    content = {"journals": [], "news_files": [], "stock_alerts": ""}

    # 本週日誌
    for i in range(7):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        p = Path("logs/daily_reports") / f"{date}.md"
        if p.exists():
            content["journals"].append(p.read_text(encoding="utf-8")[:800])

    # 本週新聞（取最近 3 天）
    for i in range(3):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        p = Path("logs/daily_news") / f"{date}.md"
        if p.exists():
            content["news_files"].append(p.read_text(encoding="utf-8")[:400])

    # 本週股市警報
    alerts_file = Path("logs/stock_alerts_today.json")
    if alerts_file.exists():
        try:
            import json
            data = json.loads(alerts_file.read_text(encoding="utf-8"))
            alerts = data.get("alerts", [])
            if alerts:
                lines = [f"- {format_name_with_code(a['name'], a['symbol'])}{a['change_pct']:+.1f}% [{a['alert']}]" for a in alerts]
                content["stock_alerts"] = "\n".join(lines)
        except Exception:
            pass

    return content


def generate_focus_with_claude(now: datetime, weekly_content: dict) -> str:
    """用 Claude 分析本週內容，生成下週關注清單"""
    try:
        journals_text = "\n---\n".join(weekly_content["journals"]) or "（本週無日誌）"
        news_text = "\n---\n".join(weekly_content["news_files"]) or "（無新聞記錄）"
        alerts_text = weekly_content["stock_alerts"] or "（本週無異常警報）"

        prompt = f"""你是反思 W，W 的週反思助理。請分析以下本週資料，生成下週關注清單。

【本週日誌摘要】
{journals_text[:1500]}

【本週市場新聞】
{news_text[:800]}

【本週股市異常警報】
{alerts_text}

請輸出以下格式（繁體中文，簡潔有力）：

## 📋 下週關注清單
### 🎯 個人行動
- （根據日誌，下週最應該優先執行的 2-3 件事）

### 📈 市場觀察
- （根據本週警報和新聞，下週需持續追蹤的 2-3 個市場動態）

### 💡 深度思考
- （一個值得下週深入思考的問題）

---
直接輸出 Markdown，不要開場白。"""

        focus = _generate_focus_with_claude(prompt)
        return focus
    except Exception as e:
        logger.warning(f"Claude 生成下週關注清單失敗: {e}")
        return "## 📋 下週關注清單\n（生成失敗，請手動填寫）"


def save_next_week_focus(focus: str, now: datetime) -> str:
    """將下週關注清單寫入 logs/next_week_focus.md，供下週其他 Agent 讀取"""
    filepath = Path("logs/next_week_focus.md")
    week_num = now.isocalendar()[1]
    header = f"# 第 {week_num + 1} 週關注清單｜由反思 W 生成於 {now.strftime('%Y-%m-%d')}\n\n"
    filepath.write_text(header + focus, encoding="utf-8")
    logger.info(f"下週關注清單已寫入 {filepath}")
    return str(filepath)


def build_reflection(now: datetime, focus: str = "") -> str:
    """組合週反思內容"""
    week_num = now.isocalendar()[1]
    week_start = (now - timedelta(days=now.weekday())).strftime("%m/%d")
    week_end = now.strftime("%m/%d")

    soul_q = random.choice(SOUL_QUESTIONS)
    bitter = random.choice(BITTER_QUOTES)

    # 讀取本週日誌日期清單
    diary_dates = []
    for i in range(7):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        if (Path("logs/daily_reports") / f"{date}.md").exists():
            diary_dates.append(date)
    diary_note = (
        f"本週已記錄 {len(diary_dates)} 天日誌（{', '.join(diary_dates[:3])}{'...' if len(diary_dates) > 3 else ''}）"
        if diary_dates
        else "本週尚無日誌記錄，記得補齊 📝"
    )

    lines = [
        f"🪞 反思 W · 第 {week_num} 週深度反思",
        f"📅 {week_start} ~ {week_end}",
        "",
        "════════════════════",
        "❓ 本週靈魂提問",
        "════════════════════",
        f"「{soul_q}」",
        "",
        "（花 5 分鐘，認真回答這個問題）",
        "",
    ]

    # 各維度反思
    lines += [
        "════════════════════",
        "🔍 週回顧六維度",
        "════════════════════",
    ]

    for title, questions in REFLECTION_DIMENSIONS:
        lines.append(f"\n{title}")
        for q in questions:
            lines.append(f"  · {q}")
            lines.append(f"    → ")

    # 本週日誌概況
    lines += [
        "",
        "════════════════════",
        "📓 本週日誌概況",
        "════════════════════",
        f"  {diary_note}",
        "",
    ]

    # 毒雞湯
    lines += [
        "════════════════════",
        "☕ 本週毒雞湯",
        "════════════════════",
        f"「{bitter}」",
        "",
        "════════════════════",
        "📌 三個本週總結句",
        "════════════════════",
        "  1. 這週我做到了：",
        "  2. 這週我沒做到：",
        "  3. 下週我承諾自己：",
        "",
    ]

    # 下週關注清單（由 Claude 生成）
    if focus:
        lines += [
            "════════════════════",
            focus,
            "════════════════════",
            "",
        ]

    lines += [
        "─────────────────────",
        "由 反思 W 每週日陪你沉澱 🌙",
    ]

    return "\n".join(lines)


def archive_reflection(content: str, now: datetime) -> str:
    """存檔反思到本地"""
    archive_dir = Path("logs/reflections")
    archive_dir.mkdir(parents=True, exist_ok=True)
    week_num = now.isocalendar()[1]
    filename = archive_dir / f"{now.year}-W{week_num:02d}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    return str(filename)


def main():
    now = datetime.now()
    notifier = LarkNotifier("config/lark_config.yaml")

    logger.info(f"Generating reflection for week {now.isocalendar()[1]}")

    # 1. 讀取本週所有 Agent 輸出
    weekly_content = load_weekly_content(now)
    logger.info(f"已讀取本週資料：{len(weekly_content['journals'])} 篇日誌、{len(weekly_content['news_files'])} 份新聞")

    # 2. 用 Claude 生成下週關注清單
    focus = generate_focus_with_claude(now, weekly_content)

    # 3. 存檔下週關注清單（供下週 Agent 讀取）
    focus_file = save_next_week_focus(focus, now)
    logger.info(f"下週關注清單已寫入 {focus_file}")

    # 4. 組合完整反思（含 focus）
    content = build_reflection(now, focus=focus)

    # 5. 存檔反思
    filepath = archive_reflection(content, now)
    logger.info(f"Archived to {filepath}")

    master_control = get_current_control()
    if master_control is not None:
        payload_path = write_payload(
            {
                "agent": "reflection",
                "artifacts": {
                    "reflection_file": filepath,
                    "next_week_focus_file": focus_file,
                },
                "notification": {
                    "feishu": {
                        "title": "🪞 反思 W 週回顧",
                        "content": content,
                    },
                    "wechat": {
                        "text": content,
                        "typing_seconds": 3,
                    },
                },
            },
            control=master_control,
        )
        logger.info(
            "Master agent 接管通知：已輸出 payload=%s，略過反思 W 直接推播",
            payload_path,
        )
        sys.exit(0)

    # 推送飛書
    chat_id = "oc_your_feishu_chat_id"
    success = notifier.send_message(chat_id, "🪞 反思 W 週回顧", content)

    if success:
        logger.info("Reflection sent to Feishu successfully")
    else:
        logger.error("Failed to send reflection")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
