#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
researcher.py — 深度研究 W (Research W)

責任：
1. 針對異常主題（股市異動、突發新聞）進行深度搜尋
2. 多組關鍵字搜尋 + 全文抓取（requests + BeautifulSoup）
3. 先查記憶庫再搜網，讓報告具有歷史脈絡
4. 用 Claude 生成研究報告（ClaudeHelper，max_tokens=3000）
5. 報告加 frontmatter metadata，方便程式化掃描
"""

import os
import sys

for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import json
import logging
import re
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

from utils.claude_helper import ClaudeHelper
from utils.lark_notifier import LarkNotifier
from utils.master_agent_control import get_current_control, write_payload

# 相容舊 duckduckgo_search 套件
warnings.filterwarnings(
    "ignore",
    message=r".*has been renamed to `ddgs`.*",
    category=RuntimeWarning,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_BASE_DIR / "logs" / "researcher.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 全文抓取
# ──────────────────────────────────────────────────────────────────────

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _fetch_article(url: str, timeout: int = 10, max_chars: int = 2000) -> str:
    """抓取網頁全文（requests + BeautifulSoup），回傳純文字。失敗回空字串。"""
    if not url or url.startswith("（"):
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.debug("bs4 未安裝，跳過全文抓取")
        return ""

    try:
        resp = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除無用元素
        for tag in soup.find_all(["script", "style", "nav", "footer", "aside", "header"]):
            tag.decompose()

        # 依序嘗試主要內容區塊
        content_el = (
            soup.find("article")
            or soup.find("main")
            or soup.find("div", role="main")
            or soup.find("body")
        )
        if not content_el:
            return ""

        text = content_el.get_text(separator="\n", strip=True)
        # 清理多餘空行
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:max_chars]
    except Exception as e:
        logger.debug("全文抓取 %s 失敗: %s", url, e)
        return ""


# ──────────────────────────────────────────────────────────────────────
# 搜尋關鍵字策略
# ──────────────────────────────────────────────────────────────────────


def _normalize_report_kind(report_kind: str) -> str:
    kind = str(report_kind or "anomaly").strip().lower()
    return kind if kind in {"anomaly", "daily_market", "us_overnight"} else "anomaly"


def report_title(report_kind: str) -> str:
    """Research W 不同報告模式的通知標題。"""
    return {
        "daily_market": "🕵️ Research W 每日市場深度研究",
        "us_overnight": "🕵️ Research W 美股收盤研究",
        "anomaly": "🕵️ Research W 異常偵測報告",
    }[_normalize_report_kind(report_kind)]


def _build_search_queries(topic: str, report_kind: str = "anomaly") -> List[str]:
    """根據主題與報告模式產生多組搜尋關鍵字。"""
    now_str = datetime.now().strftime("%Y-%m-%d")
    kind = _normalize_report_kind(report_kind)

    if kind == "daily_market":
        return [
            f"台股 盤後 法人 台積電 0050 00631L 市場分析 {now_str}",
            f"台股 今日 產業 新聞 半導體 AI 伺服器 {now_str}",
            f"Fed 美元 殖利率 原油 黃金 國際局勢 台股 影響 {now_str}",
            f"Nasdaq SOXX Nvidia TSM ADR Taiwan semiconductor market {now_str}",
            f"全球股市 今日 風險 地緣政治 關稅 匯率 {now_str}",
        ]

    if kind == "us_overnight":
        return [
            f"US stock market close Nasdaq S&P 500 SOXX Nvidia TSM ADR {now_str}",
            f"why US stocks moved today Fed Treasury yields dollar oil gold {now_str}",
            f"Nvidia TSM ADR semiconductor stocks market close analysis {now_str}",
            f"US market implications Taiwan stocks TSMC ETF {now_str}",
        ]

    queries = [f"{topic} {now_str}"]

    # 台股個股關鍵字
    if any(c.isdigit() for c in topic) or ".TW" in topic:
        queries.append(f"{topic} 法人 籌碼")
        queries.append(f"{topic} 新聞 分析")
    # 美股 / 原物料
    elif any(w in topic.upper() for w in ["NVDA", "TSM", "GOLD", "OIL", "S&P", "NASDAQ"]):
        queries.append(f"{topic} news analysis")
        queries.append(f"{topic} outlook")
    # 一般主題
    else:
        queries.append(f"{topic} 產業分析")
        queries.append(f"{topic} 台股 影響")

    return queries[:3]


# ──────────────────────────────────────────────────────────────────────
# 記憶查詢
# ──────────────────────────────────────────────────────────────────────


def record_research_health(
    report_kind: str,
    success: bool,
    *,
    duration_sec: float = 0.0,
    report_chars: int = 0,
    error: str = "",
    output_path: str = "",
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> None:
    """每篇 Research W 產出後記一筆健康狀態，供當日彙整（失敗不擋主流程）。"""
    now = now or datetime.now()
    health_path = path or (_BASE_DIR / "logs" / "research_health.jsonl")
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "report_kind": _normalize_report_kind(report_kind),
        "success": bool(success),
        "duration_sec": round(float(duration_sec), 1),
        "report_chars": int(report_chars),
        "error": str(error or "")[:200],
        "output_path": os.path.basename(str(output_path)) if output_path else "",
    }
    try:
        health_path.parent.mkdir(parents=True, exist_ok=True)
        with health_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("寫入 Research W 健康記錄失敗")


def summarize_research_health(
    *,
    date_str: Optional[str] = None,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """彙整指定日期（預設今天）的 Research W 健康狀態。"""
    now = now or datetime.now()
    date_str = date_str or now.strftime("%Y-%m-%d")
    health_path = path or (_BASE_DIR / "logs" / "research_health.jsonl")
    entries: List[Dict[str, Any]] = []
    try:
        for line in health_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("date") == date_str:
                entries.append(obj)
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("讀取 Research W 健康記錄失敗")

    total = len(entries)
    ok = sum(1 for e in entries if e.get("success"))
    by_kind: Dict[str, Dict[str, int]] = {}
    for e in entries:
        kind = str(e.get("report_kind") or "anomaly")
        slot = by_kind.setdefault(kind, {"total": 0, "ok": 0})
        slot["total"] += 1
        if e.get("success"):
            slot["ok"] += 1
    failures = [
        {"report_kind": e.get("report_kind"), "error": e.get("error") or "unknown"}
        for e in entries
        if not e.get("success")
    ]
    return {
        "date": date_str,
        "total": total,
        "ok": ok,
        "failed": total - ok,
        "by_kind": by_kind,
        "failures": failures,
    }


def format_research_health_line(summary: Dict[str, Any]) -> str:
    """把彙整結果壓成一行可讀摘要。"""
    total = int(summary.get("total") or 0)
    if total == 0:
        return f"🩺 Research W 健康（{summary.get('date')}）：今日尚無研究產出記錄。"
    ok = int(summary.get("ok") or 0)
    parts = []
    for kind in ("daily_market", "us_overnight", "anomaly"):
        slot = (summary.get("by_kind") or {}).get(kind)
        if slot and slot.get("total"):
            label = {"daily_market": "每日", "us_overnight": "美股", "anomaly": "異常"}[kind]
            parts.append(f"{label} {slot['ok']}/{slot['total']}")
    head = f"🩺 Research W 健康（{summary.get('date')}）：{ok}/{total} 成功"
    if parts:
        head += "（" + "、".join(parts) + "）"
    fails = summary.get("failures") or []
    if fails:
        head += "；失敗：" + "、".join(
            f"{f.get('report_kind')}={f.get('error')}" for f in fails[:3]
        )
    return head


def _query_memory(topic: str) -> str:
    """從記憶庫查詢歷史相關記錄。"""
    try:
        from scripts.memory_agent import MemoryAgent

        mem = MemoryAgent()
        hits = mem.retrieve(topic, n_results=3, days_back=30)
        if not hits:
            return ""
        logger.info("memory hits: %d", len(hits))
        parts = []
        for h in hits:
            meta = h.get("metadata", {})
            parts.append(
                f"[{meta.get('source', '?')} / {meta.get('date', '?')}]\n{h['content'][:500]}"
            )
        return "\n\n".join(parts)
    except Exception as e:
        logger.debug("記憶庫檢索失敗: %s", e)
        return ""


def build_research_pack_prompt_context(
    symbol: str,
    *,
    market: str = "US",
    market_data_type: str = "delayed",
    client_id: int = 451,
) -> str:
    """建立 Research W 可讀的 Research Pack prompt 區塊。

    目前僅支援美股線，且只做 dry-run / notify=False 類用途；台股線不得經過
    IBKR Research Pack，以免跨市場 provider 混用。
    """
    normalized_market = str(market or "").strip().upper()
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_market != "US":
        raise ValueError("Research Pack dry-run currently supports US market only")
    if not normalized_symbol:
        raise ValueError("research pack symbol is required")

    from utils.research_pack import build_us_research_pack, format_research_pack_for_prompt

    pack = build_us_research_pack(
        normalized_symbol,
        market_data_type=market_data_type,
        client_id=client_id,
    )
    return format_research_pack_for_prompt(pack)


# ──────────────────────────────────────────────────────────────────────
# ResearchAgent
# ──────────────────────────────────────────────────────────────────────


class ResearchAgent:
    def __init__(self) -> None:
        self.tavily_key = os.getenv("TAVILY_API_KEY")
        self.claude = ClaudeHelper()
        self.notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))

    @staticmethod
    def _safe_topic_slug(topic: str) -> str:
        topic = topic.strip() or "topic"
        topic = re.sub(r"\s+", "_", topic)
        topic = re.sub(r"[^\w\-.()\u4e00-\u9fff]", "_", topic)
        topic = re.sub(r"_+", "_", topic).strip("._")
        return topic[:80] or "topic"

    @staticmethod
    def _build_wechat_summary(topic: str, report: str, report_file: str = "", report_kind: str = "anomaly") -> str:
        normalized = re.sub(r"\n{3,}", "\n\n", str(report or "").strip())
        if not normalized:
            normalized = "（研究報告為空）"
        excerpt_limit = 1500
        if len(normalized) > excerpt_limit:
            normalized = normalized[:excerpt_limit].rstrip() + "\n\n……（後略，完整內容已存檔）"
        lines = [report_title(report_kind).replace("報告", ""), f"主題：{topic}", "", normalized]
        if report_file:
            lines.extend(["", f"📁 已存檔：{os.path.basename(report_file)}"])
        return "\n".join(lines).strip()

    # ──────────────────────────────────────────────────────────────────
    # 搜尋聚合
    # ──────────────────────────────────────────────────────────────────

    def _search_tavily(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        if not self.tavily_key:
            return []
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.tavily_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
                timeout=20,
            )
            resp.raise_for_status()
            return [
                {
                    "title": item.get("title", ""),
                    "body": item.get("content", ""),
                    "href": item.get("url", ""),
                    "source": "tavily",
                }
                for item in resp.json().get("results", [])
            ]
        except Exception as e:
            logger.warning("Tavily search failed: %s", e)
            return []

    @staticmethod
    def _search_ddgs(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        try:
            try:
                from ddgs import DDGS
                ddgs_cls = DDGS
            except Exception:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    from duckduckgo_search import DDGS as DDGSFallback
                ddgs_cls = DDGSFallback

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                ddgs_ctx = ddgs_cls()

            with ddgs_ctx as ddgs:
                raw = list(ddgs.text(query, max_results=max_results) or [])

            return [
                {
                    "title": item.get("title", ""),
                    "body": item.get("body", ""),
                    "href": item.get("href", ""),
                    "source": "ddgs",
                }
                for item in raw
            ]
        except Exception as e:
            logger.warning("DDGS search failed: %s", e)
            return []

    def _multi_query_search(self, queries: List[str], num_results: int = 8) -> List[Dict[str, Any]]:
        """多組關鍵字搜尋 + dedupe，並對前 3 筆抓取全文。"""
        combined: List[Dict[str, Any]] = []
        seen: set = set()

        for query in queries:
            logger.info("🔍 搜尋: %s", query)
            for item in self._search_tavily(query, max_results=5):
                key = (item["href"], item["title"])
                if key not in seen:
                    seen.add(key)
                    combined.append(item)

            needed = num_results - len(combined)
            if needed > 0:
                for item in self._search_ddgs(query, max_results=needed):
                    key = (item["href"], item["title"])
                    if key not in seen:
                        seen.add(key)
                        combined.append(item)

            if len(combined) >= num_results:
                break

        combined = combined[:num_results]

        # 前 3 筆嘗試全文抓取
        for i, item in enumerate(combined[:3]):
            full_text = _fetch_article(item.get("href", ""))
            if full_text:
                item["full_text"] = full_text
                logger.info("fetched full text from %s (%d chars)", item["href"], len(full_text))

        return combined

    # ──────────────────────────────────────────────────────────────────
    # 研究核心
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _format_search_results(search_results: List[Dict[str, Any]]) -> str:
        if not search_results:
            return "（搜尋結果為空；報告不可假裝已讀到外部來源，請明確標示資料不足。）"
        search_text = ""
        for i, item in enumerate(search_results, start=1):
            search_text += (
                f"[{i}] 來源: {item['source']}\n"
                f"標題: {item.get('title', '（無標題）')}\n"
                f"摘要: {item.get('body', '（無摘要）')}\n"
                f"連結: {item.get('href', '（無連結）')}\n"
            )
            if item.get("full_text"):
                search_text += f"全文節錄:\n{item['full_text']}\n"
            search_text += "\n"
        return search_text

    def _build_prompt(
        self,
        *,
        topic: str,
        context: str,
        memory_block: str,
        search_text: str,
        report_kind: str,
    ) -> str:
        now_str = datetime.now().strftime("%Y-%m-%d")
        kind = _normalize_report_kind(report_kind)

        if kind == "daily_market":
            return f"""你是一位資深的總體經濟、外資與跨市場分析師（Research W）。
