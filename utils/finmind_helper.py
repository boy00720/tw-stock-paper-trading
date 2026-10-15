#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finmind_helper.py — FinMind API 統一封裝

提供給 sepa_screener / value_screener 用，補 yfinance 台股缺項：
    - 全市場上市+上櫃清單（含產業）
    - 月營收（用於 SEPA ② 營收 YoY）
    - 季度財報（用於 SEPA ③ 淨利 YoY+QoQ、⑥ ROE、⑧ 3 年 CAGR）
    - 股利歷史（用於 Value ③ 股息年數+年化率）
    - P/E、P/B 每日值

設計:
    - 每個 method 都加簡單 in-memory cache，同一個 run 重複 call 不重複打 API
    - 無 FINMIND_TOKEN 時用免費額度（600 req/hour），自動提示
    - 重試 1 次（偶發網路錯）

用法:
    from utils.finmind_helper import FinMindHelper
    fm = FinMindHelper()
    universe = fm.get_universe()                     # pandas.DataFrame
    info = fm.get_stock_info("2330")                  # dict
    rev = fm.get_monthly_revenue("2330", months=24)   # DataFrame
    fs = fm.get_financial_statement("2330", quarters=12)
    div = fm.get_dividends("2330", years=6)
    per = fm.get_per_pbr("2330")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# SQLite cache
# ─────────────────────────────────────────────────────

_BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DB_PATH = _BASE_DIR / "logs" / "finmind_cache.db"

# TTL（秒）按資料類型區分
TTL_DEFAULT = 86400          # 1 day
TTL_UNIVERSE = 86400         # 1 day
TTL_MONTHLY_REVENUE = 86400  # 1 day
TTL_FINANCIAL = 7 * 86400    # 7 days（財報季更新）
TTL_DIVIDEND = 7 * 86400     # 7 days
TTL_PER_PBR = 3600           # 1 hour（盤中可能變）
TTL_IPO_DATE = 30 * 86400    # 30 days（幾乎不變）


