"""automation/sim/advisor/metrics.py — 四件套指標 + 0050 baseline（M6 階段二 區塊 F）

四件套（W 拍板 W6 2026-05-06：20 交易日達標即進 M7 落地評估）：
- 勝率 win_rate（盈利筆數 / 總筆數）
- 累積 PnL cum_realized_pnl（已實現損益總和；advisor 是否要納入未實現由 caller 決定）
- 最大回撤 max_drawdown_pct（從 equity_curve.jsonl 算 peak-to-trough）
- 盈虧比 profit_loss_ratio（avg_win / avg_loss 絕對值）

baseline：0050.TW 同期累積報酬率（拉 yfinance history；網路失敗回 None、
不阻擋四件套輸出）。

對外四個函數：
- compute_max_drawdown_pct(equity_curve_path, *, since=None) -> float
- compute_profit_loss_ratio(fills_path, *, since=None) -> dict{avg_win, avg_loss, ratio, wins, losses}
- compute_baseline_return(symbol="0050.TW", *, since=None) -> Optional[float]
- compute_four_pillars(fills_path, equity_curve_path, *, since=None) -> dict

since=None 表示「截至今日的全期」；since=date(YYYY,MM,DD) 表示從該日起算（含）。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_FILLS_PATH = _BASE_DIR / "logs" / "sim" / "fills.jsonl"
DEFAULT_EQUITY_CURVE_PATH = _BASE_DIR / "logs" / "sim" / "equity_curve.jsonl"
DEFAULT_EXCLUDED_DATES_PATH = _BASE_DIR / "config" / "sim_metrics_excluded_dates.yaml"


# ─────────────────────────────────────────────────────
# Helpers：jsonl tail / time filter
# ─────────────────────────────────────────────────────


def _iter_jsonl(path: Path | str) -> Iterable[dict[str, Any]]:
    """讀 jsonl 序列，損壞行略過。重用模式跟 sim.reports._iter_jsonl 一致。"""
    p = Path(path)
    if not p.exists():
        return
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except Exception:
        logger.exception("[advisor.metrics] 讀取 jsonl 失敗: %s", path)


def _parse_iso_or_none(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _is_in_window(record_time: Optional[datetime], since: Optional[date]) -> bool:
    if since is None:
        return True
    if record_time is None:
        return False
    return record_time.date() >= since


def _is_excluded(record_time: Optional[datetime], excluded: Optional[Iterable[date]]) -> bool:
    """檔內 record 日期是否落在排除清單。None record_time 永遠不視為排除。"""
    if not excluded:
        return False
    if record_time is None:
        return False
    return record_time.date() in excluded


def load_excluded_dates(path: Optional[Path | str] = None) -> set[date]:
    """讀 sim_metrics_excluded_dates.yaml，回排除日期集合。

    檔不存在 / 格式錯 / 空清單 → 回空 set，不 raise。
    """
    p = Path(path) if path else DEFAULT_EXCLUDED_DATES_PATH
    if not p.exists():
        return set()
    try:
        import yaml
    except ImportError:
        logger.warning("[advisor.metrics] yaml 未安裝，無法載入 excluded_dates")
        return set()
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.exception("[advisor.metrics] excluded_dates 讀取失敗: %s", p)
        return set()
    items = data.get("excluded_dates") or []
    excluded: set[date] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("date")
        if not raw:
            continue
        try:
            excluded.add(date.fromisoformat(str(raw)))
        except ValueError:
            logger.warning("[advisor.metrics] excluded_dates 跳過無法解析的日期: %r", raw)
            continue
    return excluded


# ─────────────────────────────────────────────────────
# 最大回撤
# ─────────────────────────────────────────────────────


def compute_max_drawdown_pct(
    equity_curve_path: Path | str = DEFAULT_EQUITY_CURVE_PATH,
    *,
    since: Optional[date] = None,
    excluded_dates: Optional[Iterable[date]] = None,
) -> float:
    """從 equity_curve.jsonl 算 peak-to-trough 最大回撤百分比（正數，例 0.083 = 8.3%）。

    無資料 → 0.0。
    since 過濾後仍無資料 → 0.0。
    excluded_dates 內的日期會整日從 equity 序列移除（影響 peak 與 trough 計算）。
    """
    excluded_set: set[date] = set(excluded_dates) if excluded_dates else set()
    equity_series: list[float] = []
    for record in _iter_jsonl(equity_curve_path):
        rt = _parse_iso_or_none(record.get("time"))
        if not _is_in_window(rt, since):
            continue
        if _is_excluded(rt, excluded_set):
            continue
        try:
            eq = float(record.get("equity"))
        except (TypeError, ValueError):
            continue
        equity_series.append(eq)

    if len(equity_series) < 2:
        return 0.0

    peak = equity_series[0]
    max_dd = 0.0
    for eq in equity_series:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


# ─────────────────────────────────────────────────────
# 盈虧比
# ─────────────────────────────────────────────────────


def compute_profit_loss_ratio(
    fills_path: Path | str = DEFAULT_FILLS_PATH,
    *,
    since: Optional[date] = None,
    excluded_dates: Optional[Iterable[date]] = None,
) -> dict[str, Any]:
    """對每筆 SELL fill 算實現損益、分盈/虧兩堆、回 avg_win / avg_loss / ratio。

    重用 sim.reports._per_fill_realized 計算每筆 sell 的 realized_pnl（含 FIFO/avg_cost
    跟 engine 一致）。

    回傳 dict：
        {wins: int, losses: int, avg_win: float, avg_loss: float (絕對值),
         ratio: float (avg_win / avg_loss; avg_loss=0 時 ratio=inf)}

    fills 為空時 wins=losses=0、ratio=0.0。
    excluded_dates 內的日期會把該日 fills 全部跳過。
    """
    excluded_set: set[date] = set(excluded_dates) if excluded_dates else set()
    try:
        from sim.reports import _per_fill_realized
    except ImportError:
        logger.warning("[advisor.metrics] sim.reports._per_fill_realized 不可用")
        return {"wins": 0, "losses": 0, "avg_win": 0.0, "avg_loss": 0.0, "ratio": 0.0}

    try:
        per_fill = _per_fill_realized(fills_path)
    except Exception:
        logger.exception("[advisor.metrics] _per_fill_realized 失敗")
        return {"wins": 0, "losses": 0, "avg_win": 0.0, "avg_loss": 0.0, "ratio": 0.0}

    # _per_fill_realized 回 {fill_id: realized}；要過 since / excluded 必須再讀一次 fills 取 fill_id → time 對照
    need_time_map = since is not None or bool(excluded_set)
    fill_id_to_time: dict[str, Optional[datetime]] = {}
    if need_time_map:
        for fill in _iter_jsonl(fills_path):
            fid = fill.get("fill_id")
            if fid:
                fill_id_to_time[fid] = _parse_iso_or_none(fill.get("filled_at"))

    wins: list[float] = []
    losses: list[float] = []
    for fid, realized in per_fill.items():
        if need_time_map:
            t = fill_id_to_time.get(fid)
            if not _is_in_window(t, since):
                continue
            if _is_excluded(t, excluded_set):
                continue
        if realized > 0:
            wins.append(realized)
        elif realized < 0:
            losses.append(realized)
        # realized == 0 不計

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss_abs = abs(sum(losses) / len(losses)) if losses else 0.0
    if avg_loss_abs > 0:
        ratio = avg_win / avg_loss_abs
    else:
        # 無虧損筆數：若有盈利視為 ∞、否則 0
        ratio = float("inf") if wins else 0.0

    return {
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": avg_win,
        "avg_loss": avg_loss_abs,
        "ratio": ratio,
    }


# ─────────────────────────────────────────────────────
# 0050 baseline（拉 yfinance）
# ─────────────────────────────────────────────────────


def compute_baseline_return(
    symbol: str = "0050.TW",
    *,
    since: Optional[date] = None,
) -> Optional[float]:
    """拉 yfinance 從 since 到今天的累積報酬率（小數，0.05 = +5%）。

    since=None → 取近 30 日。
    網路失敗 / 資料不足 → None（不 raise，daily_brief 拿到 None 會印 "—"）。
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("[advisor.metrics] yfinance 未安裝，無法算 baseline")
        return None

    if since is None:
        since = date.today() - timedelta(days=30)

    try:
        ticker = yf.Ticker(symbol)
        # period 給足夠長的範圍，再篩日期
        hist = ticker.history(start=since.isoformat(), interval="1d")
    except Exception:
        logger.exception("[advisor.metrics] yfinance history 拉取失敗 %s", symbol)
        return None

    if hist is None or len(hist) < 2:
        logger.warning("[advisor.metrics] %s history 資料不足: rows=%s", symbol, len(hist) if hist is not None else 0)
        return None

    try:
        first_close = float(hist["Close"].iloc[0])
        last_close = float(hist["Close"].iloc[-1])
    except (KeyError, IndexError, ValueError):
        logger.exception("[advisor.metrics] history close 欄位異常 %s", symbol)
        return None

    if first_close <= 0:
        return None
    return (last_close - first_close) / first_close


