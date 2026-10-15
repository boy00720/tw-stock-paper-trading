# -*- coding: utf-8 -*-

from scripts.stock_monitor import build_watchlist_institutional_leaderboard


def test_build_watchlist_institutional_leaderboard_ranks_all_watchlist_stocks():
    stocks = {
        "2330.TW": {
            "外資": {"today": -22113.589, "month": 12174.374},
            "投信": {"today": 785.054, "month": 3414.025},
            "自營商": {"today": 500.225, "month": 5194.93},
        },
        "2454.TW": {
            "外資": {"today": 1191.234, "month": 10749.23},
            "投信": {"today": 1101.517, "month": 9047.028},
            "自營商": {"today": -43.623, "month": 2647.185},
        },
        "2408.TW": {
            "外資": {"today": 9472.0, "month": -3486.0},
            "投信": {"today": 3348.0, "month": -11595.0},
            "自營商": {"today": 122.0, "month": 263.0},
        },
    }
    watch_list = [
        ("2330.TW", "台積電", 0, 0, 0, {}),
        ("2454.TW", "聯發科", 0, 0, 0, {}),
        ("2408.TW", "南亞科", 0, 0, 0, {}),
    ]

    leaders = build_watchlist_institutional_leaderboard(stocks, watch_list)

    assert [item["symbol"] for item in leaders["foreign"]["buy_top"]] == ["2408.TW", "2454.TW"]
    assert [item["symbol"] for item in leaders["foreign"]["sell_top"]] == ["2330.TW"]
    assert leaders["investment"]["buy_top"][0]["symbol"] == "2408.TW"
    assert leaders["dealer"]["buy_top"][0]["symbol"] == "2330.TW"
