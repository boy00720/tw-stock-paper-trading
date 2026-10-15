#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stock_monitor.py — 股市 W

責任：
1. 從 config/stocks.yaml 載入監控清單（取代硬編碼）
2. yfinance 報價 + FinMind 三大法人（個股並發）
3. Claude 分析（整合 profile + 歷史研究 + 記憶庫）
4. Brief 存檔到 logs/stock_briefs/，供記憶管家吸收
5. 警報 JSON merge（morning + afternoon 不覆蓋）
"""

import os
import sys

# 清除代理變數，避免 403 CONNECT tunnel failed
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import json
import logging
import time
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config_loader import load_config
from utils.lark_notifier import LarkNotifier
from utils.watchlist_schema import cross_signals, load_watchlist, pool_items
from utils.master_agent_control import get_current_control, write_payload
from utils.claude_helper import ClaudeHelper
from utils.data_validator import (
    validate_stock_data_freshness,
    validate_market_data_completeness,
    validate_with_fallback,
    DataQualityReport,
    DataStaleError,
)
from scripts.daily_stock_report import build_tracking_bucket_snapshot, render_tracking_bucket_digest
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_BASE_DIR / "logs" / "stock_monitor.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# 設定載入
# ──────────────────────────────────────────────────────────────────────

# 個股結構：(symbol, name, lower, upper, alert_pct, meta)
StockEntry = Tuple[str, str, float, float, float, Dict[str, Any]]


def extract_code(symbol: str) -> str:
    return symbol.replace(".TW", "").replace(".TWO", "").lstrip("^")


def display_name(name: str, symbol: str) -> str:
    code = extract_code(symbol)
    suffix = f"（{code}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


def format_watch_meta(meta: Dict[str, Any]) -> str:
    pool = meta.get("pool", "")
    if not pool:
        return ""

    labels = {
        "short_term": "短線池",
        "medium_term": "中線池",
        "long_term": "長線池",
        "intraday": "日內池",
    }
    parts = [labels.get(pool, pool)]

    strict_passed = meta.get("strict_passed")
    if strict_passed is True:
        parts.append("嚴格條件通過")
    elif strict_passed is False:
        blockers = meta.get("strict_hard_fails") or []
        if blockers:
            parts.append(f"嚴格條件阻塞 {','.join(blockers)}")
        else:
            parts.append("未通過嚴格條件")

    return f" [{' | '.join(parts)}]"


def load_watch_list() -> List[StockEntry]:
    """從 config/stocks.yaml 載入監控清單，合併類別預設 alert_pct。
    並自動合併 logs/watchlist/current.json 的動態觀察池（各選股 agent 輸出）。
    """
    try:
        cfg = load_config(str(_BASE_DIR / "config" / "stocks.yaml"))
    except Exception as e:
        logger.error("載入 stocks.yaml 失敗: %s，使用空清單", e)
        cfg = {}

    defaults = cfg.get("alert_defaults", {})
    entries: List[StockEntry] = []
    seen_symbols: set = set()
    for item in cfg.get("watch_list", []):
        symbol = item.get("symbol", "")
        name = item.get("name", symbol)
        if not symbol:
            continue
        lower = float(item.get("lower", 0))
        upper = float(item.get("upper", 99999))
        alert_pct = float(
            item.get("alert_pct")
            or defaults.get(item.get("category", ""), 3.0)
        )
        entries.append((symbol, name, lower, upper, alert_pct, {}))
        seen_symbols.add(symbol)

    logger.info("從 stocks.yaml 載入 %d 檔監控標的", len(entries))

    # 合併動態觀察池
    watchlist_path = _BASE_DIR / "logs" / "watchlist" / "current.json"
    if watchlist_path.exists():
        try:
            wl = load_watchlist(watchlist_path)
            dynamic_alert_pct = float(defaults.get("dynamic", 3.0))
            added = 0
            for pool_key in ("short_term", "medium_term", "long_term"):
                for item in pool_items(wl, pool_key):
                    sym = item.get("symbol", "")
                    if not sym or sym in seen_symbols:
                        continue
                    entries.append((
                        sym,
                        item.get("name", sym),
                        0.0,
                        99999.0,
                        dynamic_alert_pct,
                        {
                            "pool": pool_key,
                            "strict_passed": item.get("strict_passed"),
                            "strict_hard_fails": item.get("strict_hard_fails", []),
                        },
                    ))
                    seen_symbols.add(sym)
                    added += 1
            if added:
                logger.info("合併動態觀察池: 新增 %d 檔（短/中/長池總計）", added)
            # 跨池訊號標記（log 用）
            cross = cross_signals(wl)
            if cross:
                logger.info("⭐ 跨池訊號 %d 檔: %s", len(cross), list(cross.keys())[:5])
        except Exception as e:
            logger.warning("讀取 watchlist/current.json 失敗，跳過動態合併: %s", e)

    return entries


# ──────────────────────────────────────────────────────────────────────
# 資料取得
# ──────────────────────────────────────────────────────────────────────




def _finmind_api_get(dataset: str, *, start_date: str, data_id: str = "") -> List[Dict[str, Any]]:
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": dataset,
        "start_date": start_date,
    }
    if data_id:
        params["data_id"] = data_id

    headers: Dict[str, str] = {}
    token = os.getenv("FINMIND_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"FinMind API response missing data list: keys={list(payload.keys())}")
    return data





def fetch_institutional_data() -> Dict[str, Any]:
    """使用 FinMind 抓取台股三大法人買賣超數據（大盤總計 + 個股買賣超榜）"""
    start_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    df = None
    stock_df = None

    try:
        from FinMind.data import DataLoader

        dl = DataLoader()
        token = os.getenv("FINMIND_TOKEN")
        if token:
            dl.login_by_token(api_token=token)
        else:
            logger.warning("FINMIND_TOKEN 未設，免費額度可能不足")

        try:
            df = dl.taiwan_stock_institutional_investors_total(start_date=start_date)
        except Exception as sdk_error:
            logger.warning("FinMind SDK 大盤法人資料失敗，改走 HTTP fallback: %s", sdk_error)
            df = pd.DataFrame(_finmind_api_get("TaiwanStockTotalInstitutionalInvestors", start_date=start_date))

        try:
            stock_df = dl.taiwan_stock_institutional_investors(stock_id="2330", start_date=start_date)
        except Exception as sdk_error:
            logger.warning("FinMind SDK 個股法人資料失敗，改走 HTTP fallback: %s", sdk_error)
            stock_df = pd.DataFrame(_finmind_api_get("TaiwanStockInstitutionalInvestorsBuySell", start_date=start_date, data_id="2330"))
    except Exception as e:
        logger.error("FinMind total data fetch failed: %s", e)
        return {
            "market_total_text": f"三大法人大盤數據獲取失敗: {e}",
            "leaders": {},
        }

    if df is None or df.empty:
        return {
            "market_total_text": "無法取得大盤三大法人買賣超數據",
            "leaders": {},
        }

    recent_date = df["date"].max()
    daily_data = df[df["date"] == recent_date]

    result = [f"📅 全市場法人籌碼動向 ({recent_date})"]
    for _, row in daily_data.iterrows():
        investor = row["name"]
        net_buy_sell = float(row["buy"] or 0) - float(row["sell"] or 0)
        if investor == "total":
            continue
        net_billion = net_buy_sell / 100_000_000
        if abs(net_billion) < 0.005:
            continue
        trend = "買超" if net_billion > 0 else "賣超"

        inv_name = investor
        if investor == "Foreign_Investor":
            inv_name = "外資"
        elif investor == "Foreign_Dealer_Self":
            inv_name = "外資自營商"
        elif "Investment" in investor:
            inv_name = "投信"
        elif "Dealer_Hedging" in investor:
            inv_name = "自營商(避險)"
        elif "Dealer_self" in investor:
            inv_name = "自營商(自行買賣)"

        result.append(f"  - {inv_name}: {trend} {abs(net_billion):.2f} 億元")

    leaders: Dict[str, Any] = {}
    try:
        if stock_df is not None and not stock_df.empty:
            leaders = build_institutional_leaderboard(stock_df)
    except Exception as leaderboard_error:
        logger.warning("全市場法人個股榜抓取失敗: %s", leaderboard_error)

    return {
        "market_total_text": "\n".join(result),
        "leaders": leaders,
    }



    if df is None or getattr(df, "empty", True):
        return {}

    latest_date = df["date"].max()
    daily_df = df[df["date"] == latest_date].copy()
    if daily_df.empty:
        return {}

    def map_investor(name: str) -> Optional[str]:
        if "Foreign" in name:
            return "foreign"
        if "Investment" in name:
            return "investment"
        if "Dealer" in name:
            return "dealer"
        return None

    leaders: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "foreign": {"buy_top": [], "sell_top": []},
        "investment": {"buy_top": [], "sell_top": []},
        "dealer": {"buy_top": [], "sell_top": []},
    }

    rows: Dict[str, List[Dict[str, Any]]] = {"foreign": [], "investment": [], "dealer": []}
    for _, row in daily_df.iterrows():
        investor_key = map_investor(str(row.get("name") or ""))
        stock_id = str(row.get("stock_id") or "")
        if not investor_key or not stock_id or stock_id == "TAIEX":
            continue
        net_lots = (float(row.get("buy") or 0) - float(row.get("sell") or 0)) / 1000.0
        rows[investor_key].append({
            "symbol": f"{stock_id}.TW",
            "name": str(row.get("stock_name") or stock_id),
            "today_lots": round(net_lots, 0),
        })

    for investor_key, items in rows.items():
        buy_top = sorted([item for item in items if item["today_lots"] > 0], key=lambda item: item["today_lots"], reverse=True)[:top_n]
        sell_top = sorted([item for item in items if item["today_lots"] < 0], key=lambda item: item["today_lots"])[:top_n]
        leaders[investor_key]["buy_top"] = buy_top
        leaders[investor_key]["sell_top"] = sell_top

    return leaders




def build_institutional_leaderboard(df: Any, top_n: int = 5) -> Dict[str, Any]:
    if df is None or getattr(df, "empty", True):
        return {}

    latest_date = df["date"].max()
    daily_df = df[df["date"] == latest_date].copy()
    if daily_df.empty:
        return {}

    def map_investor(name: str) -> Optional[str]:
        if "Foreign" in name:
            return "foreign"
        if "Investment" in name:
            return "investment"
        if "Dealer" in name:
            return "dealer"
        return None

    leaders: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "foreign": {"buy_top": [], "sell_top": []},
        "investment": {"buy_top": [], "sell_top": []},
        "dealer": {"buy_top": [], "sell_top": []},
    }

    rows: Dict[str, List[Dict[str, Any]]] = {"foreign": [], "investment": [], "dealer": []}
    for _, row in daily_df.iterrows():
        investor_key = map_investor(str(row.get("name") or ""))
        stock_id = str(row.get("stock_id") or "")
        if not investor_key or not stock_id or stock_id == "TAIEX":
            continue
        net_lots = (float(row.get("buy") or 0) - float(row.get("sell") or 0)) / 1000.0
        rows[investor_key].append({
            "symbol": f"{stock_id}.TW",
            "name": str(row.get("stock_name") or stock_id),
            "today_lots": round(net_lots, 0),
        })

    for investor_key, items in rows.items():
        buy_top = sorted([item for item in items if item["today_lots"] > 0], key=lambda item: item["today_lots"], reverse=True)[:top_n]
        sell_top = sorted([item for item in items if item["today_lots"] < 0], key=lambda item: item["today_lots"])[:top_n]
        leaders[investor_key]["buy_top"] = buy_top
        leaders[investor_key]["sell_top"] = sell_top

    return leaders


def build_watchlist_institutional_leaderboard(
    stocks: Dict[str, Any],
    watch_list: List[StockEntry],
    top_n: int = 5,
) -> Dict[str, Any]:
    leaders: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "foreign": {"buy_top": [], "sell_top": []},
        "investment": {"buy_top": [], "sell_top": []},
        "dealer": {"buy_top": [], "sell_top": []},
    }
    investor_map = {
        "foreign": "外資",
        "investment": "投信",
        "dealer": "自營商",
    }
    name_map = {symbol: name for symbol, name, *_ in watch_list}

    for leader_key, investor_name in investor_map.items():
        rows: List[Dict[str, Any]] = []
        for symbol, stats in (stocks or {}).items():
            investor_stats = stats.get(investor_name) or {}
            today_lots = float(investor_stats.get("today") or 0)
            if abs(today_lots) < 0.5:
                continue
            rows.append({
                "symbol": symbol,
                "name": name_map.get(symbol, symbol),
                "today_lots": round(today_lots, 0),
            })

        leaders[leader_key]["buy_top"] = sorted(
            [item for item in rows if item["today_lots"] > 0],
            key=lambda item: item["today_lots"],
            reverse=True,
        )[:top_n]
        leaders[leader_key]["sell_top"] = sorted(
            [item for item in rows if item["today_lots"] < 0],
            key=lambda item: item["today_lots"],
        )[:top_n]

    return leaders


def format_lots(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0張"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:,.0f}張"


def format_institutional_leaders_text(leaders: Dict[str, Any]) -> str:
    if not leaders:
        return ""

    investor_labels = {
        "foreign": "外資",
        "investment": "投信",
        "dealer": "自營商",
    }

    lines: List[str] = ["── 法人買賣超榜 ──"]
    has_any = False

    for key in ["foreign", "investment", "dealer"]:
        group = leaders.get(key) or {}
        buy_top = group.get("buy_top") or []
        sell_top = group.get("sell_top") or []
        label = investor_labels[key]

        buy_text = "、".join(
            f"{display_name(item.get('name') or item.get('symbol') or '', item.get('symbol') or '')}{format_lots(item.get('today_lots'))}"
            for item in buy_top[:5]
        )
        sell_text = "、".join(
            f"{display_name(item.get('name') or item.get('symbol') or '', item.get('symbol') or '')}{format_lots(item.get('today_lots'))}"
            for item in sell_top[:5]
        )

        if buy_text:
            lines.append(f"{label}買超前五：{buy_text}")
            has_any = True
        if sell_text:
            lines.append(f"{label}賣超前五：{sell_text}")
            has_any = True

    return "\n".join(lines) if has_any else ""


def _fetch_single_stock_institutional(
    dl: Any, symbol: str, name: str, first_day: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """抓取單一個股的法人買賣超（供 ThreadPoolExecutor 使用）。"""
    stock_id = symbol.replace(".TW", "")
    try:
        df = dl.taiwan_stock_institutional_investors(
            stock_id=stock_id,
            start_date=first_day,
        )
        if df.empty:
            return None

        recent_date = df["date"].max()
        stats = {
            "外資": {"today": 0, "month": 0},
            "投信": {"today": 0, "month": 0},
            "自營商": {"today": 0, "month": 0},
        }

        for _, row in df.iterrows():
            investor = row["name"]
            net_lots = (row["buy"] - row["sell"]) / 1000.0

            if "Foreign" in investor:
                inv_key = "外資"
            elif "Investment" in investor:
                inv_key = "投信"
            elif "Dealer" in investor:
                inv_key = "自營商"
            else:
                continue

            stats[inv_key]["month"] += net_lots
            if row["date"] == recent_date:
                stats[inv_key]["today"] += net_lots

        return (symbol, stats)
    except Exception as e:
        logger.error("獲取 %s(%s) 法人數據失敗: %s", name, stock_id, e)
        return None


def fetch_stocks_institutional_data(watch_list: List[StockEntry]) -> Dict[str, Any]:
    """使用 FinMind 並發抓取個股三大法人買賣超（ThreadPoolExecutor）。"""
    try:
        from FinMind.data import DataLoader

        dl = DataLoader()
        token = os.getenv("FINMIND_TOKEN")
        if token:
            dl.login_by_token(api_token=token)

        first_day = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        tw_stocks = [(s, n) for s, n, *_ in watch_list if s.endswith(".TW")]
        inst_results: Dict[str, Any] = {}

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(_fetch_single_stock_institutional, dl, sym, name, first_day): sym
                for sym, name in tw_stocks
            }
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    inst_results[result[0]] = result[1]

        return inst_results
    except Exception as e:
        logger.error("FinMind import/init failed: %s", e)
        return {}


def save_institutional_snapshot(
    mode: str,
    now: datetime,
    market_total_text: str,
    stocks: Dict[str, Any],
    leaders: Dict[str, Any],
) -> Optional[Path]:
    """落地當日法人資料 snapshot，供下游優先讀取。"""
    snapshot_dir = _BASE_DIR / "logs" / "institutional_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    text = (market_total_text or "").strip()
    lowered = text.lower()
    has_error = any(marker in text for marker in ["獲取失敗", "無法取得"]) or any(
        marker in lowered for marker in ["failed", "error"]
    )
    status = "ok" if text and not has_error else "error"

    payload = {
        "date": now.strftime("%Y-%m-%d"),
        "mode": mode,
        "generated_at": now.isoformat(),
        "source": "stock_monitor.finmind",
        "status": status,
        "market_total_text": text,
        "stocks": stocks or {},
        "leaders": leaders or {},
        "error": text if has_error else "",
    }

    dated_path = snapshot_dir / f"{now.strftime('%Y-%m-%d')}_{mode}.json"
    current_path = snapshot_dir / "current.json"
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    dated_path.write_text(content, encoding="utf-8")
    if status == "ok":
        current_path.write_text(content, encoding="utf-8")
    else:
        restored = False
        for candidate in sorted(snapshot_dir.glob("*.json"), reverse=True):
            if candidate.name in {dated_path.name, "current.json"}:
                continue
            try:
                candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (candidate_payload.get("status") or "").lower() != "ok":
                continue
            current_path.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
            logger.warning("institutional snapshot 為 error，current.json 已回退到最近成功快照: %s", candidate.name)
            restored = True
            break
        if not restored:
            logger.warning("institutional snapshot 為 error，且找不到可回退的成功 current.json: %s", dated_path)
    logger.info("institutional snapshot 已更新: %s", dated_path)
    return dated_path


def fetch_quotes(symbols: List[str]) -> Any:
    """用 yfinance 批次取得最新報價（period=5d 防連假空值）"""
    try:
        import yfinance as yf

        data = yf.download(
            symbols,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        return data
    except Exception as e:
        logger.error("yfinance download failed: %s", e)
        return None


def parse_quotes(
    raw_data: Any,
    watch_list: List[StockEntry],
    quality_report: Optional[DataQualityReport] = None
) -> List[Dict[str, Any]]:
    """解析報價，按每檔 alert_pct 判斷警報，並驗證數據新鮮度。"""
    results: List[Dict[str, Any]] = []

    try:
        close_df = raw_data.xs("Close", level=1, axis=1)
    except Exception as e:
        logger.error("Cannot extract Close prices: %s", e)
        return results

    # 取得完整的 OHLCV 資料以進行完整性驗證
    has_full_data = hasattr(raw_data, 'xs')

    for symbol, name, lower, upper, alert_pct, meta in watch_list:
        try:
            if symbol not in close_df.columns:
                if quality_report:
                    quality_report.add_error(symbol, "數據源中找不到此商品")
                continue

            series = close_df[symbol].dropna()

            # 數據完整性驗證
            if len(series) < 2:
                if quality_report:
                    quality_report.add_warning(symbol, f"數據筆數不足 ({len(series)} < 2)")
                continue

            # 數據新鮮度驗證
            if has_full_data:
                try:
                    # 嘗試取得該商品的完整數據
                    symbol_data = raw_data[symbol] if isinstance(raw_data.columns, pd.MultiIndex) else raw_data
                    is_fresh, error_msg = validate_stock_data_freshness(
                        symbol_data, symbol, max_delay_hours=4
                    )
                    if not is_fresh:
                        logger.warning(f"{symbol} 數據可能過期: {error_msg}")
                        if quality_report:
                            quality_report.add_warning(symbol, error_msg)
                except Exception as e:
                    logger.debug(f"{symbol} 新鮮度驗證跳過: {e}")

            prev_close = float(series.iloc[-2])
            curr_close = float(series.iloc[-1])
            change = curr_close - prev_close
            change_pct = (change / prev_close) * 100 if prev_close else 0

            alert = None
            if curr_close <= lower or curr_close >= upper:
                alert = "warning"
            elif abs(change_pct) >= alert_pct:
                alert = "critical" if abs(change_pct) >= alert_pct * 1.67 else "warning"

            # 修正 currency：^TWII 為台幣指數
            currency = "TWD" if symbol.endswith(".TW") or symbol == "^TWII" else "USD"

            results.append({
                "symbol": symbol,
                "name": name,
                "price": curr_close,
                "change": change,
                "change_pct": change_pct,
                "lower": lower,
                "upper": upper,
                "alert_pct": alert_pct,
                "alert": alert,
                "currency": currency,
                "meta": meta,
                "watch_label": format_watch_meta(meta),
            })
        except Exception as e:
            logger.warning("%s 解析失敗: %s", symbol, e)
            if quality_report:
                quality_report.add_error(symbol, str(e))

    return results


# ──────────────────────────────────────────────────────────────────────
# Claude 分析 + Heuristic fallback
# ──────────────────────────────────────────────────────────────────────


def _read_profile() -> str:
    """讀取 W 的個人 profile（若存在）。"""
    profile_path = Path("/Users/w/Cursor/profile.md")
    if profile_path.exists():
        try:
            return profile_path.read_text(encoding="utf-8")[:2000]
        except Exception:
            pass
    return ""


def _read_recent_research(hours: int = 48) -> str:
    """讀取最近 N 小時內的研究報告全文。"""
    reports_dir = _BASE_DIR / "logs" / "research_reports"
    if not reports_dir.exists():
        return ""

    cutoff = time.time() - hours * 3600
    texts: List[str] = []
    for p in sorted(reports_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.stat().st_mtime >= cutoff:
            try:
                texts.append(p.read_text(encoding="utf-8")[:3000])
            except Exception:
                pass
    if not texts:
        return ""
    return "\n\n---\n\n".join(texts[:3])  # 最多 3 份


def _query_memory(query: str = "最近股市動向") -> str:
    """從記憶庫取出相關歷史。"""
    try:
        from scripts.memory_agent import MemoryAgent

        mem = MemoryAgent()
        hits = mem.retrieve(
            query,
            n_results=3,
            days_back=7,
            source_filter=["stock_brief", "stock_alerts"],
        )
        if not hits:
            return ""
        parts = []
        for h in hits:
            meta = h.get("metadata", {})
            parts.append(
                f"[{meta.get('source', '?')} / {meta.get('date', '?')}]\n{h['content'][:500]}"
            )
        return "\n\n".join(parts)
    except Exception as e:
        logger.debug("記憶庫檢索失敗: %s", e)
        return ""


def describe_watch_status(q: Dict[str, Any]) -> str:
    meta = q.get("meta") or {}
    pool = meta.get("pool")
    if not pool:
        return ""

    labels = {
        "short_term": "短線池",
        "medium_term": "中線池",
        "long_term": "長線池",
        "intraday": "日內池",
    }
    pool_label = labels.get(pool, pool)
    strict_passed = meta.get("strict_passed")
    blockers = meta.get("strict_hard_fails") or []

    if strict_passed is True:
        return f"{pool_label}，嚴格條件通過"
    if strict_passed is False and blockers:
        return f"{pool_label}，嚴格條件阻塞：{','.join(blockers)}"
    if strict_passed is False:
        return f"{pool_label}，未通過嚴格條件"
    return pool_label


def build_heuristic_summary(
    quotes: List[Dict[str, Any]],
    inst_data: str,
    alerts: List[Dict[str, Any]],
) -> str:
    """Claude 全失敗時的程式化 fallback：列出關鍵數據，不做主觀判斷。"""
    lines = ["## 市場概況（資料摘要）", ""]

    # 漲幅前 5
    sorted_up = sorted(quotes, key=lambda q: q["change_pct"], reverse=True)[:5]
    if sorted_up:
        lines.append("**漲幅前五**")
        for q in sorted_up:
            lines.append(f"- {display_name(q['name'], q['symbol'])}: {q['price']:,.1f} ({'+' if q['change_pct']>=0 else ''}{q['change_pct']:.2f}%)" + (f"（{describe_watch_status(q)}）" if describe_watch_status(q) else ""))
        lines.append("")

    # 跌幅前 5
    sorted_down = sorted(quotes, key=lambda q: q["change_pct"])[:5]
    if sorted_down:
        lines.append("**跌幅前五**")
        for q in sorted_down:
            lines.append(f"- {display_name(q['name'], q['symbol'])}: {q['price']:,.1f} ({'+' if q['change_pct']>=0 else ''}{q['change_pct']:.2f}%)" + (f"（{describe_watch_status(q)}）" if describe_watch_status(q) else ""))
        lines.append("")

    # 異常警報
    if alerts:
        lines.append("**異常標的**")
        for a in alerts:
            lines.append(f"- {display_name(a['name'], a['symbol'])}{a.get('watch_label', '')}: {a['price']:,.1f}, 漲跌 {a['change_pct']:+.2f}%" + (f"（{describe_watch_status(a)}）" if describe_watch_status(a) else ""))
        lines.append("")

    # 法人動向
    if inst_data:
        lines.append("**法人動向**")
        lines.append(inst_data)
        lines.append("")

    lines.append("*（Claude 分析暫時不可用，以上為純數據摘要）*")
    return "\n".join(lines)


def generate_market_analysis(
    quotes: List[Dict],
    inst_data: str,
    news_data: str,
    is_friday: bool = False,
    is_afternoon: bool = False,
    strategy_section: str = "",
    focus_data: str = "",
) -> str:
    """使用 Claude 產生專業分析報告，整合 profile + 歷史研究 + 記憶。"""
    # 組合報價
    quotes_summary = []
    for q in quotes:
        line = f"{display_name(q['name'], q['symbol'])}{q.get('watch_label', '')}: {q['price']:.2f} ({'+' if q['change_pct']>=0 else ''}{q['change_pct']:.2f}%)"
        if q.get("inst"):
            i = q["inst"]
            line += f" | 今日買賣超(張): 外資 {i['外資']['today']:.0f}, 投信 {i['投信']['today']:.0f}, 自營商 {i['自營商']['today']:.0f}"
        quotes_summary.append(line)
    quotes_summary_str = "\n".join(quotes_summary)

    strategy_block = f"\n\n【技術策略信號（今日掃描結果）】\n{strategy_section}" if strategy_section else ""
    focus_block = f"\n\n【反思 W 提示：本週市場關注焦點】\n{focus_data}" if focus_data else ""

    # 讀取額外上下文
    profile_text = _read_profile()
    recent_research = _read_recent_research(hours=48)
    recent_memory = _query_memory()

    # 組合 prompt
    extra_sections = ""
    if recent_research:
        extra_sections += f"\n\n【近期深度研究報告摘要】\n{recent_research[:3000]}"
    if recent_memory:
        extra_sections += f"\n\n【歷史記憶：近一週相關記錄】\n{recent_memory[:2000]}"

    prompt = f"""
