#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intraday_alert.py — 盤中即時警報

每 30 分鐘由 crontab 呼叫，檢查觀察池股票是否觸發條件，
觸發時立即推飛書通知。

觸發條件：
1. 漲跌幅 > 3%
2. 跌幅 > -3%
3. 成交量爆量（> 前5日均量 2倍）

用 yfinance 取即時報價（免費、無需 key）。
"""

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path

# 清除代理變數
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 已發送警報的紀錄（避免同一天重複推送同一支股票）
ALERT_SENT_FILE = _BASE_DIR / "logs" / "intraday_alert_sent.json"
# 連續 0 支報價計數器
ZERO_QUOTE_STATE_FILE = _BASE_DIR / "logs" / "intraday_zero_quote_state.json"
ZERO_QUOTE_ALERT_THRESHOLD = 3  # 連續 N 輪 0 支報價就告警

# 觸發門檻（fallback，優先用 stocks.yaml alert_defaults / 個股 alert_pct）
CHANGE_THRESHOLD_PCT = 3.0
VOLUME_RATIO_THRESHOLD = 2.0

CHAT_ID = os.getenv("FEISHU_DEFAULT_CHAT_ID", "")


def _load_watchlist() -> tuple[list[dict], dict, dict]:
    """從 stocks.yaml 載入觀察池、alert_defaults、market_scan 配置"""
    import yaml
    config_path = _BASE_DIR / "config" / "stocks.yaml"
    if not config_path.exists():
        logger.warning("stocks.yaml 不存在")
        return [], {}, {}
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return (
        data.get("watch_list", []),
        data.get("alert_defaults", {}),
        data.get("market_scan", {}),
    )


def _load_pushed_today() -> dict[str, dict]:
    """讀取今日已推送紀錄，回 {symbol: {change_pct, price, time}}。

    對比差異／可重推的資料基礎：記下「上次推送時的 change_pct」，供本輪判斷是否
    相對上次再變動超過步進閾值（restep）才重推，避免同一支當天一次性永久靜音。
    向後相容舊格式（{"date", "symbols": [...]}）：舊 symbol 轉成無基準紀錄
    （change_pct=None），代表已推過但無對比基準，下次顯著變動仍可重推。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if ALERT_SENT_FILE.exists():
            data = json.loads(ALERT_SENT_FILE.read_text(encoding="utf-8"))
            if data.get("date") != today:
                return {}
            pushed = data.get("pushed")
            if isinstance(pushed, dict):
                return pushed
            # 舊格式相容：symbols 清單 → 無基準紀錄
            old_symbols = data.get("symbols")
            if isinstance(old_symbols, list):
                return {str(s): {"change_pct": None, "price": None, "time": None} for s in old_symbols}
    except Exception:
        pass
    return {}


def _save_pushed(pushed: dict[str, dict]):
    """儲存今日已推送紀錄（含上次推送的 change_pct，供下輪對比/重推判斷）"""
    today = datetime.now().strftime("%Y-%m-%d")
    ALERT_SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_SENT_FILE.write_text(
        json.dumps({"date": today, "pushed": pushed}, ensure_ascii=False),
        encoding="utf-8",
    )


def _fetch_via_twse(symbols: list[str]) -> dict:
    """台灣官方 MIS 即時報價優先層（本土源，不吃 yfinance rate limit）。

    依後綴分市場：.TW→上市(tse_)、.TWO→上櫃(otc_)；回 {原始symbol: {...}}。
    只給 price/prev_close/change_pct（量比 vol_ratio 由 MIS 拿不到，留給 yfinance
    補；缺量比只少『爆量』這個加分條件，漲跌幅警報主線不受影響）。
    撮合間隙 z='-' 的標的本輪自動跳過，不硬湊。失敗/空回 {} 交由 yfinance fallback。
    """
    try:
        from utils.twse_realtime import fetch_monitor_quotes
    except Exception:
        return {}
    tse_ids, otc_ids = [], []
    code_to_sym: dict[str, str] = {}
    for sym in symbols:
        code = sym.split(".")[0]
        code_to_sym[code] = sym
        if sym.endswith(".TWO"):
            otc_ids.append(code)
        elif sym.endswith(".TW"):
            tse_ids.append(code)
    result: dict = {}
    try:
        for ids, market in ((tse_ids, "twse"), (otc_ids, "tpex")):
            if not ids:
                continue
            for code, d in fetch_monitor_quotes(ids, market=market).items():
                sym = code_to_sym.get(code)
                if not sym or d.get("price") is None:
                    continue
                result[sym] = {
                    "price": d["price"],
                    "prev_close": d.get("prev_close"),
                    "change_pct": d.get("change_pct"),
                    "volume": d.get("volume"),
                    "avg_volume": None,
                    "vol_ratio": None,
                }
    except Exception:
        logger.warning("TWSE MIS 盤中報價失敗，fallback yfinance", exc_info=True)
        return {}
    return result


