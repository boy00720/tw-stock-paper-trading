# -*- coding: utf-8 -*-

import json

import scripts.daily_stock_report as daily_stock_report

from scripts.daily_stock_report import (
    build_briefing_payload,
    build_cross_intraday_commentary,
    build_latest_stock_notification_quotes,
    build_watchlist_digest,
    enrich_watchlist_with_quotes,
    sanitize_analysis_by_truth,
)


def test_breakdown_generates_real_technical_signal_without_strategy_match():
    watchlist = {
        "summary": {"short_term_count": 1, "medium_term_count": 0, "long_term_count": 0, "intraday_count": 0, "cross_signals_count": 0},
        "formal_pools": {
            "short_term": [
                {
                    "symbol": "2454.TW",
                    "name": "聯發科（2454）",
                    "price": 2435.0,
                    "entry_price": 2435.0,
                    "stop_loss": 2313.25,
                    "take_profit": 2678.5,
                    "entry_zone": [2422.82, 2459.35],
                    "score": 83,
                    "engine": "momentum",
                    "breakdown": {"ret_5d": 20, "break_20dh": 15, "inst_buy": 15, "vol_ratio": 10},
                }
            ],
            "medium_term": [],
            "long_term": [],
        },
        "auxiliary_signals": {"intraday": []},
        "cross_signals": {},
    }

    digest = build_watchlist_digest(watchlist, {}, [])

    note = digest["top_short"][0]["technical_note"]
    assert "今日無對應技術信號" not in note
    assert "5日動能20分" in note
    assert "突破20日高點15分" in note


def test_cross_focus_has_price_and_non_trading_stop_boundary():
    watchlist_digest = {
        "summary": {"short_term_count": 0, "medium_term_count": 0, "long_term_count": 0, "intraday_count": 0, "cross_signals_count": 1},
        "top_short": [],
        "top_medium": [],
        "top_long": [],
        "top_intraday": [],
        "top_cross": [
            {
                "symbol": "2317.TW",
                "name": "鴻海（2317）",
                "price": 225.5,
                "stop_loss": None,
                "summary": "跨池成立",
                "institutional_note": "外資今+10,630張／月+133,548張；投信今-159張／月+55張；自營商今-451張／月+14,594張",
            }
        ],
    }
    stock_data = {"quotes": [], "institutional": "", "leaders": {}, "stock_institutional": {}}
    payload = build_briefing_payload(stock_data, "", watchlist_digest, [], "")

    row = payload["watchlist"]["cross_focus"][0]
    assert "現價 225.5元" in row
    assert "跨池觀察未生成交易停損" in row

    analysis = sanitize_analysis_by_truth(
        payload,
        {
            "一句話判斷": "",
            "市場看點": [],
            "短線選股日報": "",
            "跨池與日內焦點": "鴻海不升格短線池，現價與停損資料缺失。",
            "異常與風險": [],
            "明日行動清單": [],
        },
    )
    assert "現價與停損資料缺失" not in analysis["跨池與日內焦點"]
    assert "現價225.5元" in analysis["跨池與日內焦點"]
    assert "跨池觀察未生成交易停損" in analysis["跨池與日內焦點"]


def test_cross_intraday_commentary_uses_existing_cross_row_facts():
    commentary = build_cross_intraday_commentary(
        ["鴻海（2317）｜現價 225.5元｜停損 跨池觀察未生成交易停損｜外資今+10,630張／月+133,548張"],
        [],
    )

    assert "現價225.5元" in commentary
    assert "外資今+10,630張" in commentary
    assert "資料缺失" not in commentary


def test_short_trade_levels_recalculate_when_quote_overrides_price():
    watchlist = {
        "summary": {"short_term_count": 1, "medium_term_count": 0, "long_term_count": 0, "intraday_count": 0, "cross_signals_count": 0},
        "formal_pools": {
            "short_term": [
                {
                    "symbol": "2330.TW",
                    "name": "台積電（2330）",
                    "price": 2185.0,
                    "entry_price": 2185.0,
                    "stop_loss": 2075.75,
                    "take_profit": 2403.5,
                    "entry_zone": [2174.08, 2206.85],
                    "score": 73,
                    "engine": "momentum",
                    "breakdown": {"break_20dh": 15},
                }
            ],
            "medium_term": [],
            "long_term": [],
        },
        "auxiliary_signals": {"intraday": []},
        "cross_signals": {},
    }
    stock_data = {"quotes": [{"symbol": "2330.TW", "price": 2215.0}]}

    enriched = enrich_watchlist_with_quotes(watchlist, stock_data)
    item = enriched["short_term"][0]

    assert item["price"] == 2215.0
    assert item["entry_zone"] == [2203.93, 2237.15]
    assert item["stop_loss"] == 2104.25
    assert item["take_profit"] == 2436.5


def test_build_latest_stock_notification_quotes_parses_pool_tagged_lines(tmp_path, monkeypatch):
    payload_dir = tmp_path / "logs" / "master_agent_payloads" / "case"
    payload_dir.mkdir(parents=True)
    payload = {
        "notification": {
            "feishu": {
                "content": "\n".join([
                    "【台股與 20 檔追蹤名單】",
                    "  台光電（2383） [短線池 | 嚴格條件通過] 4,375.0 ▲ 1.23%",
                ])
            }
        }
    }
    (payload_dir / "stock_afternoon.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(daily_stock_report, "proj_root", lambda: tmp_path)

    quotes = build_latest_stock_notification_quotes()

    assert quotes["2383"]["symbol"] == "2383.TW"
    assert quotes["2383"]["price"] == 4375.0
