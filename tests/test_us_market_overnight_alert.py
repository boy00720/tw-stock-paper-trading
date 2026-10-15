"""美股半夜風險提醒測試（mock yfinance / 不發網路）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from scripts import us_market_overnight_alert as alert


def _fake_multi_df(values: dict[str, tuple[float, float]]) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=2)
    columns = []
    data = []
    for sym, (prev, last) in values.items():
        columns.extend([(sym, "Open"), (sym, "Close")])
    for idx in range(2):
        row = []
        for sym, (prev, last) in values.items():
            close = prev if idx == 0 else last
            row.extend([close, close])
        data.append(row)
    return pd.DataFrame(data, index=dates, columns=pd.MultiIndex.from_tuples(columns))


def test_fetch_us_market_quotes_parses_change_pct():
    raw = _fake_multi_df({
        "^IXIC": (100.0, 102.0),
        "TSM": (200.0, 196.0),
    })

    quotes = alert.fetch_us_market_quotes(
        [("^IXIC", "Nasdaq", "index"), ("TSM", "TSM ADR", "adr")],
        mode="close",
        yf_download=lambda *args, **kwargs: raw,
    )

    by_symbol = {q.symbol: q for q in quotes}
    assert by_symbol["^IXIC"].change_pct == 2.0
    assert by_symbol["TSM"].change_pct == -2.0
    assert by_symbol["^IXIC"].change_metric.basis == "previous_close"
    assert by_symbol["^IXIC"].change_metric.source == "yfinance"
    assert by_symbol["^IXIC"].change_metric.is_realtime is False


def test_fetch_us_market_quotes_intraday_uses_previous_day_close():
    idx = pd.to_datetime([
        "2026-06-01 15:55",
        "2026-06-01 16:00",
        "2026-06-02 09:30",
        "2026-06-02 10:00",
    ])
    raw = pd.DataFrame(
        {
            ("TSM", "Open"): [100.0, 100.0, 105.0, 106.0],
            ("TSM", "Close"): [99.0, 100.0, 105.0, 106.0],
        },
        index=idx,
    )
    raw.columns = pd.MultiIndex.from_tuples(raw.columns)

    quotes = alert.fetch_us_market_quotes(
        [("TSM", "TSM ADR", "adr")],
        mode="intraday",
        yf_download=lambda *args, **kwargs: raw,
    )

    assert len(quotes) == 1
    assert quotes[0].previous_close == 100.0
    assert quotes[0].price == 106.0
    assert quotes[0].change_pct == 6.0


# 注：mode=close 需过滤「今日未完成K线」（2026-06-12 修复 pre_tw 拿到未来数据 bug）。
# 关键不在几点执行，而是只要 yfinance 日线最后一根 date >= 本机今天，就跳过该根，
# 取前一根完整收盘作为 last，并让 MarketQuote.asof 同步指向实际采用的完整收盘日。

def test_fetch_us_market_quotes_close_skips_today_incomplete_k_and_uses_matching_asof():
    today = datetime.now().date()
    idx = pd.to_datetime([
        today - timedelta(days=2),
        today - timedelta(days=1),
        today,
    ])
    raw = pd.DataFrame(
        {
            ("SOXX", "Open"): [100.0, 110.0, 999.0],
            ("SOXX", "Close"): [100.0, 110.0, 999.0],
        },
        index=idx,
    )
    raw.columns = pd.MultiIndex.from_tuples(raw.columns)

    quotes = alert.fetch_us_market_quotes(
        [("SOXX", "SOXX 半導體", "semiconductor")],
        mode="close",
        yf_download=lambda *args, **kwargs: raw,
    )

    assert len(quotes) == 1
    assert quotes[0].price == 110.0
    assert quotes[0].previous_close == 100.0
    assert quotes[0].change_pct == 10.0
    assert quotes[0].asof == str(idx[-2])


def test_research_context_exposes_change_contract():
    quote = alert.MarketQuote(
        "TSM", "TSM ADR", "adr", 200.0, 2.0,
        previous_close=196.08,
        asof="2026-07-13T01:00:00+00:00",
        source="CNBC",
        is_realtime=False,
    )
    text = alert._format_quotes_for_research([quote])
    assert "basis=previous_close" in text
    assert "source=CNBC" in text
    assert "is_realtime=False" in text


def test_intraday_threshold_only_pushes_large_moves():
    quiet = [alert.MarketQuote("^IXIC", "Nasdaq", "index", 100.0, 0.8)]
    loud = [alert.MarketQuote("TSM", "TSM ADR", "adr", 200.0, -1.8)]

    assert alert.should_push_intraday(quiet) is False   # 漲 0.8% < 1.5% 漲門檻 → 不推
    assert alert.should_push_intraday(loud) is True       # 跌 1.8% >= 1% 跌門檻 → 推


def test_build_close_card_mentions_00631l_and_tsm_risk():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.2),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3),
        alert.MarketQuote("NVDA", "NVIDIA", "semiconductor", 120.0, -2.1),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -2.0),
    ]

    title, content = alert.build_overnight_card(
        quotes,
        mode="close",
        now=datetime(2026, 6, 6, 5, 10),
        entry_00631l=37.08,
    )

    assert "美股收盤摘要" in title
    assert "00631L 隔日風險" in title
    assert "00631L 實盤成本：37.08" in content
    assert "持倉風控參考，不混入 sim 帳本" in content
    assert "TSM ADR ▼ -2.30%" in content
    assert "半導體" in content
    assert "收盤定調，不是自動交易指令" in content


def test_build_card_uses_flat_position_context_not_live_cost():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, 2.2),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 185.0, 4.0),
    ]

    _title, content = alert.build_overnight_card(
        quotes,
        mode="intraday",
        now=datetime(2026, 6, 8, 21, 50),
        position_00631l=alert.Position00631L(
            position_state="flat",
            entry_price=37.08,
            exit_price=32.46,
            exit_date="2026-06-08",
        ),
    )

    assert "00631L 實盤狀態：已平倉空手" in content
    assert "上輪賣出均價：32.46" in content
    assert "上輪進場成本：37.08" in content
    assert "不代表目前仍持倉" in content
    assert "00631L 實盤成本：37.08" not in content
    assert "是否減碼/拉保護線" not in content


def test_load_00631l_position_reads_flat_state(tmp_path):
    cfg = tmp_path / "sim_watch_only.yaml"
    cfg.write_text(
        """
