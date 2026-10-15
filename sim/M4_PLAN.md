# M4 計畫：自然語言查詢（接 ClaudeHelper）

> 預估：1 個工作日
> 目標：在飛書 bot 上支援「我這個月勝率多少」、「2330 持倉成本是多少」這類自然語言查詢
> 完成標準：M3 既有 13 條結構化指令路徑零破壞、NL 查詢命中關鍵測試案例 + 端到端 smoke 通過、不誤觸普通閒聊

---

## 設計決策

> 規劃日期：2026-05-05；於 W 確認 80% 以上後開工。

### 決策 1：觸發條件 — 白名單關鍵字 + 明示前綴雙軌

REQUIREMENTS §8 原文「bot 收到非 `/` 開頭訊息 → 帶 snapshot → ClaudeHelper」太寬，會把日常閒聊（「今天累」、「3030 這檔最近怎樣」屬於選股提問而非 sim 查詢）吞進 sim 路由。

**雙軌規則**：

1. **明示前綴**（一定走 sim NL）：訊息以 `@sim` 起頭
2. **隱式關鍵字**（命中即走 sim NL）：訊息含 sim-domain 關鍵字白名單，且通常會搭配「我的 / 我這 / 我目前」之類的第一人稱所有格
3. **都不命中** → 沿用 M3 既有路徑（最終落到 LLM 普通聊天）

**關鍵字白名單草案**（可在 PR 階段加減）：

```
我的持倉 / 我這個月 / 我這週 / 我這禮拜 / 我目前 / 我累計
勝率 / 未實現損益 / 已實現損益 / 平倉記錄 / 成交記錄 / 委託記錄
最大虧損 / 最大盈利 / 持倉成本 / 平均成本 / 損益排行
```

**不放白名單的詞**（避免誤觸）：

- 「虧損」、「盈利」、「成交」單獨出現（會踩到新聞 / 閒聊）
- 「2330」、「0050」純股票代碼（M3 結構化 `/quote` 已覆蓋；NL 一定要伴隨第一人稱動詞）

**理由**：純白名單誤觸高、純前綴喪失自然感、雙軌折衷。`@sim` 給「我知道我在問 sim」的場景一個明確路由，關鍵字捕捉常見問法。

### 決策 2：snapshot 範圍 — 「快照 + 最近 N 筆」薄版

完整 history 撐爆 prompt（一個月 fills 可能上千筆），tool calling 架構是正解但 1 天做不完，留 M5+。M4 先上薄版覆蓋 80% 場景。

**Snapshot 內容**（序列化為 JSON 餵 LLM）：

| 區塊 | 來源 | 內容 |
|---|---|---|
| `account` | `engine.snapshot()` | cash / equity |
| `positions` | `engine.account.positions` | symbol / qty / avg_cost / unrealized_pnl（即時報價來自 feed） |
| `recent_fills` | `logs/sim/fills.jsonl` tail 100 | 最近 100 筆成交（含 time、symbol、side、price、qty、fee、tax、realized_pnl） |
| `pending_orders` | `engine.account.orders` filter status=pending | 最近 50 筆未成交委託 |
| `lifetime_stats` | 計算 | total_realized_fills / win_count / win_rate（簡化：盈利筆數 / 已實現筆數） |

**理由**：薄版能回答 4 個 REQUIREMENTS §8 範例查詢中的 4 個。月內成交超過 100 筆時會丟最舊的，先記為 M5 風險點。

### 決策 3：回覆風格 — system prompt 約束 + LLM 自由生成

不走模板填空（失去 NL 彈性），不走完全自由（風格會跟 M3 `/portfolio` 既有輸出脫節）。

**System prompt 約束**（草案，PR 時定稿）：

```
你是 W 的台股模擬盤助理。
- 回覆對象是 W 本人，不需要客套或免責聲明
- 台股口吻，數字與單位寫清楚（股 / 元 / NTD / %）
- 簡潔，最多 4 行，必有數字
- 不重述使用者問題、不堆砌欄位
- 風格對齊既有 /portfolio /pnl 的輸出（簡潔列點 + 必要數字）
- 找不到答案就直說「snapshot 沒有這個資訊」，不要編造
```

**理由**：M3 `_fmt_*` 已建立視覺風格基線，M4 NL 回覆要能跟它「對話起來像同一個 bot」。

---

## 任務拆解

### 區塊 A：路由 dispatcher

- [ ] 在 `automation/sim/commands.py` 或新檔 `automation/sim/nl_query.py` 加 `is_sim_nl_query(text: str) -> bool`：
  - 以 `@sim` 開頭 → True
  - 否則命中關鍵字白名單任一 → True
  - 否則 False
- [ ] 改 `scripts/feishu_bot.py` `process_message()`：
  - 既有 `is_sim_prefix` → sim 結構化（M3，零變動）
  - 新增 `is_sim_nl_query` → sim NL（M4 新路徑）
  - 都不命中 → 沿用既有 LLM 路徑（不變）

### 區塊 B：snapshot builder