【最新報價與個股籌碼資料】
{quotes_summary_str}
{focus_block}
【大盤籌碼數據】
{inst_data}

【今日重點財經新聞摘要】
{news_data if news_data else "無最新新聞"}{strategy_block}{extra_sections}
"""

    # system prompt
    if is_afternoon:
        system_prompt = (
            "你是一位穩健保守的專業台股與國際金融分析師。現在是下午盤後時間，請根據提供的台股收盤報價、最新出爐的三大法人盤後籌碼動向，以及今日新聞，"
            "撰寫一份專業且精簡的【盤後籌碼總結與明日觀望】。\n"
            "包含：\n"
            "1. 今日台股盤後籌碼與資金流向總結\n"
            "2. 新聞事件對籌碼與板塊輪動的印證\n"
            "3. 明日穩健保守的操作建議\n"
        )
    else:
        has_signals = bool(strategy_section)
        signal_instruction = (
            "4. 【技術信號點評】針對「技術策略信號」區塊中每一支個股，結合籌碼與新聞，給出具體的操作建議（買進/觀望/注意風險）與理由。\n"
            if has_signals
            else ""
        )
        system_prompt = (
            "你是一位穩健保守的專業台股與國際金融分析師。現在是早盤開盤前，請根據提供的前一日美股台積電ADR/輝達收盤報價、原油黃金趨勢、昨日台股籌碼及今早的新聞摘要，"
            "撰寫一份專業且精簡的【晨間盤前策略】。\n"
            "包含：\n"
            "1. 美股表現與國際市場新聞總結\n"
            "2. 預計對今日台股開盤的影響與籌碼觀察\n"
            "3. 今日穩健保守的操作建議\n"
            f"{signal_instruction}"
            "4. 若引用追蹤名單，只能依輸入中的池位事實描述，不可把單池、候選或法人偏多標的升格寫成跨池共振或正式中長線成立。\n"
        )

    if is_friday:
        system_prompt += "【週末特輯】從給定的台股監控名單中，挑選出 5 支目前被低估、或籌碼結構良好、適合短中長期佈局的標的，並簡述理由。\n"

    if profile_text:
        system_prompt += f"\n\n【讀者背景】\n{profile_text[:1000]}\n請根據讀者背景調整分析的深度與建議方向。"

    if recent_research:
        system_prompt += "\n請在分析中適度引用「近期深度研究報告摘要」的結論，使分析具有連貫性。"

    system_prompt += "\n直接輸出 Markdown 格式原文，不要有任何開場白或過多廢話。"

    # 呼叫 Claude（使用 ClaudeHelper，max_tokens=2500）
    try:
        helper = ClaudeHelper()
        return helper.chat(
            system=system_prompt,
            prompt=prompt,
            max_tokens=2500,
        )
    except Exception as e:
        logger.error("Claude analysis failed: %s", e)
        # Heuristic fallback
        alerts_list = [q for q in quotes if q.get("alert")]
        return build_heuristic_summary(quotes, inst_data, alerts_list)


# ──────────────────────────────────────────────────────────────────────
# Brief 組合 + 存檔
# ──────────────────────────────────────────────────────────────────────


def build_morning_brief(
    quotes: List[Dict],
    now: datetime,
    inst_data: str,
    analysis: str,
    is_afternoon: bool = False,
    strategy_section: str = "",
    outcome_section: str = "",
    leaders_text: str = "",
) -> str:
    """組合晨報/盤後報文字"""
    date_str = now.strftime("%Y-%m-%d %A")
    title = (
        f"📊 股市 W 盤後籌碼總結 · {date_str}"
        if is_afternoon
        else f"📊 股市 W 專業晨報 · {date_str}"
    )

    lines = [title]

    tw = [q for q in quotes if q["symbol"].endswith(".TW") or q["symbol"] == "^TWII"]
    tw_index = [q for q in tw if q["symbol"] == "^TWII"]
    tw_watch = [q for q in tw if q["symbol"] != "^TWII"]
    intl = [q for q in quotes if q["symbol"] in ("^DJI", "^IXIC", "NVDA", "TSM")]
    commodities = [q for q in quotes if q["symbol"] in ("GC=F", "CL=F")]

    summary_line = "（今日重點請見下方專家觀點）"
    for raw in analysis.splitlines():
        candidate = raw.strip().lstrip("#*- ").strip()
        if candidate and not candidate.startswith("```"):
            summary_line = candidate
            break

    lines += ["", "── 一句話總覽 ──", f"- {summary_line}"]

    def fmt(q: Dict) -> str:
        arrow = "▲" if q["change"] >= 0 else "▼"
        warn = " ⚠️" if q["alert"] else ""
        base = f"  {display_name(q['name'], q['symbol'])}{q.get('watch_label', '')} {q['price']:,.1f} {arrow} {abs(q['change_pct']):.2f}%{warn}"
        if q.get("inst"):
            i = q["inst"]

            def f_num(val: float) -> str:
                return f"+{val:,.0f}" if val > 0 else f"{val:,.0f}"

            base += f"\n    ↳籌碼(張) 今日: 外 {f_num(i['外資']['today'])} / 投 {f_num(i['投信']['today'])} / 自 {f_num(i['自營商']['today'])}"
            base += f"\n              本月: 外 {f_num(i['外資']['month'])} / 投 {f_num(i['投信']['month'])} / 自 {f_num(i['自營商']['month'])}"
        return base

    lines += ["", "── 市場主線 ──", "【台股大盤】"]
    for q in tw_index:
        lines.append(fmt(q))

    lines += ["", "【國際市場】"]
    for q in intl:
        lines.append(fmt(q))

    lines += ["", "【原物料】"]
    for q in commodities:
        lines.append(fmt(q))

    lines += ["", "── 重點觀察名單 ──", "【台股與 20 檔追蹤名單】"]
    for q in tw_watch:
        lines.append(fmt(q))

    lines += ["", "── 法人 / 資金面 ──", "── 大盤籌碼動向 ──", inst_data]
    if leaders_text.strip():
        lines += ["", leaders_text]

    # 警報彙整
    alerts = [q for q in quotes if q["alert"]]
    if alerts:
        lines += ["", "── 異常與風險 ──", "⚠️ 今日異常警示"]
        for q in alerts:
            lines.append(
                f"  {display_name(q['name'], q['symbol'])}{q.get('watch_label', '')} {q['price']:,.1f} "
                f"(區間 {q['lower']}~{q['upper']}, "
                f"{'+' if q['change_pct']>=0 else ''}{q['change_pct']:.2f}%, "
                f"閾值 ±{q.get('alert_pct', 3.0):.1f}%)"
                + (f"（{describe_watch_status(q)}）" if describe_watch_status(q) else "")
            )
    else:
        lines += ["", "── 異常與風險 ──", "（今日無異常警示）"]

    if strategy_section:
        lines += ["", "── 📡 技術策略信號 ──", strategy_section]

    if outcome_section:
        lines += ["", outcome_section]

    lines += [
        "",
        "── 💡 專家觀點 ──",
        analysis,
        "",
        "📌 原則：穩健・理性・不薦股 | 投資有風險，決策請自行判斷。",
    ]
    return "\n".join(lines)




def emit_stock_master_payload(
    *,
    mode: str,
    brief: str,
    alert_count: int,
    has_alerts: bool,
    alerts_file: Path,
    master_control,
    phase: str,
    complete: bool,
    deferred_sections: Optional[List[str]] = None,
) -> str:
    return write_payload(
        {
            "agent": "stock_monitor",
            "mode": mode,
            "meta": {
                "alert_count": alert_count,
                "has_alerts": has_alerts,
                "phase": phase,
                "complete": complete,
                "dispatch_ready": True,
                "deferred_sections": deferred_sections or [],
            },
            "artifacts": {
                "alerts_file": str(alerts_file),
            },
            "notification": {
                "feishu": {
                    "title": "ℹ️ 股市警報",
                    "content": brief,
                },
                "wechat": {
                    "text": brief,
                    "typing_seconds": 30,
                },
            },
        },
        control=master_control,
    )


def archive_brief(brief: str, mode: str, now: datetime) -> Path:
    """存檔 brief 到 logs/stock_briefs/，回傳檔案路徑。"""
    briefs_dir = _BASE_DIR / "logs" / "stock_briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    date_str = now.strftime("%Y-%m-%d")
    path = briefs_dir / f"{date_str}_{mode}.md"
    try:
        path.write_text(brief, encoding="utf-8")
        logger.info("簡報已存檔: %s", path)
    except Exception as e:
        logger.warning("簡報存檔失敗: %s", e)
    return path


def get_daily_news() -> str:
    """讀取今日早上爬取的新聞"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        filepath = _BASE_DIR / "logs" / "daily_news" / f"{today}.md"
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""
    except Exception as e:
        logger.warning("讀取每日新聞失敗: %s", e)
        return ""


