# IB Gateway + IBC 無人值守接入（paper / read-only）

讓 05:10 美股 close Research Pack 穩定取得 IBKR read-only 行情。完整計畫見
`../../plan_ibkr_gateway_ibc.md`。

## 檔案
| 檔案 | 作用 | 是否含憑證 |
|---|---|---|
| `config.ini.example` | IBC 設定範本（paper / `ReadOnlyApi=yes` / port 7497） | 否（範本） |
| `start_ibkr_gateway.sh` | 包 `ibcstart.sh` 的啟動腳本 | 否 |
| `com.w.ibkr-gateway.plist` | launchd 保活範本（KeepAlive + 開機自啟） | 否 |

真實 `~/ibc/config.ini`（含憑證）放 **repo 外**，不可進 git。

## 安全紅線
- 帳號固定 **paper**（`TradingMode=paper`），零實盤下單風險。
- `ReadOnlyApi=yes` + `AllowBlindTrading=no`：Gateway 端鎖死唯讀 API。
- provider 端另有 `readonly=True`（三重保險）。
- 憑證只由你填入 `~/ibc/config.ini`，AI 不經手。

## 啟用步驟
1. 安裝 **IB Gateway**（IBKR 官網）與 **IBC**（IbcAlpha/IBC GitHub release，建議解到 `~/ibc`）。
2. `cp ops/ibkr/config.ini.example ~/ibc/config.ini`，填入 paper 帳號 `IbLoginId` / `IbPassword`。
3. 編輯 `start_ibkr_gateway.sh`：設定 `TWS_MAJOR_VRSN`（Gateway 主版本號，如 `1030`）與 `TWS_PATH`（實際安裝路徑）。
4. 手動測試：
   ```bash
   bash ops/ibkr/start_ibkr_gateway.sh
   ```
   確認 Gateway 登入、API 起在 7497。
5. read-only probe 驗證：
   ```bash
   /Users/w/.homebrew/bin/python3 scripts/probe_ibkr_readonly.py \
     --port 7497 --client-id 482 --symbol SPY --json
   ```
   應得 `ok=true` / `connected=true` / `readonly=true` + delayed 報價。
6. 載入保活：
   ```bash
   cp ops/ibkr/com.w.ibkr-gateway.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.w.ibkr-gateway.plist
   ```
   測試保活：`launchctl kill TERM gui/$(id -u)/com.w.ibkr-gateway`，確認自動拉回、7497 重新 listen。

## OS 層注意（影響 05:10 可用性）
- IB Gateway 是 GUI app，需保持在**已登入的桌面 session**（建議開機自動登入，否則停在登入畫面時無法繪製）。
- 確認 Mac 夜間不睡眠（Energy Saver / `pmset`），否則 05:10 仍可能不在線。
- `AutoRestartTime=02:00` 已避開 05:10；如需調整改 `~/ibc/config.ini`。

## 回滾
```bash
launchctl bootout gui/$(id -u)/com.w.ibkr-gateway
rm ~/Library/LaunchAgents/com.w.ibkr-gateway.plist
```
automation 端停用只需刪 `.env` 的 `RESEARCH_PACK_US_CLOSE_ENABLED=1`，與本 Gateway 設定互不影響。