- [ ] 新建 `automation/sim/nl_query.py`（路由與 snapshot 同檔）：
  - `def build_snapshot(engine, fills_path, orders_path, feed=None, n_fills=100, n_orders=50) -> dict`
  - 內含 `_tail_jsonl(path, n)`、`_compute_lifetime_stats(fills)` 兩個內部 helper
  - 序列化用 `json.dumps(default=str)` 處理 Enum / Decimal

### 區塊 C：LLM 呼叫 + system prompt

- [ ] 確認 `scripts/feishu_bot.py` 內 `call_claude()` 簽名是否支援 system prompt 參數（**實作前 1 分鐘 grep 確認**，若不支援則新增包裝）
- [ ] 新增 `def answer_nl_query(text, snapshot) -> str`：
  - 構造 messages：system prompt + `f"{text}\n\n## 模擬盤狀態（JSON）\n{snapshot}"`
  - 重用既有 LLM 呼叫鏈，不新增 anthropic SDK 直連
  - 包 try/except，LLM 失敗時回「NL 查詢暫不可用，請改用 /portfolio /pnl 等指令」
- [ ] 在 `_audit_sim` 同套機制下，把 NL 查詢落 `logs/sim/audit.jsonl`，加 source=nl 欄位

### 區塊 D：測試

- [ ] 新建 `automation/sim/tests/test_nl_query.py`：
  - `test_is_sim_nl_query_at_prefix`：`@sim 隨便` → True
  - `test_is_sim_nl_query_keyword_hit`：「我這個月勝率多少」 → True
  - `test_is_sim_nl_query_pure_stock_no_match`：「2330」 → False（純代碼，避開選股場景）
  - `test_is_sim_nl_query_chitchat`：「今天天氣很好」 → False
  - `test_build_snapshot_recent_fills_tail`：寫 150 筆 fills → snapshot 應只取後 100 筆
  - `test_build_snapshot_win_rate`：3 筆已實現中 2 筆盈利 → win_rate=2/3
  - `test_answer_nl_query_with_mock_llm`：mock LLM 回固定字串，驗證 messages 結構與 snapshot 注入

---

## 驗收條件

1. `cd /Users/w/Cursor/automation && python3 -m pytest sim/tests/` → 95 + 7 = **102/102 通過**（無回歸）
2. `python3 -m py_compile sim/nl_query.py scripts/feishu_bot.py` 全通過
3. 飛書端到端 smoke（人工，重啟 bot 後）：
   - `@sim 我這個月勝率多少` → 回覆含數字 + 百分比
   - `我目前最大未實現虧損是哪一檔` → 回覆含 symbol + 金額
   - `今天天氣很好` → 走原 LLM 路徑（不誤觸）
   - `/portfolio` → M3 結構化路徑（不破壞）
4. `logs/sim/audit.jsonl` 新增 source=nl 條目

---

## 影響檔案清單

| 檔案 | 動作 |
|---|---|
| `automation/sim/nl_query.py` | **新建**（路由判斷 + snapshot builder + LLM 呼叫包裝） |
| `automation/sim/tests/test_nl_query.py` | **新建**（≥7 項測試） |
| `automation/sim/__init__.py` | 改（export `is_sim_nl_query`、`build_snapshot`、`answer_nl_query`） |
| `automation/sim/commands.py` | **不動**（路由邏輯放 nl_query.py，不污染 M3 既有檔） |
| `automation/sim/engine.py` | **不動** |
| `scripts/feishu_bot.py` | 改（`process_message` 加 NL 路由分支 + audit source=nl） |
| `automation/sim/M4_PLAN.md` | **本檔（新建）** |

---

## 風險與不確定點

1. **`call_claude()` 簽名未確認**：實作前先 grep `def call_claude` 在 `scripts/feishu_bot.py` 的具體簽名；若不支援 system prompt，需新增包裝或調用 `api_server.py` 的 `/chat` 路徑
2. **snapshot prompt size**：100 筆 fills + 50 筆 orders 估計 5–15KB，遠低於 cache 上限；但要實測，避免過大時的延遲
3. **關鍵字白名單覆蓋不全**：開放後第一週 audit log 必須撈一輪「使用者問了但被當閒聊吞掉」的案例，迭代擴充
4. **fills.jsonl tail 效能**：M4 簡單一次讀完取後 100 筆；fills 若超過 1 萬筆要改 reverse iteration 或加 index，列為 M5 風險
5. **誤觸 LLM 成本**：M3 既有 audit 已涵蓋，M4 只多 source=nl 標記；無新成本面風險
6. **prompt 注入**：使用者輸入直接拼進 user message，目前不過濾；sim 場景單一使用者 + 不執行回傳指令，可接受
7. **B 防護依賴**：M4 實作時必須在新 Claude session（讓 `automation/.claude/settings.json` 的 deny 規則生效，避免 plan mode 撞 32MB）

---

## 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0 草稿 | 2026-05-05 | 三決策 + 任務拆解 + 驗收 + 風險（W 確認後進實作） |
