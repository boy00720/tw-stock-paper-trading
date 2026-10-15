from __future__ import annotations

import importlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import yaml

risk = importlib.import_module("scripts.00631l_live_risk_monitor")


def _cfg(tmp_path: Path) -> dict:
    return {
        "enabled": True,
        "state_path": str(tmp_path / "state.json"),
        "audit_path": str(tmp_path / "audit.jsonl"),
        "cooldown_minutes": 60,
        "symbols": [
            {"symbol": "00631L.TW", "name": "元大台灣50正2", "role": "target"},
            {"symbol": "0050.TW", "name": "元大台灣50", "role": "baseline"},
            {"symbol": "2330.TW", "name": "台積電", "role": "key_weight"},
        ],
        "trading_window": {"start": "09:00", "end": "13:30", "close_check_start": "13:20"},
        "thresholds": {
            "yellow_loss_pct": 0.01,
            "red_loss_pct": 0.02,
            "strong_red_loss_pct": 0.03,
            "target_yellow_day_drop_pct": 0.025,
            "target_red_day_drop_pct": 0.04,
            "target_strong_day_drop_pct": 0.06,
            "baseline_yellow_drop_pct": 0.012,
            "baseline_red_drop_pct": 0.02,
            "key_weight_yellow_drop_pct": 0.018,
            "key_weight_red_drop_pct": 0.03,
            "intraday_drawdown_yellow_pct": 0.025,
            "intraday_drawdown_red_pct": 0.04,
            "profit_protect_pct": 0.03,
            "profit_trail_pct": 0.05,
            "profit_drawdown_trigger_pct": 0.02,
        },
        "message": "人工風控檢查",
    }


def _q(symbol: str, price: float, change_pct: float, high: float | None = None) -> risk.LiveQuote:
    names = {"00631L.TW": "元大台灣50正2", "0050.TW": "元大台灣50", "2330.TW": "台積電"}
    return risk.LiveQuote(
        symbol=symbol,
        name=names.get(symbol, symbol),
        role="target",
        price=price,
        previous_close=price / (1 + change_pct / 100),
        change_pct=change_pct,
        day_high=high,
        asof="2026-06-08 10:00:00",
    )


def test_evaluate_cost_strong_red(tmp_path):
    cfg = _cfg(tmp_path)
    quotes = {"00631L.TW": _q("00631L.TW", 35.80, -4.0, high=37.2)}

    ev = risk.evaluate_risk(quotes, entry_price=37.08, config=cfg)

    assert ev.level_rank == 3
    assert ev.level == "strong_red"
    assert any("跌破實盤成本" in r for r in ev.reasons)
    assert ev.target_return_pct is not None and ev.target_return_pct < -3


def test_evaluate_profit_watch_and_drawdown(tmp_path):
    cfg = _cfg(tmp_path)
    quotes = {"00631L.TW": _q("00631L.TW", 39.10, 1.0, high=40.2)}

    ev = risk.evaluate_risk(quotes, entry_price=37.08, config=cfg)

    assert ev.level_rank >= 2
    assert any("浮盈" in r or "回撤" in r for r in ev.reasons)
    assert ev.target_return_pct is not None and ev.target_return_pct > 5
    assert ev.target_drawdown_pct is not None and ev.target_drawdown_pct < -2


def test_evaluate_baseline_and_tsm_weakness(tmp_path):
    cfg = _cfg(tmp_path)
    quotes = {
        "00631L.TW": _q("00631L.TW", 37.20, -1.0),
        "0050.TW": _q("0050.TW", 100.0, -2.2),
        "2330.TW": _q("2330.TW", 800.0, -3.1),
    }

    ev = risk.evaluate_risk(quotes, entry_price=37.08, config=cfg)

    assert ev.level_rank == 2
    assert any("0050 轉弱" in r for r in ev.reasons)
    assert any("台積電轉弱" in r for r in ev.reasons)


