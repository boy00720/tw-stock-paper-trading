"""sim/tests/test_advisor_new_buy_gates.py — 新倉失控防火牆測試

驗證：
- daily_new_position_cap 正確計入今日已買且仍持有的 symbol
- morning_cooldown_gate 只在 09:00–09:45 擋新 symbol buy
- loss_probation_gate 擋近期已實現虧損標的新買
- no_new_positions_gate 在保守觀察期只擋新 symbol buy，不動 sell/hold/既有持倉
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor._types import Decision
from sim.advisor.runner import (
    _clamp_qty_by_risk,
    _gate_daily_new_position_cap,
    _gate_leveraged_etf_exposure,
    _gate_low_quality_buys,
    _gate_morning_new_buys,
    _gate_no_new_positions_mode,
    _gate_recent_loss_probation,
    _load_sector_map,
    _new_position_mode,
)
from sim.engine import MatchEngine
from sim.models import Position


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_watchlist(path: Path, *, short_term=None, cross=None, leveraged_etf=None) -> None:
    """寫出 load_watchlist_pools 可讀的 watchlist json（formal_pools + auxiliary_signals + cross_signals）。"""
    data = {
        "formal_pools": {"short_term": short_term or []},
        "auxiliary_signals": {"leveraged_etf": leveraged_etf or []},
        "cross_signals": cross or {},
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _buy(sym: str, qty: int = 1000) -> Decision:
    return Decision(
        action="buy", symbol=sym, qty=qty, type="MKT",
        stop_loss=95.0, reason=f"test buy {sym}",
    )


def _sell(sym: str, qty: int = 1000) -> Decision:
    return Decision(action="sell", symbol=sym, qty=qty, type="MKT", reason="risk_off")


def _engine_with_positions(*symbols: str) -> MatchEngine:
    e = MatchEngine()
    for sym in symbols:
        e.account.positions[sym] = Position(symbol=sym, qty=1000, avg_cost=100.0)
    return e


def test_leveraged_etf_pool_can_pass_quality_gate(tmp_dir):
    """槓反 ETF 可透過獨立子池進 sim 候選，但不混入一般 short_term。"""
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, leveraged_etf=[{
        "symbol": "00631L.TW", "score": 90, "min_score": 85,
        "direction": "long", "underlying": "tw50",
    }])

    out, gated = _gate_low_quality_buys([_buy("00631L.TW")], wl)

    assert out[0].action == "buy"
    assert gated == []


def test_leveraged_etf_blocks_opposite_direction_same_underlying(tmp_dir):
    """同 underlying 正反 ETF 不可同時持有/買入。"""
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, leveraged_etf=[
        {"symbol": "00631L.TW", "direction": "long", "underlying": "tw50", "score": 90},
        {"symbol": "00632R.TW", "direction": "inverse", "underlying": "tw50", "score": 90},
    ])
    e = _engine_with_positions("00631L.TW")

    out, gated = _gate_leveraged_etf_exposure([_buy("00632R.TW")], e, wl)

    assert out[0].action == "hold"
    assert gated[0]["symbol"] == "00632R.TW"
    assert gated[0]["blocked_by_direction"] == "long"


def test_leveraged_etf_risk_budget_is_reduced(tmp_dir):
    """槓反 ETF 使用較小 risk budget，qty clamp 比一般股票更保守。"""
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, leveraged_etf=[{
        "symbol": "00631L.TW", "score": 90, "min_score": 85,
        "direction": "long", "underlying": "tw50", "risk_budget_multiplier": 0.5,
    }])
    d = _buy("00631L.TW", qty=5000)
    d.stop_loss = 95.0
    out, adjusted = _clamp_qty_by_risk(
        [d], {"00631L.TW": 100.0}, MatchEngine(), watchlist_path=wl,
    )

    assert out[0].qty == 2000
    assert adjusted[0]["is_leveraged_etf"] is True
    assert adjusted[0]["risk_budget_multiplier"] == 0.5


# ─── daily_new_position_cap ───


def test_daily_cap_counts_today_buys_even_if_still_held(tmp_dir):
    """5/29 類型 bug：今日已買且仍持有的 symbol 仍要計入新倉數。"""
    now = datetime(2026, 5, 29, 9, 32)
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "2881.TW", "side": "BUY", "qty": 1700, "price": 110.0,
         "filled_at": "2026-05-29T09:04:02", "fill_id": "f1"},
        {"symbol": "2377.TW", "side": "BUY", "qty": 1500, "price": 123.6,
         "filled_at": "2026-05-29T09:09:46", "fill_id": "f2"},
        {"symbol": "2891.TW", "side": "BUY", "qty": 100, "price": 58.0,
         "filled_at": "2026-05-29T09:20:00", "fill_id": "f3"},
    ])
    e = _engine_with_positions("2881.TW", "2377.TW")

    out, gated = _gate_daily_new_position_cap([_buy("2303.TW")], e, fills, now=now)

    assert out[0].action == "hold"
    assert "daily_new_position_cap" in out[0].reason
    assert gated[0]["today_new_buys"] == 3
    assert set(gated[0]["today_new_symbols"]) == {"2881.TW", "2377.TW", "2891.TW"}


def test_daily_cap_allows_existing_position_add(tmp_dir):
    now = datetime(2026, 5, 29, 10, 0)
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "2881.TW", "side": "BUY", "qty": 1700, "price": 110.0,
         "filled_at": "2026-05-29T09:04:02", "fill_id": "f1"},
        {"symbol": "2377.TW", "side": "BUY", "qty": 1500, "price": 123.6,
         "filled_at": "2026-05-29T09:09:46", "fill_id": "f2"},
        {"symbol": "2891.TW", "side": "BUY", "qty": 100, "price": 58.0,
         "filled_at": "2026-05-29T09:20:00", "fill_id": "f3"},
    ])
    e = _engine_with_positions("2881.TW")

    out, gated = _gate_daily_new_position_cap([_buy("2881.TW")], e, fills, now=now)

    assert out[0].action == "buy"
    assert gated == []


def test_daily_cap_does_not_count_start_of_day_holding_adds(tmp_dir):
    """昨天已持有、今天加倉，不應消耗今日新 symbol 額度。"""
    now = datetime(2026, 5, 29, 10, 0)
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "2881.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-28T09:00:00", "fill_id": "old-b"},
        {"symbol": "2881.TW", "side": "BUY", "qty": 500, "price": 101.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-29T09:10:00", "fill_id": "add-b"},
        {"symbol": "2377.TW", "side": "BUY", "qty": 1000, "price": 120.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-29T09:15:00", "fill_id": "new-1"},
        {"symbol": "2891.TW", "side": "BUY", "qty": 1000, "price": 58.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-29T09:20:00", "fill_id": "new-2"},
        {"symbol": "2303.TW", "side": "BUY", "qty": 1000, "price": 140.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-29T09:25:00", "fill_id": "new-3"},
    ])
    e = _engine_with_positions("2881.TW")

    out, gated = _gate_daily_new_position_cap([_buy("2356.TW")], e, fills, now=now)

    assert out[0].action == "hold"
    assert gated[0]["today_new_buys"] == 3
    assert set(gated[0]["today_new_symbols"]) == {"2377.TW", "2891.TW", "2303.TW"}
    assert "2881.TW" in gated[0]["start_of_day_held_symbols"]
    assert "2881.TW" not in gated[0]["today_new_symbols"]


# ─── morning_cooldown_gate ───


def test_morning_gate_blocks_new_buy_during_opening_window():
    e = MatchEngine()
    now = datetime(2026, 5, 29, 9, 10)

    out, gated = _gate_morning_new_buys([_buy("2303.TW")], e, now=now)

    assert out[0].action == "hold"
    assert "morning_cooldown" in out[0].reason
    assert gated[0]["block_until"] == "09:45"


def test_morning_gate_passes_after_0945():
    e = MatchEngine()
    now = datetime(2026, 5, 29, 9, 45)

    out, gated = _gate_morning_new_buys([_buy("2303.TW")], e, now=now)

    assert out[0].action == "buy"
    assert gated == []


def test_morning_gate_does_not_block_existing_position_or_sell():
    e = _engine_with_positions("2303.TW")
    now = datetime(2026, 5, 29, 9, 10)
    decs = [_buy("2303.TW"), _sell("2881.TW"), Decision(action="hold", reason="watch")]

    out, gated = _gate_morning_new_buys(decs, e, now=now)

    assert [d.action for d in out] == ["buy", "sell", "hold"]
    assert gated == []


# ─── loss_probation_gate ───


def test_loss_probation_blocks_symbol_with_recent_realized_loss(tmp_dir):
    fills = tmp_dir / "fills.jsonl"
    now = datetime(2026, 5, 29, 10, 0)
    _write_jsonl(fills, [
        {"symbol": "2303.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-28T09:00:00", "fill_id": "b1"},
        {"symbol": "2303.TW", "side": "SELL", "qty": 1000, "price": 95.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-28T10:00:00", "fill_id": "s1"},
    ])

    out, gated = _gate_recent_loss_probation([_buy("2303.TW")], MatchEngine(), fills, now=now)

    assert out[0].action == "hold"
    assert "loss_probation" in out[0].reason
    assert gated[0]["symbol"] == "2303.TW"
    assert gated[0]["last_loss_pnl"] == -5000.0


def test_loss_probation_ignores_old_loss(tmp_dir):
    fills = tmp_dir / "fills.jsonl"
    now = datetime(2026, 5, 29, 10, 0)
    _write_jsonl(fills, [
        {"symbol": "2303.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-20T09:00:00", "fill_id": "b1"},
        {"symbol": "2303.TW", "side": "SELL", "qty": 1000, "price": 95.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-20T10:00:00", "fill_id": "s1"},
    ])

    out, gated = _gate_recent_loss_probation([_buy("2303.TW")], MatchEngine(), fills, now=now)

    assert out[0].action == "buy"
    assert gated == []


def test_loss_probation_does_not_block_existing_position_add(tmp_dir):
    fills = tmp_dir / "fills.jsonl"
    now = datetime(2026, 5, 29, 10, 0)
    _write_jsonl(fills, [
        {"symbol": "2303.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-28T09:00:00", "fill_id": "b1"},
        {"symbol": "2303.TW", "side": "SELL", "qty": 1000, "price": 95.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-05-28T10:00:00", "fill_id": "s1"},
    ])
    e = _engine_with_positions("2303.TW")

    out, gated = _gate_recent_loss_probation([_buy("2303.TW")], e, fills, now=now)

    assert out[0].action == "buy"
    assert gated == []


# ─── no_new_positions_gate ───


def test_no_new_positions_mode_blocks_only_new_symbol_buy():
    e = _engine_with_positions("2891.TW")
    decs = [_buy("2303.TW"), _buy("2891.TW"), _sell("2881.TW"), Decision(action="hold", reason="watch")]

    out, gated = _gate_no_new_positions_mode(decs, e, enabled=True)

    assert [d.action for d in out] == ["hold", "buy", "sell", "hold"]
    assert "no_new_positions_mode" in out[0].reason
    assert len(gated) == 1
    assert gated[0]["symbol"] == "2303.TW"


def test_no_new_positions_mode_can_be_disabled():
    out, gated = _gate_no_new_positions_mode([_buy("2303.TW")], MatchEngine(), enabled=False)

    assert out[0].action == "buy"
    assert gated == []


# ─── new_position_mode 解析 ───


def test_new_position_mode_defaults_defensive(monkeypatch):
    monkeypatch.delenv("SIM_ADVISOR_NEW_POSITION_MODE", raising=False)
    monkeypatch.delenv("SIM_ADVISOR_NO_NEW_POSITIONS_MODE", raising=False)
    assert _new_position_mode() == "defensive"


def test_new_position_mode_probe_via_env(monkeypatch):
    monkeypatch.setenv("SIM_ADVISOR_NEW_POSITION_MODE", "probe")
    assert _new_position_mode() == "probe"


def test_new_position_mode_unknown_falls_back_defensive(monkeypatch):
    monkeypatch.setenv("SIM_ADVISOR_NEW_POSITION_MODE", "yolo")
    assert _new_position_mode() == "defensive"


def test_new_position_mode_legacy_disable_maps_to_probe(monkeypatch):
    """未設新開關、但舊 NO_NEW_POSITIONS=0 → 視為 probe（向後相容）。"""
    monkeypatch.delenv("SIM_ADVISOR_NEW_POSITION_MODE", raising=False)
    monkeypatch.setenv("SIM_ADVISOR_NO_NEW_POSITIONS_MODE", "0")
    assert _new_position_mode() == "probe"


def test_no_new_gate_auto_enabled_in_defensive(monkeypatch):
    """預設防守：no_new gate 不帶 enabled 時，新 symbol buy 仍被擋。"""
    monkeypatch.delenv("SIM_ADVISOR_NEW_POSITION_MODE", raising=False)
    monkeypatch.delenv("SIM_ADVISOR_NO_NEW_POSITIONS_MODE", raising=False)
    out, gated = _gate_no_new_positions_mode([_buy("2303.TW")], MatchEngine())
    assert out[0].action == "hold"
    assert len(gated) == 1


def test_no_new_gate_auto_disabled_in_probe(monkeypatch):
    """probe 模式：no_new gate 自動關閉，放行交給動態 daily cap。"""
    monkeypatch.setenv("SIM_ADVISOR_NEW_POSITION_MODE", "probe")
    out, gated = _gate_no_new_positions_mode([_buy("2303.TW")], MatchEngine())
    assert out[0].action == "buy"
    assert gated == []


# ─── probe 動態額度（daily_new_position_cap mode=probe） ───


def test_probe_allows_first_new_position_regardless_of_score(tmp_dir):
    """probe 基礎額度：今日第 1 筆新倉即使分數低也放行（已過前面品質 gate）。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, short_term=[{"symbol": "2303.TW", "score": 50}])
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], MatchEngine(), fills,
        watchlist_path=wl, now=now, mode="probe",
    )

    assert out[0].action == "buy"
    assert gated == []


