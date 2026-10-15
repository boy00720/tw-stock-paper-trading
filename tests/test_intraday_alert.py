#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_intraday_alert.py — 台股盤中即時警報：事件式可重推 + 對比差異

覆蓋階段 2 新行為：
- 首次觸發即推
- 已推過：相對上次推送變化不足 → 靜音（不洗版）
- 已推過：相對上次推送顯著變化 → 重推 + 帶「📊 較上次」對比行
- 下跌方向步進閾值更敏感（配合 2 倍槓桿）
- _load_pushed_today 向後相容舊格式（symbols 清單）
- _delta_descriptor 變化描述
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.intraday_alert as ia


def _wl(symbol, name, alert_pct=3.0, category="tw_tech"):
    return {"symbol": symbol, "name": name, "alert_pct": alert_pct, "category": category}


def _q(change_pct, price=100.0, vol_ratio=None):
    return {"price": price, "change_pct": change_pct, "vol_ratio": vol_ratio}


# ── _delta_descriptor ──

def test_delta_descriptor_basic():
    assert ia._delta_descriptor(3.0, 8.0) == "漲幅擴大"
    assert ia._delta_descriptor(8.0, 3.0) == "漲幅收斂"
    assert ia._delta_descriptor(-3.0, -8.0) == "跌幅擴大"
    assert ia._delta_descriptor(-8.0, -3.0) == "跌幅收斂"
    assert ia._delta_descriptor(-1.0, 2.0) == "翻紅"
    assert ia._delta_descriptor(2.0, -1.0) == "翻黑"
    assert ia._delta_descriptor(3.0, 3.05) == "大致持平"


# ── 首次觸發 ──

def test_first_trigger_pushes():
    wl = [_wl("2330.TW", "台積電", alert_pct=3.0)]
    quotes = {"2330.TW": _q(3.5)}
    alerts = ia._check_alerts(wl, quotes, sent={}, alert_defaults={})
    assert len(alerts) == 1
    assert alerts[0]["symbol"] == "2330.TW"
    assert alerts[0]["compare_line"] is None  # 首次無對比基準
    assert alerts[0]["is_restep"] is False


def test_below_threshold_no_push():
    wl = [_wl("2330.TW", "台積電", alert_pct=3.0)]
    quotes = {"2330.TW": _q(1.5)}  # 未達門檻
    alerts = ia._check_alerts(wl, quotes, sent={}, alert_defaults={})
    assert alerts == []


# ── 可重推：變化不足 → 靜音 ──

def test_restep_suppressed_when_change_insufficient():
    wl = [_wl("2330.TW", "台積電", alert_pct=3.0)]
    quotes = {"2330.TW": _q(4.0)}  # 達門檻，但相對上次 +3.5 只 +0.5 < restep_up 3.0
    sent = {"2330.TW": {"change_pct": 3.5, "price": 100.0, "time": "09:30"}}
    alerts = ia._check_alerts(wl, quotes, sent, alert_defaults={}, restep_up_pct=3.0, restep_down_pct=2.5)
    assert alerts == []  # 變化不足，靜音不洗版


# ── 可重推：漲幅顯著擴大 → 重推 + 對比行 ──

def test_restep_pushes_when_up_significant():
    wl = [_wl("2330.TW", "台積電", alert_pct=3.0)]
    quotes = {"2330.TW": _q(8.5)}  # 相對上次 +3.5 → +8.5，delta +5.0 >= restep_up 3.0
    sent = {"2330.TW": {"change_pct": 3.5, "price": 100.0, "time": "09:30"}}
    alerts = ia._check_alerts(wl, quotes, sent, alert_defaults={}, restep_up_pct=3.0, restep_down_pct=2.5)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["is_restep"] is True
    assert a["compare_line"] is not None
    assert "+3.50%→+8.50%" in a["compare_line"]
    assert "漲幅擴大" in a["compare_line"]


# ── 下跌方向更敏感（配合 2 倍槓桿）──

def test_restep_down_more_sensitive():
    wl = [_wl("00631L.TW", "元大台灣50正2", alert_pct=2.0)]
    # 上次 -3.0，本輪 -5.6：delta -2.6，超過 down 閾值 2.5（但不到 up 3.0）→ 重推
    quotes = {"00631L.TW": _q(-5.6)}
    sent = {"00631L.TW": {"change_pct": -3.0, "price": 30.0, "time": "10:00"}}
    alerts = ia._check_alerts(wl, quotes, sent, alert_defaults={}, restep_up_pct=3.0, restep_down_pct=2.5)
    assert len(alerts) == 1
    assert "跌幅擴大" in alerts[0]["compare_line"]


def test_restep_down_insufficient_suppressed():
    wl = [_wl("00631L.TW", "元大台灣50正2", alert_pct=2.0)]
    # 上次 -3.0，本輪 -5.0：delta -2.0，未達 down 閾值 2.5 → 靜音
    quotes = {"00631L.TW": _q(-5.0)}
    sent = {"00631L.TW": {"change_pct": -3.0, "price": 30.0, "time": "10:00"}}
    alerts = ia._check_alerts(wl, quotes, sent, alert_defaults={}, restep_up_pct=3.0, restep_down_pct=2.5)
    assert alerts == []


# ── 向後相容舊 sent 格式 ──

def test_load_pushed_today_backward_compat(tmp_path, monkeypatch):
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    f = tmp_path / "sent.json"
    f.write_text(json.dumps({"date": today, "symbols": ["2330.TW", "2317.TW"]}), encoding="utf-8")
    monkeypatch.setattr(ia, "ALERT_SENT_FILE", f)
    pushed = ia._load_pushed_today()
    assert set(pushed.keys()) == {"2330.TW", "2317.TW"}
    # 舊格式無基準：change_pct=None（已推過但無對比基準，下次顯著變動仍可重推）
    assert pushed["2330.TW"]["change_pct"] is None


def test_load_pushed_today_new_format(tmp_path, monkeypatch):
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    f = tmp_path / "sent.json"
    f.write_text(json.dumps({
        "date": today,
        "pushed": {"2330.TW": {"change_pct": 3.5, "price": 100.0, "time": "09:30"}},
    }), encoding="utf-8")
    monkeypatch.setattr(ia, "ALERT_SENT_FILE", f)
    pushed = ia._load_pushed_today()
    assert pushed["2330.TW"]["change_pct"] == 3.5


def test_load_pushed_today_stale_date_returns_empty(tmp_path, monkeypatch):
    f = tmp_path / "sent.json"
    f.write_text(json.dumps({"date": "2020-01-01", "pushed": {"2330.TW": {"change_pct": 3.5}}}), encoding="utf-8")
    monkeypatch.setattr(ia, "ALERT_SENT_FILE", f)
    assert ia._load_pushed_today() == {}


# ── 舊格式紀錄（change_pct=None）仍可在顯著變動時重推 ──

def test_restep_from_baseless_old_record():
    wl = [_wl("2330.TW", "台積電", alert_pct=3.0)]
    quotes = {"2330.TW": _q(5.0)}
    sent = {"2330.TW": {"change_pct": None, "price": None, "time": None}}
    alerts = ia._check_alerts(wl, quotes, sent, alert_defaults={}, restep_up_pct=3.0, restep_down_pct=2.5)
    # 無基準（change_pct=None）→ 視為可推，但無對比行
    assert len(alerts) == 1
    assert alerts[0]["compare_line"] is None