def test_data_missing_is_yellow_warning(tmp_path):
    cfg = _cfg(tmp_path)

    ev = risk.evaluate_risk({}, entry_price=37.08, config=cfg)

    assert ev.level == "data_missing"
    assert ev.level_rank == 1
    assert any("報價不可得" in r for r in ev.reasons)


def test_should_send_on_escalation_and_cooldown(tmp_path):
    now = datetime(2026, 6, 8, 10, 0)
    ev_yellow = risk.RiskEvaluation("yellow", 1, "🟡", ("r",), "s", None, None)
    ev_red = risk.RiskEvaluation("red", 2, "🔴", ("r",), "s", None, None)
    state = {"last_level_rank": 1, "last_sent_ts": now.timestamp() - 60}

    assert risk.should_send(ev_yellow, state, now=now, cooldown_minutes=60) is False
    assert risk.should_send(ev_red, state, now=now, cooldown_minutes=60) is True


def test_trading_window_weekday_only(tmp_path):
    cfg = _cfg(tmp_path)
    assert risk.is_trading_window(datetime(2026, 6, 8, 9, 1), cfg) is True
    assert risk.is_trading_window(datetime(2026, 6, 8, 8, 59), cfg) is False
    assert risk.is_trading_window(datetime(2026, 6, 6, 9, 1), cfg) is False


def test_quotes_are_fresh_uses_00631l_asof():
    quotes = {"00631L.TW": _q("00631L.TW", 37.0, 0.0)}
    quotes["00631L.TW"] = risk.LiveQuote(
        symbol="00631L.TW",
        name="元大台灣50正2",
        role="target",
        price=37.0,
        previous_close=37.0,
        change_pct=0.0,
        day_high=37.0,
        asof="2026-06-08T10:00:00+08:00",
    )

    tz = timezone(timedelta(hours=8))
    assert risk.quotes_are_fresh(quotes, now=datetime(2026, 6, 8, 10, 20, tzinfo=tz), max_age_minutes=30) is True
    assert risk.quotes_are_fresh(quotes, now=datetime(2026, 6, 8, 10, 45, tzinfo=tz), max_age_minutes=30) is False


def test_fetch_live_quotes_parses_intraday_previous_close():
    idx = pd.to_datetime([
        "2026-06-05 13:25",
        "2026-06-05 13:30",
        "2026-06-08 09:00",
        "2026-06-08 09:05",
    ])
    columns = []
    rows = []
    for sym in ["00631L.TW"]:
        columns.extend([(sym, "Open"), (sym, "High"), (sym, "Low"), (sym, "Close")])
    for close, high, low in [(37.0, 37.1, 36.9), (37.08, 37.2, 37.0), (37.5, 37.6, 37.4), (38.0, 38.2, 37.8)]:
        rows.append([close, high, low, close])
    raw = pd.DataFrame(rows, index=idx, columns=pd.MultiIndex.from_tuples(columns))

    quotes = risk.fetch_live_quotes(
        [{"symbol": "00631L.TW", "name": "元大台灣50正2", "role": "target"}],
        yf_download=lambda *args, **kwargs: raw,
    )

    q = quotes["00631L.TW"]
    assert q.previous_close == 37.08
    assert q.price == 38.0
    assert round(q.change_pct, 2) == 2.48
    assert q.day_high == 38.2


