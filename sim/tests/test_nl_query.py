"""
sim/tests/test_nl_query.py — M4 自然語言查詢測試

涵蓋：
- is_sim_nl_query：明示前綴 / 隱式關鍵字 / 邊界
- _tail_jsonl：tail N + 略過壞行 + 缺檔
- _compute_lifetime_stats：勝率計算 + 無 realized 邊界
- build_snapshot：snapshot 結構 + unrealized_pnl 補充 + 缺 feed 路徑
- answer_nl_query：mock helper + @sim 前綴剝除 + helper 失敗降級
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sim.nl_query import (
    _compute_lifetime_stats,
    _tail_jsonl,
    answer_nl_query,
    build_snapshot,
    is_sim_nl_query,
)


# ─────────────────────────────────────────────────────
# is_sim_nl_query
# ─────────────────────────────────────────────────────

class TestIsSimNlQuery:
    def test_at_sim_prefix_lowercase(self):
        assert is_sim_nl_query("@sim 我這個月勝率多少") is True

    def test_at_sim_prefix_uppercase(self):
        # 大小寫不敏感
        assert is_sim_nl_query("@SIM 隨便問") is True
        assert is_sim_nl_query("@Sim 隨便問") is True

    def test_at_sim_prefix_with_leading_whitespace(self):
        assert is_sim_nl_query("  @sim 隨便問") is True

    def test_keyword_hit_position(self):
        assert is_sim_nl_query("我的持倉現在多少") is True

    def test_keyword_hit_winrate(self):
        assert is_sim_nl_query("我這個月勝率多少") is True

    def test_keyword_hit_unrealized(self):
        assert is_sim_nl_query("我目前最大未實現損益是哪一檔") is True

    def test_keyword_hit_simplified_winrate(self):
        # 簡體：「我这个月胜率多少」應命中（飛書輸入法可能輸出簡體）
        assert is_sim_nl_query("我这个月胜率多少") is True

    def test_keyword_hit_simplified_position(self):
        # 簡體：「我的持仓现在赚多少」應命中
        assert is_sim_nl_query("我的持仓现在赚多少") is True
        # 簡繁混（含 mention 前綴）也命中
        assert is_sim_nl_query("@_user_1 我目前的持仓现在赚多少") is True

    def test_keyword_hit_simplified_max_loss(self):
        assert is_sim_nl_query("我目前最大亏损是哪一檔") is True

    def test_pure_stock_code_no_match(self):
        # 純股票代碼不命中（M3 /quote 已覆蓋；NL 必須有第一人稱）
        assert is_sim_nl_query("2330") is False
        assert is_sim_nl_query("3030 怎麼看") is False

    def test_chitchat_no_match(self):
        assert is_sim_nl_query("今天天氣很好") is False
        assert is_sim_nl_query("好累喔") is False

    def test_empty_or_none(self):
        assert is_sim_nl_query("") is False
        assert is_sim_nl_query(None) is False  # type: ignore[arg-type]

    def test_lone_keyword_substring_no_first_person(self):
        # 「成交」「虧損」單獨出現不命中（避免吞情緒抒發 / 新聞討論）
        assert is_sim_nl_query("這檔成交量很大") is False
        assert is_sim_nl_query("市場大虧損") is False


# ─────────────────────────────────────────────────────
# _tail_jsonl
# ─────────────────────────────────────────────────────

class TestTailJsonl:
    def test_returns_last_n_records(self, tmp_path: Path):
        path = tmp_path / "fills.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for i in range(150):
                f.write(json.dumps({"i": i}) + "\n")

        records = _tail_jsonl(path, n=100)
        assert len(records) == 100
        # 最後 100 筆應為 i=50..149
        assert records[0] == {"i": 50}
        assert records[-1] == {"i": 149}

    def test_skips_invalid_json_lines(self, tmp_path: Path):
        path = tmp_path / "fills.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"a": 1}) + "\n")
            f.write("not json\n")
            f.write(json.dumps({"a": 2}) + "\n")
            f.write("\n")  # 空白行
            f.write(json.dumps({"a": 3}) + "\n")

        records = _tail_jsonl(path, n=100)
        assert records == [{"a": 1}, {"a": 2}, {"a": 3}]

    def test_missing_file_returns_empty(self, tmp_path: Path):
        path = tmp_path / "does_not_exist.jsonl"
        assert _tail_jsonl(path, n=10) == []

    def test_empty_file_returns_empty(self, tmp_path: Path):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        assert _tail_jsonl(path, n=10) == []


# ─────────────────────────────────────────────────────
# _compute_lifetime_stats
# ─────────────────────────────────────────────────────

class TestComputeLifetimeStats:
    def test_basic_win_rate(self):
        fills = [
            {"realized_pnl": 100},   # win
            {"realized_pnl": -50},   # loss
            {"realized_pnl": 200},   # win
            {"realized_pnl": 0},     # 不計（買入或無平倉）
            {"realized_pnl": None},  # 不計
        ]
        stats = _compute_lifetime_stats(fills)
        assert stats["total_realized_fills"] == 3
        assert stats["win_count"] == 2
        assert stats["win_rate"] == pytest.approx(2 / 3)

    def test_no_realized_returns_none_win_rate(self):
        fills = [
            {"realized_pnl": 0},
            {"realized_pnl": None},
            {},  # 完全沒這欄位
        ]
        stats = _compute_lifetime_stats(fills)
        assert stats["total_realized_fills"] == 0
        assert stats["win_count"] == 0
        assert stats["win_rate"] is None

    def test_string_realized_pnl_parses(self):
        # jsonl 反序列化可能拿到字串型數字
        fills = [{"realized_pnl": "100.5"}, {"realized_pnl": "-20"}]
        stats = _compute_lifetime_stats(fills)
        assert stats["total_realized_fills"] == 2
        assert stats["win_count"] == 1
        assert stats["win_rate"] == 0.5

    def test_invalid_realized_pnl_skipped(self):
        fills = [{"realized_pnl": "abc"}, {"realized_pnl": 100}]
        stats = _compute_lifetime_stats(fills)
        assert stats["total_realized_fills"] == 1
        assert stats["win_count"] == 1


# ─────────────────────────────────────────────────────
# build_snapshot
# ─────────────────────────────────────────────────────

class _FakeAccount:
    def __init__(self, positions):
        self.positions = positions


class _FakeEngine:
    """模擬 MatchEngine：只實作 build_snapshot 會用到的介面。"""

    def __init__(self, snapshot_dict, positions_dict):
        self._snap = snapshot_dict
        self.account = _FakeAccount(positions_dict)

    def snapshot(self, quotes=None):
        return self._snap


class _FakeFeed:
    def __init__(self, quotes):
        self._quotes = quotes

    def get_quotes(self, symbols):
        return {s: self._quotes[s] for s in symbols if s in self._quotes}


class TestBuildSnapshot:
    def _make_engine(self, positions, pending=None):
        """positions: {sym: {"qty": int, "avg_cost": float, "realized_pnl": float}}"""
        snap = {
            "account": {
                "cash": 800000.0,
                "initial_cash": 1_000_000.0,
                "total_realized_pnl": 5000.0,
                "positions": {sym: dict(data) for sym, data in positions.items()},
                "equity": 1_005_000.0,
                "total_return_pct": 0.5,
            },
            "pending_orders": pending or [],
        }
        # account.positions 也要 callable，但 build_snapshot 只用 keys
        return _FakeEngine(snap, positions_dict={sym: object() for sym in positions})

    def test_with_feed_adds_current_quote_and_unrealized(self, tmp_path: Path):
        engine = self._make_engine({
            "2330.TW": {"symbol": "2330.TW", "qty": 100, "avg_cost": 600.0, "realized_pnl": 0.0},
        })
        feed = _FakeFeed({"2330.TW": 650.0})

        # 用 tmp_path 避開預設 fills 路徑
        empty_fills = tmp_path / "fills.jsonl"
        empty_fills.write_text("", encoding="utf-8")

        snap = build_snapshot(engine, feed=feed, fills_path=empty_fills)

        pos = snap["account"]["positions"]["2330.TW"]
        assert pos["current_quote"] == 650.0
        assert pos["unrealized_pnl"] == pytest.approx((650.0 - 600.0) * 100)

    def test_without_feed_quotes_are_none(self, tmp_path: Path):
        engine = self._make_engine({
            "2330.TW": {"symbol": "2330.TW", "qty": 100, "avg_cost": 600.0, "realized_pnl": 0.0},
        })
        empty_fills = tmp_path / "fills.jsonl"
        empty_fills.write_text("", encoding="utf-8")

        snap = build_snapshot(engine, feed=None, fills_path=empty_fills)

        pos = snap["account"]["positions"]["2330.TW"]
        assert pos["current_quote"] is None
        assert pos["unrealized_pnl"] is None

    def test_includes_recent_fills_and_lifetime_stats(self, tmp_path: Path):
        engine = self._make_engine({})
        fills_path = tmp_path / "fills.jsonl"
        with open(fills_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"order_id": "1", "realized_pnl": 100}) + "\n")
            f.write(json.dumps({"order_id": "2", "realized_pnl": -50}) + "\n")
            f.write(json.dumps({"order_id": "3", "realized_pnl": 200}) + "\n")

        snap = build_snapshot(engine, feed=None, fills_path=fills_path)

        assert len(snap["recent_fills"]) == 3
        assert snap["lifetime_stats"]["total_realized_fills"] == 3
        assert snap["lifetime_stats"]["win_count"] == 2
        assert snap["lifetime_stats"]["win_rate"] == pytest.approx(2 / 3)

    def test_pending_orders_truncated_to_n_orders(self, tmp_path: Path):
        engine = self._make_engine({}, pending=[{"order_id": f"o{i}"} for i in range(80)])
        empty_fills = tmp_path / "fills.jsonl"
        empty_fills.write_text("", encoding="utf-8")

        snap = build_snapshot(engine, feed=None, n_orders=50, fills_path=empty_fills)
        assert len(snap["pending_orders"]) == 50
        # 取最後 50 筆
        assert snap["pending_orders"][0]["order_id"] == "o30"
        assert snap["pending_orders"][-1]["order_id"] == "o79"


# ─────────────────────────────────────────────────────
# answer_nl_query
# ─────────────────────────────────────────────────────

class TestAnswerNlQuery:
    def test_with_mock_helper_returns_reply(self):
        helper = MagicMock()
        helper.chat.return_value = "本月勝率 60%（5 勝 2 敗）"

        snapshot = {"account": {"cash": 1_000_000}, "lifetime_stats": {"win_rate": 0.6}}
        reply = answer_nl_query("我這個月勝率多少", snapshot, helper=helper)

        assert reply == "本月勝率 60%（5 勝 2 敗）"
        helper.chat.assert_called_once()

    def test_strips_at_sim_prefix_before_llm(self):
        helper = MagicMock()
        helper.chat.return_value = "ok"

        answer_nl_query("@sim 我這個月勝率多少", {"account": {}}, helper=helper)

        kwargs = helper.chat.call_args.kwargs
        # @sim 前綴不應出現在送給 LLM 的 prompt 裡
        assert "@sim" not in kwargs["prompt"].lower().split("##")[0]
        # 但問句本體要保留
        assert "我這個月勝率多少" in kwargs["prompt"]
        # snapshot 要被注入
        assert "模擬盤狀態" in kwargs["prompt"]
        assert "account" in kwargs["prompt"]

    def test_system_prompt_passed_through(self):
        helper = MagicMock()
        helper.chat.return_value = "ok"

        answer_nl_query("我的持倉現在多少", {"account": {}}, helper=helper)

        kwargs = helper.chat.call_args.kwargs
        assert kwargs["system"]  # 有 system prompt
        assert "台股模擬盤助理" in kwargs["system"]

    def test_helper_failure_returns_fallback_message(self):
        helper = MagicMock()
        helper.chat.side_effect = RuntimeError("LLM down")

        reply = answer_nl_query("我的持倉現在多少", {"account": {}}, helper=helper)

        assert "❌" in reply
        assert "/portfolio" in reply or "/pnl" in reply  # 提示降級到結構化指令

    def test_empty_query_after_at_sim_returns_hint(self):
        helper = MagicMock()
        # 不應呼叫 helper（前置檢查就攔下）
        reply = answer_nl_query("@sim", {"account": {}}, helper=helper)

        assert "❌" in reply
        assert "@sim" in reply  # 提示正確用法
        helper.chat.assert_not_called()

    def test_empty_llm_reply_returns_placeholder(self):
        helper = MagicMock()
        helper.chat.return_value = ""

        reply = answer_nl_query("我的持倉現在多少", {"account": {}}, helper=helper)
        assert reply  # 不該是空字串
        assert "稍後" in reply or "為空" in reply
