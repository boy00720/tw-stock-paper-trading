#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watchlist_merger.py — 合併三個選股 agent 的觀察池，產生 watchlist/current.json

供股市W 讀取作為監控依據。

資料流:
    短線選股W      → logs/screener_tracker/active_picks.json
    成長選股W      → logs/sepa_tracker/watchlist_state.json
    價值選股W      → logs/value_tracker/watchlist_state.json
    日內選股W      → logs/intraday/YYYY-MM-DD.json
                     ↓
    watchlist_merger
                     ↓
    logs/watchlist/current.json ← 股市W 讀此檔監控

功能:
    1. 讀取三個來源
    2. 合併為統一格式（short_term / medium_term / long_term / intraday）
    3. 標註跨池訊號（同一 symbol 出現在多池）
    4. 歷史快照保存到 watchlist/history/YYYY-MM-DD.json

用法:
    python3 utils/watchlist_merger.py                # 合併並輸出
    python3 utils/watchlist_merger.py --dry-run      # 不寫檔，只印
    python3 utils/watchlist_merger.py --show         # 顯示當前 watchlist
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Set, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.intraday_screener import result_from_snapshot, recompute_result_flags
from utils.watchlist_schema import cross_signals as schema_cross_signals, pool_items as schema_pool_items, watchlist_summary as schema_watchlist_summary

_BASE_DIR = Path(__file__).resolve().parent.parent

# 來源檔
SHORT_PICKS_FILE = _BASE_DIR / "logs" / "screener_tracker" / "active_picks.json"
SEPA_STATE_FILE = _BASE_DIR / "logs" / "sepa_tracker" / "watchlist_state.json"
VALUE_STATE_FILE = _BASE_DIR / "logs" / "value_tracker" / "watchlist_state.json"
INTRADAY_DIR = _BASE_DIR / "logs" / "intraday"
LEGACY_INTRADAY_FROZEN_SNAPSHOT = _BASE_DIR / "docs" / "snapshots" / "2026-04-22_intraday_frozen_snapshot.json"
LEGACY_WATCHLIST_FROZEN_SNAPSHOT = _BASE_DIR / "docs" / "snapshots" / "2026-04-22_watchlist_frozen_snapshot.json"
M1_INTRADAY_FROZEN_SNAPSHOT = _BASE_DIR / "docs" / "snapshots" / "2026-04-22_intraday_m1_frozen_snapshot.json"
M1_WATCHLIST_FROZEN_SNAPSHOT = _BASE_DIR / "docs" / "snapshots" / "2026-04-22_watchlist_m1_frozen_snapshot.json"

# 輸出
OUT_DIR = _BASE_DIR / "logs" / "watchlist"
OUT_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR = OUT_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
CURRENT_FILE = OUT_DIR / "current.json"


# ─────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("watchlist_merger")


def _is_freshness_expired(entry: Dict[str, Any], today: Optional[date] = None) -> bool:
    """value/sepa tracker entry 的 freshness_until 若已過期回 True；缺欄位或解析失敗回 False。

    fail-open：解析失敗時保留標的，避免格式微調導致整池被清空。
    """
    fresh = entry.get("freshness_until")
    if not fresh:
        return False
    try:
        d = datetime.fromisoformat(str(fresh).split("T")[0]).date()
    except (ValueError, TypeError):
        return False
    return d < (today or date.today())


def extract_code(symbol: str) -> str:
    return symbol.replace(".TWO", "").replace(".TW", "").lstrip("^")


def format_name_with_code(name: str, symbol: str) -> str:
    code = extract_code(symbol)
    if not code:
        return name
    suffix = f"（{code}）"
    return name if name.endswith(suffix) else f"{name}{suffix}"


