"""sim/tests/test_advisor_daily_brief.py — M6 階段二 區塊 J 測試"""

import json
import sys
import tempfile
import types
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor.daily_brief import render_daily_brief, write_daily_brief


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture(autouse=True)
def fake_yfinance(monkeypatch):
    """所有測試自動 stub yfinance（避免真實連網）。"""
    fake_yf = types.SimpleNamespace()

    class FakeTicker:
        def __init__(self, *a, **kw):
            pass

        def history(self, **kwargs):
            raise RuntimeError("offline-test")

    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _empty_paths(tmp_dir: Path) -> dict:
    fills = tmp_dir / "fills.jsonl"
    equity = tmp_dir / "eq.jsonl"
    audit = tmp_dir / "audit.jsonl"
    fills.touch()
    equity.touch()
    audit.touch()
    return {"fills_path": fills, "equity_curve_path": equity, "advisor_audit_path": audit}


def test_brief_empty_data_does_not_panic(tmp_dir):
    """空檔案路徑 → 產出 markdown、含所有區塊標題、不 raise。"""
    md = render_daily_brief(brief_date=date(2026, 5, 6), **_empty_paths(tmp_dir))
    assert "# 模擬交易日報 2026-05-06" in md
    assert "## 四件套指標" in md
    assert "## 策略切點（M6 績效分段提醒）" in md
    assert "2026-05-29" in md  # 策略性轉防守切點
    assert "## 帳戶快照" in md
    assert "## 今日成交" in md
    assert "## Equity 曲線變化" in md
    assert "## Advisor 今日決策" in md
    assert "## 風險觀察" in md
    # 空狀態下應印「無 ...」字樣
    assert "無成交" in md
    assert "無 decision_round" in md


def test_brief_advisor_multi_rounds_expands_last(tmp_dir):
    """多輪 advisor_audit → 最末輪展開、舊輪只列摘要。"""
    paths = _empty_paths(tmp_dir)
    _write_jsonl(paths["advisor_audit_path"], [
        {"type": "decision_round", "time": "2026-05-06T08:00:00",
         "status": "decide_failed", "decided": 0, "passed": 0, "submitted": 0,
         "blocked": 0, "hold": 0, "error": "early_round_error"},
        {"type": "decision_round", "time": "2026-05-06T09:00:00",
         "status": "ok", "decided": 3, "passed": 2, "submitted": 1, "blocked": 1, "hold": 0,
         "summary": "末輪摘要文字", "metrics_self_eval": "末輪自評",
         "model": "claude-sonnet-4-6", "provider_id": "p1",
         "decisions_log": [{"action": "buy", "symbol": "2330.TW", "qty": 100,
                            "type": "MKT", "result": "submitted",
                            "reason": "高信心訊號"}]},
    ])
    md = render_daily_brief(brief_date=date(2026, 5, 6), **paths)
    assert "今日共 2 輪" in md
    # 最末輪細節
    assert "末輪摘要文字" in md
    assert "末輪自評" in md
    assert "buy 2330.TW 100" in md
    # 其餘輪次（早輪）應出現但只在「其餘輪次」列出，且不展開 summary
    assert "其餘輪次" in md
    assert "decide_failed" in md
    # 風險觀察應計到 1 次 error、1 次 blocked、1 次非 ok
    assert "blocked 決策累計：1 筆" in md
    assert "error 輪次：1" in md


def test_brief_today_fills_table(tmp_dir):
    """有今日 fills → markdown 含表格行。"""
    paths = _empty_paths(tmp_dir)
    _write_jsonl(paths["fills_path"], [
        {"order_id": "o1", "symbol": "2330.TW", "side": "BUY", "qty": 100,
         "price": 1000.0, "fee": 142.5, "tax": 0, "fill_id": "f1",
         "filled_at": "2026-05-06T09:30:15"},
        {"order_id": "o2", "symbol": "2317.TW", "side": "BUY", "qty": 200,
         "price": 200.0, "fee": 57, "tax": 0, "fill_id": "f2",
         "filled_at": "2026-05-05T14:00:00"},  # 昨天的，不該出現
    ])
    md = render_daily_brief(brief_date=date(2026, 5, 6), **paths)
    assert "| 09:30:15 | 2330.TW | BUY | 100 | 1000.00" in md
    assert "| 14:00:00 | 2317.TW" not in md  # 昨天 fill 應被今日成交表過濾掉
    assert "共 1 筆" in md