def _fetch_quotes(symbols: list[str]) -> dict:
    """批量取即時報價：台灣官方 MIS 優先，未覆蓋/失敗的標的再用 yfinance 補。"""
    result = _fetch_via_twse(symbols)
    missing = [s for s in symbols if s not in result]
    if not missing:
        return result
    import yfinance as yf

    try:
        tickers = yf.Tickers(" ".join(missing))
        for sym in missing:
            try:
                ticker = tickers.tickers.get(sym)
                if not ticker:
                    continue
                info = ticker.fast_info
                price = getattr(info, "last_price", None)
                prev_close = getattr(info, "previous_close", None)
                volume = getattr(info, "last_volume", None)

                # 取 5 日均量
                hist = ticker.history(period="5d")
                avg_vol = hist["Volume"].mean() if len(hist) > 0 else None

                if price and prev_close:
                    change_pct = (price - prev_close) / prev_close * 100
                    vol_ratio = (volume / avg_vol) if (volume and avg_vol and avg_vol > 0) else None
                    result[sym] = {
                        "price": round(price, 2),
                        "prev_close": round(prev_close, 2),
                        "change_pct": round(change_pct, 2),
                        "volume": volume,
                        "avg_volume": round(avg_vol) if avg_vol else None,
                        "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                    }
            except Exception as e:
                logger.debug(f"取 {sym} 報價失敗: {e}")
    except Exception as e:
        logger.error(f"yfinance 批量取價失敗: {e}")
    return result


def _delta_descriptor(prev_pct: float, cur_pct: float, *, flat_eps: float = 0.1) -> str:
    """描述本輪 change_pct 相對上次推送的變化（擴大/收斂/翻紅/翻黑/持平）。

    與 us_market_overnight_alert._delta_descriptor 同語意；盤中對比段共用此措辭，
    讓台股盤中與美股各段的「較上次/較上一輪」用詞一致。
    """
    if abs(cur_pct - prev_pct) < flat_eps:
        return "大致持平"
    if prev_pct <= 0 < cur_pct:
        return "翻紅"
    if prev_pct >= 0 > cur_pct:
        return "翻黑"
    if cur_pct < 0:
        return "跌幅擴大" if cur_pct < prev_pct else "跌幅收斂"
    return "漲幅擴大" if cur_pct > prev_pct else "漲幅收斂"


def _check_alerts(
    watchlist: list[dict],
    quotes: dict,
    sent: dict,
    alert_defaults: dict,
    *,
    restep_up_pct: float = 3.0,
    restep_down_pct: float = 2.5,
) -> list[dict]:
    """檢查觸發條件，回傳警報清單（支援分檔差異化門檻 + 事件式可重推 + 對比差異）。

    事件式可重推：已推過的標的不再整天靜音；相對「上次推送的 change_pct」再變動
    超過步進閾值（漲 restep_up_pct / 跌 restep_down_pct，配合 2 倍槓桿下跌更敏感）
    才解除靜音、再推一次，並帶上「📊 較上次」對比基準；變化不足則維持靜音不洗版。
    """
    alerts = []
    for stock in watchlist:
        sym = stock.get("symbol", "")
        name = stock.get("name", sym)
        if not sym:
            continue
        q = quotes.get(sym)
        if not q:
            continue

        # 門檻優先級：個股 alert_pct > category default > 全域 fallback
        threshold = stock.get("alert_pct") or alert_defaults.get(stock.get("category", ""), CHANGE_THRESHOLD_PCT)

        reasons = []
        change = q["change_pct"]
        if abs(change) >= threshold:
            direction = "📈 漲" if change > 0 else "📉 跌"
            reasons.append(f"{direction} {change:+.2f}%（門檻 {threshold}%）")

        vol_ratio = q.get("vol_ratio")
        if vol_ratio and vol_ratio >= VOLUME_RATIO_THRESHOLD:
            reasons.append(f"🔥 量比 {vol_ratio:.1f}x")

        if not reasons:
            continue

        # 事件式可重推：已推過則需相對上次推送有顯著變化才重推
        prev = sent.get(sym)
        prev_change = None
        if isinstance(prev, dict) and prev.get("change_pct") is not None:
            prev_change = float(prev["change_pct"])
            delta = change - prev_change
            # 步進閾值：上漲方向用 up、下跌方向用 down（下跌更敏感）
            stepped = (delta >= restep_up_pct) or (delta <= -restep_down_pct)
            if not stepped:
                continue  # 變化不足，維持靜音不洗版

        compare_line = None
        if prev_change is not None:
            compare_line = (
                f"📊 較上次 {prev_change:+.2f}%→{change:+.2f}%"
                f"（{_delta_descriptor(prev_change, change)}）"
            )

        alerts.append({
            "symbol": sym,
            "name": name,
            "price": q["price"],
            "change_pct": change,
            "vol_ratio": vol_ratio,
            "reasons": reasons,
            "compare_line": compare_line,
            "is_restep": prev_change is not None,
        })
    return alerts


def _send_alerts(alerts: list[dict], market_picks: list[dict] | None = None):
    """推送警報到飛書（關注池 + 全市場異動）"""
    from utils.lark_notifier import LarkNotifier
    notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))

    now_str = datetime.now().strftime("%H:%M")
    lines = [f"⏰ {now_str} 盤中即時警報\n"]

    if alerts:
        lines.append(f"🔴 **關注池觸發**（{len(alerts)} 支）\n")
        for a in alerts:
            reason_str = "、".join(a["reasons"])
            restep_tag = "（再觸發）" if a.get("is_restep") else ""
            lines.append(f"**{a['name']}** ${a['price']}（{a['change_pct']:+.2f}%）{restep_tag}")
            lines.append(f"  觸發：{reason_str}")
            if a.get("compare_line"):
                lines.append(f"  {a['compare_line']}")
            lines.append("")

    if market_picks:
        lines.append(f"🟡 **全市場異動精選**（Top {len(market_picks)}）\n")
        for m in market_picks:
            turnover_str = f"{m['turnover'] / 1e8:.1f}億" if m['turnover'] >= 1e8 else f"{m['turnover'] / 1e4:.0f}萬"
            lines.append(f"**{m['name']}（{m['stock_id']}）** ${m['price']}（{m['change_pct']:+.2f}%）成交額 {turnover_str}")
            lines.append("")

    total = len(alerts) + (len(market_picks) if market_picks else 0)
    lines.append(f"共 {total} 支觸發")
    content = "\n".join(lines)

    notifier.send_message(CHAT_ID, "⚡ 盤中即時警報", content)
    logger.info(f"已推送 {len(alerts)} 則關注池 + {len(market_picks) if market_picks else 0} 則全市場警報")