def test_probe_unlocks_second_with_high_score_and_flat_realized(tmp_dir):
    """第 2 筆：score≥90 且當日 realized≥0 → 解鎖。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, short_term=[{"symbol": "2330.TW", "score": 92}])
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "1101.TW", "side": "BUY", "qty": 1000, "price": 30.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:50:00", "fill_id": "n1"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2330.TW")], _engine_with_positions("1101.TW"), fills,
        watchlist_path=wl, now=now, mode="probe",
    )

    assert out[0].action == "buy"
    assert gated == []


def test_probe_blocks_second_when_low_conf(tmp_dir):
    """第 2 筆：分數不足且不在 cross → 擋下 second_locked_low_conf。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, short_term=[{"symbol": "2330.TW", "score": 70}])
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "1101.TW", "side": "BUY", "qty": 1000, "price": 30.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:50:00", "fill_id": "n1"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2330.TW")], _engine_with_positions("1101.TW"), fills,
        watchlist_path=wl, now=now, mode="probe",
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "second_locked_low_conf"
    assert gated[0]["slot"] == 2


def test_probe_blocks_second_when_today_realized_negative(tmp_dir):
    """第 2 筆：即使高信心(cross)，當日 realized<0 仍擋下 second_locked_realized_negative。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, cross={"2330.TW": {"score": 99}})
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        # 昨日買入、今日虧損平倉 → 當日 realized < 0；該 symbol 不消耗今日新倉額度
        {"symbol": "1234.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-04T09:00:00", "fill_id": "old"},
        {"symbol": "1234.TW", "side": "SELL", "qty": 1000, "price": 90.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:30:00", "fill_id": "loss"},
        # 今日第 1 筆新倉
        {"symbol": "1101.TW", "side": "BUY", "qty": 1000, "price": 30.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:50:00", "fill_id": "n1"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2330.TW")], _engine_with_positions("1101.TW"), fills,
        watchlist_path=wl, now=now, mode="probe",
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "second_locked_realized_negative"
    assert gated[0]["today_realized"] == -10000.0


def test_probe_round_cap_one_new_symbol_per_round(tmp_dir):
    """單輪最多 1 筆：同一輪兩個高信心新 symbol，第 2 個被 round_cap 擋。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, short_term=[
        {"symbol": "AAA.TW", "score": 99}, {"symbol": "BBB.TW", "score": 99},
    ])
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [])

    out, gated = _gate_daily_new_position_cap(
        [_buy("AAA.TW"), _buy("BBB.TW")], MatchEngine(), fills,
        watchlist_path=wl, now=now, mode="probe",
    )

    assert out[0].action == "buy"
    assert out[1].action == "hold"
    assert gated[0]["block_reason"] == "round_cap"


