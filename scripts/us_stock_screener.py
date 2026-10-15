#!/usr/bin/env python3
"""IBKR 美股選股 agent（read-only、flag-gated；與台股選股線完全獨立）。

流程：IBKR 掃描器多榜並集（活躍/漲幅/爆量）→ 候選用日K 技術過濾
（MA 多頭排列、動量、距高、量能、RS vs SPY）→ 綜合分排序 → top N 觀察清單。

安全邊界：
- 全程 read-only（掃描器 + 歷史 bar，無下單能力）。
- delayed 數據；清單屬觀察參考，非即時行情、非交易指令。
- 預設關（US_STOCK_SCREENER_ENABLED）；與台股 screener / 美股報告卡解耦。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("us_stock_screener")

US_STOCK_SCREENER_ENABLED_ENV = "US_STOCK_SCREENER_ENABLED"
US_STOCK_SCREENER_CLIENT_ID_ENV = "US_STOCK_SCREENER_CLIENT_ID"

# 掃描器 filter（排除仙股 / 低流動性；漲幅榜本質小票主導，靠這層 + 技術過濾把關）
_SCAN_MIN_PRICE = 10.0
_SCAN_MIN_VOLUME = 1_000_000
_SCAN_ROWS = 20

# 泛大盤/基準 ETF 黑名單（選它們無選股意義；行業/槓桿 ETF 仍保留）
_EXCLUDE_SYMBOLS = {"SPY", "QQQ", "QQQM", "DIA", "IWM", "VOO", "VTI", "IVV", "VTV", "VUG"}

# 場景 → 掃描器 scanCode 組合（多榜並集；MOST_ACTIVE 帶主流大票進池，平衡漲幅榜的冷門小票）
SCENE_SCANS: dict[str, list[str]] = {
    "premarket": ["MOST_ACTIVE", "TOP_PERC_GAIN"],
    "intraday": ["MOST_ACTIVE", "TOP_PERC_GAIN", "HOT_BY_VOLUME"],
    "postclose": ["MOST_ACTIVE", "TOP_PERC_GAIN", "HOT_BY_VOLUME"],
}

_MAX_CANDIDATES = 20    # 控制歷史請求數，規避 IBKR pacing（多榜命中者優先）
_HIST_SLEEP = 0.4       # 每檔歷史請求間隔（規避 pacing violation）
_TOP_N = 10             # 輸出清單長度
_SCENE_LABELS = {
    "premarket": "盤前",
    "intraday": "盤中",
    "postclose": "盤後",
}


@dataclass
class USCandidate:
    symbol: str
    last: Optional[float] = None
    change_20d_pct: Optional[float] = None
    rs_vs_spy: Optional[float] = None
    above_ma20: bool = False
    above_ma50: bool = False
    ma_stacked: bool = False              # close > MA20 > MA50
    vol_ratio: Optional[float] = None      # 最近量 / 20 日均量
    pct_from_high: Optional[float] = None  # 距 70 日高 %（負=低於高點）
    score: float = 0.0
    sources: list[str] = field(default_factory=list)


def _enabled() -> bool:
    raw = os.getenv(US_STOCK_SCREENER_ENABLED_ENV)
    return bool(raw) and str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _client_id() -> int:
    return int(os.getenv(US_STOCK_SCREENER_CLIENT_ID_ENV, "483"))


def _make_provider():
    """建立 read-only IBKR provider。

    抽成獨立函式作為注入點：測試以 monkeypatch 替換它注入 fake provider，
    避免依賴 sys.modules patch（受測試順序污染、會誤真連 Gateway）。
    """
    from utils.ibkr_provider import IBKRReadOnlyProvider
    return IBKRReadOnlyProvider(client_id=_client_id())


def _sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _series_return(closes: list[float], n: int) -> Optional[float]:
    if len(closes) <= n or not closes[-(n + 1)]:
        return None
    return (closes[-1] - closes[-(n + 1)]) / closes[-(n + 1)] * 100.0


def _compute(symbol: str, rows: list[dict], spy_ret_20d: Optional[float], sources: list[str]) -> Optional[USCandidate]:
    closes = [r["close"] for r in rows if r.get("close") is not None]
    highs = [r["high"] for r in rows if r.get("high") is not None]
    vols = [r["volume"] for r in rows if r.get("volume") is not None]
    if len(closes) < 50:
        return None

    last = closes[-1]
    ma20 = _sma(closes, 20)
    ma50 = _sma(closes, 50)
    ret20 = _series_return(closes, 20)

    vol_ratio = None
    if len(vols) >= 20:
        avg20 = sum(vols[-20:]) / 20
        if avg20 > 0:
            vol_ratio = vols[-1] / avg20

    hi = max(highs[-70:]) if highs else None
    pct_from_high = ((last - hi) / hi * 100.0) if hi else None
    rs = (ret20 - spy_ret_20d) if (ret20 is not None and spy_ret_20d is not None) else None

    cand = USCandidate(
        symbol=symbol, last=last, change_20d_pct=ret20, rs_vs_spy=rs,
        above_ma20=bool(ma20 and last > ma20),
        above_ma50=bool(ma50 and last > ma50),
        ma_stacked=bool(ma20 and ma50 and last > ma20 > ma50),
        vol_ratio=vol_ratio, pct_from_high=pct_from_high, sources=sources,
    )

    # 評分：強勢動能 + 結構（截斷避免單因子失衡）
    s = 0.0
    if cand.ma_stacked:
        s += 30
    elif cand.above_ma20:
        s += 15
    if ret20 is not None:
        s += max(min(ret20, 30.0), -10.0)
    if rs is not None and rs > 0:
        s += min(rs, 20.0)
    if vol_ratio is not None and vol_ratio > 1.5:
        s += 10
    if pct_from_high is not None and pct_from_high > -5:
        s += 10
    cand.score = round(s, 1)
    return cand


def run_screener(
    scene: str,
    *,
    dry_run: bool = False,
    top_n: int = _TOP_N,
    provider: Any = None,
    enabled_override: Optional[bool] = None,
) -> Optional[list[USCandidate]]:
    """拉榜 → 技術過濾 → 評分 → top N。未啟用回 None，無候選回 []。

    provider：傳入則複用既有 IBKR 連線（呼叫端管理生命週期，本函式不關閉）；
    不傳則自建（跑完關閉）。卡片/飛書複用連線時傳入，省握手。
    enabled_override：True 時繞過 US_STOCK_SCREENER_ENABLED gate（卡片內嵌用獨立
    US_STOCK_SCREENER_CARD_ENABLED 控制、透傳此參數，兩 flag 互不綁架）。
    """
    if enabled_override is not True and not _enabled():
        logger.info("US stock screener 未啟用（%s 未設）", US_STOCK_SCREENER_ENABLED_ENV)
        return None
    scan_codes = SCENE_SCANS.get(scene)
    if not scan_codes:
        logger.error("未知場景: %s（可選 %s）", scene, list(SCENE_SCANS))
        return None

    owns_provider = provider is None
    if owns_provider:
        try:
            provider = _make_provider()
        except Exception:
            logger.exception("IBKR provider 建立失敗")
            return None

    candidates: dict[str, list[str]] = {}
    spy_ret20: Optional[float] = None
    out: list[USCandidate] = []
    try:
        ctx = provider if owns_provider else _NullCM(provider)
        with ctx as p:
            # 1) 多榜並集
            for code in scan_codes:
                try:
                    rows = p.fetch_scanner(code, above_price=_SCAN_MIN_PRICE,
                                           above_volume=_SCAN_MIN_VOLUME, number_of_rows=_SCAN_ROWS)
                    for r in rows:
                        if r.symbol in _EXCLUDE_SYMBOLS:
                            continue
                        candidates.setdefault(r.symbol, []).append(code)
                except Exception:
                    logger.warning("掃描器 %s 失敗，略過", code, exc_info=True)
            if not candidates:
                logger.warning("掃描器無候選，跳過本輪")
                return []
            # 多榜命中者優先進技術過濾（既活躍又強勢）
            syms = sorted(candidates, key=lambda s: len(candidates[s]), reverse=True)[:_MAX_CANDIDATES]
            logger.info("候選 %d 檔，取前 %d 算技術指標", len(candidates), len(syms))

            # 2) SPY 基準（算 RS）
            try:
                spy_bars = p.fetch_historical_bars("SPY", duration="70 D", bar_size="1 day")
                spy_closes = [r["close"] for r in spy_bars.rows if r.get("close") is not None]
                spy_ret20 = _series_return(spy_closes, 20)
            except Exception:
                logger.warning("SPY 基準取得失敗，RS 略過", exc_info=True)

            # 3) 候選技術過濾
            for sym in syms:
                try:
                    bars = p.fetch_historical_bars(sym, duration="70 D", bar_size="1 day")
                    cand = _compute(sym, bars.rows, spy_ret20, candidates[sym])
                    if cand:
                        out.append(cand)
                except Exception:
                    logger.warning("歷史指標 %s 失敗，略過", sym, exc_info=True)
                time.sleep(_HIST_SLEEP)
    except Exception:
        logger.exception("US screener 連線/執行失敗")
        return None

    out.sort(key=lambda c: c.score, reverse=True)
    picks = out[:top_n]
    if dry_run:
        _print_picks(scene, picks, spy_ret20)
    return picks


class _NullCM:
    """對已開啟的 provider 做 no-op context manager（複用連線時用）。"""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def __enter__(self) -> Any:
        return self._provider

    def __exit__(self, *exc) -> bool:
        return False


def format_picks(scene: str, picks: list[USCandidate], spy_ret20: Optional[float] = None) -> list[str]:
    """清單格式化（CLI / 卡片 / 飛書共用）。回傳逐行 list。"""
    scene_label = _SCENE_LABELS.get(scene, scene)
    head = f"全市場強勢股（{scene_label}，延遲行情）"
    if spy_ret20 is not None:
        head += f"  SPY 20日 {spy_ret20:+.1f}%"
    lines = [head, "（觀察清單，非交易指令）"]
    if not picks:
        lines.append("本輪無符合條件候選")
        return lines
    for i, c in enumerate(picks, 1):
        bits: list[str] = []
        if c.last is not None:
            bits.append(f"${c.last:.2f}")
        if c.change_20d_pct is not None:
            bits.append(f"20日漲跌 {c.change_20d_pct:+.1f}%")
        if c.rs_vs_spy is not None:
            bits.append(f"相對SPY {c.rs_vs_spy:+.1f}")
        if c.vol_ratio is not None:
            bits.append(f"量比 {c.vol_ratio:.1f}")
        struct = "多頭排列" if c.ma_stacked else ("站上MA20" if c.above_ma20 else "弱結構")
        lines.append(f"{i}. {c.symbol} 分{c.score} {struct} " + " ".join(bits) + f" [{'/'.join(c.sources)}]")
    return lines


def _print_picks(scene: str, picks: list[USCandidate], spy_ret20: Optional[float]) -> None:
    print("# IBKR 美股選股")
    for line in format_picks(scene, picks, spy_ret20):
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="IBKR 美股選股（唯讀 / 延遲行情）")
    parser.add_argument("--scene", choices=list(SCENE_SCANS), default="intraday")
    parser.add_argument("--dry-run", action="store_true", help="只輸出清單，不推送")
    parser.add_argument("--top", type=int, default=_TOP_N)
    args = parser.parse_args()
    run_screener(args.scene, dry_run=args.dry_run, top_n=args.top)


if __name__ == "__main__":
    main()
