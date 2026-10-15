#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
universe_validator.py — 驗證 stock_universe.yaml 中的代碼是否有效

用法：
    python3 scripts/universe_validator.py          # 靜態檢查候選池格式、重複代號與已知市場後綴
    python3 scripts/universe_validator.py --finmind # 用 FinMind 驗證疑似失效代碼
    python3 scripts/universe_validator.py --all --finmind # 用 FinMind 驗證整個候選池
"""

import argparse
import logging
import re
from pathlib import Path
from typing import Dict, List

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
SYMBOL_RE = re.compile(r"^(\^\w+|\d{4}\.(TW|TWO))$")
KNOWN_MARKET_SUFFIXES = {
    "8069": "TWO",  # 元太
    "3105": "TWO",  # 穩懋
    "6488": "TWO",  # 環球晶
    "4123": "TWO",  # 晟德
    "3529": "TWO",  # 力旺
    "5347": "TWO",  # 世界先進
    "5483": "TWO",  # 中美晶
    "4904": "TW",   # 遠傳
}


def load_universe() -> List[Dict]:
    path = _BASE_DIR / "config" / "stock_universe.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("universe", [])


def symbol_stock_id(symbol: str) -> str:
    return symbol.replace(".TWO", "").replace(".TW", "").lstrip("^")


def validate_static(universe: List[Dict]) -> List[str]:
    errors = []
    seen = set()

    for idx, item in enumerate(universe, start=1):
        symbol = str(item.get("symbol") or "").strip()
        name = str(item.get("name") or "").strip()
        if not symbol:
            errors.append(f"第 {idx} 筆缺少 symbol")
            continue
        if not SYMBOL_RE.match(symbol):
            errors.append(f"{symbol} 格式不符合 ^TWII 或 0000.TW/TWO")
        if symbol in seen:
            errors.append(f"{symbol} 重複出現在候選池")
        seen.add(symbol)

        if symbol.startswith("^"):
            continue
        stock_id, suffix = symbol.split(".", 1)
        expected = KNOWN_MARKET_SUFFIXES.get(stock_id)
        if expected and suffix != expected:
            label = f"{name} " if name else ""
            errors.append(f"{label}{symbol} 市場後綴錯配，應為 {stock_id}.{expected}")

    return errors


def validate_with_finmind(symbols: List[str]) -> Dict[str, bool]:
    try:
        from datetime import datetime, timedelta
        from FinMind.data import DataLoader

        dl = DataLoader()
    except ImportError:
        logger.error("FinMind 未安裝")
        return {}

    first_day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    results = {}

    for symbol in symbols:
        stock_id = symbol_stock_id(symbol)
        try:
            df = dl.taiwan_stock_institutional_investors(
                stock_id=stock_id,
                start_date=first_day,
            )
            if df is not None and not df.empty:
                results[symbol] = True
                logger.info(f"✅ {symbol:12} — 有效 ({len(df)} 筆法人資料)")
            else:
                results[symbol] = False
                logger.warning(f"❌ {symbol:12} — 無法人資料")
        except Exception as e:
            results[symbol] = False
            logger.warning(f"⚠️  {symbol:12} — {str(e)[:50]}")

    return results


def select_finmind_symbols(universe: List[Dict], validate_all: bool) -> List[str]:
    symbols = [str(s["symbol"]) for s in universe if not str(s.get("symbol", "")).startswith("^")]
    if validate_all:
        return symbols

    known_symbols = {f"{stock_id}.{suffix}" for stock_id, suffix in KNOWN_MARKET_SUFFIXES.items()}
    return [symbol for symbol in symbols if symbol in known_symbols]


def main() -> int:
    parser = argparse.ArgumentParser(description="驗證股票代碼有效性")
    parser.add_argument("--all", action="store_true", help="搭配 --finmind 驗證所有代碼")
    parser.add_argument("--finmind", action="store_true", help="額外呼叫 FinMind 驗證代碼有效性")
    args = parser.parse_args()

    universe = load_universe()
    errors = validate_static(universe)

    if errors:
        logger.error("靜態檢查失敗：%d 個問題", len(errors))
        for error in errors:
            logger.error("  - %s", error)
        return 1

    logger.info("靜態檢查通過：%d 檔，無格式、重複或已知後綴錯配", len(universe))

    if not args.finmind:
        return 0

    to_validate = select_finmind_symbols(universe, args.all)
    logger.info("FinMind 驗證 %d 檔代碼...", len(to_validate))
    results = validate_with_finmind(to_validate)

    valid = sum(1 for v in results.values() if v)
    invalid = sum(1 for v in results.values() if not v)
    logger.info("\n驗證完成：%d 有效，%d 無效", valid, invalid)

    if invalid > 0:
        logger.info("\n無效代碼（建議人工確認）：")
        for sym, valid_flag in results.items():
            if not valid_flag:
                logger.info("  - %s", sym)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