def test_probe_hard_cap_at_three(tmp_dir):
    """硬上限 3：今日已 3 筆新倉，第 4 筆即使高信心也擋 hard_cap。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, cross={"2303.TW": {"score": 99}})
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "A.TW", "side": "BUY", "qty": 1000, "price": 50.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:05:00", "fill_id": "f1"},
        {"symbol": "B.TW", "side": "BUY", "qty": 1000, "price": 50.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:10:00", "fill_id": "f2"},
        {"symbol": "C.TW", "side": "BUY", "qty": 1000, "price": 50.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:15:00", "fill_id": "f3"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], MatchEngine(), fills,
        watchlist_path=wl, now=now, mode="probe",
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "hard_cap"
    assert gated[0]["slot"] == 4


def _slot3_setup(tmp_dir):
    """共用：今日已 2 筆新倉 A.TW/B.TW（持倉中、成本 100），候選 2303.TW 走 cross。"""
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, cross={"2303.TW": {"score": 99}})
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "A.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:05:00", "fill_id": "f1"},
        {"symbol": "B.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:10:00", "fill_id": "f2"},
    ])
    return _engine_with_positions("A.TW", "B.TW"), fills, wl


def test_probe_unlocks_third_when_all_conditions_met(tmp_dir):
    """第 3 筆：高信心+realized≥0+大盤不弱+新倉健康+集中度OK → 解鎖。"""
    now = datetime(2026, 6, 5, 10, 0)
    e, fills, wl = _slot3_setup(tmp_dir)

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], e, fills, watchlist_path=wl, now=now, mode="probe",
        quote_cache={"A.TW": 101.0, "B.TW": 101.0}, baseline_trend=0.005,
    )

    assert out[0].action == "buy"
    assert gated == []


def test_probe_blocks_third_low_conf(tmp_dir):
    """第 3 筆：score<95 且非 cross → third_locked_low_conf。"""
    now = datetime(2026, 6, 5, 10, 0)
    e, fills, wl = _slot3_setup(tmp_dir)
    _write_watchlist(wl, short_term=[{"symbol": "2303.TW", "score": 90}])  # 90<95

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], e, fills, watchlist_path=wl, now=now, mode="probe",
        quote_cache={"A.TW": 101.0, "B.TW": 101.0}, baseline_trend=0.005,
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "third_locked_low_conf"


def test_probe_blocks_third_market_weak(tmp_dir):
    """第 3 筆：0050 當日 < -0.3% → third_locked_market_weak。"""
    now = datetime(2026, 6, 5, 10, 0)
    e, fills, wl = _slot3_setup(tmp_dir)

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], e, fills, watchlist_path=wl, now=now, mode="probe",
        quote_cache={"A.TW": 101.0, "B.TW": 101.0}, baseline_trend=-0.01,
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "third_locked_market_weak"


def test_probe_blocks_third_position_unhealthy(tmp_dir):
    """第 3 筆：當日新倉有一檔浮虧 >3% → third_locked_position_unhealthy。"""
    now = datetime(2026, 6, 5, 10, 0)
    e, fills, wl = _slot3_setup(tmp_dir)

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], e, fills, watchlist_path=wl, now=now, mode="probe",
        quote_cache={"A.TW": 101.0, "B.TW": 90.0}, baseline_trend=0.005,  # B -10%
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "third_locked_position_unhealthy"
    assert any("B.TW" in u for u in gated[0]["unhealthy"])


def test_probe_blocks_third_concentration(tmp_dir):
    """第 3 筆：現金/權益 < 10% → third_locked_concentration。"""
    now = datetime(2026, 6, 5, 10, 0)
    e, fills, wl = _slot3_setup(tmp_dir)
    e.account.cash = 10_000.0  # 持倉市值 ~202,000，現金比 ~4.7%

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], e, fills, watchlist_path=wl, now=now, mode="probe",
        quote_cache={"A.TW": 101.0, "B.TW": 101.0}, baseline_trend=0.005,
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "third_locked_concentration"


def test_defensive_mode_keeps_flat_cap_behavior(tmp_dir):
    """defensive/flat：第 2 筆不受 probe 解鎖條件限制（slot≤3 即放行），維持既有語義。"""
    now = datetime(2026, 6, 5, 10, 0)
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "1101.TW", "side": "BUY", "qty": 1000, "price": 30.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:50:00", "fill_id": "n1"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2330.TW")], _engine_with_positions("1101.TW"), fills,
        now=now, mode="defensive",
    )

    assert out[0].action == "buy"
    assert gated == []


# ─── probe 產業不重疊（sector overlap） ───


def test_load_sector_map_reads_universe():
    """_load_sector_map 從 stock_universe.yaml 讀出 symbol→sector（資料源連通）。"""
    m = _load_sector_map()
    assert m.get("2330.TW") == "半導體"
    assert m.get("2303.TW") == "半導體"
    assert m.get("2891.TW") == "金融"


def test_probe_blocks_second_when_sector_overlap(tmp_dir):
    """第 2 筆：高信心+realized≥0 但與既有持倉同產業 → second_locked_sector_overlap。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, short_term=[{"symbol": "2330.TW", "score": 92}])
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "2303.TW", "side": "BUY", "qty": 1000, "price": 50.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:50:00", "fill_id": "n1"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2330.TW")], _engine_with_positions("2303.TW"), fills,
        watchlist_path=wl, now=now, mode="probe",
        sector_map={"2330.TW": "半導體", "2303.TW": "半導體"},
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "second_locked_sector_overlap"
    assert gated[0]["candidate_sector"] == "半導體"
    assert gated[0]["sector_overlap"] is True
    assert gated[0]["held_sectors"] == ["半導體"]