watch_only:
  00631L.TW:
    position_state: flat
    entry_price: 37.08
    exit_price: 32.46
    exit_date: '2026-06-08'
""".strip(),
        encoding="utf-8",
    )

    pos = alert.load_00631l_position(cfg)

    assert pos.position_state == "flat"
    assert pos.entry_price == 37.08
    assert pos.exit_price == 32.46
    assert pos.exit_date == "2026-06-08"


def test_intraday_freshness_guard_rejects_stale_data():
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 200.0, 2.0, asof="2026-06-02 10:00:00")]

    assert alert.is_intraday_data_fresh(
        quotes,
        now=datetime(2026, 6, 2, 11, 0),
        max_age_hours=8,
    ) is True
    assert alert.is_intraday_data_fresh(
        quotes,
        now=datetime(2026, 6, 3, 11, 0),
        max_age_hours=8,
    ) is False


def test_intraday_freshness_ignores_fresh_commodity_if_us_stale():
    quotes = [
        alert.MarketQuote("TSM", "TSM ADR", "adr", 200.0, 2.0, asof="2026-06-02 10:00:00"),
        alert.MarketQuote("CL=F", "原油", "commodity", 90.0, 2.0, asof="2026-06-03 10:30:00"),
    ]

    assert alert.is_intraday_data_fresh(
        quotes,
        now=datetime(2026, 6, 3, 11, 0),
        max_age_hours=8,
    ) is False


def test_close_freshness_rejects_old_us_daily_data():
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 200.0, -2.0, asof="2026-06-02 00:00:00")]

    assert alert.is_close_data_recent(
        quotes,
        now=datetime(2026, 6, 3, 5, 0),
        max_age_hours=40,
    ) is True
    assert alert.is_close_data_recent(
        quotes,
        now=datetime(2026, 6, 4, 18, 0),
        max_age_hours=40,
    ) is False


def test_alert_state_prevents_duplicate_keys(tmp_path):
    state = tmp_path / "state.json"
    key = "close:2026-06-05 16:00:00"

    assert alert._already_sent(key, path=state) is False
    alert._mark_sent(key, path=state)
    assert alert._already_sent(key, path=state) is True


def test_build_close_card_includes_market_reason_section():
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]
    reason = alert.MarketReasonNews(
        summary="Stocks fell as Treasury yields rose and chip shares weakened.",
        sources=["Nasdaq drops as chip stocks slide", "TSM ADR falls with AI names"],
    )

    _title, content = alert.build_overnight_card(
        quotes,
        mode="close",
        now=datetime(2026, 6, 6, 5, 10),
        entry_00631l=37.08,
        reason_news=reason,
    )

    assert "二、新聞面與市場解讀" in content
    assert "Stocks fell as Treasury yields rose" in content
    assert "來源線索：Nasdaq drops as chip stocks slide" in content
    assert "三、對 00631L 實盤的提醒" in content


def test_build_close_card_reason_fallback_when_news_missing():
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]

    _title, content = alert.build_overnight_card(
        quotes,
        mode="close",
        now=datetime(2026, 6, 6, 5, 10),
        entry_00631l=37.08,
        reason_news=None,
    )

    assert "二、新聞面與市場解讀" in content
    assert "暫無可靠新聞摘要" in content


def test_fetch_market_reason_news_parses_tavily_response(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "answer": "Markets fell after yields rose and megacap tech sold off.",
                "results": [
                    {"title": "Stocks fall as yields climb"},
                    {"title": "Chip stocks lead Nasdaq lower"},
                ],
            }

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResp()

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]

    reason = alert.fetch_market_reason_news(quotes, mode="intraday", requester=fake_post)

    assert reason is not None
    assert "Markets fell" in reason.summary
    assert reason.sources == ["Stocks fall as yields climb", "Chip stocks lead Nasdaq lower"]
    assert calls[0][0] == "https://api.tavily.com/search"
    assert "why are US stocks falling" in calls[0][1]["json"]["query"]


def test_fetch_yfinance_ticker_news_parses_relevant_titles():
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def news(self):
            return [
                {"content": {"title": "Random lifestyle story"}},
                {"content": {"title": "Stock Market Today: Nasdaq Sinks 4% On Rate-Hike Worries; Nvidia Falls Amid Chip Sell-Off"}},
            ]

    reason = alert.fetch_yfinance_ticker_news(yf_ticker_factory=FakeTicker)

    assert reason is not None
    assert "Nasdaq Sinks" in reason.sources[0]
    assert "Random lifestyle" not in "\n".join(reason.sources)


def test_fetch_yahoo_reason_news_parses_titles():
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "news": [
                    {"title": "PlanetPlay launches environmental campaign", "publisher": "PR Newswire"},
                    {"title": "Nasdaq tumbles as yields rise", "publisher": "Yahoo Finance"},
                    {"title": "Chip stocks slide after AI selloff", "publisher": "Reuters"},
                ]
            }

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResp()

    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]
    reason = alert.fetch_yahoo_reason_news(quotes, mode="close", requester=fake_get)

    assert reason is not None
    assert "新聞標題線索" in reason.summary
    assert reason.sources[0] == "Nasdaq tumbles as yields rise（Yahoo Finance）"
    assert calls[0][0] == "https://query1.finance.yahoo.com/v1/finance/search"


def test_fetch_market_reason_news_without_tavily_uses_fallback(monkeypatch):
    called = []

    def fake_fallback(quotes, *, mode):
        called.append(mode)
        return alert.MarketReasonNews(summary="fallback", sources=["Fallback title"])

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(alert, "_fallback_reason_news", fake_fallback)

    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]
    reason = alert.fetch_market_reason_news(quotes, mode="intraday")

    assert reason is not None
    assert reason.summary == "fallback"
    assert called == ["intraday"]


def test_fetch_market_reason_news_fails_closed(monkeypatch):
    def fake_post(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(alert, "_fallback_reason_news", lambda quotes, *, mode: None)
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]

    assert alert.fetch_market_reason_news(quotes, mode="close", requester=fake_post) is None


def test_human_readable_positive_risk_card():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, 2.2),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 185.0, 1.6),
    ]

    title, content = alert.build_overnight_card(
        quotes,
        mode="intraday",
        now=datetime(2026, 6, 6, 1, 30),
        entry_00631l=None,
    )

    assert title.startswith("🟢")
    assert "美股科技/半導體偏強" in content
    assert "盤中" in title



def test_intraday_card_has_dynamic_news_asof_and_playbook():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.4, asof="2026-06-10 13:05:00"),
        alert.MarketQuote("QQQ", "QQQ", "tech", 520.0, -1.2, asof="2026-06-10 13:05:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -2.0, asof="2026-06-10 13:05:00"),
        alert.MarketQuote("NVDA", "NVIDIA", "semiconductor", 120.0, -2.3, asof="2026-06-10 13:05:00"),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.8, asof="2026-06-10 13:05:00"),
        alert.MarketQuote("CL=F", "原油", "commodity", 82.0, 1.2, asof="2026-06-10 13:05:00"),
        alert.MarketQuote("GC=F", "黃金", "commodity", 2350.0, 0.9, asof="2026-06-10 13:05:00"),
    ]
    reason = alert.MarketReasonNews(
        summary="Stocks extended losses as yields climbed and semiconductor shares sold off.",
        sources=[
            "Nasdaq extends losses as yields climb",
            "Nvidia leads chip stocks lower",
            "TSM ADR weakens with AI supply chain",
            "Oil rises on geopolitical risk",
            "Gold gains as investors seek hedges",
        ],
    )

    _title, content = alert.build_overnight_card(
        quotes,
        mode="intraday",
        now=datetime(2026, 6, 10, 1, 30),
        position_00631l=alert.Position00631L("holding", 34.81),
        reason_news=reason,
    )

    assert "📡 資料時間：2026-06-10 13:05" in content
    assert "二、新聞面與市場解讀（中文同步；因果需盤後再確認）" in content
    assert "盤中新聞密度：已抓到 5 條相關來源" in content
    assert "這張卡是依本輪行情與新聞重新整理，不是固定模板回覆" in content
    assert "來源線索：Gold gains as investors seek hedges" in content
    assert "四、盤中動態觀察" in content
    assert "00631L 持倉盤中雷達" in content
    assert "五、操作口徑" in content
    assert "盤中動態雷達" in content
    # 口徑已 regime 化：固定模板字串改為「本輪盤勢／口徑建議／待確認訊號」三段
    assert "本輪盤勢：" in content
    assert "口徑建議：" in content
    assert "待確認訊號：" in content
    assert "Research W 美股收盤研究摘要" not in content


class _ChartResp:
    def __init__(self, price, prev, ts=1749000000):
        self._d = {"chart": {"result": [{"meta": {
            "regularMarketPrice": price,
            "chartPreviousClose": prev,
            "regularMarketTime": ts,
        }}], "error": None}}

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


def test_fetch_quotes_via_yahoo_chart_parses_meta():
    payloads = {
        "QQQ": _ChartResp(722.6, 700.3),
        "TSM": _ChartResp(196.0, 200.0),
    }

    def fake_get(url, **kwargs):
        return payloads[url.rsplit("/", 1)[-1]]

    quotes = alert.fetch_quotes_via_yahoo_chart(
        [("QQQ", "QQQ", "tech"), ("TSM", "TSM ADR", "adr")],
        mode="close",
        requester=fake_get,
    )

    by = {q.symbol: q for q in quotes}
    assert round(by["QQQ"].change_pct, 2) == 3.18
    assert by["TSM"].change_pct == -2.0
    assert by["QQQ"].asof  # asof 應由 regularMarketTime 帶出，供 freshness gate 使用


def test_fetch_quotes_via_yahoo_chart_skips_bad_payload():
    class BadResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"chart": {"result": [], "error": "Not Found"}}

    quotes = alert.fetch_quotes_via_yahoo_chart(
        [("QQQ", "QQQ", "tech")],
        mode="close",
        requester=lambda url, **kwargs: BadResp(),
    )
    assert quotes == []


class _CnbcResp:
    def __init__(self, last, prev, last_time="2026-06-10T16:00:00.000-0400"):
        self._d = {"FormattedQuoteResult": {"FormattedQuote": [{
            "last": last,
            "previous_day_closing": prev,
            "last_time": last_time,
        }]}}

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


def test_fetch_quotes_via_cnbc_parses_and_maps_symbols():
    payloads = {
        ".SPX": _CnbcResp("5,500.00", "5,450.00"),
        "TSM": _CnbcResp("196.00", "200.00"),
    }
    seen = []

    def fake_get(url, **kwargs):
        sym = kwargs["params"]["symbols"]
        seen.append(sym)
        return payloads[sym]

    quotes = alert.fetch_quotes_via_cnbc(
        [("^GSPC", "S&P 500", "index"), ("TSM", "TSM ADR", "adr")],
        mode="close",
        requester=fake_get,
    )

    by = {q.symbol: q for q in quotes}
    # ^GSPC 應映射成 CNBC 的 .SPX；千分位逗號要被去掉
    assert ".SPX" in seen
    assert round(by["^GSPC"].change_pct, 4) == round((5500 - 5450) / 5450 * 100, 4)
    assert by["^GSPC"].price == 5500.0
    assert by["TSM"].change_pct == -2.0
    assert by["^GSPC"].asof == "2026-06-10T16:00:00-04:00"  # 去毫秒＋補時區冒號


def test_fetch_quotes_via_cnbc_skips_unmapped_symbols():
    called = []

    def fake_get(url, **kwargs):
        called.append(kwargs["params"]["symbols"])
        return _CnbcResp("100.00", "99.00")

    # ES=F 不在 _CNBC_SYMBOL_MAP，應整檔跳過、不發請求
    quotes = alert.fetch_quotes_via_cnbc(
        [("ES=F", "標普期貨", "futures")],
        mode="pre_us",
        requester=fake_get,
    )
    assert quotes == []
    assert called == []


def test_fetch_quotes_via_cnbc_skips_bad_payload():
    class BadResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"FormattedQuoteResult": {"FormattedQuote": []}}

    quotes = alert.fetch_quotes_via_cnbc(
        [("^GSPC", "S&P 500", "index")],
        mode="close",
        requester=lambda url, **kwargs: BadResp(),
    )
    assert quotes == []


def test_normalize_cnbc_asof_handles_millis_and_tz_and_invalid():
    assert alert._normalize_cnbc_asof("2026-06-10T16:00:00.000-0400") == "2026-06-10T16:00:00-04:00"
    assert alert._normalize_cnbc_asof("") == ""
    assert alert._normalize_cnbc_asof("not-a-time") == ""


def _capture_notifier(sink: list):
    def _notify(title: str, content: str) -> bool:
        sink.append((title, content))
        return True
    return _notify


def test_record_fetch_outcome_alerts_after_threshold(tmp_path):
    state = tmp_path / "state.json"
    sent: list = []
    notifier = _capture_notifier(sent)

    a1 = alert._record_fetch_outcome(False, "close", path=state, notifier=notifier, threshold=2)
    assert a1 is False and sent == []  # 單次失敗未達門檻，不告警

    a2 = alert._record_fetch_outcome(False, "close", path=state, notifier=notifier, threshold=2)
    assert a2 is True and len(sent) == 1  # 連續 2 次達門檻，告警一次
    assert "連續 2 次" in sent[0][0]

    a3 = alert._record_fetch_outcome(False, "close", path=state, notifier=notifier, threshold=2)
    assert a3 is False and len(sent) == 1  # streak 內不重複告警


def test_record_fetch_outcome_success_resets_and_no_alert(tmp_path):
    import json as _json

    state = tmp_path / "state.json"
    sent: list = []
    notifier = _capture_notifier(sent)

    alert._record_fetch_outcome(False, "close", path=state, notifier=notifier, threshold=2)
    alert._record_fetch_outcome(False, "close", path=state, notifier=notifier, threshold=2)
    assert len(sent) == 1

    # 抓取成功（含週末抓到舊資料）→ 歸零、解除告警旗標，且不發任何東西
    alert._record_fetch_outcome(True, "close", path=state, notifier=notifier, threshold=2)
    data = _json.loads(state.read_text(encoding="utf-8"))
    assert data["consecutive_fetch_fail"] == 0
    assert data["fetch_fail_alerted"] is False
    assert len(sent) == 1


def test_record_fetch_outcome_preserves_sent_keys(tmp_path):
    import json as _json

    state = tmp_path / "state.json"
    alert._mark_sent("close:2026-06-05 16:00:00", path=state)
    alert._record_fetch_outcome(False, "close", path=state, notifier=_capture_notifier([]), threshold=2)

    data = _json.loads(state.read_text(encoding="utf-8"))
    assert "close:2026-06-05 16:00:00" in data["sent_keys"]  # 失敗計數不可清掉既有 sent_keys
    assert data["consecutive_fetch_fail"] == 1


def test_open_mode_card_title():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, 0.5),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 185.0, 0.6),
    ]
    title, _content = alert.build_overnight_card(quotes, mode="open", now=datetime(2026, 6, 9, 21, 35))
    assert "開盤雷達" in title
    assert "00631L 隔日風險" in title



def test_open_mode_card_has_chinese_news_and_actionable_watchlist():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -0.8),
        alert.MarketQuote("QQQ", "QQQ", "tech", 520.0, -0.6),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -1.4),
        alert.MarketQuote("NVDA", "NVIDIA", "semiconductor", 120.0, -1.7),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.2),
        alert.MarketQuote("CL=F", "原油", "commodity", 82.0, 1.1),
        alert.MarketQuote("GC=F", "黃金", "commodity", 2350.0, 0.8),
    ]
    reason = alert.MarketReasonNews(
        summary="Stocks opened lower as yields rose and chip shares weakened.",
        sources=[
            "Nasdaq slips as Treasury yields climb",
            "Nvidia falls as chip stocks pull back",
            "TSM ADR weakens with semiconductor names",
            "Oil rises amid geopolitical risk",
            "Gold gains as investors seek hedges",
        ],
    )

    _title, content = alert.build_overnight_card(
        quotes,
        mode="open",
        now=datetime(2026, 6, 9, 21, 35),
        position_00631l=alert.Position00631L("holding", 34.81),
        reason_news=reason,
    )

    assert "二、新聞面與市場解讀（中文同步；因果需盤後再確認）" in content
    assert "科技線目前偏弱" in content
    assert "半導體線索目前偏弱" in content
    assert "新聞摘要：Stocks opened lower" in content
    assert "開盤新聞密度：已抓到 5 條相關來源" in content
    assert "來源線索：Gold gains as investors seek hedges" in content
    assert "四、開盤後優先觀察" in content
    assert "半導體同步性" in content
    assert "五、操作口徑" in content
    # 口徑已 regime 化：固定模板字串改為「本輪盤勢／口徑建議／待確認訊號」三段
    assert "本輪盤勢：" in content
    assert "口徑建議：" in content
    assert "待確認訊號：" in content
    assert "Research W 美股收盤研究摘要" not in content


def test_open_and_intraday_both_push_on_small_move(monkeypatch):
    # open 與 intraday 皆不設門檻：小波動也推（intraday 改為每段必推，靠「較上一輪」對比避免重複感）
    small = [alert.MarketQuote("TSM", "TSM ADR", "adr", 185.0, 0.5, asof="2026-06-09 09:35:00")]
    monkeypatch.setattr(alert, "fetch_us_market_quotes", lambda *a, **k: small)
    monkeypatch.setattr(alert, "fetch_market_reason_news", lambda *a, **k: None)
    monkeypatch.setattr(alert, "run_us_overnight_research", lambda *a, **k: None)
    monkeypatch.setattr(alert, "generate_llm_market_read", lambda *a, **k: None)
    monkeypatch.setattr(alert, "generate_llm_playbook_read", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_load_round_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_save_round_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(alert, "is_intraday_data_fresh", lambda *a, **k: True)
    monkeypatch.setattr(alert, "_already_sent", lambda *a, **k: False)
    monkeypatch.setattr(alert, "_mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_record_fetch_outcome", lambda *a, **k: False)
    monkeypatch.setattr(
        alert, "load_00631l_position",
        lambda *a, **k: alert.Position00631L("flat", 37.08, 32.46, "2026-06-08"),
    )
    sent: list = []
    monkeypatch.setattr(alert, "send_dm", lambda title, content: sent.append(title) or True)

    assert alert.run("open") == 0
    assert len(sent) == 1  # open 不設門檻，推了

    sent.clear()
    assert alert.run("intraday") == 0
    assert len(sent) == 1  # intraday 已取消波動門檻，小波動同樣推送（每段必推）


def test_should_push_intraday_asymmetric():
    # 不對稱門檻：跌 >=1% 推、漲 <1.5% 不推、漲 >=1.5% 推（下跌更敏感）
    down = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.2)]
    up_small = [alert.MarketQuote("TSM", "TSM ADR", "adr", 188.0, 1.0)]
    up_big = [alert.MarketQuote("TSM", "TSM ADR", "adr", 190.0, 1.6)]
    assert alert.should_push_intraday(down) is True        # 跌 1.2% → 推
    assert alert.should_push_intraday(up_small) is False   # 漲 1.0% → 不推（漲門檻 1.5%）
    assert alert.should_push_intraday(up_big) is True       # 漲 1.6% → 推


def test_build_close_card_appends_research_summary():
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]
    research = {
        "success": True,
        "report": "# 🌙 Research W 美股收盤研究\n\n昨夜半導體偏弱，需觀察台積電開盤承接。",
        "output_path": "/tmp/20260610_us_overnight.md",
    }

    _title, content = alert.build_overnight_card(
        quotes,
        mode="close",
        now=datetime(2026, 6, 10, 5, 10),
        position_00631l=alert.Position00631L("holding", 34.81),
        research_result=research,
    )

    assert "Research W 美股收盤研究摘要" in content
    assert "昨夜半導體偏弱" in content
    assert "完整研究已存檔：20260610_us_overnight.md" in content
    # close 現在也接收盤定調＋操作口徑，Research W 段編號動態接在其後（不再寫死「五、」）
    assert "四、收盤定調與隔日準備" in content
    assert "操作口徑" in content


def test_close_research_context_includes_research_pack_when_enabled(monkeypatch):
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3)]
    monkeypatch.setenv(alert.RESEARCH_PACK_US_CLOSE_ENABLED_ENV, "1")
    monkeypatch.setenv(alert.RESEARCH_PACK_US_CLOSE_SYMBOL_ENV, "SPY")
    monkeypatch.setenv(alert.RESEARCH_PACK_US_MARKET_DATA_TYPE_ENV, "delayed")
    monkeypatch.setenv(alert.RESEARCH_PACK_US_CLOSE_CLIENT_ID_ENV, "481")
    monkeypatch.setattr(
        alert,
        "_build_us_close_research_pack_context",
        lambda: "【Research Pack（結構化資料，供 Research W 使用）】\n- market: US\n- provider_map.quote: IBKR\n- is_realtime: False",
    )

    context = alert._build_us_overnight_research_context(
        quotes,
        reason_news=alert.MarketReasonNews("reason", ["source"]),
        position_00631l=alert.Position00631L("holding", 34.81),
        research_pack_context=alert._build_us_close_research_pack_context(),
    )

    assert "Research Pack" in context
    assert "market: US" in context
    assert "provider_map.quote: IBKR" in context
    assert "is_realtime: False" in context
    assert "自動交易指令" in context


def test_close_research_pack_context_degrades_on_failure(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("IBKR unavailable for U0000000")

    monkeypatch.setenv(alert.RESEARCH_PACK_US_CLOSE_ENABLED_ENV, "1")
    monkeypatch.setattr("scripts.researcher.build_research_pack_prompt_context", boom)

    context = alert._build_us_close_research_pack_context()

    assert "Research Pack" in context
    assert "unavailable: RuntimeError" in context
    assert "U0000000" not in context
    assert "U***" in context
    assert "已降級使用原本行情快照" in context
    assert "不得因 Research Pack 缺失" in context


def test_close_research_pack_context_disabled_by_default(monkeypatch):
    monkeypatch.delenv(alert.RESEARCH_PACK_US_CLOSE_ENABLED_ENV, raising=False)
    assert alert._build_us_close_research_pack_context() == ""


def test_close_mode_runs_us_overnight_research_but_open_does_not(monkeypatch):
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -2.3, asof="2026-06-10 00:00:00")]
    calls = []
    sent = []

    monkeypatch.setattr(alert, "fetch_us_market_quotes", lambda *a, **k: quotes)
    monkeypatch.setattr(alert, "fetch_market_reason_news", lambda *a, **k: alert.MarketReasonNews("reason", ["source"]))
    monkeypatch.setattr(alert, "is_close_data_recent", lambda *a, **k: True)
    monkeypatch.setattr(alert, "is_intraday_data_fresh", lambda *a, **k: True)
    monkeypatch.setattr(alert, "_already_sent", lambda *a, **k: False)
    monkeypatch.setattr(alert, "_mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_record_fetch_outcome", lambda *a, **k: False)
    monkeypatch.setattr(alert, "load_00631l_position", lambda *a, **k: alert.Position00631L("holding", 34.81))
    monkeypatch.setattr(alert, "generate_llm_market_read", lambda *a, **k: None)
    monkeypatch.setattr(alert, "generate_llm_playbook_read", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_load_round_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_save_round_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(alert, "send_dm", lambda title, content: sent.append((title, content)) or True)

    def fake_research(*args, **kwargs):
        calls.append(kwargs.get("dry_run"))
        return {"success": True, "report": "Research body", "output_path": "/tmp/us.md"}

    monkeypatch.setattr(alert, "run_us_overnight_research", fake_research)

    assert alert.run("close") == 0
    assert calls == [False]
    assert "Research W 美股收盤研究摘要" in sent[-1][1]

    calls.clear()
    sent.clear()
    assert alert.run("open") == 0
    assert calls == []
    assert "Research W 美股收盤研究摘要" not in sent[-1][1]


# ── #11 較上一輪變化行 ───────────────────────────────────────────────


def test_prev_round_line_shows_delta_descriptors():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.4, asof="2026-06-10 01:30:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -0.5, asof="2026-06-10 01:30:00"),
    ]
    prev_round = {
        "asof": "2026-06-09 23:30:00",
        "mode": "intraday",
        "saved_at": "2026-06-10T01:00",
        "quotes": {"^IXIC": -0.8, "SOXX": -1.1},
    }
    line = alert._format_prev_round_line(quotes, prev_round, now=datetime(2026, 6, 10, 1, 30))
    assert line is not None
    assert line.startswith("📊 較上一輪（盤中 01:00）：")
    assert "Nasdaq -0.80%→-1.40%（跌幅擴大）" in line
    assert "SOXX -1.10%→-0.50%（跌幅收斂）" in line


def test_prev_round_line_lists_all_changed_and_folds_flat():
    # 每段必推後：有變化的標的全列（不再砍到 3 檔），持平者收斂成尾註
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.4, asof="2026-06-10 03:00:00"),
        alert.MarketQuote("QQQ", "QQQ", "tech", 480.0, -1.5, asof="2026-06-10 03:00:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -2.0, asof="2026-06-10 03:00:00"),
        alert.MarketQuote("NVDA", "NVIDIA", "semiconductor", 130.0, 0.02, asof="2026-06-10 03:00:00"),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 185.0, 0.01, asof="2026-06-10 03:00:00"),
    ]
    prev_round = {
        "asof": "2026-06-10 01:30:00",
        "mode": "intraday",
        "saved_at": "2026-06-10T01:30",
        "quotes": {"^IXIC": -0.8, "QQQ": -1.1, "SOXX": -1.0, "NVDA": 0.0, "TSM": 0.0},
    }
    line = alert._format_prev_round_line(quotes, prev_round, now=datetime(2026, 6, 10, 3, 0))
    assert line is not None
    # 3 檔有變化全列（不砍到 3 檔上限以外的問題；此處剛好 3 檔變化）
    assert "Nasdaq -0.80%→-1.40%（跌幅擴大）" in line
    assert "QQQ -1.10%→-1.50%（跌幅擴大）" in line
    assert "SOXX -1.00%→-2.00%（跌幅擴大）" in line
    # NVDA/TSM 變化 < flat_eps → 收斂成尾註，不逐檔洗版
    assert "其餘 2 檔大致持平" in line


def test_prev_round_line_all_flat_says_little_change():
    # 全體與上一輪持平：明說「變化不大」，避免連推顯得像重複卡
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -0.82, asof="2026-06-10 03:00:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -1.05, asof="2026-06-10 03:00:00"),
    ]
    prev_round = {
        "asof": "2026-06-10 01:30:00",
        "mode": "intraday",
        "saved_at": "2026-06-10T01:30",
        "quotes": {"^IXIC": -0.8, "SOXX": -1.1},
    }
    line = alert._format_prev_round_line(quotes, prev_round, now=datetime(2026, 6, 10, 3, 0))
    assert line is not None
    assert "核心標的較上一輪變化不大" in line


def test_prev_round_line_none_when_same_asof_or_stale():
    quotes = [alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.4, asof="2026-06-10 01:30:00")]
    # 同一份行情（asof 相同）→ 不對比
    same = {"asof": "2026-06-10 01:30:00", "mode": "intraday", "saved_at": "2026-06-10T01:00",
            "quotes": {"^IXIC": -0.8}}
    assert alert._format_prev_round_line(quotes, same, now=datetime(2026, 6, 10, 1, 30)) is None
    # 快照過舊（跨日）→ 不對比
    stale = {"asof": "2026-06-09 03:00:00", "mode": "intraday", "saved_at": "2026-06-09T03:00",
             "quotes": {"^IXIC": -0.8}}
    assert alert._format_prev_round_line(quotes, stale, now=datetime(2026, 6, 10, 1, 30)) is None
    # 無上一輪 → None
    assert alert._format_prev_round_line(quotes, None, now=datetime(2026, 6, 10, 1, 30)) is None


def test_save_and_load_round_snapshot_roundtrip(tmp_path):
    state = tmp_path / "state.json"
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.23, asof="2026-06-10 01:30:00"),
        alert.MarketQuote("CL=F", "原油", "commodity", 82.0, 0.9, asof="2026-06-10 01:30:00"),
    ]
    alert._save_round_snapshot(quotes, "intraday", path=state, now=datetime(2026, 6, 10, 1, 30))
    snap = alert._load_round_snapshot(path=state)
    assert snap is not None
    assert snap["mode"] == "intraday"
    assert snap["quotes"]["^IXIC"] == -1.23
    # 非核心標的（原油）不入快照
    assert "CL=F" not in snap["quotes"]


def test_delta_descriptor_flip_and_flat():
    assert alert._delta_descriptor(-0.5, 0.8) == "翻紅"
    assert alert._delta_descriptor(0.5, -0.8) == "翻黑"
    assert alert._delta_descriptor(-1.0, -1.05) == "大致持平"
    assert alert._delta_descriptor(1.0, 1.8) == "漲幅擴大"


# ── #13 新聞 relevance filter 擴詞庫 ─────────────────────────────────


def test_relevance_filter_accepts_supply_chain_and_geopolitics():
    # 台股/供應鏈中文標題
    assert alert._is_relevant_reason_title("台積電法說會釋出 AI 訂單展望，供應鏈同步走強") is True
    # 地緣政治 + 關稅
    assert alert._is_relevant_reason_title("US tariff on chips raises supply chain risk for Taiwan") is True
    # 匯率
    assert alert._is_relevant_reason_title("台幣匯率走貶，外資調節半導體持股") is True
    # 純生活風格仍應被擋
    assert alert._is_relevant_reason_title("Best summer travel destinations 2026") is False


# ── #15 LLM 研判 ────────────────────────────────────────────────────


def test_build_market_interpretation_puts_llm_read_first():
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.2),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.5),
    ]
    lines = alert._build_market_interpretation(
        quotes, None, mode="intraday", llm_read="科技與半導體同步走弱，隔日台股偏防守；因果待確認。"
    )
    body = "\n".join(lines)
    assert "本輪研判：科技與半導體同步走弱" in body
    # 研判行需排在規則化解讀之前
    assert body.index("本輪研判") < body.index("科技線目前")


def test_generate_llm_market_read_returns_none_on_failure(monkeypatch):
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.5)]

    class _BoomHelper:
        def chat(self, *a, **k):
            raise RuntimeError("route down")

    import utils.claude_helper as ch
    monkeypatch.setattr(ch, "ClaudeHelper", _BoomHelper)
    out = alert.generate_llm_market_read(quotes, reason_news=None, position_00631l=None, mode="intraday")
    assert out is None  # 失敗時回 None，由卡片退回規則化解讀


def test_generate_llm_market_read_now_covers_pre_us_and_close(monkeypatch):
    """三時段精華各自優化後：pre_us / close 也接 LM 研判（不再被 mode 閘門擋掉）。"""
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.5)]

    class _Helper:
        def chat(self, *a, **k):
            return "半導體偏弱，隔日台股偏防守；因果待確認。"

    import utils.claude_helper as ch
    monkeypatch.setattr(ch, "ClaudeHelper", lambda *a, **k: _Helper())

    for mode in ("close", "pre_us", "pre_tw"):
        out = alert.generate_llm_market_read(quotes, reason_news=None, position_00631l=None, mode=mode)
        assert out is not None and "偏防守" in out


def test_run_open_exercises_real_llm_read_signature(monkeypatch, tmp_path):
    """堵盲點：run() 真實呼叫 generate_llm_market_read（不 mock 它本身），
    只 mock 內部 ClaudeHelper；可抓出 run() 傳錯參數給研判函式的簽名錯誤。"""
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.4, asof="2026-06-09 09:35:00"),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.6, asof="2026-06-09 09:35:00"),
    ]
    monkeypatch.setattr(alert, "fetch_us_market_quotes", lambda *a, **k: quotes)
    monkeypatch.setattr(alert, "fetch_market_reason_news", lambda *a, **k: None)
    monkeypatch.setattr(alert, "is_intraday_data_fresh", lambda *a, **k: True)
    monkeypatch.setattr(alert, "_already_sent", lambda *a, **k: False)
    monkeypatch.setattr(alert, "_mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_record_fetch_outcome", lambda *a, **k: False)
    monkeypatch.setattr(alert, "_load_round_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(alert, "_save_round_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(alert, "load_00631l_position", lambda *a, **k: alert.Position00631L("holding", 34.81))

    class _Helper:
        def chat(self, *a, **k):
            return "科技與半導體同步走弱，隔日台股偏防守；因果待確認。"

    import utils.claude_helper as ch
    monkeypatch.setattr(ch, "ClaudeHelper", _Helper)

    sent: list = []
    monkeypatch.setattr(alert, "send_dm", lambda title, content: sent.append((title, content)) or True)

    assert alert.run("open") == 0
    assert len(sent) == 1
    assert "本輪研判：科技與半導體同步走弱" in sent[0][1]
    # 同時堵 generate_llm_playbook_read 簽名盲點：口徑建議也應吃到 LM 輸出
    assert "口徑建議：科技與半導體同步走弱" in sent[0][1]


# ── 操作口徑動態化（regime + LM 口徑增強）─────────────────────────────


def test_assess_market_regime_risk_up_on_synchronized_weakness():
    # 半導體同步走弱 + Nasdaq 明顯下挫 → 風險升溫（即使較上一輪持平）
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.76, asof="2026-06-10 01:30:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -3.14),
        alert.MarketQuote("NVDA", "NVIDIA", "stock", 170.0, -3.6),
        alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -4.18),
    ]
    # 上一輪 Nasdaq 也在 -1.7 附近（持平），不應因此誤判成 calm
    prev = {"quotes": {"^IXIC": -1.7}}
    out = alert._assess_market_regime(quotes, prev_round=prev, mode="intraday")
    assert out["regime"] == "risk_up"
    assert "風險升溫" in out["regime_sentence"]
    assert "半導體同步走弱" in out["regime_sentence"]


def test_assess_market_regime_risk_down_on_recovery():
    # 較上一輪跌幅收斂 → 風險降溫
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -0.5, asof="2026-06-10 01:30:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -0.4),
    ]
    prev = {"quotes": {"^IXIC": -1.6}}
    out = alert._assess_market_regime(quotes, prev_round=prev, mode="intraday")
    assert out["regime"] == "risk_down"
    assert "風險降溫" in out["regime_sentence"]


def test_assess_market_regime_diverge_on_split_semis():
    # 半導體分歧（SOXX 弱、NVDA 強）→ 訊號分歧
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -0.2, asof="2026-06-10 01:30:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, -1.2),
        alert.MarketQuote("NVDA", "NVIDIA", "stock", 170.0, 1.5),
    ]
    out = alert._assess_market_regime(quotes, mode="intraday")
    assert out["regime"] == "diverge"
    assert "訊號分歧" in out["regime_sentence"]


def test_assess_market_regime_calm_on_quiet_tape():
    # 小波動、無同步走弱 → 波動平穩
    quotes = [
        alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, 0.2, asof="2026-06-10 01:30:00"),
        alert.MarketQuote("SOXX", "SOXX 半導體", "semiconductor", 220.0, 0.1),
    ]
    out = alert._assess_market_regime(quotes, mode="intraday")
    assert out["regime"] == "calm"
    assert "波動平穩" in out["regime_sentence"]


def test_trade_playbook_falls_back_to_rule_stance_without_llm():
    # playbook_read 為 None 時，口徑退回規則化（依 regime × 持倉狀態）
    regime = {"regime": "risk_up", "regime_sentence": "半導體同步走弱，風險升溫。",
              "key_signal": "Nasdaq 能否守住本輪低點。"}
    lines = alert._format_trade_playbook(
        "intraday",
        position_00631l=alert.Position00631L("holding", 34.81),
        regime=regime,
        playbook_read=None,
    )
    body = "\n".join(lines)
    assert "本輪盤勢：半導體同步走弱，風險升溫。" in body
    assert "口徑建議：" in body
    assert "防守權重放到進攻之前" in body  # holding × risk_up 規則化口徑
    assert "待確認訊號：Nasdaq 能否守住本輪低點。" in body


def test_trade_playbook_prefers_llm_read_when_present():
    # playbook_read 有值時，口徑主體用 LM 輸出，並標註僅供人工參考
    regime = {"regime": "risk_up", "regime_sentence": "半導體同步走弱，風險升溫。",
              "key_signal": "Nasdaq 能否守住本輪低點。"}
    lines = alert._format_trade_playbook(
        "intraday",
        position_00631l=alert.Position00631L("flat", 37.08, 32.46, "2026-06-08"),
        regime=regime,
        playbook_read="隔日先按防守，等台積電開盤承接再評估低接。",
    )
    body = "\n".join(lines)
    assert "口徑建議：隔日先按防守" in body
    assert "僅供人工參考" in body


def test_generate_llm_playbook_read_returns_none_on_failure(monkeypatch):
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.5)]

    class _BoomHelper:
        def chat(self, *a, **k):
            raise RuntimeError("route down")

    import utils.claude_helper as ch
    monkeypatch.setattr(ch, "ClaudeHelper", _BoomHelper)
    out = alert.generate_llm_playbook_read(
        quotes, reason_news=None, position_00631l=None, mode="intraday", regime="風險升溫",
    )
    assert out is None  # 失敗回 None，由卡片退回規則化口徑


def test_generate_llm_playbook_read_now_covers_close_and_pre_us(monkeypatch):
    """close/pre_us 已納入 LM 口徑（精華段三時段對齊）；pre_tw 仍跳過。"""
    quotes = [alert.MarketQuote("TSM", "TSM ADR", "adr", 180.0, -1.5)]

    class _Helper:
        def chat(self, *a, **k):
            return "隔日先看半導體是否止穩。"

    import utils.claude_helper as ch
    monkeypatch.setattr(ch, "ClaudeHelper", _Helper)

    for mode in ("close", "pre_us", "pre_tw"):
        out = alert.generate_llm_playbook_read(
            quotes, reason_news=None, position_00631l=None, mode=mode, regime="風險升溫",
        )
        assert out == "隔日先看半導體是否止穩。"


def test_overnight_futures_disabled_by_default(monkeypatch):
    monkeypatch.delenv(alert.OVERNIGHT_FUTURES_ENABLED_ENV, raising=False)
    assert alert._fetch_overnight_futures_quotes("close") == []


def test_overnight_futures_uses_ibkr_when_enabled(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv(alert.OVERNIGHT_FUTURES_ENABLED_ENV, "1")

    def fake_ibkr_futures(**kwargs):
        return [
            SimpleNamespace(symbol="ES", permission_status="ok", last=7544.25,
                            close=7492.75, change_pct=0.687, asof="2026-06-19T11:00:00+08:00"),
            SimpleNamespace(symbol="NQ", permission_status="ok", last=30517.75,
                            close=29998.75, change_pct=1.73, asof=""),
        ]

    monkeypatch.setattr("utils.ibkr_provider.fetch_overnight_futures", fake_ibkr_futures)

    def _no_yf(*_a, **_k):
        raise AssertionError("IBKR 成功時不應呼叫 yfinance 兜底")

    monkeypatch.setattr(alert, "fetch_us_market_quotes", _no_yf)

    out = alert._fetch_overnight_futures_quotes("close")

    assert [q.symbol for q in out] == ["ES=F", "NQ=F"]
    assert out[0].name == "S&P500 期貨"
    assert out[0].price == 7544.25
    assert out[0].previous_close == 7492.75
    assert round(out[0].change_pct, 2) == 0.69


def test_overnight_futures_skips_invalid_ibkr_quote(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv(alert.OVERNIGHT_FUTURES_ENABLED_ENV, "1")

    def fake_ibkr_futures(**kwargs):
        return [
            SimpleNamespace(symbol="ES", permission_status="ok", last=7544.25,
                            close=7492.75, change_pct=0.687, asof=""),
            # NQ：無 close → 視為無效，跳過不混入
            SimpleNamespace(symbol="NQ", permission_status="ok", last=30517.75,
                            close=None, change_pct=None, asof=""),
        ]

    monkeypatch.setattr("utils.ibkr_provider.fetch_overnight_futures", fake_ibkr_futures)
    monkeypatch.setattr(alert, "fetch_us_market_quotes",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不應走 yfinance")))

    out = alert._fetch_overnight_futures_quotes("close")
    assert [q.symbol for q in out] == ["ES=F"]


def test_overnight_futures_falls_back_to_yfinance_on_ibkr_failure(monkeypatch):
    monkeypatch.setenv(alert.OVERNIGHT_FUTURES_ENABLED_ENV, "1")

    def boom(**_kwargs):
        raise RuntimeError("gateway down for U0000000")

    monkeypatch.setattr("utils.ibkr_provider.fetch_overnight_futures", boom)

    yf_quotes = [
        alert.MarketQuote("ES=F", "S&P500 期貨", "index", 7561.75, 1.85),
        alert.MarketQuote("NQ=F", "Nasdaq 期貨", "index", 30634.75, 3.19),
    ]
    monkeypatch.setattr(alert, "fetch_us_market_quotes", lambda *_a, **_k: list(yf_quotes))

    out = alert._fetch_overnight_futures_quotes("intraday")
    assert [q.symbol for q in out] == ["ES=F", "NQ=F"]
    assert out[0].price == 7561.75


def test_build_card_renders_overnight_futures():
    quotes = [alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.2)]
    futures = [
        alert.MarketQuote("ES=F", "S&P500 期貨", "index", 7544.25, 0.69),
        alert.MarketQuote("NQ=F", "Nasdaq 期貨", "index", 30517.75, 1.73),
    ]

    _title, content = alert.build_overnight_card(
        quotes, mode="intraday", now=datetime(2026, 6, 19, 1, 30),
        overnight_futures=futures,
    )

    assert "S&P500 期貨 ES=F" in content
    assert "Nasdaq 期貨 NQ=F" in content
    assert "+0.69%" in content
    assert "+1.73%" in content


def test_build_card_without_overnight_futures_has_no_futures_line():
    quotes = [alert.MarketQuote("^IXIC", "Nasdaq", "index", 18000.0, -1.2)]

    _title, content = alert.build_overnight_card(
        quotes, mode="intraday", now=datetime(2026, 6, 19, 1, 30),
    )

    assert "ES=F" not in content
    assert "NQ=F" not in content