今天（{now_str}），請撰寫《每日市場深度研究》。這不是單純行情複述，而是要把台股、新聞面、國際局勢與美股連動整合成可用於隔日觀察的研究。

【研究主題】{topic}

【盤後/系統上下文】
{context}
{memory_block}

【最新網搜資訊（含部分全文節錄）】
{search_text}

請嚴格區分「已確認數據事實」「市場解讀」「待驗證假設」。如果某項資料沒有來源或上下文不足，要明確寫資料不足，不能硬下結論。
字數控制在 1200-2200 字，直接輸出 Markdown，格式如下：
# 🕵️ Research W 每日市場深度研究：[一句話主線]
## 1. 今日市場主線
## 2. 台股與族群/權值股變化
## 3. 新聞面與國際局勢
## 4. 美股/半導體/匯率/商品連動
## 5. 對台積電、0050、00631L 與隔日台股的推演
## 6. 風險、反證與明日優先追蹤

語氣客觀、具體，不寫空泛建議；所有操作相關文字都必須提醒這是研究參考，不是自動交易指令。
"""

        if kind == "us_overnight":
            return f"""你是一位資深的美股、半導體與台股連動分析師（Research W）。
今天（{now_str}），請根據美股收盤行情、新聞原因與系統上下文，撰寫《美股收盤研究》。重點不是重複行情卡，而是解釋昨夜美股如何影響台積電、0050、00631L 與今日台股風險偏好。

