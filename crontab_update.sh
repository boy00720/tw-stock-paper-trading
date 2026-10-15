#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/Users/w/.homebrew/bin/python3.13}"
TEMP_CRON="$SCRIPT_DIR/temp_cron"

# 取得目前 crontab
crontab -l > "$TEMP_CRON"

# 先移除舊的晨間/盤後設定以避免重複
sed -i '' '/stock_monitor.py --mode morning/d' "$TEMP_CRON"
sed -i '' '/stock_monitor.py --mode afternoon/d' "$TEMP_CRON"
sed -i '' '/orchestrator.py --mode morning/d' "$TEMP_CRON"
sed -i '' '/orchestrator.py --mode afternoon/d' "$TEMP_CRON"
sed -i '' '/run_automation.sh --mode morning/d' "$TEMP_CRON"
sed -i '' '/run_automation.sh --mode afternoon/d' "$TEMP_CRON"

# 加入新的雙模式排程 (週一到週五，統一走 orchestrator)
# 晨間盤前 08:45
echo "45 8 * * 1-5 cd $SCRIPT_DIR && PYTHON_BIN=$PYTHON_BIN ./run_automation.sh --mode morning >> logs/cron.log 2>&1" >> "$TEMP_CRON"
# 盤後籌碼 16:00
echo "0 16 * * 1-5 cd $SCRIPT_DIR && PYTHON_BIN=$PYTHON_BIN ./run_automation.sh --mode afternoon >> logs/cron.log 2>&1" >> "$TEMP_CRON"

# 安裝新 crontab 並刪除暫存
crontab "$TEMP_CRON"
rm "$TEMP_CRON"
echo "Crontab updated successfully"
echo "Using Python: $PYTHON_BIN"