def _check_zero_quote(count: int):
    """連續 N 輪 0 支報價時發告警"""
    today = datetime.now().strftime("%Y-%m-%d")
    state = {"date": today, "consecutive": 0, "alerted": False}
    try:
        if ZERO_QUOTE_STATE_FILE.exists():
            state = json.loads(ZERO_QUOTE_STATE_FILE.read_text(encoding="utf-8"))
            if state.get("date") != today:
                state = {"date": today, "consecutive": 0, "alerted": False}
    except Exception:
        pass

    if count == 0:
        state["consecutive"] = state.get("consecutive", 0) + 1
    else:
        state["consecutive"] = 0
        state["alerted"] = False

    ZERO_QUOTE_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )

    if state["consecutive"] >= ZERO_QUOTE_ALERT_THRESHOLD and not state.get("alerted"):
        state["alerted"] = True
        ZERO_QUOTE_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        _send_zero_quote_alert(state["consecutive"])


def _send_zero_quote_alert(consecutive: int):
    """推送 0 支報價告警"""
    from utils.lark_notifier import LarkNotifier
    notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))
    now_str = datetime.now().strftime("%H:%M")
    content = (
        f"⏰ {now_str}\n"
        f"⚠️ yfinance 連續 {consecutive} 輪取得 0 支報價\n"
        f"盤中警報可能啞掉，請檢查網路或資料源狀態"
    )
    notifier.send_message(CHAT_ID, "⚠️ 報價資料源異常", content)
    logger.warning(f"連續 {consecutive} 輪 0 支報價，已發告警")