【研究主題】{topic}

【美股收盤/系統上下文】
{context}
{memory_block}

【最新網搜資訊（含部分全文節錄）】
{search_text}

請嚴格區分「已確認數據事實」「市場解讀」「待驗證假設」。如果新聞只提供標題線索，必須明說因果仍需確認。
字數控制在 900-1600 字，直接輸出 Markdown，格式如下：
# 🌙 Research W 美股收盤研究：[一句話主線]
## 1. 昨夜美股發生什麼事
## 2. 驅動因素：Fed/利率/美元/油金/新聞面
## 3. 半導體與 TSM ADR / NVIDIA / SOXX 線索
## 4. 對今日台股、台積電、0050、00631L 的盤前定調
## 5. 今日開盤後要驗證的 3 件事

不得把研究結論寫成自動買賣指令；必要時寫明「僅供人工決策參考」。
"""

        return f"""你是一位資深的總體經濟與外資分析師（Research W）。
今天（{now_str}），我們觀測到市場發生以下異常 / 重點主題：
【研究主題】{topic}

【觸發上下文】
{context}
{memory_block}

【最新網搜資訊（含部分全文節錄）】
{search_text}

請根據以上資料，撰寫一份《異常事件速寫研究報告》。
字數控制在 800-1500 字，保持客觀、專業、一針見血。引用搜尋到的具體數據與事實。
格式如下：
# 🚨 異常事件速寫：[精煉一句話標題]
## 1. 發生什麼事（簡潔的現況總結）
## 2. 為什麼發生（彙整搜尋結果中的主因：籌碼面/基本面/消息面）
## 3. 數據佐證（引用具體的漲跌幅、法人買賣超、國際連動等數字）
## 4. 下一步關注點（針對這個異常，接下來 48 小時內要注意什麼？）

