#!/bin/bash
# 啟動微信通知所需的兩個服務
# 1. Node.js wechat_bridge.mjs  — 接收 Python 推播 (port 3001)
# 2. Python api_server.py       — 處理 bridge 轉發的指令 (port 3003)
#
# 注意：
# - 正式環境以 launchd 常駐為主，不要和本腳本重複啟動。
# - 若 launchd 已管理服務，本腳本僅顯示狀態與操作提示。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
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


BRIDGE_LABEL="com.w.wechat-bridge-3001"
API_LABEL="com.w.wechat-api-3003"

port_pid() {
    local port="$1"
    lsof -ti TCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1
}

launchd_loaded() {
    local label="$1"
    launchctl list | grep -q "$label"
}

echo "🚀 檢查微信通知服務..."
echo ""

if launchd_loaded "$BRIDGE_LABEL" || launchd_loaded "$API_LABEL"; then
    echo "ℹ️  偵測到 launchd 已管理微信服務，避免重複手動啟動。"
    echo "   • Bridge label: $BRIDGE_LABEL"
    echo "   • API label:    $API_LABEL"
    echo ""
    echo "   目前埠口狀態："
    echo "   • 3001 PID: $(port_pid 3001 || true)"
    echo "   • 3003 PID: $(port_pid 3003 || true)"
    echo ""
    echo "   若要重啟，請使用："
    echo "   • launchctl kickstart -k gui/$(id -u)/$BRIDGE_LABEL"
    echo "   • launchctl kickstart -k gui/$(id -u)/$API_LABEL"
    echo ""
    echo "   查看 log："
    echo "   • tail -f $LOG_DIR/wechat_bridge.log"
    echo "   • tail -f $LOG_DIR/api_server.log"
    exit 0
fi

# 先檢查 node_modules
if [ ! -d "$SCRIPT_DIR/node_modules" ]; then
    echo "📦 安裝 npm 依賴..."
    cd "$SCRIPT_DIR" && npm install
fi

# 啟動 Node.js bridge（background，log 分開）
BRIDGE_PID=""
EXISTING_BRIDGE_PID="$(port_pid 3001 || true)"
if [ -n "$EXISTING_BRIDGE_PID" ]; then
    BRIDGE_PID="$EXISTING_BRIDGE_PID"
    echo "📡 檢測到 port 3001 已在使用中，沿用既有服務 (PID: $BRIDGE_PID)"
else
    echo "📡 啟動 wechat_bridge.mjs..."
    export WECHAT_BRIDGE_ENABLE_POLLING=1
    export WECHAT_CONTEXT_MAX_AGE_HOURS=24
    cd "$SCRIPT_DIR" && node wechat_bridge.mjs >> "$LOG_DIR/wechat_bridge.log" 2>&1 &
    BRIDGE_PID=$!
    echo "   模式: 輪詢已啟用 (WECHAT_BRIDGE_ENABLE_POLLING=1)"
    echo "   PID: $BRIDGE_PID → $LOG_DIR/wechat_bridge.log"
fi

# 啟動 Python API server（background，log 分開）
API_PID=""
EXISTING_API_PID="$(port_pid 3003 || true)"
if [ -n "$EXISTING_API_PID" ]; then
    API_PID="$EXISTING_API_PID"
    echo "🔌 檢測到 port 3003 已在使用中，沿用既有服務 (PID: $API_PID)"
else
    echo "🔌 啟動 api_server.py..."
    echo "   Python: $PYTHON_BIN"
    cd "$SCRIPT_DIR" && "$PYTHON_BIN" api_server.py >> "$LOG_DIR/api_server.log" 2>&1 &
    API_PID=$!
    echo "   PID: $API_PID → $LOG_DIR/api_server.log"
fi

echo ""
echo "✅ 兩個服務已啟動"
echo "   • Bridge:  http://127.0.0.1:3001/send"
echo "   • API:     http://127.0.0.1:3003/health"
echo "   • Polling: 已啟用（自動刷新 context_token）"
echo ""
echo "   停止：kill $BRIDGE_PID $API_PID"
echo ""
echo "   查看 log："
echo "   • tail -f $LOG_DIR/wechat_bridge.log"
echo "   • tail -f $LOG_DIR/api_server.log"