def _run_market_scan(market_scan_cfg: dict, watchlist_symbols: set[str], sent: set[str]) -> list[dict]:
    """全市場掃描：TWSE/TPEX 即時報價 → 過濾 → Top N"""
    try:
        from utils.finmind_helper import FinMindHelper
        from utils.twse_realtime import fetch_all_realtime
    except ImportError as e:
        logger.warning(f"全市場掃描缺少依賴: {e}")
        return []

    min_turnover = market_scan_cfg.get("min_turnover", 50_000_000)
    change_threshold = market_scan_cfg.get("change_threshold_pct", 4.0)
    top_n = market_scan_cfg.get("top_n", 5)

    fm = FinMindHelper()
    universe = fm.get_universe(include_tpex=True)
    universe = fm.filter_common_stocks(universe)

    if universe.empty:
        logger.warning("全市場清單為空")
        return []

    twse_ids = universe[universe["type"] == "twse"]["stock_id"].tolist()
    tpex_ids = universe[universe["type"] == "tpex"]["stock_id"].tolist()
    logger.info(f"全市場掃描：上市 {len(twse_ids)} + 上櫃 {len(tpex_ids)} 檔")

    quotes = fetch_all_realtime(twse_ids, tpex_ids)
    if not quotes:
        logger.warning("全市場即時報價為空")
        return []

    # 過濾：成交額 + 漲跌幅 + 排除 watchlist 已有的 + 排除今日已推送的
    watchlist_codes = {s.replace(".TW", "").replace(".TWO", "") for s in watchlist_symbols}
    candidates = []
    for sid, q in quotes.items():
        if sid in watchlist_codes:
            continue
        if sid in sent:
            continue
        if q["turnover"] < min_turnover:
            continue
        if abs(q["change_pct"]) < change_threshold:
            continue
        candidates.append(q)

    # 按漲跌幅絕對值排序，取 Top N
    candidates.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    picks = candidates[:top_n]
    logger.info(f"全市場掃描通過 {len(candidates)} 檔，取 Top {len(picks)}")
    return picks


def main():
    """主程式"""
    now = datetime.now()

    # 只在交易時段執行（台股 9:00-13:30）
    hour_min = now.hour * 100 + now.minute
    if hour_min < 900 or hour_min > 1330:
        logger.debug("非交易時段，跳過")
        return

    # 週末跳過
    if now.weekday() >= 5:
        logger.debug("週末，跳過")
        return

    watchlist, alert_defaults, market_scan_cfg = _load_watchlist()
    if not watchlist:
        watchlist = []

    # 併入 00631L 相關性追蹤池（tw 時段可抓現貨的標的）；只供風險連動觀察，
    # 不進選股池。已在 watchlist 的標的不重複加，相關性標的給較敏感的預設門檻。
    restep_up, restep_down = 3.0, 2.5
    try:
        from utils.correlation_pool import symbols_for_market, load_comparison_params
        existing = {s.get("symbol") for s in watchlist}
        for cs in symbols_for_market("tw"):
            if cs.symbol in existing:
                continue
            watchlist.append({
                "symbol": cs.symbol,
                "name": cs.name,
                "category": "correlation",
                # 相關性標的（含 00631L/0050/2330）對連動更敏感，門檻略低於通用 3%
                "alert_pct": 2.0 if cs.importance == "core" else 2.5,
            })
            existing.add(cs.symbol)
        cmp_params = load_comparison_params()
        restep_up, restep_down = cmp_params.restep_up_pct, cmp_params.restep_down_pct
    except Exception:
        logger.warning("相關性追蹤池併入失敗，僅用 stocks.yaml watchlist", exc_info=True)

    if not watchlist:
        logger.warning("觀察池為空")
        return

    symbols = [s["symbol"] for s in watchlist if s.get("symbol")]
    logger.info(f"檢查 {len(symbols)} 支股票（含相關性追蹤池）...")

    quotes = _fetch_quotes(symbols)
    logger.info(f"取得 {len(quotes)} 支報價")

    # 追蹤連續 0 支報價（僅針對 watchlist 的 yfinance）
    _check_zero_quote(len(quotes))

    sent = _load_pushed_today()
    alerts = _check_alerts(
        watchlist, quotes, sent, alert_defaults,
        restep_up_pct=restep_up, restep_down_pct=restep_down,
    )

    # 全市場掃描
    market_picks = []
    if market_scan_cfg.get("enabled"):
        market_picks = _run_market_scan(market_scan_cfg, set(symbols), sent)

    if alerts or market_picks:
        _send_alerts(alerts, market_picks)
        # 記錄本次推送快照（symbol → change_pct/price/time），供下次「可重推」對比
        now_hm = now.strftime("%H:%M")
        for a in alerts:
            sent[a["symbol"]] = {
                "change_pct": a["change_pct"],
                "price": a["price"],
                "time": now_hm,
            }
        for m in market_picks:
            sent[m["stock_id"]] = {
                "change_pct": m["change_pct"],
                "price": m["price"],
                "time": now_hm,
            }
        _save_pushed(sent)
    else:
        logger.info("無觸發警報")


if __name__ == "__main__":
    main()