直接輸出 Markdown。
"""

    def research(self, topic: str, context: str = "", report_kind: str = "anomaly") -> str:
        """根據主題進行深度分析與總結。"""
        kind = _normalize_report_kind(report_kind)
        logger.info("開始深度研究主題：%s (%s)", topic, kind)

        # 1. 先查記憶庫
        memory_context = _query_memory(topic)

        # 2. 多組搜尋 + 全文
        queries = _build_search_queries(topic, report_kind=kind)
        search_results = self._multi_query_search(queries, num_results=10 if kind != "anomaly" else 8)
        search_text = self._format_search_results(search_results)

        # 歷史記憶
        memory_block = ""
        if memory_context:
            memory_block = f"\n\n【歷史背景（記憶庫相關記錄）】\n{memory_context}"

        prompt = self._build_prompt(
            topic=topic,
            context=context,
            memory_block=memory_block,
            search_text=search_text,
            report_kind=kind,
        )
        max_tokens = 4500 if kind == "daily_market" else 3800 if kind == "us_overnight" else 3000
        return self.claude.chat(prompt=prompt, max_tokens=max_tokens)

    # ──────────────────────────────────────────────────────────────────
    # 執行 + 推送
    # ──────────────────────────────────────────────────────────────────

    def run(self, topic: str, context: str = "", report_kind: str = "anomaly", notify: bool = True) -> dict[str, Any]:
        """完整的研究與推送流程。notify=False 時只存檔並回傳報告，不外發。"""
        kind = _normalize_report_kind(report_kind)
        title = report_title(kind)
        start_time = time.time()
        try:
            report = self.research(topic, context, report_kind=kind)
            master_control = get_current_control()

            # 加 frontmatter metadata
            now = datetime.now()
            frontmatter = (
                "---\n"
                f"topic: \"{topic}\"\n"
                f"report_kind: \"{kind}\"\n"
                f"triggered_at: \"{now.strftime('%Y-%m-%dT%H:%M:%S')}\"\n"
                f"trigger_reason: \"{context[:100]}\"\n"
                "---\n\n"
            )
            report_with_meta = frontmatter + report

            # 存檔
            log_dir = _BASE_DIR / "logs" / "research_reports"
            log_dir.mkdir(parents=True, exist_ok=True)
            slug = self._safe_topic_slug(f"{kind}_{topic}")
            filename = log_dir / f"{now.strftime('%Y%m%d_%H%M')}_{slug}.md"
            filename.write_text(report_with_meta, encoding="utf-8")
            logger.info("研究報告已存檔至 %s", filename)

            if not notify:
                record_research_health(
                    kind, True, duration_sec=time.time() - start_time,
                    report_chars=len(report or ""), output_path=str(filename),
                )
                return {
                    "success": True,
                    "output_path": str(filename),
                    "payload_path": None,
                    "title": title,
                    "report_kind": kind,
                    "report": report,
                }

            payload_path: Optional[Path] = None
            if master_control is not None:
                wechat_summary = self._build_wechat_summary(topic, report, str(filename), report_kind=kind)
                payload_path = write_payload(
                    {
                        "agent": "researcher",
                        "topic": topic,
                        "meta": {
                            "report_kind": kind,
                        },
                        "artifacts": {
                            "report_file": str(filename),
                        },
                        "notification": {
                            "feishu": {
                                "title": title,
                                "content": report,
                            },
                            "wechat": {
                                "text": wechat_summary,
                                "typing_seconds": 8,
                            },
                        },
                    },
                    control=master_control,
                )
                logger.info(
                    "Master agent 接管通知：已輸出 payload=%s",
                    payload_path,
                )
                record_research_health(
                    kind, True, duration_sec=time.time() - start_time,
                    report_chars=len(report or ""), output_path=str(filename),
                )
                return {
                    "success": True,
                    "output_path": str(filename),
                    "payload_path": str(payload_path) if payload_path else None,
                    "title": title,
                    "report_kind": kind,
                }

            # 推送飛書
            success = self.notifier.send_message(
                "oc_your_feishu_chat_id",
                title,
                report,
            )
            if not success:
                logger.warning("Research report saved but Feishu push failed")
            record_research_health(
                kind, bool(success), duration_sec=time.time() - start_time,
                report_chars=len(report or ""),
                error="" if success else "Feishu push failed",
                output_path=str(filename),
            )
            return {
                "success": bool(success),
                "output_path": str(filename),
                "payload_path": None,
                "title": title,
                "report_kind": kind,
            }
        except Exception as e:
            logger.error("Research execution failed: %s", e, exc_info=True)
            record_research_health(
                kind, False, duration_sec=time.time() - start_time, error=str(e),
            )
            return {
                "success": False,
                "error": str(e),
                "title": title,
                "report_kind": kind,
            }



def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", type=str, required=True, help="研究主題")
    parser.add_argument("--context", type=str, default="", help="補充上下文")
    parser.add_argument(
        "--report-kind",
        choices=["anomaly", "daily_market", "us_overnight"],
        default="anomaly",
        help="報告類型：異常事件 / 每日市場深度研究 / 美股收盤研究",
    )
    parser.add_argument("--dry-run", action="store_true", help="只存檔不外發通知")
    parser.add_argument("--with-research-pack", action="store_true", help="把 Research Pack 加入 Research W 上下文")
    parser.add_argument("--symbol", type=str, default="", help="Research Pack 標的，例如 SPY")
    parser.add_argument("--market", type=str, default="US", help="Research Pack 市場；目前只支援 US")
    parser.add_argument(
        "--market-data-type",
        choices=["delayed", "live"],
        default="delayed",
        help="IBKR market data type；未訂閱 live 時會明確標 no_live_subscription",
    )
    parser.add_argument("--ibkr-client-id", type=int, default=451, help="IBKR API client id")
    args = parser.parse_args()

    context = args.context
    if args.with_research_pack:
        if not args.symbol:
            parser.error("--with-research-pack requires --symbol")
        pack_context = build_research_pack_prompt_context(
            args.symbol,
            market=args.market,
            market_data_type=args.market_data_type,
            client_id=args.ibkr_client_id,
        )
        context = "\n\n".join(part for part in [context, pack_context] if part)

    agent = ResearchAgent()
    result = agent.run(
        args.topic,
        context,
        report_kind=args.report_kind,
        notify=not args.dry_run,
    )
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