def get_next_week_focus() -> str:
    """週一晨報讀取反思 W 產出的下週關注清單"""
    try:
        filepath = _BASE_DIR / "logs" / "next_week_focus.md"
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("讀取下週關注清單失敗: %s", e)
    return ""


def _merge_alerts(
    alerts_file: Path, mode: str, date_str: str, new_alerts: List[Dict[str, Any]]
) -> None:
    """
    Merge 當前 mode 的 alerts 到既有檔案（不覆蓋其他 mode 的資料）。
    """
    existing: Dict[str, Any] = {"date": date_str, "alerts": []}
    if alerts_file.exists():
        try:
            existing = json.loads(alerts_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    kept = [a for a in existing.get("alerts", []) if a.get("mode") != mode]
    for a in new_alerts:
        a["mode"] = mode
    kept.extend(new_alerts)

    existing["date"] = date_str
    existing["mode"] = mode
    existing["alerts"] = kept

    try:
        alerts_file.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "警報已寫入 %s（本次 %d 筆，累計 %d 筆）",
            alerts_file,
            len(new_alerts),
            len(kept),
        )
    except Exception as e:
        logger.warning("寫入警報檔失敗: %s", e)


def _select_priority_watch_list_for_morning(watch_list, quotes, limit: int = 12):
    if not watch_list:
        return []

    symbol_to_quote = {q.get("symbol"): q for q in quotes if q.get("symbol")}
    scored = []
    for item in watch_list:
        symbol, name, lower, upper, alert_pct, meta = item
        meta = meta or {}
        quote = symbol_to_quote.get(symbol) or {}
        score = 0
        if quote.get("alert"):
            score += 100
        if meta.get("pool") == "cross":
            score += 80
        if meta.get("strict_passed"):
            score += 60
        if meta.get("pool") in {"short_term", "medium_term", "long_term"}:
            score += 30
        score += max(0, int(abs(float(quote.get("change_pct") or 0)) * 2))
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="股市 W 監控工具")
    parser.add_argument(
        "--mode",
        choices=["morning", "afternoon"],
        default="morning",
        help="執行模式 (晨報 / 盤後總結)",
    )
    args = parser.parse_args()

    now = datetime.now()
    is_friday = now.weekday() == 4
    is_afternoon = args.mode == "afternoon"
    master_control = get_current_control()
    master_mode = master_control is not None

    notifier = LarkNotifier(str(_BASE_DIR / "config" / "lark_config.yaml"))

    watch_list = load_watch_list()
    if not watch_list:
        logger.error("監控清單為空，中止執行")
        sys.exit(1)

    logger.info("正在取得股價報價...")
    symbols = [s for s, *_ in watch_list]
    raw = fetch_quotes(symbols)

    if raw is None or (hasattr(raw, "empty") and raw.empty):
        logger.error("yfinance 未回傳資料")
        sys.exit(1)

    quality_report = DataQualityReport()
    quotes = parse_quotes(raw, watch_list, quality_report=quality_report)
    logger.info("已解析 %d 筆報價", len(quotes))

    if len(quotes) < len(watch_list) * 0.5:
        logger.error(f"數據品質過差：僅解析 {len(quotes)}/{len(watch_list)} 檔標的")
        if quality_report.errors:
            logger.error(f"錯誤詳情: {[e['msg'] for e in quality_report.errors[:3]]}")
        if is_afternoon:
            sys.exit(1)

    quality_dict = quality_report.to_dict()
    if not quality_report.is_healthy():
        logger.warning(f"數據品質報告: {quality_dict['total_warnings']} 警告, {quality_dict['total_errors']} 錯誤")

    logger.info("正在取得大盤法人資料...")
    inst_payload = fetch_institutional_data()
    inst_data = inst_payload.get("market_total_text") or ""
    inst_leaders = inst_payload.get("leaders") or {}

    if not is_afternoon:
        individual_watch_list = _select_priority_watch_list_for_morning(watch_list, quotes)
        logger.info("正在取得優先標的法人資料... %d 檔", len(individual_watch_list))
    else:
        individual_watch_list = watch_list
        logger.info("正在並行取得個股法人資料...")

    inst_individual = fetch_stocks_institutional_data(individual_watch_list)
    inst_leaders = build_watchlist_institutional_leaderboard(inst_individual, individual_watch_list)
    logger.info("法人買賣超榜已使用監控池法人資料重建，覆蓋單檔暫存榜")
    leaders_text = format_institutional_leaders_text(inst_leaders)
    save_institutional_snapshot(args.mode, now, inst_data, inst_individual, inst_leaders)

    for q in quotes:
        q["inst"] = inst_individual.get(q["symbol"])

    logger.info("正在讀取今日新聞...")
    news_data = get_daily_news()

    focus_data = ""
    if not is_afternoon and now.weekday() == 0:
        focus_data = get_next_week_focus()
        if focus_data:
            logger.info("已讀取反思 W 下週關注清單，將融入晨報")

    alerts_file = _BASE_DIR / "logs" / "stock_alerts_today.json"
    base_alerts = [q for q in quotes if q.get("alert")]
    base_alerts_payload = [
        {
            "symbol": q["symbol"],
            "name": q["name"],
            "price": q["price"],
            "change_pct": round(q["change_pct"], 2),
            "alert": q["alert"],
            "watch_label": q.get("watch_label", ""),
            "pool": (q.get("meta") or {}).get("pool"),
            "strict_passed": (q.get("meta") or {}).get("strict_passed"),
            "strict_hard_fails": (q.get("meta") or {}).get("strict_hard_fails", []),
        }
        for q in base_alerts
    ]
    _merge_alerts(alerts_file, args.mode, now.strftime("%Y-%m-%d"), base_alerts_payload)

    if master_mode and not is_afternoon:
        logger.info("正在以規則摘要生成晨報初版...")
        analysis = build_heuristic_summary(quotes, inst_data, base_alerts)
        brief = build_morning_brief(
            quotes,
            now,
            inst_data,
            analysis,
            is_afternoon,
            "",
            "",
            leaders_text,
        )
        logger.info("晨報初版已生成")
        print(brief)
        archive_brief(brief, args.mode, now)
        payload_path = emit_stock_master_payload(
            mode=args.mode,
            brief=brief,
            alert_count=len(base_alerts_payload),
            has_alerts=bool(base_alerts_payload),
            alerts_file=alerts_file,
            master_control=master_control,
            phase="initial",
            complete=False,
            deferred_sections=[
                "strategy_scan",
                "tracking_bucket_digest",
                "signal_watch_summary",
                "screener_tracker_summary",
                "claude_deep_analysis",
                "extended_trigger_detection",
                "extended_alert_merge",
            ],
        )
        logger.info(
            "Master agent 接管通知：已提前輸出 morning 初版 payload=%s，先發晨報後跳過重型 enrich",
            payload_path,
        )
        sys.exit(0)

    strategy_section = ""
    signals: List[Any] = []
    if not is_afternoon:
        logger.info("Running strategy scanner...")
        try:
            from scripts.strategy_scanner import scan, format_signals
            scan_list = [(s, n, lo, hi) for s, n, lo, hi, _, _ in watch_list]
            signals = scan(scan_list)
            strategy_section = format_signals(signals)
        except Exception as e:
            logger.warning("策略掃描失敗: %s", e)
            strategy_section = "（策略掃描執行失敗）"

    if not is_afternoon and signals:
        try:
            from scripts.strategy_scanner import save_signals
            save_signals(signals, now)
        except Exception as e:
            logger.warning("信號歷史記錄失敗: %s", e)

    bucket_digest = ""
    try:
        bucket_snapshot = build_tracking_bucket_snapshot(inst_individual, signals if signals else None)
        bucket_digest = render_tracking_bucket_digest(bucket_snapshot.get("tracking_buckets") or {})
        if bucket_digest:
            if strategy_section:
                strategy_section += "\n\n" + bucket_digest
            else:
                strategy_section = bucket_digest
    except Exception as e:
        logger.debug(f"五池追蹤摘要生成失敗: {e}")

    try:
        from utils.signal_watch_manager import get_signal_watch_manager
        manager = get_signal_watch_manager()
        watch_summary = manager.generate_daily_summary()
        if watch_summary and strategy_section and "（" not in strategy_section:
            strategy_section += watch_summary
    except Exception as e:
        logger.debug(f"策略信號摘要生成失敗: {e}")

    try:
        from utils.screener_tracker import get_screener_tracker
        tracker = get_screener_tracker()
        screener_summary = tracker.get_active_summary()
        if screener_summary:
            if strategy_section:
                strategy_section += screener_summary
            else:
                strategy_section = screener_summary
    except Exception as e:
        logger.debug(f"選股追蹤摘要生成失敗: {e}")

    outcome_section = ""
    if is_afternoon:
        try:
            from scripts.strategy_scanner import backfill_outcomes
            outcome_section = backfill_outcomes(quotes, now)
        except Exception as e:
            logger.warning("信號回填失敗: %s", e)

        try:
            from utils.signal_watch_manager import get_signal_watch_manager
            manager = get_signal_watch_manager()
            for q in quotes:
                symbol = q.get("symbol")
                price = q.get("price")
                if symbol and price:
                    manager.update_outcome(symbol, price)
        except Exception as e:
            logger.debug(f"策略信號監控更新失敗: {e}")

        try:
            from utils.screener_tracker import get_screener_tracker
            tracker = get_screener_tracker()
            price_map = {q["symbol"]: q["price"] for q in quotes if "symbol" in q and "price" in q}
            completed = tracker.update_daily_outcomes(price_map, now)
            if completed > 0:
                logger.info(f"[ScreenerTracker] {completed} 個選股已結案")
            if now.weekday() == 4:
                perf = tracker.generate_weekly_report()
                report_text = tracker.format_weekly_report(perf)
                weekly_file = tracker.weekly_dir / f"{now.strftime('%Y-W%W')}_report.md"
                weekly_file.write_text(report_text, encoding="utf-8")
                logger.info(f"[ScreenerTracker] 週報告已生成: {weekly_file}")
        except Exception as e:
            logger.debug(f"選股追蹤更新失敗: {e}")

    logger.info("正在透過 Claude 生成專家分析...")
    analysis = generate_market_analysis(
        quotes, inst_data, news_data, is_friday, is_afternoon, strategy_section, focus_data
    )

    brief = build_morning_brief(
        quotes,
        now,
        inst_data,
        analysis,
        is_afternoon,
        strategy_section,
        outcome_section,
        leaders_text,
    )
    logger.info("%s 簡報已生成", args.mode)
    print(brief)
    archive_brief(brief, args.mode, now)

    try:
        quality_log_dir = _BASE_DIR / "logs" / "data_quality"
        quality_log_dir.mkdir(parents=True, exist_ok=True)
        quality_file = quality_log_dir / f"{now.strftime('%Y-%m-%d')}_{args.mode}_quality.json"
        quality_file.write_text(
            json.dumps(quality_report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.debug(f"數據品質報告存檔失敗: {e}")

    extended_alert_items = []
    try:
        from utils.research_trigger_detector import get_trigger_detector
        detector = get_trigger_detector()
        price_data = {}
        for q in quotes:
            symbol = q.get("symbol")
            if symbol:
                try:
                    df = yf.download(symbol, period="30d", interval="1d", progress=False)
                    if not df.empty:
                        df.columns = [c.lower() for c in df.columns]
                        price_data[symbol] = df
                except Exception:
                    pass
        inst_data_map = {q["symbol"]: q.get("inst") for q in quotes if q.get("inst")}
        extended_triggers = detector.scan_all_triggers(quotes, inst_data_map, price_data)
        for trigger in extended_triggers:
            if trigger.severity == "critical":
                extended_alert_items.append({
                    "symbol": trigger.symbol,
                    "name": trigger.name,
                    "price": 0,
                    "change_pct": 0,
                    "alert": "critical",
                    "trigger_type": trigger.trigger_type,
                    "trigger_desc": trigger.description,
                })
        logger.info("[ResearchTrigger] 擴充觸發共 %d 筆", len(extended_alert_items))
    except Exception as e:
        logger.debug(f"擴充觸發檢測失敗: {e}")

    alerts = [q for q in quotes if q.get("alert")]
    alerts.extend(extended_alert_items)
    alerts_payload = [
        {
            "symbol": q["symbol"],
            "name": q["name"],
            "price": q["price"],
            "change_pct": round(q["change_pct"], 2),
            "alert": q["alert"],
            "watch_label": q.get("watch_label", ""),
            "pool": (q.get("meta") or {}).get("pool"),
            "strict_passed": (q.get("meta") or {}).get("strict_passed"),
            "strict_hard_fails": (q.get("meta") or {}).get("strict_hard_fails", []),
            "trigger_type": q.get("trigger_type"),
            "trigger_desc": q.get("trigger_desc"),
        }
        for q in alerts
    ]
    _merge_alerts(alerts_file, args.mode, now.strftime("%Y-%m-%d"), alerts_payload)

    if master_mode:
        if is_afternoon:
            payload_path = emit_stock_master_payload(
                mode=args.mode,
                brief=brief,
                alert_count=len(alerts_payload),
                has_alerts=bool(alerts_payload),
                alerts_file=alerts_file,
                master_control=master_control,
                phase="final",
                complete=True,
                deferred_sections=[],
            )
            logger.info(
                "Master agent 接管通知：已輸出 payload=%s，略過子 agent 直接推播",
                payload_path,
            )
        success = True
    else:
        success = notifier.send_stock_alert("info", brief)
        if success:
            logger.info("股市簡報（%s）已成功送至飛書", args.mode)
        else:
            logger.error("股市簡報發送失敗")

        logger.info("正在嘗試發送到微信...")
        from utils.wechat_notifier import WechatNotifier
        wx_notifier = WechatNotifier()
        wx_to_user = os.getenv("WECHAT_PUSH_TO_USER")
        wx_context_token = os.getenv("WECHAT_PUSH_CONTEXT_TOKEN")
        wx_success = wx_notifier.send_message(
            brief,
            to_user=wx_to_user,
            typing_seconds=30,
            context_token=wx_context_token,
        )
        if wx_success:
            logger.info("股市簡報已成功送至微信")
        else:
            wx_error = wx_notifier.last_error or "未知錯誤"
            logger.error("股市簡報送至微信失敗：%s", wx_error)
            notifier.send_error_notification(
                "微信推播失敗",
                f"股市 W（{args.mode}）已發送飛書，但微信推播失敗。\n原因：{wx_error}",
            )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