def _recover_short_pick_metadata(symbol: str, pick_date: str) -> Dict[str, Any]:
    if not pick_date:
        return {}
    screener_file = _BASE_DIR / "logs" / "screener" / f"{pick_date}.json"
    if not screener_file.exists():
        return {}
    try:
        payload = json.loads(screener_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("讀取 screener 檔失敗 %s: %s", screener_file.name, e)
        return {}

    for group_key in ["momentum_picks", "rebound_picks"]:
        for item in payload.get(group_key) or []:
            if item.get("symbol") != symbol:
                continue
            return {
                "breakdown": item.get("breakdown") or {},
                "entry_zone": item.get("entry_zone") or [],
            }
    return {}


# ─────────────────────────────────────────────────────
# 來源解析
# ─────────────────────────────────────────────────────

def load_short_term() -> List[Dict[str, Any]]:
    """從短線選股 tracker 讀取觀察池

    active_picks.json 的每檔 pick 尚未觸發停損或時間停損 → 算作觀察池成員
    """
    if not SHORT_PICKS_FILE.exists():
        logger.warning("短線資料不存在: %s", SHORT_PICKS_FILE)
        return []
    with open(SHORT_PICKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = []
    for p in data.get("picks", []):
        if p.get("outcome") == "pending":
            recovered = _recover_short_pick_metadata(p["symbol"], p.get("pick_date") or "")
            breakdown = p.get("breakdown") or recovered.get("breakdown")
            entry_zone = p.get("entry_zone") or recovered.get("entry_zone")
            items.append({
                "symbol": p["symbol"],
                "name": format_name_with_code(p["name"], p["symbol"]),
                "engine": p.get("engine", "momentum"),
                "pick_date": p.get("pick_date"),
                "entry_price": p.get("entry_price"),
                "price": p.get("price") if p.get("price") is not None else p.get("entry_price"),
                "stop_loss": p.get("stop_loss"),
                "take_profit": p.get("take_profit"),
                "time_stop_date": p.get("time_stop_date"),
                "score": p.get("score"),
                "inst_signal": p.get("inst_signal"),
                "breakdown": breakdown,
                "entry_zone": entry_zone,
            })
    return items


def load_medium_term() -> List[Dict[str, Any]]:
    """從成長選股 state 讀取觀察池（in_pool=True）"""
    if not SEPA_STATE_FILE.exists():
        logger.warning("SEPA 資料不存在: %s", SEPA_STATE_FILE)
        return []
    with open(SEPA_STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    items = []
    for sym, e in state.get("symbols", {}).items():
        if e.get("in_pool"):
            items.append({
                "symbol": sym,
                "name": format_name_with_code(e.get("name", ""), sym),
                "sector": e.get("sector", ""),
                "framework": "SEPA",
                "pool_entry_date": e.get("pool_entry_date"),
                "freshness_until": e.get("freshness_until"),
                "consecutive_weeks": e.get("consecutive_weeks"),
                "last_passed": e.get("last_passed"),
            })
    return items


def load_long_term() -> List[Dict[str, Any]]:
    """從價值選股 state 讀取觀察池（in_pool=True 且 freshness_until 尚未過期）。

    value_screener 是月頻；若超過 12 天沒跑、freshness_until 已過今天，會自動退池避免 stale 資料污染 cross_signals。
    """
    if not VALUE_STATE_FILE.exists():
        logger.warning("Value 資料不存在: %s", VALUE_STATE_FILE)
        return []
    with open(VALUE_STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    items = []
    expired = 0
    for sym, e in state.get("symbols", {}).items():
        if not e.get("in_pool"):
            continue
        if _is_freshness_expired(e):
            expired += 1
            continue
        items.append({
            "symbol": sym,
            "name": format_name_with_code(e.get("name", ""), sym),
            "sector": e.get("sector", ""),
            "framework": "Value",
            "pool_entry_date": e.get("pool_entry_date"),
            "freshness_until": e.get("freshness_until"),
            "consecutive_months": e.get("consecutive_months"),
            "last_passed": e.get("last_passed"),
        })
    if expired:
        logger.info("long_term: 跳過 %d 檔 freshness 已過期", expired)
    return items


def load_intraday() -> List[Dict[str, Any]]:
    """從日內選股當日結果讀取候選（passed=True）"""
    today_file = INTRADAY_DIR / f"{date.today().isoformat()}.json"
    if not today_file.exists():
        logger.warning("日內資料不存在: %s", today_file)
        return []

    with open(today_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = []
    for result in data.get("results", []):
        if not result.get("passed"):
            continue
        symbol = result["symbol"]
        items.append({
            "symbol": symbol,
            "name": format_name_with_code(result.get("name", ""), symbol),
            "sector": result.get("sector", ""),
            "framework": "Intraday",
            "scan_date": data.get("date"),
            "strict_passed": result.get("strict_passed", False),
            "strict_hard_fails": result.get("values", {}).get("strict_hard_fails", []),
            "checks": result.get("checks", {}),
            "values": result.get("values", {}),
            "notes": result.get("notes", []),
        })
    return items


def load_intraday_snapshot(snapshot_path: Path) -> List[Dict[str, Any]]:
    if not snapshot_path.exists():
        logger.warning("日內 frozen snapshot 不存在: %s", snapshot_path)
        return []

    with open(snapshot_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = []
    for raw in data.get("results", []):
        result = recompute_result_flags(result_from_snapshot(raw))
        if not result.passed:
            continue
        symbol = result.symbol
        items.append({
            "symbol": symbol,
            "name": format_name_with_code(result.name, symbol),
            "sector": result.sector,
            "framework": "Intraday",
            "scan_date": data.get("date"),
            "strict_passed": result.strict_passed,
            "strict_hard_fails": result.values.get("strict_hard_fails", []),
            "checks": result.checks,
            "values": result.values,
            "notes": result.notes,
        })
    return items



def compute_cross_signals(
    short: List[Dict], medium: List[Dict], long_: List[Dict], intraday: List[Dict]
) -> Dict[str, Dict[str, Any]]:
    """對出現在多池的個股標記交叉訊號"""
    symbol_pools: Dict[str, Set[str]] = {}
    pool_data: Dict[str, Dict[str, Any]] = {}

    def add(items, pool_key):
        for item in items:
            sym = item["symbol"]
            symbol_pools.setdefault(sym, set()).add(pool_key)
            pool_data.setdefault(sym, {})["name"] = item.get("name", "")
            pool_data[sym].setdefault("_notes", {})[pool_key] = item

    add(short, "short_term")
    add(medium, "medium_term")
    add(long_, "long_term")
    add(intraday, "intraday")

    def build_summary(pools_list: List[str], level: str, pool_notes: Dict[str, str]) -> str:
        pool_label_map = {
            "short_term": "短線",
            "medium_term": "中線",
            "long_term": "長線",
            "intraday": "日內",
        }
        pool_text = "、".join(pool_label_map.get(pool, pool) for pool in pools_list) if pools_list else "跨池"
        note_text = "；".join(str(value).strip() for value in pool_notes.values() if str(value).strip())
        parts = [part for part in [pool_text, level, note_text] if part]
        return "｜".join(parts) if parts else "跨池觀察"

    cross = {}
    for sym, pools in symbol_pools.items():
        if len(pools) <= 1:
            continue

        name = format_name_with_code(pool_data[sym]["name"], sym)
        notes = pool_data[sym]["_notes"]
        pools_list = sorted(pools)

        if len(pools) >= 4:
            level = "⭐⭐⭐⭐⭐ 四池齊亮，極罕見"
        elif "long_term" in pools and "medium_term" in pools and "short_term" in pools:
            level = "⭐⭐⭐⭐ 三池齊亮，主升段候選"
        elif "intraday" in pools and "short_term" in pools:
            level = "⭐⭐⭐ 短線共振，盤中加速"
        elif "intraday" in pools and "medium_term" in pools:
            level = "⭐⭐⭐ 成長股盤中點火"
        elif "intraday" in pools and "long_term" in pools:
            level = "⭐⭐ 低估股出現短打訊號"
        elif "long_term" in pools and "medium_term" in pools:
            level = "⭐⭐⭐ 低估的高成長股（最強）"
        elif "long_term" in pools and "short_term" in pools:
            level = "⭐⭐ 便宜股有人開始買"
        elif "medium_term" in pools and "short_term" in pools:
            level = "⭐⭐ 成長股技術突破"
        else:
            level = "⭐"

        pool_notes = {}
        if "short_term" in notes:
            s = notes["short_term"]
            engine_label = _humanize_short_engine(s.get("engine"))
            pool_notes["short"] = f"{engine_label}評分 {s.get('score')}"
        if "medium_term" in notes:
            m = notes["medium_term"]
            pool_notes["medium"] = f"SEPA 進池 {m.get('pool_entry_date')}"
        if "long_term" in notes:
            l = notes["long_term"]
            pool_notes["long"] = f"價值進池 {l.get('pool_entry_date')}"
        if "intraday" in notes:
            i = notes["intraday"]
            blocker = ",".join(i.get("strict_hard_fails", []))
            suffix = f"，strict 阻塞 {blocker}" if blocker else ""
            pool_notes["intraday"] = f"日內掃描 {i.get('scan_date')} 通過數 {i.get('values', {}).get('通過數')}{suffix}"

        cross[sym] = {
            "name": name,
            "pools": pools_list,
            "level": level,
            "notes": pool_notes,
            "summary": build_summary(pools_list, level, pool_notes),
        }

    return cross


# ─────────────────────────────────────────────────────
# 合併輸出
# ─────────────────────────────────────────────────────

def build_watchlist(intraday_override: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    short = load_short_term()
    medium = load_medium_term()
    long_ = load_long_term()
    intraday = intraday_override if intraday_override is not None else load_intraday()
    cross = compute_cross_signals(short, medium, long_, intraday)

    return {
        "updated_at": datetime.now().isoformat(),
        "schema_version": 2,
        "summary": {
            "short_term_count": len(short),
            "medium_term_count": len(medium),
            "long_term_count": len(long_),
            "intraday_count": len(intraday),
            "intraday_strict_count": sum(1 for item in intraday if item.get("strict_passed")),
            "cross_signals_count": len(cross),
        },
        "formal_pools": {
            "short_term": short,
            "medium_term": medium,
            "long_term": long_,
        },
        "auxiliary_signals": {
            "intraday": intraday,
        },
        "cross_signals": cross,
    }


def save_watchlist(watchlist: Dict[str, Any]) -> None:
    # 當前
    with open(CURRENT_FILE, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2, default=str)
    logger.info("✓ current.json 已更新: %s", CURRENT_FILE)

    # 歷史快照
    today = date.today().isoformat()
    hist_file = HISTORY_DIR / f"{today}.json"
    with open(hist_file, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2, default=str)
    logger.info("✓ 歷史快照: %s", hist_file)




def _resolve_frozen_paths(profile: str) -> tuple[Path, Path]:
    if profile == "m1":
        return M1_INTRADAY_FROZEN_SNAPSHOT, M1_WATCHLIST_FROZEN_SNAPSHOT
    return LEGACY_INTRADAY_FROZEN_SNAPSHOT, LEGACY_WATCHLIST_FROZEN_SNAPSHOT


def save_frozen_watchlist(watchlist: Dict[str, Any], output_path: Path = LEGACY_WATCHLIST_FROZEN_SNAPSHOT) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2, default=str)
    logger.info("✓ frozen watchlist 已更新: %s", output_path)



# ─────────────────────────────────────────────────────
# 顯示
# ─────────────────────────────────────────────────────



def _humanize_short_engine(engine: Any) -> str:
    if engine == "momentum":
        return "量價突破"
    if engine == "rebound":
        return "均值回歸"
    return str(engine)



def show_watchlist(watchlist: Dict[str, Any]) -> None:
    s = schema_watchlist_summary(watchlist)
    print(f"=== Watchlist (updated {watchlist.get('updated_at', '')}) ===")
    print(
        f"短線: {s['short_term_count']} | 中線: {s['medium_term_count']} | "
        f"長線: {s['long_term_count']} | 日內: {s.get('intraday_count', 0)} "
        f"(嚴格 {s.get('intraday_strict_count', 0)}) | 跨池訊號: {s['cross_signals_count']}"
    )
    print()

    def print_pool(name: str, items: List[Dict], fmt_fn):
        if not items:
            print(f"[{name}] 空")
            return
        print(f"[{name}] {len(items)} 檔")
        for item in items:
            print(f"  {fmt_fn(item)}")
        print()

    print_pool(
        "短線雙引擎", schema_pool_items(watchlist, "short_term"),
        lambda i: f"{i['name']} 分數 {i.get('score')} 策略 {_humanize_short_engine(i.get('engine'))} 進場 {i.get('pick_date')}",
    )
    print_pool(
        "中線 SEPA", schema_pool_items(watchlist, "medium_term"),
        lambda i: f"{i['name']} 進池 {i.get('pool_entry_date')} 保鮮至 {i.get('freshness_until')}",
    )
    print_pool(
        "長線價值", schema_pool_items(watchlist, "long_term"),
        lambda i: f"{i['name']} 進池 {i.get('pool_entry_date')} 保鮮至 {i.get('freshness_until')}",
    )
    print_pool(
        "日內動能", schema_pool_items(watchlist, "intraday"),
        lambda i: f"{i['name']} 掃描日 {i.get('scan_date')} 通過數 {i.get('values', {}).get('通過數')} 嚴格 {'是' if i.get('strict_passed') else '否'}"
        + (f" 阻塞 {','.join(i.get('strict_hard_fails', []))}" if i.get('strict_hard_fails') else ""),
    )

    cross = schema_cross_signals(watchlist)
    if cross:
        print(f"[⭐ 跨池訊號] {len(cross)} 檔")
        for sym, c in cross.items():
            print(f"  {c['name']} — {c['level']}")
            for k, v in c['notes'].items():
                print(f"    └ {k}: {v}")


# ─────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="合併觀察池")
    parser.add_argument("--dry-run", action="store_true", help="不寫檔")
    parser.add_argument("--show", action="store_true", help="顯示當前 watchlist")
    parser.add_argument("--refresh-frozen", action="store_true", help="用版控 intraday snapshot 刷新 frozen watchlist")
    parser.add_argument("--frozen-profile", choices=["legacy", "m1"], default="legacy", help="選擇 frozen fixture profile")
    args = parser.parse_args()

    if args.show:
        if CURRENT_FILE.exists():
            with open(CURRENT_FILE, "r", encoding="utf-8") as f:
                show_watchlist(json.load(f))
        else:
            print("current.json 不存在，先跑合併")
        return

    if args.refresh_frozen:
        intraday_snapshot, watchlist_snapshot = _resolve_frozen_paths(args.frozen_profile)
        watchlist = build_watchlist(intraday_override=load_intraday_snapshot(intraday_snapshot))
        save_frozen_watchlist(watchlist, output_path=watchlist_snapshot)
        show_watchlist(watchlist)
        return

    watchlist = build_watchlist()
    if args.dry_run:
        print(json.dumps(watchlist, ensure_ascii=False, indent=2, default=str))
    else:
        save_watchlist(watchlist)
        show_watchlist(watchlist)


if __name__ == "__main__":
    main()
