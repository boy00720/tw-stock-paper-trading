#!/bin/bash
# 啟動飛書機器人伺服器
# 注意：正式環境以 launchd 常駐為主，避免與手動背景進程重複啟動。

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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

PYTHON_BIN="${PYTHON_BIN:-/Users/w/.homebrew/bin/python3.13}"
export PYTHON_BIN

resolve_ca_file() {
  for candidate in \
    "/Users/w/.homebrew/etc/ca-certificates/cert.pem" \
    "/Users/w/.homebrew/etc/openssl@3/cert.pem"; do
    if [ -f "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

resolve_ca_dir() {
  for candidate in \
    "/Users/w/.homebrew/etc/openssl@3/certs" \
    "/Users/w/.homebrew/etc/ca-certificates/certs"; do
    if [ -d "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

SSL_CERT_FILE_VALUE="$(resolve_ca_file || true)"
SSL_CERT_DIR_VALUE="$(resolve_ca_dir || true)"
if [ -n "$SSL_CERT_FILE_VALUE" ]; then
  export SSL_CERT_FILE="$SSL_CERT_FILE_VALUE"
  export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE_VALUE"
  export CURL_CA_BUNDLE="$SSL_CERT_FILE_VALUE"
fi
if [ -n "$SSL_CERT_DIR_VALUE" ]; then
  export SSL_CERT_DIR="$SSL_CERT_DIR_VALUE"
fi

FEISHU_LABEL="com.w.feishu-bot"
LAUNCHD_DOMAIN="gui/$(id -u)"

launchd_loaded() {
  local label="$1"
  launchctl list | grep -q "$label"
}

feishu_pid() {
  pgrep -f "/Users/w/Cursor/automation/scripts/feishu_bot.py" | head -n 1
}

echo "🚀 檢查飛書機器人服務..."
echo

if launchd_loaded "$FEISHU_LABEL"; then
  echo "ℹ️  偵測到 launchd 已管理飛書機器人，避免重複手動啟動。"
  echo "   • Label:  $FEISHU_LABEL"
  echo "   • PID:    $(feishu_pid || true)"
  echo "   • Python: $PYTHON_BIN"
  echo "   • CA:     ${SSL_CERT_FILE:-<unset>}"
  echo
  echo "   若要重啟，請使用："
  echo "   • launchctl kickstart -k $LAUNCHD_DOMAIN/$FEISHU_LABEL"
  echo
  echo "   查看 log："
  echo "   • tail -f $LOG_DIR/feishu_bot.log"
  echo "   • tail -f $LOG_DIR/feishu_bot_error.log"
  exit 0
fi

echo "[$(date)] 啟動 feishu_bot.py..."
echo "[$(date)] Python: $PYTHON_BIN"
echo "[$(date)] SSL_CERT_FILE: ${SSL_CERT_FILE:-<unset>}"
cd "$SCRIPT_DIR"
"$PYTHON_BIN" scripts/feishu_bot.py >> "$LOG_DIR/feishu_bot.log" 2>> "$LOG_DIR/feishu_bot_error.log" &
BOT_PID=$!
echo $BOT_PID > "$LOG_DIR/feishu_bot.pid"

echo "[$(date)] Bot 已啟動 (PID: $BOT_PID)"
echo "[$(date)] 運作模式：Feishu WebSocket 長連線，無 HTTP health endpoint"
echo "[$(date)] 查看狀態請用：tail -f $LOG_DIR/feishu_bot.log"
