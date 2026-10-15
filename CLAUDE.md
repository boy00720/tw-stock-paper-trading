# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 執行前必讀（強制）

- 所有任務在開始任何分析、修改、測試或執行命令前，必須先閱讀本檔 `CLAUDE.md`。
- 若 `automation/` 與其他子專案規則衝突，先依目標目錄對應規則檔處理，再執行操作。

## 工作目錄（固定）

- 所有命令都先在 `automation/` 執行：
  - `cd /Users/w/Cursor/automation`
- 原因：本專案大量使用相對路徑（`config/`、`logs/`、`.env`）。


## 規則層級

- 全域規則：`/Users/w/CLAUDE.md`（語言、核心原則）
- 本檔：automation 專案執行規則與架構
- 任務歸檔：`/Users/w/AUTOMATION_ARCHIVE.md`

## 任務結束歸檔（強制）

- 適用範圍：每次完成 bug 修復、報錯處理、效能/流程優化、新增功能後，必須歸檔。
- 強制要求：任務結束時，需將可重用結論寫入 `/Users/w/AUTOMATION_ARCHIVE.md`；未歸檔視為未完成。
- 收錄原則：只寫可重用決策與驗證結論，不貼長篇 log、不寫流水帳。
- 驗證必填：至少包含 1 條可重現的驗證指令或日誌關鍵字。
- 長度治理：保留近期高價值條目；舊條目定期彙整到 `docs/CHANGELOG.md`（若該檔存在）。

## 輸出上限保護（強制）

- 預設採短輸出模式，避免接近 32k 輸出上限：先給結論、Top-N 重點、關鍵證據、文件路徑與驗證結果。
- 排障與歸檔時不得一次性貼完整 `logs/*.log`、完整 diff、大型 JSON/CSV、全文檔案、多 agent 原文彙總或逐行長篇分析；除非使用者明確要求全文。
- 大範圍審查、研究、排障、報告或多 agent 彙總時，子任務只回「結論 / 證據 / 風險 / 路徑」，主回覆只做去重摘要；完整內容優先落盤並回報路徑，或按使用者指定分段展開。

## 混合版：快速開發 + 運維重點


### 1) 快速開發命令（最常用）

```bash
cd /Users/w/Cursor/automation

# frozen baseline 一鍵刷新（先重算 intraday snapshot，再刷新 watchlist snapshot）
python3 scripts/refresh_frozen_intraday_baseline.py

# 依賴
pip3 install -r requirements.txt
npm install

# 主流程（優先）
python3 orchestrator.py --mode morning
python3 orchestrator.py --mode afternoon
python3 orchestrator.py --mode evening
python3 orchestrator.py --mode all
```

### 2) 單測 / 快速驗證

```bash
cd /Users/w/Cursor/automation

python3 -m pytest tests/test_data_validator.py
python3 -m pytest tests/test_data_validator.py::TestValidateMarketDataCompleteness::test_complete_data
python3 test_finmind.py
python3 test_print.py
```

### 3) 本地服務啟停（運維）

```bash
cd /Users/w/Cursor/automation

bash start_wechat.sh
bash start_feishu_bot.sh
bash wechat_health_check.sh
python3 api_server.py
```

### 4) 模型路由同步

```bash
cd /Users/w/Cursor/automation

./sync_cursor_model.sh
./sync_cursor_model.sh claude-sonnet-4-6
```

## 核心架構（Big Picture）

### A. `orchestrator.py` 是單一調度入口

- 晨間/盤後/晚間/每週流程由 `orchestrator.py --mode ...` 編排。
- 若需求是「跑完整流程」，優先走 orchestrator，不要手動拼多支腳本。

### B. WeChat 鏈路（三段式）

- `wechat_bridge.mjs`：對接 WeChat（`/send`、`/repair`、`/health`）
- `api_server.py`：控制面與 `/chat` 路由
- `orchestrator.py`：實際任務執行與通知

資料流：WeChat → `wechat_bridge.mjs` → `api_server.py` → `orchestrator.py` → 回覆。

#### WeChat Bridge 常駐設定（重要）