# ─────────────────────────────────────────────────────
# 主函數：聚合四件套
# ─────────────────────────────────────────────────────


def compute_four_pillars(
    fills_path: Path | str = DEFAULT_FILLS_PATH,
    equity_curve_path: Path | str = DEFAULT_EQUITY_CURVE_PATH,
    *,
    since: Optional[date] = None,
    baseline_symbol: str = "0050.TW",
    excluded_dates: Optional[Iterable[date]] = None,
) -> dict[str, Any]:
    """聚合四件套指標 + baseline。

    回傳 dict：
        {
          win_rate: {total, win, rate},
          cum_realized_pnl: float,
          max_drawdown_pct: float,
          profit_loss: {wins, losses, avg_win, avg_loss, ratio},
          baseline: {symbol, return_pct (Optional)},
          since: ISO date or "all_time",
          excluded_dates: list[ISO date],
        }

    excluded_dates 預設讀 config/sim_metrics_excluded_dates.yaml；明確傳入會覆蓋。
    baseline 不受 excluded_dates 影響（外部市場資料）。
    """
    # 預設從 config 載；caller 明確傳入空 list/set 表示「不排除」
    if excluded_dates is None:
        excluded_set: set[date] = load_excluded_dates()
    else:
        excluded_set = set(excluded_dates)

    # 重用 sim.reports.win_rate + iter_fills + _per_fill_realized；過 since
    # 注意：win_rate 需要 fills 已含 realized_pnl 欄位，所以先用 _per_fill_realized 補上
    try:
        from sim.reports import _per_fill_realized, iter_fills, win_rate
    except ImportError:
        wr = {"total": 0, "win": 0, "rate": 0.0}
        cum_pnl = 0.0
    else:
        try:
            per_fill = _per_fill_realized(fills_path)
        except Exception:
            logger.exception("[advisor.metrics] _per_fill_realized 失敗")
            per_fill = {}

        fills_in_window: list[dict[str, Any]] = []
        cum_pnl = 0.0
        for fill in iter_fills(fills_path):
            t = _parse_iso_or_none(fill.get("filled_at"))
            if not _is_in_window(t, since):
                continue
            if _is_excluded(t, excluded_set):
                continue
            fid = fill.get("fill_id")
            if fid in per_fill:
                fill = {**fill, "realized_pnl": per_fill[fid]}
                cum_pnl += per_fill[fid]
            fills_in_window.append(fill)
        wr = win_rate(fills_in_window)
        if wr.get("rate") is None:
            wr = {**wr, "rate": 0.0}

    mdd = compute_max_drawdown_pct(equity_curve_path, since=since, excluded_dates=excluded_set)
    pl = compute_profit_loss_ratio(fills_path, since=since, excluded_dates=excluded_set)
    baseline = compute_baseline_return(baseline_symbol, since=since)

    return {
        "win_rate": wr,
        "cum_realized_pnl": cum_pnl,
        "max_drawdown_pct": mdd,
        "profit_loss": pl,
        "baseline": {
            "symbol": baseline_symbol,
            "return_pct": baseline,
        },
        "since": since.isoformat() if since else "all_time",
        "excluded_dates": sorted(d.isoformat() for d in excluded_set),
    }
