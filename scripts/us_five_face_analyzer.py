#!/usr/bin/env python3
"""美股五面分析 agent（多源、口徑分線；與 IBKR 選股 agent 並列）。

五面：技術面 / 估值面 / 基本面 / 資金面 / 宏觀與政策面。
- 技術面：IBKR 日K（前結算口徑）——MA 多頭/動量/RS vs SPY/量能
- 估值面：yfinance Ticker.info——PE/forwardPE/PB/PEG
- 基本面：yfinance Ticker.info——盈餘成長/營收成長/利潤率/ROE
- 資金面：yfinance——機構持股比例 + 量能
- 宏觀面：yfinance 巨表指標 VIX/^TNX/DXY（市場層級，全標的共用）

口徑分線（守紀律）：技術面走 IBKR、其餘走 yfinance，各面獨立子分、報告標清來源，不混算。
每面獨立 try/except，取數失敗標 unavailable、不阻斷其他面。
read-only、無下單。等權綜合（首版，待跑幾支校準）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("us_five_face")

US_FIVE_FACE_ENABLED_ENV = "US_FIVE_FACE_ENABLED"
US_FIVE_FACE_CLIENT_ID_ENV = "US_FIVE_FACE_CLIENT_ID"

_FACE_ORDER = ["技術面", "估值面", "基本面", "資金面", "宏觀面"]
# 差異化權重（2026-06-19 校準）：四個個股面加權，宏觀面不入平均、改作環境係數
_FACE_WEIGHTS = {"技術面": 0.30, "基本面": 0.30, "估值面": 0.25, "資金面": 0.15}
_FACE_SOURCE = {
    "技術面": "IBKR 日K（前結算）",
    "估值面": "yfinance info",
    "基本面": "yfinance info/財報",
    "資金面": "yfinance 機構持股+量能",
    "宏觀面": "yfinance VIX/^TNX/DXY",
}


@dataclass
class FaceResult:
    name: str
    available: bool
    score: float          # 0-100，高=有利/偏多
    source: str
    metrics: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class FiveFaceResult:
    symbol: str
    faces: list           # list[FaceResult]
    composite: Optional[float]
    asof: str = ""


def _enabled() -> bool:
    raw = os.getenv(US_FIVE_FACE_ENABLED_ENV)
    return bool(raw) and str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _client_id() -> int:
    return int(os.getenv(US_FIVE_FACE_CLIENT_ID_ENV, "486"))


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _sma(vals: list[float], n: int) -> Optional[float]:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _ret(closes: list[float], n: int) -> Optional[float]:
    if len(closes) <= n or not closes[-(n + 1)]:
        return None
    return (closes[-1] - closes[-(n + 1)]) / closes[-(n + 1)] * 100.0


# ---------------- 技術面（IBKR） ----------------
def _face_technical(provider: Any, symbol: str, spy_ret20: Optional[float]) -> FaceResult:
    src = _FACE_SOURCE["技術面"]
    try:
        from utils.us_research_metrics import technical_metrics

        bars = provider.fetch_historical_bars(symbol, duration="70 D", bar_size="1 day")
        metrics = technical_metrics(list(bars.rows), [])
        if not metrics["available"]:
            return FaceResult("技術面", False, 0.0, src, note="日K 不足 50 根")
        last = metrics["last_close"]
        ma20, ma50 = metrics["ma20"], metrics["ma50"]
        ret20 = metrics["ret20_pct"]
        ma_stacked = bool(metrics["ma_stacked"])
        above_ma20 = bool(ma20 and last > ma20)
        vol_ratio = metrics["volume_ratio_20d"]
        rs = (ret20 - spy_ret20) if (ret20 is not None and spy_ret20 is not None) else None

        score = 50.0
        score += 20 if ma_stacked else (8 if above_ma20 else -15)
        if ret20 is not None:
            score += _clamp(ret20, -15, 15)
        if rs is not None:
            score += _clamp(rs, -10, 10)
        if vol_ratio is not None and vol_ratio > 1.5:
            score += 5
        return FaceResult("技術面", True, _clamp(score), src, metrics={
            "last": round(last, 2), "ret20_pct": round(ret20, 1) if ret20 is not None else None,
            "rs_vs_spy": round(rs, 1) if rs is not None else None,
            "ma_stacked": ma_stacked, "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        })
    except Exception as exc:
        logger.warning("技術面取數失敗 %s: %s", symbol, exc)
        return FaceResult("技術面", False, 0.0, src, note=f"unavailable: {type(exc).__name__}")


# ---------------- 估值面（yfinance，高=便宜/有吸引力；PEG+PE+PB+股息多因子） ----------------
def _dividend_yield_ratio(info: dict) -> tuple[Optional[float], Optional[str]]:
    """向後相容測試入口；正式正規化邏輯由共用 fundamentals helper 提供。"""
    from utils.us_fundamentals import dividend_yield_ratio
    return dividend_yield_ratio(info)


def _face_valuation(info: dict) -> FaceResult:
    src = _FACE_SOURCE["估值面"]
    try:
        from utils.us_research_metrics import info_sections

        normalized = info_sections(info)["valuation"]
        metrics = normalized["metrics"]
        peg = metrics["peg_ratio"]
        fpe = metrics["forward_pe"]
        pb = metrics["price_to_book"]
        div = metrics["dividend_yield_ratio"]
        div_source = metrics["dividend_yield_source"]
        if not normalized["available"]:
            return FaceResult("估值面", False, 0.0, src, note="無估值資料")
        score = 50.0
        if peg is not None:
            score += 18 if peg < 1 else (8 if peg < 2 else (0 if peg < 3 else -12))
        if fpe is not None:
            score += 12 if fpe < 15 else (4 if fpe < 25 else (-4 if fpe < 40 else -12))
        if pb is not None:
            score += 12 if pb < 1.5 else (4 if pb < 4 else (-2 if pb < 10 else -8))
        if div is not None and div > 0:
            score += 10 if div > 0.03 else (4 if div > 0.01 else 0)
        return FaceResult("估值面", True, _clamp(score), src, metrics={
            "pegRatio": peg, "forwardPE": round(fpe, 1) if fpe else None,
            "priceToBook": round(pb, 1) if pb else None,
            "dividendYieldRatio": round(div, 6) if div is not None else None,
            "dividendYieldSource": div_source,
            "trailingPE": round(info.get("trailingPE"), 1) if info.get("trailingPE") else None,
        })
    except Exception as exc:
        return FaceResult("估值面", False, 0.0, src, note=f"unavailable: {type(exc).__name__}")


# ---------------- 基本面（yfinance，高=體質強） ----------------
def _face_fundamental(info: dict) -> FaceResult:
    src = _FACE_SOURCE["基本面"]
    try:
        from utils.us_research_metrics import info_sections

        normalized = info_sections(info)["fundamental"]
        metrics = normalized["metrics"]
        eg = metrics["earnings_growth"]
        rg = metrics["revenue_growth"]
        pm = metrics["profit_margins"]
        roe = metrics["return_on_equity"]
        if not normalized["available"]:
            return FaceResult("基本面", False, 0.0, src, note="無基本面資料")
        score = 50.0
        if eg is not None:
            score += 15 if eg > 0.2 else (5 if eg > 0 else -10)
        if rg is not None:
            score += 10 if rg > 0.15 else (3 if rg > 0 else -8)
        if pm is not None:
            score += 10 if pm > 0.2 else (3 if pm > 0.05 else -10)
        if roe is not None:
            score += 10 if roe > 0.15 else (3 if roe > 0 else -8)
        return FaceResult("基本面", True, _clamp(score), src, metrics={
            "earningsGrowth": round(eg, 2) if eg is not None else None,
            "revenueGrowth": round(rg, 2) if rg is not None else None,
            "profitMargins": round(pm, 2) if pm is not None else None,
            "returnOnEquity": round(roe, 2) if roe is not None else None,
        })
    except Exception as exc:
        return FaceResult("基本面", False, 0.0, src, note=f"unavailable: {type(exc).__name__}")


# ---------------- 資金面（yfinance 機構持股 + 量能） ----------------
def _face_capital(info: dict, tech: FaceResult) -> FaceResult:
    src = _FACE_SOURCE["資金面"]
    try:
        from utils.us_research_metrics import info_sections

        normalized = info_sections(info)["capital"]
        held = normalized["metrics"]["held_percent_institutions"]
        vol_ratio = tech.metrics.get("vol_ratio") if tech.available else None
        if not normalized["available"] and vol_ratio is None:
            return FaceResult("資金面", False, 0.0, src, note="無資金面資料")
        score = 50.0
        if held is not None:
            score += 15 if held > 0.7 else (8 if held > 0.4 else 0)
        if vol_ratio is not None:
            score += 15 if vol_ratio > 1.5 else (5 if vol_ratio > 1 else (-10 if vol_ratio < 0.7 else 0))
        return FaceResult("資金面", True, _clamp(score), src, metrics={
            "heldPercentInstitutions": round(held, 3) if held is not None else None,
            "vol_ratio": vol_ratio,
        })
    except Exception as exc:
        return FaceResult("資金面", False, 0.0, src, note=f"unavailable: {type(exc).__name__}")


# ---------------- 宏觀面（yfinance 巨表，市場層級、全標的共用，高=risk-on） ----------------
def _face_macro() -> FaceResult:
    src = _FACE_SOURCE["宏觀面"]
    try:
        import yfinance as yf
        score = 50.0
        metrics: dict = {}
        try:
            vix = yf.download("^VIX", period="5d", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
            if len(vix):
                v = float(vix.iloc[-1]); metrics["vix"] = round(v, 1)
                score += 20 if v < 15 else (0 if v < 25 else -20)
        except Exception:
            pass
        try:
            tnx = yf.download("^TNX", period="10d", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
            if len(tnx) >= 5:
                chg = float(tnx.iloc[-1] - tnx.iloc[-5]); metrics["tnx_chg_5d"] = round(chg, 2)
                score += 5 if abs(chg) < 0.1 else (-10 if chg > 0.2 else 0)
        except Exception:
            pass
        try:
            dxy = yf.download("DX-Y.NYB", period="10d", progress=False, auto_adjust=False)["Close"].squeeze().dropna()
            if len(dxy) >= 5:
                chg = (float(dxy.iloc[-1]) / float(dxy.iloc[-5]) - 1) * 100; metrics["dxy_chg_5d_pct"] = round(chg, 2)
                score += 5 if abs(chg) < 1 else (-10 if chg > 2 else 0)
        except Exception:
            pass
        if not metrics:
            return FaceResult("宏觀面", False, 0.0, src, note="巨表指標皆取數失敗")
        return FaceResult("宏觀面", True, _clamp(score), src, metrics=metrics,
                          note="政策/經濟日曆首版未接，僅巨表指標")
    except Exception as exc:
        return FaceResult("宏觀面", False, 0.0, src, note=f"unavailable: {type(exc).__name__}")


def _make_provider():
    """建立 read-only IBKR provider（測試以 monkeypatch 替換注入 fake）。"""
    from utils.ibkr_provider import IBKRReadOnlyProvider
    return IBKRReadOnlyProvider(client_id=_client_id())


def _load_info(symbol: str) -> dict:
    """yfinance Ticker.info（估值/基本/資金共用）。抽成函式作為測試注入點。"""
    try:
        import yfinance as yf
        return yf.Ticker(symbol).info or {}
    except Exception as exc:
        logger.warning("yfinance info 取數失敗 %s: %s", symbol, exc)
        return {}


def build_research_faces(symbol: str, provider: Any, info: dict) -> dict[str, FaceResult]:
    """以既有 provider/info 建立可供 Research Pack 共用的四個個股面。"""
    spy_ret20 = None
    try:
        spy = provider.fetch_historical_bars("SPY", duration="70 D", bar_size="1 day")
        spy_closes = [r["close"] for r in spy.rows if r.get("close") is not None]
        spy_ret20 = _ret(spy_closes, 20)
    except Exception:
        pass
    technical = _face_technical(provider, symbol, spy_ret20)
    return {
        "technical": technical,
        "valuation": _face_valuation(info),
        "fundamental": _face_fundamental(info),
        "capital": _face_capital(info, technical),
    }


def _composite(faces: list) -> tuple:
    """差異化權重綜合：技術/基本/估值/資金 加權平均（按可用面重正規化），
    宏觀面不入平均、改作環境係數（risk-on/off 微調 ±10%）。
    回傳 (composite|None, macro_factor)。
    """
    by = {f.name: f for f in faces}
    stock = [(by[n], _FACE_WEIGHTS[n]) for n in _FACE_WEIGHTS if by.get(n) and by[n].available]
    if not stock:
        return None, 1.0
    wsum = sum(w for _, w in stock)
    base = sum(f.score * w for f, w in stock) / wsum
    macro = by.get("宏觀面")
    factor = 1.0
    if macro and macro.available:
        factor = 1 + (macro.score - 50) / 50 * 0.10   # 宏觀 50=中性×1.0，極端 ±10%
    return round(_clamp(base * factor), 1), round(factor, 3)


def analyze(symbol: str, *, provider: Any = None, enabled_override: Optional[bool] = None) -> Optional[FiveFaceResult]:
    """單檔五面分析。未啟用回 None。每面獨立降級、不互相阻斷。"""
    if enabled_override is not True and not _enabled():
        logger.info("五面分析未啟用（%s 未設）", US_FIVE_FACE_ENABLED_ENV)
        return None
    symbol = symbol.strip().upper()

    # yfinance 三面（估值/基本/資金共用一個 Ticker.info）
    info = _load_info(symbol)

    # IBKR 技術面（連線；失敗則技術面 unavailable，其他面照算）
    owns = provider is None
    tech = FaceResult("技術面", False, 0.0, _FACE_SOURCE["技術面"], note="provider 未建立")
    if owns:
        try:
            provider = _make_provider()
        except Exception as exc:
            logger.warning("IBKR provider 建立失敗，技術面略過: %s", exc)
            provider = None
    if provider is not None:
        try:
            ctx = provider if owns else _NullCM(provider)
            with ctx as p:
                tech = build_research_faces(symbol, p, info)["technical"]
        except Exception as exc:
            logger.warning("技術面連線失敗 %s: %s", symbol, exc)

    faces = [
        tech,
        _face_valuation(info),
        _face_fundamental(info),
        _face_capital(info, tech),
        _face_macro(),
    ]
    composite, _factor = _composite(faces)
    return FiveFaceResult(symbol=symbol, faces=faces, composite=composite)


class _NullCM:
    def __init__(self, p): self._p = p
    def __enter__(self): return self._p
    def __exit__(self, *exc): return False


def format_report(result: FiveFaceResult) -> list[str]:
    """五面報告格式化（CLI/飛書/卡片共用）。"""
    comp = f"{result.composite}" if result.composite is not None else "暫無資料"
    lines = [f"【{result.symbol} 五面分析】綜合 {comp}/100（技30/基30/估25/資15 加權，宏觀作環境係數，延遲行情，非交易指令）"]
    order = {f.name: f for f in result.faces}
    for name in _FACE_ORDER:
        f = order.get(name)
        if not f:
            continue
        tag = "（環境係數）" if name == "宏觀面" else ""
        if not f.available:
            lines.append(f"· {name}{tag}：暫無資料（{f.note}）[{f.source}]")
            continue
        m = "，".join(f"{k}={v}" for k, v in f.metrics.items() if v is not None)
        lines.append(f"· {name}{tag}：{f.score:.0f}/100 [{f.source}]" + (f"｜{m}" if m else ""))
    lines.append("口徑：技術面=IBKR 前結算；估值/基本/資金=yfinance；宏觀=巨表指標（只微調綜合±10%）。各面獨立、不混算。")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="美股五面分析（多源 / 延遲行情 / 唯讀）")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--dry-run", action="store_true", help="只輸出，不推送")
    args = parser.parse_args()
    result = analyze(args.symbol, enabled_override=True if args.dry_run else None)
    if result is None:
        return
    if args.dry_run:
        print("\n".join(format_report(result)))


if __name__ == "__main__":
    main()
