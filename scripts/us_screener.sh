#!/bin/bash
# IBKR 美股選股 CLI 便利包裝：封裝 flag / 絕對 python / cd / --dry-run，
# 避免長命令貼上終端時斷行（python3 與腳本名被拆開會誤進交互模式）。
#
# 用法：
#   scripts/us_screener.sh                      # 預設 intraday 清單
#   scripts/us_screener.sh --scene premarket    # 盤前/隔夜
#   scripts/us_screener.sh --scene postclose    # 盤後復盤
#
# read-only / delayed；清單屬觀察參考，非即時行情、非交易指令。
cd /Users/w/Cursor/automation || exit 1
exec env US_STOCK_SCREENER_ENABLED=1 \
  /Users/w/.homebrew/bin/python3 scripts/us_stock_screener.py --dry-run "$@"