def test_probe_allows_second_when_different_sector(tmp_dir):
    """第 2 筆：高信心+realized≥0 且不同產業 → 放行。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, cross={"2891.TW": {"score": 99}})
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "2303.TW", "side": "BUY", "qty": 1000, "price": 50.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:50:00", "fill_id": "n1"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("2891.TW")], _engine_with_positions("2303.TW"), fills,
        watchlist_path=wl, now=now, mode="probe",
        sector_map={"2891.TW": "金融", "2303.TW": "半導體"},
    )

    assert out[0].action == "buy"
    assert gated == []


def test_probe_skips_sector_check_when_candidate_sector_unknown(tmp_dir):
    """第 2 筆：候選產業未知（不在 sector_map）→ 不據此擋單（fail-open）。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, cross={"ZZZ.TW": {"score": 99}})
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "2303.TW", "side": "BUY", "qty": 1000, "price": 50.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:50:00", "fill_id": "n1"},
    ])

    out, gated = _gate_daily_new_position_cap(
        [_buy("ZZZ.TW")], _engine_with_positions("2303.TW"), fills,
        watchlist_path=wl, now=now, mode="probe",
        sector_map={"2303.TW": "半導體"},  # 候選 ZZZ.TW 不在 map → 未知
    )

    assert out[0].action == "buy"
    assert gated == []