def test_run_once_dry_run_outputs_manual_risk_card(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"live_risk": cfg}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(risk, "load_00631l_position", lambda: {
        "position_state": "holding", "entry_price": 37.08, "exit_price": None, "exit_date": None,
    })
    monkeypatch.setattr(risk, "fetch_live_quotes", lambda symbols, yf_download=None: {
        "00631L.TW": _q("00631L.TW", 35.8, -4.0, high=37.0),
    })

    rc = risk.run_once(
        dry_run=True,
        ignore_time=True,
        config_path=cfg_path,
        now=datetime(2026, 6, 8, 10, 0),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "00631L 實盤風控雷達" in out
    assert "實盤成本" in out
    assert "不是自動交易指令" in out
    assert "推送判定=True" in out


def _re_cfg() -> dict:
    return {
        "re_entry": {
            "band_low_pct": 0.05,
            "band_high_pct": 0.10,
            "baseline_strength_pct": 0.003,
            "key_weight_strength_pct": 0.0,
            "target_stabilize_drawdown_pct": 0.015,
            "cooldown_minutes": 120,
            "message": "00631L 已空手；再進場機會觀察，非自動買進指令。",
        }
    }


def test_evaluate_re_entry_zone():
    quotes = {
        "00631L.TW": _q("00631L.TW", 35.0, -1.0, high=35.2),
        "0050.TW": _q("0050.TW", 100.0, 0.5),
        "2330.TW": _q("2330.TW", 800.0, 0.2),
    }
    ev = risk.evaluate_re_entry(quotes, exit_price=38.0, config=_re_cfg())
    assert ev.mode == "flat"
    assert ev.level == "re_entry_zone"
    assert ev.level_rank == 2
    assert any("再進場觀察帶" in r for r in ev.reasons)


def test_evaluate_re_entry_watch_when_market_weak():
    quotes = {
        "00631L.TW": _q("00631L.TW", 35.0, -1.0, high=35.2),
        "0050.TW": _q("0050.TW", 100.0, -0.5),
        "2330.TW": _q("2330.TW", 800.0, 0.2),
    }
    ev = risk.evaluate_re_entry(quotes, exit_price=38.0, config=_re_cfg())
    assert ev.level == "re_entry_watch"
    assert ev.level_rank == 1


def test_evaluate_re_entry_not_in_band():
    quotes = {"00631L.TW": _q("00631L.TW", 37.5, -0.5, high=37.8)}
    ev = risk.evaluate_re_entry(quotes, exit_price=38.0, config=_re_cfg())
    assert ev.level == "flat_watch"
    assert ev.level_rank == 0


def test_evaluate_re_entry_missing_exit_price():
    quotes = {"00631L.TW": _q("00631L.TW", 35.0, -1.0, high=35.2)}
    ev = risk.evaluate_re_entry(quotes, exit_price=None, config=_re_cfg())
    assert ev.level_rank == 0
    assert any("賣出價" in r for r in ev.reasons)


def test_sell_signal_profit_take(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["sell_signal"] = {"enabled": True, "profit_take_pct": 0.08, "trend_break_drawdown_pct": 0.03}
    quotes = {"00631L.TW": _q("00631L.TW", 40.1, 1.0, high=40.2)}
    ev = risk.evaluate_risk(quotes, entry_price=37.08, config=cfg)
    assert ev.level == "sell_watch"
    assert any("止盈" in r for r in ev.reasons)


def test_run_once_flat_outputs_re_entry_card(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    cfg["re_entry"] = _re_cfg()["re_entry"]
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"live_risk": cfg}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(risk, "load_00631l_position", lambda: {
        "position_state": "flat", "entry_price": 37.08, "exit_price": 38.0, "exit_date": "2026-06-10",
    })
    monkeypatch.setattr(risk, "fetch_live_quotes", lambda symbols, yf_download=None: {
        "00631L.TW": _q("00631L.TW", 35.0, -1.0, high=35.2),
        "0050.TW": _q("0050.TW", 100.0, 0.5),
        "2330.TW": _q("2330.TW", 800.0, 0.2),
    })

    rc = risk.run_once(dry_run=True, ignore_time=True, config_path=cfg_path, now=datetime(2026, 6, 8, 10, 0))

    assert rc == 0
    out = capsys.readouterr().out
    assert "空手再進場雷達" in out
    assert "上輪賣出價" in out
    assert "不是自動交易指令" in out
