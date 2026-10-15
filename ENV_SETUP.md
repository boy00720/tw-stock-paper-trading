# 環境變數設定指南

## 快速設定

```bash
# 1. 複製模板
cp .env.template .env

# 2. 編輯 .env 填入實際值
nano .env  # 或使用其他編輯器

# 3. 驗證設定
python3 check_env.py
```

## 必需變數

### 飛書配置
- `FEISHU_APP_ID`: 飛書應用 ID
- `FEISHU_APP_SECRET`: 飛書應用密鑰
- `FEISHU_DEFAULT_CHAT_ID`: 預設推送群組 ID
- `FEISHU_USER_OPEN_ID`: 用戶 Open ID

**取得方式**：
1. 前往 [飛書開放平台](https://open.feishu.cn/)
2. 創建應用並取得 App ID 和 Secret
3. 在群組中添加機器人，取得 Chat ID

### 微信配置
- `WECHAT_PUSH_TO_USER`: 微信推送目標用戶 ID

**取得方式**：
1. 啟動 `wechat_bridge.mjs`
2. 在微信中向機器人發送訊息
3. 查看日誌中的 `userId`

### Claude API
- `ANTHROPIC_API_KEY`: Claude API 金鑰

**取得方式**：
1. 前往 [Anthropic Console](https://console.anthropic.com/)
2. 創建 API Key

### 資料源
- `FINMIND_TOKEN`: FinMind 資料源 Token

**取得方式**：
1. 前往 [FinMind](https://finmindtrade.com/)
2. 註冊並取得 API Token

## 可選變數

- `WECHAT_PUSH_CONTEXT_TOKEN`: 微信上下文 Token（用於主動推送）
- `WECHAT_BRIDGE_ENABLE_POLLING`: 啟用輪詢機制（預設 0）
- `WECHAT_CONTEXT_MAX_AGE_HOURS`: 上下文最大有效時間（預設 8 小時）

## 驗證

執行檢查工具：
```bash
python3 check_env.py
```

應該看到所有必需變數都標記為 ✓。

## 安全提醒

- **不要**將 `.env` 檔案提交到 Git
- `.env` 已在 `.gitignore` 中
- 定期更換 API 金鑰
- 不要在日誌中輸出完整的 Token