def test_brief_account_snapshot_with_positions(tmp_dir):
    """有持倉 → 持倉區塊應列出。"""
    paths = _empty_paths(tmp_dir)
    _write_jsonl(paths["equity_curve_path"], [
        {"time": "2026-05-06T09:00:00", "cash": 500000.0,
         "position_value": 500000.0, "equity": 1000000.0, "total_realized_pnl": 0.0},
    ])
    _write_jsonl(paths["fills_path"], [
        {"order_id": "o1", "symbol": "2330.TW", "side": "BUY", "qty": 100,
         "price": 1000.0, "fee": 142.5, "tax": 0, "fill_id": "f1",
         "filled_at": "2026-05-06T09:00:00"},
    ])
    md = render_daily_brief(brief_date=date(2026, 5, 6), **paths)
    assert "source：equity_curve" in md
    assert "equity：1,000,000" in md
    assert "cash：500,000" in md
    assert "2330.TW：100 股" in md


def test_brief_stale_equity_snapshot_uses_fills_replay(tmp_dir):
    """equity_curve 停在舊日不可直接顯示；應警告並改用 fills replay 快照。"""
    paths = _empty_paths(tmp_dir)
    _write_jsonl(paths["equity_curve_path"], [
        {"time": "2026-05-06T09:00:00", "cash": 999999.0,
         "position_value": 0.0, "equity": 999999.0, "total_realized_pnl": 0.0},
    ])
    _write_jsonl(paths["fills_path"], [
        {"order_id": "b1", "symbol": "2330.TW", "side": "BUY", "qty": 100,
         "price": 1000.0, "fee": 100.0, "tax": 0, "fill_id": "fb1",
         "filled_at": "2026-05-28T09:00:00"},
        {"order_id": "s1", "symbol": "2330.TW", "side": "SELL", "qty": 50,
         "price": 1100.0, "fee": 50.0, "tax": 165.0, "fill_id": "fs1",
         "filled_at": "2026-05-28T10:00:00"},
    ])

    md = render_daily_brief(brief_date=date(2026, 5, 28), **paths)

    assert "equity_curve 最後快照非報告日" in md
    assert "source：fills_replay" in md
    assert "position_value / equity 為持倉成本估算，非即時市值" in md
    assert "快照時間：fills_replay" in md
    assert "total_realized_pnl：+4,735" in md
    assert "2330.TW：50 股" in md


def test_brief_account_snapshot_with_mtm_and_stop_floor(tmp_dir, monkeypatch):
    """有 quote + auto-stop → 顯示未實現、MTM equity、stop floor。"""
    paths = _empty_paths(tmp_dir)
    orders_path = tmp_dir / "orders.jsonl"
    watch_only_path = tmp_dir / "watch_only.yaml"
    watch_only_path.write_text("watch_only: {}\n", encoding="utf-8")
    paths["orders_path"] = orders_path
    paths["watch_only_path"] = watch_only_path

    _write_jsonl(paths["equity_curve_path"], [
        {"time": "2026-05-06T09:00:00", "cash": 989900.0,
         "position_value": 10100.0, "equity": 1000000.0, "total_realized_pnl": 0.0},
    ])
    _write_jsonl(paths["fills_path"], [
        {"order_id": "b1", "symbol": "2330.TW", "side": "BUY", "qty": 100,
         "price": 100.0, "fee": 100.0, "tax": 0, "fill_id": "fb1",
         "filled_at": "2026-05-06T09:00:00"},
    ])
    _write_jsonl(orders_path, [
        {"symbol": "2330.TW", "side": "SELL", "type": "STOP", "qty": 100,
         "price": 98.0, "order_id": "stop1", "status": "PENDING",
         "created_at": "2026-05-06T09:05:00", "notes": "[auto-stop] test"},
    ])

    class FakeQuoteFeed:
        def get_quotes(self, symbols):
            return {"2330.TW": 120.0}

    fake_feed = types.SimpleNamespace(QuoteFeed=lambda: FakeQuoteFeed())
    monkeypatch.setitem(sys.modules, "sim.feed", fake_feed)

    md = render_daily_brief(brief_date=date(2026, 5, 6), **paths)

    assert "### 綜合績效口徑" in md
    assert "unrealized_pnl（有 quote 持倉）：+1,900" in md
    assert "mark_to_market_equity：1,001,900" in md
    assert "stop_floor_equity：999,700" in md
    assert "| 2330.TW | 100 | 101.00 | 120" in md


def test_brief_manual_live_position_watch_is_listed(tmp_dir, monkeypatch):
    """manual_live_position 只列入人工/實盤觀察，不混成 sim 持倉。"""
    paths = _empty_paths(tmp_dir)
    orders_path = tmp_dir / "orders.jsonl"
    watch_only_path = tmp_dir / "watch_only.yaml"
    orders_path.touch()
    watch_only_path.write_text(
        """
watch_only:
  00631L.TW:
    source: manual_live_position
    entry_price: 37.08
    reason: 實盤開倉重點觀察
    review_rule: 跌破成本 2% 人工檢查
    action_rule:
      type: below_avg_cost_pct
      pct: 0.02
      message: 跌破成本 2%
""".strip() + "\n",
        encoding="utf-8",
    )
    paths["orders_path"] = orders_path
    paths["watch_only_path"] = watch_only_path

    class FakeQuoteFeed:
        def get_quotes(self, symbols):
            return {"00631L.TW": 36.0}

    fake_feed = types.SimpleNamespace(QuoteFeed=lambda: FakeQuoteFeed())
    monkeypatch.setitem(sys.modules, "sim.feed", fake_feed)

    md = render_daily_brief(brief_date=date(2026, 5, 6), **paths)

    assert "### 00631L / 人工實盤 vs sim 策略觀察" in md
    assert "兩本帳" in md
    assert "00631L.TW" in md
    assert "37.08" in md
    assert "ACTION NEEDED：跌破成本 2%" in md
    assert "sim 尚未進場" in md
    assert "實盤開倉重點觀察" in md


