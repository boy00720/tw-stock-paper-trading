#!/usr/bin/env bash
# ============================================================
# IB Gateway + IBC 啟動腳本（paper / read-only）
# 由 launchd com.w.ibkr-gateway 呼叫，或手動測試執行。
# 前置：
#   1) 已安裝 IB Gateway 與 IBC
#   2) 已 cp ops/ibkr/config.ini.example -> ~/ibc/config.ini 並填好 paper 憑證
#   3) 下方 TWS_MAJOR_VRSN 等路徑已依實際安裝調整
# 憑證從 config.ini 讀，不走命令列參數，避免出現在 `ps`。
# ============================================================
set -euo pipefail

# ---- 路徑設定（安裝後依實際情況調整；也可用環境變數覆寫）----
# IBC 安裝目錄（含 ibcstart.sh / scripts / IBC.jar）
IBC_PATH="${IBC_PATH:-$HOME/ibc}"
# 真實 config.ini（含憑證，放 repo 外）
IBC_INI="${IBC_INI:-$HOME/ibc/config.ini}"
# IB Gateway 安裝路徑（依實際安裝位置調整）
TWS_PATH="${TWS_PATH:-$HOME/Applications/IB Gateway}"
# Gateway 設定目錄
TWS_SETTINGS_PATH="${TWS_SETTINGS_PATH:-$HOME/Jts}"
# IB Gateway 主版本號（安裝後填，如 1030）——必填
TWS_MAJOR_VRSN="${TWS_MAJOR_VRSN:-PUT_GATEWAY_MAJOR_VERSION_HERE}"
# 若系統無 Java 且 IBC 找不到 Gateway 內建 JRE，取消下行註解並指向 JRE
# export JAVA_PATH="/path/to/jre/bin"

LOG_DIR="${LOG_DIR:-$HOME/Cursor/automation/logs}"
mkdir -p "$LOG_DIR"

# ---- 前置檢查（任一不過直接退出，避免 launchd 空轉重啟）----
fail() { echo "[start_ibkr_gateway] ERROR: $*" >&2; exit 1; }
[ -x "$IBC_PATH/ibcstart.sh" ] || fail "找不到 ibcstart.sh: $IBC_PATH/ibcstart.sh（先安裝 IBC 或設 IBC_PATH）"
[ -f "$IBC_INI" ]              || fail "找不到 config.ini: $IBC_INI（cp config.ini.example 並填 paper 憑證）"
[ -d "$TWS_PATH" ]             || fail "找不到 IB Gateway: $TWS_PATH（先安裝或設 TWS_PATH）"
[ "$TWS_MAJOR_VRSN" != "PUT_GATEWAY_MAJOR_VERSION_HERE" ] || fail "請設定 TWS_MAJOR_VRSN（IB Gateway 主版本號）"

# ---- 啟動 ----
exec "$IBC_PATH/ibcstart.sh" "$TWS_MAJOR_VRSN" \
  --gateway \
  --mode=paper \
  --ibc-path="$IBC_PATH" \
  --ibc-ini="$IBC_INI" \
  --tws-path="$TWS_PATH" \
  --tws-settings-path="$TWS_SETTINGS_PATH"