- 若正式環境由 launchd 管理 `com.w.wechat-bridge-3001`，`WECHAT_BRIDGE_ENABLE_POLLING` 應維持為 `1`。
- 原因：晨間這類「主動推播」流程若只依賴舊 `context_token` cache，隔夜後容易出現 `stale context_token` / `missing valid context_token`，表面上看起來像排程沒觸發，實際上是 bridge 無法送達。
- 排障時先分辨兩件事：
  1. `orchestrator.py --mode morning` 是否實際有跑
  2. `http://127.0.0.1:3001/health` 是否 `ready_for_push=true` 且 `polling_enabled=true`

### WeChat 聊天送達閉環（強制）

- 目標：確認「聊天回覆是否真的送到微信」，不可只看 API 成功。
- 每次排障必做三段驗證：
  1. `logs/wechat_bridge.log`：有 `[收到]`（橋接收到用戶訊息）
  2. `logs/api_server.log`：有 `chat_completed`（API/LLM 流程完成）
  3. `logs/wechat_delivery.jsonl`：有 `source=chat_reply` 且 `status=success|error`（最終送達落地）
- 判定規則：
  - 三段都存在且 delivery 為 `success`：判定「已送達」。
  - 前兩段存在、第三段 `error`：判定「發送失敗（以 delivery 錯誤欄位定位）」。
  - 前兩段存在、第三段缺失：判定「審計異常」，需先修復 bridge 審計或進程版本。

### C. Feishu / TG 鏈路（長連線）

- `scripts/feishu_bot.py` 為長連線 bot（非 HTTP callback server）。
- `/code ...` 會轉發至 `api_server.py` 的同一套受控編程流程。
- 若啟用 TG bot，視同長連線通道管理；排障優先看 TG bot 自身 log，而非假設 HTTP webhook 健康即可代表可送達。

### Feishu / TG 聊天送達閉環（強制）

- 目標：確認「聊天回覆是否真的送到飛書 / TG」，不可只看 API 成功。
- 每次排障必做三段驗證（兩平台同規則）：
  1. 通道接收日誌：
     - Feishu：`logs/feishu_bot.log` 有 `收到訊息` / message receive 記錄
     - TG：TG bot log 有收到訊息記錄（檔名依實際部署）
  2. `logs/api_server.log`：有 `chat_completed`（API/LLM 流程完成）
  3. 通道送達結果：
     - Feishu：`logs/feishu_bot.log` 不得出現 `飛書發送失敗` / `發送訊息例外`
     - TG：TG bot delivery/audit log 有最終 `success|error`（若尚未落地，先補審計）
- 判定規則：
  - 三段都成立且最終為成功：判定「已送達」。
  - 前兩段成立、第三段為 error：判定「發送失敗（以通道錯誤欄位定位）」。
  - 前兩段成立、第三段缺失：判定「審計異常」，需先補齊送達審計。


### D. 控制面集中在 `api_server.py`

- `/chat`：先攔截 `code/service/workflow`，再決定是否走 LLM。
- `/service/*`：服務狀態與重啟控制。
- `/code *`：受控編程 FSM（create/approve/run/status/cancel）。

## 雙平台互修矩陣（運維關鍵）

### Service 控制

- 允許來源：`wechat-bridge`、`feishu-bot`
- 防自修：
  - 來源 `wechat-bridge`：禁止重啟 `wechat-bridge`/`wechat-api`
  - 來源 `feishu-bot`：禁止重啟 `feishu-bot`

### Code 控制

- 可用 target：`feishu`、`wechat-bridge`、`wechat-api`
- 來源可操作 target：
  - `wechat-bridge` → `feishu`
  - `feishu-bot` → `wechat-bridge`、`wechat-api`

## 排障先看這些檔案

- `logs/service_control_audit.jsonl`：service 控制審計
- `logs/code_control_audit.jsonl`：code 控制審計
- `logs/code_control_tasks.json`：code 任務狀態
- `logs/workflow_control_audit.jsonl`：workflow 控制審計
- `logs/api_server.log`、`logs/wechat_bridge.log`、`logs/feishu_bot.log`：通道日誌
- `logs/orchestrator.log`：調度主日誌