def test_probe_blocks_third_when_sector_overlap(tmp_dir):
    """第 3 筆：其餘條件全達成，但與既有持倉同產業 → third_locked_sector_overlap。"""
    now = datetime(2026, 6, 5, 10, 0)
    wl = tmp_dir / "wl.json"
    _write_watchlist(wl, cross={"2303.TW": {"score": 99}})
    fills = tmp_dir / "fills.jsonl"
    _write_jsonl(fills, [
        {"symbol": "2330.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:05:00", "fill_id": "f1"},
        {"symbol": "2317.TW", "side": "BUY", "qty": 1000, "price": 100.0,
         "fee": 0.0, "tax": 0.0, "filled_at": "2026-06-05T09:10:00", "fill_id": "f2"},
    ])
    e = _engine_with_positions("2330.TW", "2317.TW")

    out, gated = _gate_daily_new_position_cap(
        [_buy("2303.TW")], e, fills, watchlist_path=wl, now=now, mode="probe",
        quote_cache={"2330.TW": 101.0, "2317.TW": 101.0}, baseline_trend=0.005,
        sector_map={"2330.TW": "半導體", "2317.TW": "電子代工", "2303.TW": "半導體"},
    )

    assert out[0].action == "hold"
    assert gated[0]["block_reason"] == "third_locked_sector_overlap"
    assert gated[0]["sector_overlap"] is True

