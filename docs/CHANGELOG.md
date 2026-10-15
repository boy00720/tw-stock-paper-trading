# CHANGELOG

automation 專案歷史任務歸檔。新條目先寫入 `CLAUDE.md` 的「任務歸檔」區，隔日或更晚再彙整到此處。

---

## 2026-04-18

### bugfix：修復 WeChat `missing valid context_token` 反覆報錯

- 目的：改為安全排隊與自動補發。
- 關鍵改動：
  - `wechat_bridge.mjs`: `/send` 在無有效 context 時不再直接拋錯，改寫入 repair queue 並回傳 `status=queued`。
  - `wechat_bridge.mjs`: 輪詢收到新訊息刷新 token 後，自動呼叫 `replayQueuedDeliveries` 補發待送訊息。
  - `wechat_bridge.mjs`: 新增 `queued_waiting_context` 審計事件與自動補發成功日誌。
- 驗證結果：
  - 指令：`node --check wechat_bridge.mjs && launchctl kickstart -k gui/$(id -u)/com.w.wechat-bridge-3001 && curl -s http://127.0.0.1:3001/health`
  - 指令：`curl -s -X POST "http://127.0.0.1:3001/send" -H "Content-Type: application/json" -d '{"to_user":"test-user-no-context@im.wechat","text":"queue-test"}'`
  - 日誌關鍵字：`queued_waiting_context`、`♻️ 已自動補發`
- 影響檔案：`wechat_bridge.mjs`、`CLAUDE.md`

### bugfix：修復微信聊天一條訊息回兩條

- 目的：避免自動補發與當前 chat reply 疊加。
- 關鍵改動：
  - `wechat_bridge.mjs`: 新增 `isRepairTriggerText`，只在 `ping`、`repair`、`補發` 等明確修復指令時才觸發自動補發。
  - `wechat_bridge.mjs`: 一般聊天訊息不再於 polling 入口先執行 `replayQueuedDeliveries`，避免舊佇列訊息與當前 AI 回覆同時送出。
- 驗證結果：
  - 指令：`node --check wechat_bridge.mjs && launchctl kickstart -k gui/$(id -u)/com.w.wechat-bridge-3001 && curl -s http://127.0.0.1:3001/health`
  - 日誌關鍵字：`HTTP Bridge 已啟動`、`polling_enabled":true`、`pending_failed_count":0`
- 影響檔案：`wechat_bridge.mjs`、`CLAUDE.md`

### bugfix：防止同一條微信訊息被輪詢重複接收

- 目的：避免回覆兩次。
- 關鍵改動：
  - `wechat_bridge.mjs`: 新增 `buildInboundFingerprint` 與 `isDuplicateInbound`，以訊息 client_msg_id 或 user+text 為指紋，在 8 秒內重複即忽略。
  - `wechat_bridge.mjs`: 輪詢入口 `for (msg of updates.msgs)` 中，於 `[收到]` 前先去重，跳過時印 `[去重]` 日誌。
- 驗證結果：
  - 指令：`node --check wechat_bridge.mjs && launchctl kickstart -k gui/$(id -u)/com.w.wechat-bridge-3001 && curl -s http://127.0.0.1:3001/health`
  - 日誌關鍵字：`[去重] 已忽略重複訊息`
- 影響檔案：`wechat_bridge.mjs`、`CLAUDE.md`

### bugfix：修復 `/deep-query` 中文股票名稱與數字代號無法解析

- 目的：解決重複報錯。
- 關鍵改動：
  - `api_server.py`: `_STOCK_NAME_MAP` 新增「兆赫」→ 6226、「臻鼎」→ 4958，解決中文名稱查詢找不到代號的問題。
  - `api_server.py`: `_resolve_stock_symbol` 正則從 `\b(\d{4})\b` 改為 `(?<!\d)(\d{4})(?!\d)`，解決數字+中文組合（如「4958走势」）無法匹配的問題。
- 驗證結果：
  - 指令：`curl -s -X POST "http://127.0.0.1:3003" -H "Content-Type: application/json" -d '{"command":"/deep-query","query":"兆赫这只股票分析走势","source":"wechat-bridge","from_user":"test"}'`
  - 指令：`curl -s -X POST "http://127.0.0.1:3003" -H "Content-Type: application/json" -d '{"command":"/deep-query","query":"4958走势","source":"wechat-bridge","from_user":"test"}'`
  - 回覆關鍵字：`6226 深度分析`、`4958 深度分析`
- 影響檔案：`api_server.py`、`CLAUDE.md`
