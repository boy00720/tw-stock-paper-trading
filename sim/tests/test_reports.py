"""
M5 驗收測試：sim/reports.py 的資料層、格式化層、CLI、cross-validation。

執行：
    python3 -m pytest sim/tests/test_reports.py
    python3 -m pytest sim/tests/test_reports.py::test_iter_fills_skips_corrupted_lines

主要分組：
- 資料層 (6)：iter_fills、iter_fills_in_period、today_realized_pnl、
  last_n_days_realized_pnl、win_rate、equity_curve_append
- 格式化 (3)：fmt_realtime_fill / fmt_daily_report / fmt_weekly_report
- CLI (3)：mock _send_dm 驗 daily / weekly / --no-send
- Cross-validation (3)：engine vs _position_math.replay_positions_from_fills 一致
- 邊界 (3)：無 fills、跨日界、jsonl 損壞行
- equity_curve (1)：append 後讀回欄位完整
"""

import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from sim import reports
from sim._position_math import replay_positions_from_fills as _pure_replay
from sim.engine import MatchEngine
from sim.models import Fill, Order, OrderSide, OrderType


# ─────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────

def _write_fills(path: Path, fills: list[dict]) -> None:
    """把 fill dict list 寫到 jsonl（測試用 fixture）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for fill in fills:
            f.write(json.dumps(fill, ensure_ascii=False) + "\n")


def _fill(
    *,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    fee: float = 100.0,
    tax: float = 0.0,
    fill_id: str | None = None,
    filled_at: str | None = None,
) -> dict:
    """建一個 fill dict（對齊 fills.jsonl 實際結構）。

    fill_id 預設用 uuid 確保每次唯一（避免 _sum_realized_from_fills 期間判斷誤判）。
    """
    fid = fill_id or f"fill-{uuid.uuid4().hex[:12]}"
    return {
        "order_id": f"ord-{uuid.uuid4().hex[:12]}",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "fee": fee,
        "tax": tax,
        "fill_id": fid,
        "filled_at": filled_at or datetime.now().isoformat(timespec="seconds"),
    }


def _make_engine(tmp_dir: Path) -> MatchEngine:
    """建 engine 並把 jsonl 重定向 tmp_dir（與 test_e2e_smoke._make_engine 同 pattern）。"""
    e = MatchEngine()
    e.orders_log_path = tmp_dir / "orders.jsonl"
    e.fills_log_path = tmp_dir / "fills.jsonl"
    return e


# ═════════════════════════════════════════════════════
# 資料層 (6)
# ═════════════════════════════════════════════════════

def test_iter_fills_basic(tmp_path):
    fills_path = tmp_path / "fills.jsonl"
    _write_fills(fills_path, [
        _fill(symbol="2330.TW", side="BUY", qty=100, price=600.0),
        _fill(symbol="2330.TW", side="SELL", qty=50, price=620.0),
    ])
    fills = list(reports.iter_fills(fills_path))
    assert len(fills) == 2
    assert fills[0]["symbol"] == "2330.TW"
    assert fills[1]["side"] == "SELL"


def test_iter_fills_in_period_filters_correctly(tmp_path):
    fills_path = tmp_path / "fills.jsonl"
    _write_fills(fills_path, [
        _fill(symbol="A", side="BUY", qty=10, price=100, filled_at="2026-05-03T10:00:00"),
        _fill(symbol="B", side="BUY", qty=10, price=100, filled_at="2026-05-04T10:00:00"),
        _fill(symbol="C", side="BUY", qty=10, price=100, filled_at="2026-05-05T10:00:00"),
    ])
    start = datetime(2026, 5, 4)
    end = datetime(2026, 5, 5)
    fills = reports.iter_fills_in_period(fills_path, start, end)
    assert len(fills) == 1
    assert fills[0]["symbol"] == "B"


def test_today_realized_pnl_sums_today_only(tmp_path):
    """跨日界：今天的 SELL 算 realized，昨天的 SELL 不算。"""
    fills_path = tmp_path / "fills.jsonl"
    today = datetime(2026, 5, 5, 10, 0, 0)
    yesterday = datetime(2026, 5, 4, 10, 0, 0)
    _write_fills(fills_path, [
        _fill(symbol="A", side="BUY", qty=100, price=100.0, fee=0,
              filled_at=yesterday.isoformat(timespec="seconds")),
        _fill(symbol="A", side="SELL", qty=50, price=110.0, fee=0, tax=0,
              filled_at=yesterday.isoformat(timespec="seconds")),
        _fill(symbol="A", side="SELL", qty=50, price=120.0, fee=0, tax=0,
              filled_at=today.isoformat(timespec="seconds")),
    ])
    today_pnl = reports.today_realized_pnl(fills_path, now=today)
    # 今天那筆：50 股 SELL @ 120 - 50 股 cost_basis @ 100 = +1000
    assert today_pnl == pytest.approx(1000.0, abs=0.01)


def test_last_n_days_realized_pnl_window(tmp_path):
    """近 N 日：取末 N 日所有 SELL realized。"""
    fills_path = tmp_path / "fills.jsonl"
    now = datetime(2026, 5, 5, 12, 0, 0)
    d0 = now - timedelta(days=10)  # 10 天前的 SELL，應在 30 天窗內
    d1 = now - timedelta(days=40)  # 40 天前，超出 30 天窗
    _write_fills(fills_path, [
        _fill(symbol="A", side="BUY", qty=200, price=100.0, fee=0,
              filled_at=(d1 - timedelta(hours=1)).isoformat(timespec="seconds")),
        _fill(symbol="A", side="SELL", qty=100, price=90.0, fee=0, tax=0,
              filled_at=d1.isoformat(timespec="seconds")),  # 40 天前 -1000
        _fill(symbol="A", side="SELL", qty=100, price=110.0, fee=0, tax=0,
              filled_at=d0.isoformat(timespec="seconds")),  # 10 天前 +1000
    ])
    pnl_30 = reports.last_n_days_realized_pnl(fills_path, 30, now=now)
    # 只有 d0 那筆在窗內：50 股 cost @ 100，但其實是 100 股
    # avg_cost = 100（fee=0），SELL 100 股 @ 110 → realized = 100*110 - 100*100 = +1000
    assert pnl_30 == pytest.approx(1000.0, abs=0.01)


def test_win_rate_basic():
    fills = [
        {"realized_pnl": 100.0},   # win
        {"realized_pnl": -50.0},   # lose
        {"realized_pnl": 200.0},   # win
        {"realized_pnl": 0.0},     # 不算（buy 或無實現）
        {},                         # 無欄位
    ]
    result = reports.win_rate(fills)
    assert result["total"] == 3
    assert result["win"] == 2
    assert result["rate"] == pytest.approx(2 / 3)


def test_equity_curve_append_writes_jsonl(tmp_path):
    """equity_curve_append 寫一行欄位完整 jsonl。"""
    eq_path = tmp_path / "equity_curve.jsonl"
    reports.equity_curve_append(
        eq_path,
        time="2026-05-05T10:00:00",
        cash=900_000.0,
        position_value=100_000.0,
        equity=1_000_000.0,
        total_realized_pnl=0.0,
    )
    with open(eq_path, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["time"] == "2026-05-05T10:00:00"
    assert rec["cash"] == 900_000.0
    assert rec["equity"] == 1_000_000.0
    assert rec["total_realized_pnl"] == 0.0


def test_per_fill_exit_context_tracks_entry_price_and_holding_time(tmp_path):
    fills_path = tmp_path / "fills.jsonl"
    _write_fills(fills_path, [
        _fill(symbol="A", side="BUY", qty=100, price=100.0, fee=0,
              filled_at="2026-05-05T09:30:00", fill_id="buy1"),
        _fill(symbol="A", side="SELL", qty=50, price=110.0, fee=0, tax=0,
              filled_at="2026-05-06T10:45:00", fill_id="sell1"),
    ])

    ctx = reports._per_fill_exit_context(fills_path)["sell1"]

    assert ctx["realized_pnl"] == pytest.approx(500.0)
    assert ctx["entry_avg_cost"] == pytest.approx(100.0)
    assert ctx["entry_time"] == "2026-05-05T09:30:00"
    assert ctx["holding_period"] == "1天1小時"


def test_enrich_fill_with_order_context_reads_advisor_notes(tmp_path):
    orders_path = tmp_path / "orders.jsonl"
    order = {
        "order_id": "ord-1",
        "symbol": "2330.TW",
        "side": "BUY",
        "type": "MKT",
        "qty": 100,
        "status": "FILLED",
        "notes": '[advisor] {"reason":"突破 entry_zone","source_pools":["short_term"],"stop_loss":570}',
    }
    orders_path.write_text(json.dumps(order, ensure_ascii=False) + "\n", encoding="utf-8")

    enriched = reports.enrich_fill_with_order_context(
        {"order_id": "ord-1", "symbol": "2330.TW", "side": "BUY"},
        orders_path=orders_path,
    )

    assert enriched["trade_reason"] == "突破 entry_zone"
    assert enriched["source_pools"] == ["short_term"]
    assert enriched["planned_stop_loss"] == 570


# ═════════════════════════════════════════════════════
# 格式化 (3)
# ═════════════════════════════════════════════════════

def test_fmt_realtime_fill_buy():
    fill = _fill(symbol="2330.TW", side="BUY", qty=100, price=600.0, fee=85.0)
    fill["trade_reason"] = "短線突破且 cross_signals 命中"
    fill["source_pools"] = ["short_term", "cross_signals"]
    fill["planned_stop_loss"] = 570.0
    snapshot = {"account": {"cash": 939_915.0, "total_realized_pnl": 0.0}}
    text = reports.fmt_realtime_fill(fill, snapshot)
    assert "2330.TW" in text
    assert "買進" in text
    assert "100" in text
    assert "600" in text
    assert "買進理由：短線突破且 cross_signals 命中" in text
    assert "觸發來源：short_term, cross_signals" in text
    assert "計畫停損：570.00" in text
    assert "939,915" in text


def test_fmt_realtime_fill_sell():
    fill = _fill(symbol="2330.TW", side="SELL", qty=50, price=620.0, fee=44.0, tax=93.0)
    fill["realized_pnl"] = 850.0
    fill["entry_avg_cost"] = 602.0
    fill["entry_time"] = "2026-05-05T09:30:00"
    fill["holding_period"] = "1天0小時"
    fill["exit_reason"] = "[auto-stop] trail raised from 590.0"
    snapshot = {
        "account": {
            "cash": 970_000.0,
            "total_realized_pnl": 1_500.0,
            "positions": {
                "2330.TW": {
                    "qty": 50,
                    "avg_cost": 602.0,
                    "quote": 630.0,
                    "unrealized_pnl": 1400.0,
                    "unrealized_pct": 4.65,
                }
            },
        }
    }
    text = reports.fmt_realtime_fill(fill, snapshot)
    assert "賣出" in text
    assert "稅" in text
    assert "出場原因：移動停損觸發；這筆原本的保護停損曾從 590.00 上調" in text
    assert "開單均價：602.00　入場：2026-05-05T09:30:00　持有：1天0小時" in text
    assert "本筆已實現：+850（盈利）" in text
    assert "持倉浮盈/浮虧：2330.TW +1,400 / +4.65% @ 630.00" in text
    assert "+1,500" in text


def test_fmt_realtime_fill_humanizes_common_exit_reasons():
    snapshot = {"account": {"cash": 970_000.0, "total_realized_pnl": 1_500.0}}

    forced = _fill(symbol="2330.TW", side="SELL", qty=50, price=620.0, fee=44.0, tax=93.0)
    forced["exit_reason"] = "forced_sell_out_of_pool_loser"
    forced["realized_pnl"] = -300.0
    forced_text = reports.fmt_realtime_fill(forced, snapshot)
    assert "標的已退出觀察池且仍處於虧損" in forced_text
    assert "forced_sell_out_of_pool_loser" not in forced_text

    risk_off = _fill(symbol="2317.TW", side="SELL", qty=100, price=100.0, fee=20.0, tax=30.0)
    risk_off["exit_reason"] = "risk_off"
    risk_off["realized_pnl"] = 120.0
    risk_text = reports.fmt_realtime_fill(risk_off, snapshot)
    assert "風險狀態轉弱" in risk_text
    assert "risk_off" not in risk_text

    synced_stop = _fill(symbol="6415.TW", side="SELL", qty=100, price=444.6, fee=63.0, tax=133.0)
    synced_stop["exit_reason"] = "[auto-stop] buy_synced sl=444.6"
    synced_stop["realized_pnl"] = -500.0
    stop_text = reports.fmt_realtime_fill(synced_stop, snapshot)
    assert "保護性停損觸發" in stop_text
    assert "觸發價 444.60" in stop_text
    assert "[auto-stop]" not in stop_text


def test_fmt_daily_report_no_fills():
    text = reports.fmt_daily_report(
        date="2026-05-05",
        fills_today=[],
        positions={},
        account={"cash": 1_000_000, "total_realized_pnl": 0, "equity": 1_000_000},
        today_pnl=0.0,
    )
    assert "2026-05-05" in text
    assert "今日無成交" in text
    assert "無持倉" in text


def test_fmt_daily_report_includes_pending_and_advisor_stats():
    text = reports.fmt_daily_report(
        date="2026-05-07",
        fills_today=[{
            "filled_at": "2026-05-07T09:05:37",
            "symbol": "2330.TW",
            "side": "SELL",
            "qty": 120,
            "price": 2247.75,
            "fee": 384.36,
            "tax": 809.19,
        }],
        positions={"3034.TW": {"qty": 400, "avg_cost": 442.13}},
        account={"cash": 63_032, "total_realized_pnl": -1250.33, "equity": 998_750},
        today_pnl=-1250.33,
        pending_orders=[{"symbol": "2317.TW", "side": "BUY", "type": "LMT", "qty": 700, "price": 241.9}],
        advisor_stats={
            "decision_round": 29,
            "dispatch_round": 483,
            "risk_reject": 4,
            "latest_summary": "維持現有持倉",
        },
    )
    assert "每日交易彙總" in text
    assert "一、今日成交" in text
    assert "09:05 賣出 2330.TW" in text
    assert "PENDING 1" in text
    assert "decision_round=29" in text
    assert "risk_reject=4" in text
    assert "維持現有持倉" in text


def test_fmt_weekly_report_with_equity_delta():
    text = reports.fmt_weekly_report(
        week_range="2026-05-04 ~ 2026-05-10",
        fills_week=[{"realized_pnl": 100}, {"realized_pnl": -50}],
        week_realized_pnl=50.0,
        week_win_rate={"total": 2, "win": 1, "rate": 0.5},
        lifetime_stats={
            "total_realized_pnl": 1000.0,
            "win_rate": 0.6,
            "equity_start": 1_000_000.0,
            "equity_end": 1_002_000.0,
        },
    )
    assert "2026-05-04" in text
    assert "本週成交 2" in text
    assert "+50" in text
    assert "50.0%" in text
    assert "1,000,000" in text
    assert "1,002,000" in text
    assert "+0.20%" in text  # delta_pct


# ═════════════════════════════════════════════════════
# CLI (3)
# ═════════════════════════════════════════════════════

def test_cli_daily_no_send_prints_to_stdout(tmp_path, capsys, monkeypatch):
    """daily --no-send 不呼叫 _send_dm，只 print 報告到 stdout。"""
    fills_path = tmp_path / "fills.jsonl"
    _write_fills(fills_path, [])  # 空 fills
    monkeypatch.setattr(reports, "DEFAULT_FILLS_PATH", fills_path)
    monkeypatch.setattr(reports, "DEFAULT_SIM_CONFIG", tmp_path / "missing.yaml")  # 用 1M 預設
    with patch.object(reports, "_send_dm") as mock_send:
        rc = reports.main(["daily", "--no-send", "--date", "2026-05-05"])
    assert rc == 0
    mock_send.assert_not_called()
    out = capsys.readouterr().out
    assert "2026-05-05" in out
    assert "今日無成交" in out


def test_cli_daily_invokes_send_dm(tmp_path, monkeypatch):
    """daily（無 --no-send）會呼叫 _send_dm。"""
    fills_path = tmp_path / "fills.jsonl"
    _write_fills(fills_path, [])
    monkeypatch.setattr(reports, "DEFAULT_FILLS_PATH", fills_path)
    monkeypatch.setattr(reports, "DEFAULT_SIM_CONFIG", tmp_path / "missing.yaml")
    with patch.object(reports, "_send_dm", return_value=True) as mock_send:
        rc = reports.main(["daily", "--date", "2026-05-05", "--force"])
    assert rc == 0
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    title = call_args[0][0]
    assert "📅" in title
    assert "2026-05-05" in title


def test_cli_weekly_no_send_prints_summary(tmp_path, capsys, monkeypatch):
    """weekly --no-send 印週報到 stdout。"""
    fills_path = tmp_path / "fills.jsonl"
    eq_path = tmp_path / "equity_curve.jsonl"
    _write_fills(fills_path, [])
    eq_path.touch()
    monkeypatch.setattr(reports, "DEFAULT_FILLS_PATH", fills_path)
    monkeypatch.setattr(reports, "DEFAULT_EQUITY_CURVE_PATH", eq_path)
    monkeypatch.setattr(reports, "DEFAULT_SIM_CONFIG", tmp_path / "missing.yaml")
    with patch.object(reports, "_send_dm") as mock_send:
        rc = reports.main(["weekly", "--no-send"])
    assert rc == 0
    mock_send.assert_not_called()
    out = capsys.readouterr().out
    assert "週報" in out
    assert "本週成交" in out


# ═════════════════════════════════════════════════════
# Cross-validation (3) — engine vs _position_math 必須 byte-equal
# ═════════════════════════════════════════════════════
#
# 這 3 項是 PLAN 風險點 2 的把關：reports CLI 從 fills.jsonl replay 出的持倉、
# 累計 realized 必須跟 engine.account 完全一致，否則日報會跟即時通知飄移。

def _engine_apply_dict_fill(engine: MatchEngine, fill_dict: dict) -> None:
    """把 fill dict 餵給 engine.apply_fills（要先轉 Fill model）。"""
    fill = Fill(
        order_id=fill_dict["order_id"],
        symbol=fill_dict["symbol"],
        side=OrderSide(fill_dict["side"]),
        qty=int(fill_dict["qty"]),
        price=float(fill_dict["price"]),
        fee=float(fill_dict["fee"]),
        tax=float(fill_dict["tax"]),
        fill_id=fill_dict["fill_id"],
        filled_at=fill_dict["filled_at"],
    )
    engine.apply_fills([fill])


def test_cross_validation_partial_close(tmp_path):
    """BUY 100 → SELL 50（部分平倉）：engine 與 _position_math 結果一致。"""
    e = _make_engine(tmp_path)
    fills_dict = [
        _fill(symbol="2330.TW", side="BUY", qty=100, price=600.0, fee=85.0),
        _fill(symbol="2330.TW", side="SELL", qty=50, price=620.0, fee=44.0, tax=93.0),
    ]
    for fd in fills_dict:
        _engine_apply_dict_fill(e, fd)

    pure = _pure_replay(fills_dict)

    # positions 一致
    pos_engine = e.account.positions["2330.TW"]
    pos_pure = pure["positions"]["2330.TW"]
    assert pos_engine.qty == pos_pure["qty"]
    assert pos_engine.avg_cost == pytest.approx(pos_pure["avg_cost"])
    # total_realized_pnl 一致
    assert e.account.total_realized_pnl == pytest.approx(pure["total_realized_pnl"])
    # cash_delta 對齊（engine cash 從 initial_cash 開始）
    cash_delta_engine = e.account.cash - e.account.initial_cash
    assert cash_delta_engine == pytest.approx(pure["cash_delta"])


def test_cross_validation_full_close(tmp_path):
    """BUY 100 → SELL 100（全平倉）：positions 為空、realized 一致。"""
    e = _make_engine(tmp_path)
    fills_dict = [
        _fill(symbol="2330.TW", side="BUY", qty=100, price=600.0, fee=85.0),
        _fill(symbol="2330.TW", side="SELL", qty=100, price=620.0, fee=88.0, tax=186.0),
    ]
    for fd in fills_dict:
        _engine_apply_dict_fill(e, fd)
    pure = _pure_replay(fills_dict)

    # 全平倉：兩邊 positions 都不含此 symbol
    assert "2330.TW" not in e.account.positions
    assert "2330.TW" not in pure["positions"]
    # realized 一致
    assert e.account.total_realized_pnl == pytest.approx(pure["total_realized_pnl"])
    # cash_delta 一致
    assert (e.account.cash - e.account.initial_cash) == pytest.approx(pure["cash_delta"])


def test_cross_validation_multi_symbol_interleaved(tmp_path):
    """多 symbol 交錯 BUY/SELL：各 symbol 持倉與 total_realized 一致。"""
    e = _make_engine(tmp_path)
    fills_dict = [
        _fill(symbol="2330.TW", side="BUY", qty=100, price=600.0, fee=85.0),
        _fill(symbol="0050.TW", side="BUY", qty=200, price=130.0, fee=37.0),
        _fill(symbol="2330.TW", side="BUY", qty=50, price=590.0, fee=42.0),
        _fill(symbol="0050.TW", side="SELL", qty=100, price=135.0, fee=19.0, tax=40.0),
        _fill(symbol="2330.TW", side="SELL", qty=80, price=610.0, fee=69.0, tax=146.0),
    ]
    for fd in fills_dict:
        _engine_apply_dict_fill(e, fd)
    pure = _pure_replay(fills_dict)

    for sym in ["2330.TW", "0050.TW"]:
        pos_e = e.account.positions[sym]
        pos_p = pure["positions"][sym]
        assert pos_e.qty == pos_p["qty"], f"{sym} qty mismatch"
        assert pos_e.avg_cost == pytest.approx(pos_p["avg_cost"]), f"{sym} avg_cost mismatch"
    assert e.account.total_realized_pnl == pytest.approx(pure["total_realized_pnl"])
    assert (e.account.cash - e.account.initial_cash) == pytest.approx(pure["cash_delta"])


# ═════════════════════════════════════════════════════
# 邊界 (3)
# ═════════════════════════════════════════════════════

def test_iter_fills_skips_corrupted_lines(tmp_path):
    """壞行 JSON / 空行 → 略過不炸（與 nl_query._tail_jsonl 一致策略）。"""
    fills_path = tmp_path / "fills.jsonl"
    fills_path.write_text(
        '{"symbol": "A", "side": "BUY", "qty": 100, "price": 600, "fee": 0, "tax": 0, '
        '"fill_id": "fa", "filled_at": "2026-05-05T10:00:00"}\n'
        '\n'  # 空行
        'NOT VALID JSON\n'  # 壞行
        '{"symbol": "B", "side": "BUY", "qty": 50, "price": 100, "fee": 0, "tax": 0, '
        '"fill_id": "fb", "filled_at": "2026-05-05T11:00:00"}\n',
        encoding="utf-8",
    )
    fills = list(reports.iter_fills(fills_path))
    assert len(fills) == 2
    assert fills[0]["symbol"] == "A"
    assert fills[1]["symbol"] == "B"


def test_today_realized_pnl_handles_missing_file(tmp_path):
    """檔案不存在時 → 0.0，不炸。"""
    missing = tmp_path / "no_such_file.jsonl"
    pnl = reports.today_realized_pnl(missing)
    assert pnl == 0.0


def test_iter_fills_in_period_cross_day_boundary(tmp_path):
    """跨日界：23:59 不在「明日」窗內、00:01 在「明日」窗內。"""
    fills_path = tmp_path / "fills.jsonl"
    _write_fills(fills_path, [
        _fill(symbol="A", side="BUY", qty=10, price=100,
              filled_at="2026-05-04T23:59:00"),
        _fill(symbol="B", side="BUY", qty=10, price=100,
              filled_at="2026-05-05T00:01:00"),
    ])
    start = datetime(2026, 5, 5)
    end = datetime(2026, 5, 6)
    fills = reports.iter_fills_in_period(fills_path, start, end)
    assert len(fills) == 1
    assert fills[0]["symbol"] == "B"


# ═════════════════════════════════════════════════════
# 自包執行（不靠 pytest）
# ═════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
