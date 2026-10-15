#!/bin/bash
# 同步當前可用的直連模型設定到 automation/.env，並乾淨重啟 wechat-api

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
CURSOR_PROJECT_DIR="${CLAUDE_CURSOR_PROJECT_DIR:-$HOME/.claude/projects/-Users-w-Cursor}"
MANUAL_MODEL="${1:-}"
MANUAL_BASE_URL="${2:-}"
MANUAL_AUTH_TOKEN="${3:-}"

if [ -n "$MANUAL_BASE_URL" ] || [ -n "$MANUAL_AUTH_TOKEN" ]; then
  echo "⚠️ sync_cursor_model.sh 已停止同步路由與金鑰，只會更新模型。忽略額外的 base_url/token 參數。"
fi

detect_model() {
  if [ -n "$MANUAL_MODEL" ]; then
    echo "$MANUAL_MODEL"
    return 0
  fi

  CURSOR_PROJECT_DIR="$CURSOR_PROJECT_DIR" python3 - <<'PY'
import glob
import json
import os

project_dir = os.path.expanduser(os.environ.get("CURSOR_PROJECT_DIR", "~/.claude/projects/-Users-w-Cursor"))

model = None
files = sorted(
    glob.glob(os.path.join(project_dir, "*.jsonl")),
    key=lambda p: os.path.getmtime(p),
    reverse=True,
)

for path in files:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        continue

    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message") or {}
        m = msg.get("model")
        if m and m != "<synthetic>":
            model = m
            break
    if model:
        break

if not model:
    settings_path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        env = settings.get("env", {})
        model = env.get("ANTHROPIC_MODEL") or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
    except Exception:
        model = None

print(model or "sonnet")
PY
}

upsert_env_var() {
  local key="$1"
  local value="$2"

  [ -z "$value" ] && return 0

  delete_env_var "$key"
  printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
}

delete_env_var() {
  local key="$1"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i '' -E "/^${key}=.*$/d" "$ENV_FILE"
  fi
}

MODEL="$(detect_model)"

if [ -z "$MODEL" ]; then
  echo "❌ 無法判斷模型"
  exit 1
fi

echo "🎯 目標模型: $MODEL"
echo "🔒 路由與金鑰維持既有 official/cc-switch 邊界，不再由此腳本鏡像寫入 .env"

# 更新 .env（新增或覆寫）
if [ ! -f "$ENV_FILE" ]; then
  touch "$ENV_FILE"
fi

upsert_env_var "ANTHROPIC_MODEL" "$MODEL"
delete_env_var "ANTHROPIC_BASE_URL"
delete_env_var "ANTHROPIC_AUTH_TOKEN"
delete_env_var "ANTHROPIC_FALLBACK_MODEL"
delete_env_var "ANTHROPIC_FALLBACK_MODELS"

# 清空會污染模型路由的環境變數，避免 pm2 繼承舊值
UNSET_ENV_VARS=(
  "ANTHROPIC_MODEL"
  "ANTHROPIC_AUTH_TOKEN"
  "ANTHROPIC_BASE_URL"
  "ANTHROPIC_REASONING_MODEL"
  "ANTHROPIC_DEFAULT_HAIKU_MODEL"
  "ANTHROPIC_DEFAULT_OPUS_MODEL"
  "ANTHROPIC_DEFAULT_SONNET_MODEL"
  "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"
  "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"
  "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"
  "ANTHROPIC_CUSTOM_MODEL_OPTION"
  "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"
  "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"
  "CLAUDE_CODE_SUBAGENT_MODEL"
)

ENV_CMD=(env)
for key in "${UNSET_ENV_VARS[@]}"; do
  ENV_CMD+=("-u" "$key")
done

if pm2 describe wechat-api >/dev/null 2>&1; then
  "${ENV_CMD[@]}" pm2 delete wechat-api >/dev/null 2>&1 || true
fi

(
  cd "$SCRIPT_DIR"
  "${ENV_CMD[@]}" pm2 start api_server.py --name wechat-api --interpreter /usr/bin/python3 >/dev/null
)

APP_ID="$(pm2 id wechat-api | tr -cd '0-9\n' | head -n 1)"

echo "✅ 已同步到 $ENV_FILE"
python3 - "$ENV_FILE" <<'PY'
import pathlib
import sys

env_path = pathlib.Path(sys.argv[1])
for index, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
    if line.startswith("ANTHROPIC_MODEL="):
        print(f"{index}:{line}")
PY

if [ -n "$APP_ID" ]; then
  echo "🔎 wechat-api (id=$APP_ID) 目前環境:"
  ENV_SNAPSHOT="$(pm2 env "$APP_ID" | python3 -c 'import sys
for idx, line in enumerate(sys.stdin, start=1):
    if any(token in line for token in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_REASONING_MODEL",
        "ANTHROPIC_DEFAULT_",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    )):
        print(f"{idx}:{line.rstrip()}")')"
  if [ -n "$ENV_SNAPSHOT" ]; then
    echo "$ENV_SNAPSHOT"
  else
    echo "（無固定 ANTHROPIC* 進程環境，將由 .env 動態讀取）"
  fi
fi

echo "🏁 完成"
