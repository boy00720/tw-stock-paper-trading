#!/bin/bash
# 單一 Agent 執行腳本，供 crontab 呼叫

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

AGENT=$1
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
LOG_FILE="$LOG_DIR/${AGENT}_${TIMESTAMP}.log"

echo "[$(date)] Starting $AGENT..." >> "$LOG_FILE"
python3 "$SCRIPT_DIR/scripts/${AGENT}.py" >> "$LOG_FILE" 2>&1
EXIT=$?

if [ $EXIT -eq 0 ]; then
    echo "[$(date)] $AGENT completed successfully." >> "$LOG_FILE"
else
    echo "[$(date)] $AGENT failed with exit code $EXIT." >> "$LOG_FILE"
fi

exit $EXIT