#!/bin/bash
# 全架構調度入口（走 orchestrator）

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

for key in \
  http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY socks_proxy SOCKS_PROXY \
  ANTHROPIC_MODEL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_BASE_URL \
  ANTHROPIC_REASONING_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
  ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME ANTHROPIC_DEFAULT_OPUS_MODEL_NAME ANTHROPIC_DEFAULT_SONNET_MODEL_NAME \
  ANTHROPIC_CUSTOM_MODEL_OPTION ANTHROPIC_CUSTOM_MODEL_OPTION_NAME ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION \
  CLAUDE_CODE_SUBAGENT_MODEL; do
  unset "$key" 2>/dev/null || true
done

PYTHON_BIN="${PYTHON_BIN:-/Users/w/.homebrew/bin/python3.13}"
export PYTHON_BIN

MODE="${1:-all}"
if [[ "$MODE" == "--mode" ]]; then
  MODE="${2:-all}"
fi

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/orchestrator_full_$TIMESTAMP.log"

echo "🚀 Starting full orchestrator run..."
echo "   mode: $MODE"
echo "   python: $PYTHON_BIN"
echo "   log: $LOG_FILE"

cd "$PROJECT_DIR"
"$PYTHON_BIN" orchestrator.py --mode "$MODE" 2>&1 | tee -a "$LOG_FILE"

echo "🎉 Full orchestrator run completed."