def test_brief_manual_live_compares_with_sim_position(tmp_dir, monkeypatch):
    """00631L 可同時有實盤成本與 sim 持倉；兩本帳不得混淆。"""
    paths = _empty_paths(tmp_dir)
    orders_path = tmp_dir / "orders.jsonl"
    watch_only_path = tmp_dir / "watch_only.yaml"
    paths["orders_path"] = orders_path
    paths["watch_only_path"] = watch_only_path
    watch_only_path.write_text(
        """
watch_only:
  00631L.TW:
    source: manual_live_position
    entry_price: 37.08
    reason: 實盤開倉重點觀察
    action_rule:
      type: leveraged_etf_watch
      profit_protect_pct: 0.03
      profit_trail_pct: 0.05
""".strip() + "\n",
        encoding="utf-8",
    )
    _write_jsonl(paths["fills_path"], [
        {"order_id": "sim-buy", "symbol": "00631L.TW", "side": "BUY", "qty": 1000,
         "price": 38.0, "fee": 0.0, "tax": 0.0, "fill_id": "sim-f1",
         "filled_at": "2026-05-06T10:00:00"},
    ])
    _write_jsonl(orders_path, [
        {"symbol": "00631L.TW", "side": "SELL", "type": "STOP", "qty": 1000,
         "price": 36.5, "order_id": "stop1", "status": "PENDING",
         "created_at": "2026-05-06T10:05:00", "notes": "[auto-stop] test"},
    ])

    class FakeQuoteFeed:
        def get_quotes(self, symbols):
            return {"00631L.TW": 39.0}

    fake_feed = types.SimpleNamespace(QuoteFeed=lambda: FakeQuoteFeed())
    monkeypatch.setitem(sys.modules, "sim.feed", fake_feed)

    md = render_daily_brief(brief_date=date(2026, 5, 6), **paths)

    assert "00631L / 人工實盤 vs sim 策略觀察" in md
    assert "37.08" in md  # 實盤成本
    assert "已持有 1,000 股 @ 38.00" in md  # sim avg_cost
    assert "36.50" in md  # sim stop
    assert "兩本帳" in md


def test_write_daily_brief_writes_file(tmp_dir):
    """write_daily_brief 寫入指定路徑、檔案存在、bytes > 0。"""
    paths = _empty_paths(tmp_dir)
    out = tmp_dir / "out" / "brief.md"
    written = write_daily_brief(
        output_path=out,
        brief_date=date(2026, 5, 6),
        **paths,
    )
    assert written == out
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "模擬交易日報 2026-05-06" in content
    assert len(content) > 200


def test_brief_manual_live_flat_shows_settlement(tmp_dir, monkeypatch):
    """00631L 已平倉(flat) → 雙軌卡顯示本輪結算與空手狀態，不報跌破成本。"""
    paths = _empty_paths(tmp_dir)
    orders_path = tmp_dir / "orders.jsonl"
    watch_only_path = tmp_dir / "watch_only.yaml"
    orders_path.touch()
    paths["orders_path"] = orders_path
    paths["watch_only_path"] = watch_only_path
    watch_only_path.write_text(
        """
watch_only:
  00631L.TW:
    source: manual_live_position
    position_state: flat
    entry_price: 37.08
    exit_price: 39.0
    exit_date: '2026-05-05'
    reason: 已平倉觀察再進場
    action_rule:
      type: leveraged_etf_watch
""".strip() + "\n",
        encoding="utf-8",
    )

    class FakeQuoteFeed:
        def get_quotes(self, symbols):
            return {"00631L.TW": 36.0}

    fake_feed = types.SimpleNamespace(QuoteFeed=lambda: FakeQuoteFeed())
    monkeypatch.setitem(sys.modules, "sim.feed", fake_feed)

    md = render_daily_brief(brief_date=date(2026, 5, 6), **paths)

    assert "00631L.TW（已平倉）" in md
    assert "| 標的 | 實盤基準 |" in md
    assert "37.08→39.00" in md
    assert "| 標的 | 實盤成本 |" not in md
    assert "本輪實盤結算" in md
    assert "已平倉空手（再進場由盤中雷達觀察）" in md
    assert "減碼" not in md
    assert "補風控" not in md
