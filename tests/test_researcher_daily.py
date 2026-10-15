from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakeClaude:
    def chat(self, prompt: str, max_tokens: int = 0):
        self.last_prompt = prompt
        self.last_max_tokens = max_tokens
        return "# fake report\n\nbody"


class _FakeNotifier:
    def __init__(self, *_args, **_kwargs):
        self.sent = []

    def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return True


if "scripts.memory_agent" not in sys.modules:
    mem_mod = types.ModuleType("scripts.memory_agent")

    class _MemoryAgent:
        def retrieve(self, *_args, **_kwargs):
            return []

    mem_mod.MemoryAgent = _MemoryAgent
    sys.modules["scripts.memory_agent"] = mem_mod

from scripts import researcher  # noqa: E402


def test_report_title_modes():
    assert researcher.report_title("daily_market") == "🕵️ Research W 每日市場深度研究"
    assert researcher.report_title("us_overnight") == "🕵️ Research W 美股收盤研究"
    assert researcher.report_title("unknown") == "🕵️ Research W 異常偵測報告"


def test_daily_market_prompt_requires_news_international_and_us(monkeypatch):
    fake = _FakeClaude()
    monkeypatch.setattr(researcher, "ClaudeHelper", lambda: fake)
    monkeypatch.setattr(researcher, "LarkNotifier", _FakeNotifier)
    monkeypatch.setattr(researcher.ResearchAgent, "_multi_query_search", lambda self, queries, num_results=8: [])

    agent = researcher.ResearchAgent()
    output = agent.research("每日市場研究", "盤後 context", report_kind="daily_market")

    assert output.startswith("# fake report")
    assert "每日市場深度研究" in fake.last_prompt
    assert "新聞面與國際局勢" in fake.last_prompt
    assert "美股/半導體/匯率/商品連動" in fake.last_prompt
    assert "已確認數據事實" in fake.last_prompt
    assert fake.last_max_tokens == 4500


def test_research_pack_prompt_context_formats_delayed_boundary(monkeypatch):
    class _FakePack:
        def to_dict(self):
            return {
                "market": "US",
                "symbol": "SPY",
                "asof": "2026-06-18T09:30:00+08:00",
                "provider_map": {"quote": "IBKR", "news": [], "fundamentals": []},
                "quote": {
                    "provider": "IBKR",
                    "market_data_type": "delayed",
                    "session": "delayed",
                    "is_realtime": False,
                    "permission_status": "ok",
                    "bid": None,
                    "ask": None,
                    "last": 745.6,
                    "close": 740.96,
                    "market_price": 745.6,
                    "warnings": ["IBKR is showing delayed market data because live data is not subscribed"],
                },
                "news": [],
                "fundamentals": None,
                "warnings": [],
            }

    def fake_build(symbol, *, market_data_type="delayed", client_id=451, include_account_snapshot=False):
        assert symbol == "SPY"
        assert market_data_type == "delayed"
        assert client_id == 451
        return _FakePack()

    monkeypatch.setattr("utils.research_pack.build_us_research_pack", fake_build)

    context = researcher.build_research_pack_prompt_context(
        "spy",
        market="US",
        market_data_type="delayed",
        client_id=451,
    )

    assert "Research Pack" in context
    assert "market: US" in context
    assert "symbol: SPY" in context
    assert "provider_map.quote: IBKR" in context
    assert "market_data_type: delayed" in context
    assert "is_realtime: False" in context
    assert "permission_status: ok" in context
    assert "news_count: 0" in context
    assert "fundamentals_available: False" in context
    assert "不得寫成即時行情" in context
    assert "不是自動交易或下單指令" in context


def test_research_pack_prompt_context_rejects_non_us_market():
    try:
        researcher.build_research_pack_prompt_context("2330", market="TW")
    except ValueError as exc:
        assert "US market only" in str(exc)
    else:
        raise AssertionError("TW Research Pack should be rejected")


def test_run_notify_false_saves_report_without_sending(tmp_path, monkeypatch):
    fake = _FakeClaude()
    notifier = _FakeNotifier()
    monkeypatch.setattr(researcher, "ClaudeHelper", lambda: fake)
    monkeypatch.setattr(researcher, "LarkNotifier", lambda *_args, **_kwargs: notifier)
    monkeypatch.setattr(researcher.ResearchAgent, "_multi_query_search", lambda self, queries, num_results=8: [])
    monkeypatch.setattr(researcher, "_BASE_DIR", tmp_path)

    agent = researcher.ResearchAgent()
    result = agent.run("美股收盤研究", "context", report_kind="us_overnight", notify=False)

    assert result["success"] is True
    assert result["report_kind"] == "us_overnight"
    assert result["report"] == "# fake report\n\nbody"
    assert notifier.sent == []
    report_path = Path(result["output_path"])
    assert report_path.exists()
    text = report_path.read_text(encoding="utf-8")
    assert 'report_kind: "us_overnight"' in text
    assert "# fake report" in text


def test_research_health_record_and_summary(tmp_path):
    """health 記錄與當日彙整：成功/失敗分別計數、by_kind 正確、一行摘要可讀。"""
    health = tmp_path / "research_health.jsonl"
    researcher.record_research_health(
        "daily_market", True, duration_sec=12.3, report_chars=1800,
        path=health,
    )
    researcher.record_research_health(
        "us_overnight", True, duration_sec=8.0, report_chars=1200,
        path=health,
    )
    researcher.record_research_health(
        "anomaly", False, error="route down", path=health,
    )

    summary = researcher.summarize_research_health(path=health)
    assert summary["total"] == 3
    assert summary["ok"] == 2
    assert summary["failed"] == 1
    assert summary["by_kind"]["daily_market"]["ok"] == 1
    assert summary["by_kind"]["anomaly"]["ok"] == 0
    assert summary["failures"][0]["error"] == "route down"

    line = researcher.format_research_health_line(summary)
    assert "Research W 健康" in line
    assert "2/3 成功" in line
    assert "route down" in line


def test_research_health_summary_empty(tmp_path):
    """無記錄日：摘要不報錯、給友善訊息。"""
    summary = researcher.summarize_research_health(path=tmp_path / "nope.jsonl")
    assert summary["total"] == 0
    line = researcher.format_research_health_line(summary)
    assert "尚無研究產出記錄" in line