def _init_cache_db() -> sqlite3.Connection:
    CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB_PATH), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS finmind_cache (
            cache_key TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON finmind_cache(created_at)")
    conn.commit()
    return conn


def _make_cache_key(method: str, **kwargs) -> str:
    """產生快取鍵，排序 kwargs 確保穩定"""
    key_raw = f"{method}:" + json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.md5(key_raw.encode("utf-8")).hexdigest()


def _cache_get(conn: sqlite3.Connection, key: str) -> Optional[Any]:
    row = conn.execute(
        "SELECT data_json, created_at, ttl_seconds FROM finmind_cache WHERE cache_key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    data_json, created_at, ttl = row
    if time.time() - created_at > ttl:
        conn.execute("DELETE FROM finmind_cache WHERE cache_key = ?", (key,))
        conn.commit()
        return None
    return json.loads(data_json)


def _cache_set(conn: sqlite3.Connection, key: str, data: Any, ttl_seconds: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO finmind_cache(cache_key, data_json, created_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (key, json.dumps(data, default=str), time.time(), ttl_seconds),
    )
    conn.commit()


def _df_to_cacheable(df: Optional[pd.DataFrame]) -> Optional[List[Dict]]:
    if df is None:
        return None
    return df.to_dict(orient="records")


def _cacheable_to_df(data: Optional[List[Dict]]) -> Optional[pd.DataFrame]:
    if data is None:
        return None
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def cache_stats() -> Dict[str, Any]:
    """查看 cache 統計（條目數、大小）"""
    conn = _init_cache_db()
    try:
        row = conn.execute("SELECT COUNT(*), SUM(LENGTH(data_json)) FROM finmind_cache").fetchone()
        count, total_size = row if row else (0, 0)
        # 按 TTL 過期統計
        now = time.time()
        expired = conn.execute(
            "SELECT COUNT(*) FROM finmind_cache WHERE ? - created_at > ttl_seconds", (now,)
        ).fetchone()[0]
        return {
            "count": count or 0,
            "total_size_kb": round((total_size or 0) / 1024, 1),
            "expired_count": expired or 0,
            "db_path": str(CACHE_DB_PATH),
        }
    finally:
        conn.close()


def cache_clear(expired_only: bool = True) -> int:
    """清理 cache；expired_only=True 只清過期，False 全清"""
    conn = _init_cache_db()
    try:
        if expired_only:
            cur = conn.execute(
                "DELETE FROM finmind_cache WHERE ? - created_at > ttl_seconds", (time.time(),)
            )
        else:
            cur = conn.execute("DELETE FROM finmind_cache")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


class FinMindHelper:
    """FinMind 資料存取封裝，帶 SQLite cache 與簡易 retry"""

    def __init__(self, token: Optional[str] = None, enable_cache: bool = True):
        from FinMind.data import DataLoader
        self._dl = DataLoader()
        token = token or os.getenv("FINMIND_TOKEN")
        if token:
            try:
                self._dl.login_by_token(api_token=token)
                logger.info("FinMind 登入成功（付費 token）")
            except Exception as e:
                logger.warning("FinMind token 登入失敗，走免費額度: %s", e)
        else:
            logger.info("FinMind 未設 token，走免費額度（600 req/hour）")

        # memory cache（run 內）
        self._universe_cache: Optional[pd.DataFrame] = None
        self._stock_cache: Dict[str, Dict[str, Any]] = {}
        self._mem_cache: Dict[str, pd.DataFrame] = {}

        # SQLite cache（跨 run）
        self._enable_cache = enable_cache
        self._conn: Optional[sqlite3.Connection] = _init_cache_db() if enable_cache else None

    def __del__(self):
        if getattr(self, "_conn", None):
            try:
                self._conn.close()
            except Exception:
                pass

    # ─────────────────────────────────────────────
    # Utility
    # ─────────────────────────────────────────────

    def _fetch_with_retry(self, fn, *args, retries: int = 1, **kwargs) -> Optional[pd.DataFrame]:
        for attempt in range(retries + 1):
            try:
                df = fn(*args, **kwargs)
                return df
            except Exception as e:
                msg = str(e)
                if "402" in msg or "exceed" in msg.lower() or "429" in msg:
                    logger.warning("FinMind 額度耗盡或限流: %s", e)
                    return None
                if attempt < retries:
                    time.sleep(1.5)
                    continue
                logger.debug("FinMind 呼叫失敗: %s (%s)", fn.__name__, e)
        return None

    def _cached_fetch_df(
        self,
        method_name: str,
        fetcher: Callable[[], Optional[pd.DataFrame]],
        ttl: int,
        **kwargs,
    ) -> Optional[pd.DataFrame]:
        """通用 cache-first 取 DataFrame"""
        cache_key = _make_cache_key(method_name, **kwargs)
        # 1. memory cache
        if cache_key in self._mem_cache:
            return self._mem_cache[cache_key]
        # 2. SQLite cache
        if self._enable_cache and self._conn is not None:
            cached = _cache_get(self._conn, cache_key)
            if cached is not None:
                df = _cacheable_to_df(cached)
                self._mem_cache[cache_key] = df
                return df
        # 3. API
        df = self._fetch_with_retry(fetcher)
        if df is not None:
            self._mem_cache[cache_key] = df
            if self._enable_cache and self._conn is not None:
                _cache_set(self._conn, cache_key, _df_to_cacheable(df), ttl)
        return df

    @staticmethod
    def _to_stock_id(symbol: str) -> str:
        """'2330.TW' / '2330.TWO' / '2330' → '2330'

        注意：必須先 replace '.TWO' 再 '.TW'，否則 '5347.TWO' 會變成 '5347O'
        """
        return symbol.replace(".TWO", "").replace(".TW", "").strip()

    # ─────────────────────────────────────────────
    # 全市場清單
    # ─────────────────────────────────────────────

    def get_universe(self, include_tpex: bool = True, force_refresh: bool = False) -> pd.DataFrame:
        """取得全市場上市+上櫃股票清單（含 SQLite cache）"""
        if self._universe_cache is not None and not force_refresh:
            df = self._universe_cache
        else:
            df = self._cached_fetch_df(
                "universe",
                lambda: self._dl.taiwan_stock_info(),
                ttl=TTL_UNIVERSE,
            )
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.drop_duplicates(subset=["stock_id"], keep="first")
            self._universe_cache = df

        if not include_tpex:
            df = df[df["type"] == "twse"]

        return df[["stock_id", "stock_name", "industry_category", "type"]].reset_index(drop=True)

    def filter_common_stocks(self, df: pd.DataFrame) -> pd.DataFrame:
        """過濾掉非普通股（ETF、權證、特別股、KY、DR 等）

        排除邏輯：
            - stock_id 長度 != 4（排除 ETF 如 0050、權證 6 碼）
            - 名稱含 "KY" / "DR" / "特別股" / "優先股"
            - 部分 industry 分類排除
        """
        if df.empty:
            return df
        # 4 碼股票（普通股上市上櫃）
        mask = df["stock_id"].str.len() == 4
        # 排除關鍵字
        name_exclude = df["stock_name"].str.contains("KY|DR|-R|優先|特別", na=False, regex=True)
        # 排除 ETF 類產業（00 開頭的也是 4 碼，但是 ETF）
        etf_mask = df["stock_id"].str.startswith("00")
        return df[mask & ~name_exclude & ~etf_mask].reset_index(drop=True)

    # ─────────────────────────────────────────────
    # 單檔資訊
    # ─────────────────────────────────────────────

    def get_stock_info(self, symbol: str) -> Dict[str, Any]:
        """取得單檔股資訊（名稱、產業）"""
        sid = self._to_stock_id(symbol)
        if sid in self._stock_cache:
            return self._stock_cache[sid]

        universe = self.get_universe()
        row = universe[universe["stock_id"] == sid]
        if row.empty:
            info = {"stock_id": sid, "name": "", "industry": "", "type": ""}
        else:
            r = row.iloc[0]
            info = {
                "stock_id": sid,
                "name": r["stock_name"],
                "industry": r["industry_category"],
                "type": r["type"],
            }
        self._stock_cache[sid] = info
        return info

    def get_ipo_date(self, symbol: str) -> Optional[datetime]:
        """取得上市日期（cache 30 天）"""
        sid = self._to_stock_id(symbol)
        cache_key = _make_cache_key("ipo_date", stock_id=sid)

        # cache
        if self._enable_cache and self._conn is not None:
            cached = _cache_get(self._conn, cache_key)
            if cached is not None and cached.get("ipo"):
                return datetime.fromisoformat(cached["ipo"])

        # API
        try:
            df = self._dl.taiwan_stock_daily(
                stock_id=sid,
                start_date="2000-01-01",
                end_date=(datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
            )
            if df is None or df.empty:
                return None
            earliest = df["date"].min()
            ipo_dt = pd.to_datetime(earliest).to_pydatetime()
            if self._enable_cache and self._conn is not None:
                _cache_set(self._conn, cache_key, {"ipo": ipo_dt.isoformat()}, TTL_IPO_DATE)
            return ipo_dt
        except Exception as e:
            logger.debug("get_ipo_date(%s) 失敗: %s", sid, e)
            return None

    # ─────────────────────────────────────────────
    # 月營收（SEPA ② 營收 YoY）
    # ─────────────────────────────────────────────

    def get_monthly_revenue(self, symbol: str, months: int = 24) -> Optional[pd.DataFrame]:
        """近 N 月營收（cache 1 天）"""
        sid = self._to_stock_id(symbol)
        start = (datetime.now() - timedelta(days=months * 31 + 30)).strftime("%Y-%m-%d")
        df = self._cached_fetch_df(
            "monthly_revenue",
            lambda: self._dl.taiwan_stock_month_revenue(stock_id=sid, start_date=start),
            ttl=TTL_MONTHLY_REVENUE,
            stock_id=sid,
            months=months,
        )
        if df is None or df.empty:
            return None
        return df.sort_values("date").reset_index(drop=True)

    @staticmethod
    def compute_revenue_yoy(df: pd.DataFrame) -> Optional[float]:
        """從月營收 DataFrame 算最近月的 YoY"""
        if df is None or len(df) < 13:
            return None
        latest = df.iloc[-1]
        # 找去年同月
        year_ago = df[(df["revenue_year"] == latest["revenue_year"] - 1) &
                       (df["revenue_month"] == latest["revenue_month"])]
        if year_ago.empty or year_ago.iloc[0]["revenue"] == 0:
            return None
        return (latest["revenue"] - year_ago.iloc[0]["revenue"]) / year_ago.iloc[0]["revenue"]

    # ─────────────────────────────────────────────
    # 財報（SEPA ③ ⑥ ⑧、Value ④）
    # ─────────────────────────────────────────────

    def get_financial_statement(self, symbol: str, quarters: int = 16) -> Optional[pd.DataFrame]:
        """近 N 季財報（cache 7 天）"""
        sid = self._to_stock_id(symbol)
        start = (datetime.now() - timedelta(days=quarters * 95 + 60)).strftime("%Y-%m-%d")
        df = self._cached_fetch_df(
            "financial_stmt",
            lambda: self._dl.taiwan_stock_financial_statement(stock_id=sid, start_date=start),
            ttl=TTL_FINANCIAL,
            stock_id=sid,
            quarters=quarters,
        )
        if df is None or df.empty:
            return None
        return df.sort_values("date").reset_index(drop=True)

    @staticmethod
    def compute_net_income_yoy_qoq(fs_df: pd.DataFrame) -> Dict[str, Optional[float]]:
        """從財報算淨利 YoY / QoQ

        Returns:
            {'yoy': 0.35, 'qoq': 0.12, 'latest_net_income': 12345}
        """
        if fs_df is None or fs_df.empty:
            return {"yoy": None, "qoq": None, "latest_net_income": None}

        # FinMind 財報是 long format，type 欄有多種指標，其中 "IncomeAfterTaxes" 或 "NetIncome"
        target_types = ["IncomeAfterTaxes", "NetIncome", "ProfitAfterTax"]
        for t in target_types:
            sub = fs_df[fs_df["type"] == t]
            if not sub.empty:
                break
        else:
            return {"yoy": None, "qoq": None, "latest_net_income": None}

        sub = sub.sort_values("date").reset_index(drop=True)
        if len(sub) < 5:
            return {"yoy": None, "qoq": None, "latest_net_income": None}

        latest = sub.iloc[-1]["value"]
        qoq_prev = sub.iloc[-2]["value"]  # 前一季
        yoy_prev = sub.iloc[-5]["value"]  # 去年同季（往前 4 季）

        qoq = (latest - qoq_prev) / abs(qoq_prev) if qoq_prev != 0 else None
        yoy = (latest - yoy_prev) / abs(yoy_prev) if yoy_prev != 0 else None

        return {"yoy": yoy, "qoq": qoq, "latest_net_income": latest}

    @staticmethod
    def compute_roe_and_cagr(fs_df: pd.DataFrame) -> Dict[str, Optional[float]]:
        """從財報算近四季 ROE 與 3 年淨利 CAGR

        Returns:
            {'roe_ttm': 0.25, 'cagr_3y': 0.30}
        """
        result = {"roe_ttm": None, "cagr_3y": None}
        if fs_df is None or fs_df.empty:
            return result

        # ROE: FinMind 有 "ReturnOnEquity" 或計算 = 淨利/股東權益
        roe_rows = fs_df[fs_df["type"].isin(["ReturnOnEquity", "ROE"])]
        if not roe_rows.empty:
            recent_4q = roe_rows.sort_values("date").tail(4)
            if len(recent_4q) == 4:
                # ROE 可能是季度百分比，加總當年 TTM
                result["roe_ttm"] = float(recent_4q["value"].sum()) / 100 if recent_4q["value"].max() > 1 else float(recent_4q["value"].sum())

        # 3 年 CAGR: 用年度淨利
        for t in ["IncomeAfterTaxes", "NetIncome", "ProfitAfterTax"]:
            sub = fs_df[fs_df["type"] == t].copy()
            if sub.empty:
                continue
            sub["date"] = pd.to_datetime(sub["date"])
            # 每年 Q4（或最後一季）累加當年淨利
            sub["year"] = sub["date"].dt.year
            yearly = sub.groupby("year")["value"].sum()
            if len(yearly) >= 4:
                recent = yearly.iloc[-1]
                three_ago = yearly.iloc[-4]
                # 兩端都必須為正才能算 CAGR；任一虧損直接視為不達標
                if three_ago > 0 and recent > 0:
                    result["cagr_3y"] = float((recent / three_ago) ** (1/3) - 1)
                else:
                    # 有虧損 → 不符長期成長定義，直接返回 -1（會判 False）
                    result["cagr_3y"] = -1.0
                    result["cagr_note"] = f"近3年有虧損（{three_ago:.0f}→{recent:.0f}）"
            break

        return result

    # ─────────────────────────────────────────────
    # 股利（Value ③）
    # ─────────────────────────────────────────────

    def get_dividends(self, symbol: str, years: int = 6) -> Optional[pd.DataFrame]:
        """近 N 年股利（cache 7 天）"""
        sid = self._to_stock_id(symbol)
        start = (datetime.now() - timedelta(days=years * 370)).strftime("%Y-%m-%d")
        return self._cached_fetch_df(
            "dividends",
            lambda: self._dl.taiwan_stock_dividend(stock_id=sid, start_date=start),
            ttl=TTL_DIVIDEND,
            stock_id=sid,
            years=years,
        )

    @staticmethod
    def compute_dividend_stats(div_df: pd.DataFrame, years: int = 5) -> Dict[str, Any]:
        """計算近 N 年每年是否發放 + 總股息

        Returns:
            {'years_paid': [2020, 2021, 2022, 2023, 2024], 'total_cash_div': 15.5}
        """
        result = {"years_paid": [], "total_cash_div": 0.0}
        if div_df is None or div_df.empty:
            return result

        current_year = datetime.now().year
        div_df = div_df.copy()
        div_df["year"] = pd.to_datetime(div_df["date"]).dt.year
        # 只看現金股利（排除股票股利）
        cash_col = "CashEarningsDistribution" if "CashEarningsDistribution" in div_df.columns else None
        if cash_col is None:
            # 有些版本欄位叫 CashDividend 或 dividend
            for c in ["CashDividend", "cash_earnings_distribution", "cash_dividend"]:
                if c in div_df.columns:
                    cash_col = c
                    break

        if cash_col is None:
            # fallback: 從所有數值欄位找
            numeric_cols = div_df.select_dtypes(include="number").columns.tolist()
            if numeric_cols:
                cash_col = numeric_cols[0]

        if cash_col:
            yearly = div_df.groupby("year")[cash_col].sum()
            within = yearly[yearly.index >= current_year - years]
            result["years_paid"] = sorted([int(y) for y, v in within.items() if v > 0], reverse=True)
            result["total_cash_div"] = float(within.sum())

        return result

    # ─────────────────────────────────────────────
    # P/E P/B（Value ①）
    # ─────────────────────────────────────────────

    def get_per_pbr(self, symbol: str) -> Optional[pd.DataFrame]:
        """最近 30 天 P/E、P/B、殖利率（cache 1 小時）"""
        sid = self._to_stock_id(symbol)
        start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        return self._cached_fetch_df(
            "per_pbr",
            lambda: self._dl.taiwan_stock_per_pbr(stock_id=sid, start_date=start),
            ttl=TTL_PER_PBR,
            stock_id=sid,
        )

    def get_industry_pe_mean(self, industry: str, universe: Optional[pd.DataFrame] = None, max_peers: int = 30) -> Optional[float]:
        """取得某產業的 P/E 中位數（用於 Value ① 產業比較）

        結果有 SQLite cache（1 天 TTL），避免每檔股票都重掃產業
        """
        if not industry:
            return None

        cache_key = _make_cache_key("industry_pe_mean", industry=industry, max_peers=max_peers)

        # 1. memory cache
        if cache_key in self._mem_cache:
            val = self._mem_cache[cache_key]
            return val if isinstance(val, float) else None

        # 2. SQLite cache
        if self._enable_cache and self._conn is not None:
            cached = _cache_get(self._conn, cache_key)
            if cached is not None:
                val = cached.get("pe_mean")
                if val is not None:
                    self._mem_cache[cache_key] = val
                    return val

        # 3. 計算
        if universe is None:
            universe = self.get_universe()
        if universe.empty:
            return None

        peers = universe[universe["industry_category"] == industry]
        pes = []
        for sid in peers["stock_id"].head(max_peers):
            df = self.get_per_pbr(sid)
            if df is not None and not df.empty and "PER" in df.columns:
                latest_pe = df.iloc[-1]["PER"]
                if latest_pe and latest_pe > 0 and latest_pe < 200:  # 剔除極端值
                    pes.append(float(latest_pe))

        if not pes:
            return None
        pe_median = float(pd.Series(pes).median())

        # 存 cache
        self._mem_cache[cache_key] = pe_median
        if self._enable_cache and self._conn is not None:
            _cache_set(self._conn, cache_key, {"pe_mean": pe_median, "peers_count": len(pes)}, TTL_MONTHLY_REVENUE)

        return pe_median
