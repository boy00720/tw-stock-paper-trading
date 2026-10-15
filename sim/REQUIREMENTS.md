# automation/sim/ 模擬盤需求 v1.0

> 設計日期：2026-05-04
> 狀態：v1.0 拍板版（基於 v0.1 草稿 + 6 拍板點 + 資料源策略 A）

---

## 1. 範圍與定位

| 維度 | 內容 |
|---|---|
| **路線** | B 完整模擬交易（非簡易記帳，非策略沙盒） |
| **標的** | 台股、ETF（v1）；期貨（v2） |
| **互動** | 飛書 bot 內，混合（基本下單結構化、複雜查詢自然語言） |
| **架構** | `automation/sim/` 子模組，重用既有 `utils/lark_notifier`、`utils/wechat_notifier`、cron、profile |
| **與 dsa 關係** | data_provider 必要邏輯抄一份至 `automation/sim/data_provider/`，**不依賴 dsa**（避免 fork upstream 衝突） |

---

## 2. 訂單類型

| 類型 | 代號 | 說明 |
|---|---|---|
| 市價單 | MKT | 即時成交在當下報價 + 滑價 |
| 限價單 | LMT | 報價達到限價或更優才成交 |
| 停損單 | STOP | 觸發價突破後轉市價 |
| 停利單 | TP | 達到目標自動賣出 |
| OCO 二擇一 | OCO | 兩張子單，一張成交另一張取消 |
| IFD 連動 | IFD | 第一張成交後第二張才生效 |

---

## 3. 撮合規則

- **撮合頻率**：每 **30 秒** 拉一次 watchlist 報價，對未成交委託逐一檢查
- **市價滑價**：預設 **0.1%**（買單成交價 = quote × 1.001，賣單 = quote × 0.999），可在 `config/sim_config.yaml` 調整
- **限價成交價**：限價或更優（買單成交價 ≤ 限價）
- **停損觸發後**：按市價單規則成交（含滑價）
- **漲跌停外**：限價單視為無效，回報失敗
- **撮合精度警告**：報價來源是 yfinance（~15 分鐘延遲），實際撮合是「對 15 分鐘前的報價判斷」。**v1 不追求即時，重點是流程驗證**

---

## 4. 手續費 / 稅金

| 標的 | 證交稅 / 期交稅 | 券商手續費 | 備註 |
|---|---|---|---|
| **台股** | 0.3%（賣方） | 0.1425%（雙邊，最低 20 元） | 標準台股費率 |
| **ETF（國內）** | 0.1%（賣方） | 0.1425%（雙邊，最低 20 元） | ETF 稅率較低 |
| **期貨（v2）** | 0.002%（雙邊） | **每口 50 元**（雙邊） | 大台 / 小台 / 電子 / 金融 統一費率 |

---

## 5. 資料源（策略 A）

| 用途 | 來源 | 頻率 | 備註 |
|---|---|---|---|
| **盤中報價**（撮合用） | `yfinance` | 每 30 秒拉一次 watchlist | ~15 分鐘延遲，免費 |
| **EOD 日結算 / 技術分析** | FinMind 免費版 | 盤後一次 | 已有 `FINMIND_TOKEN` |
| **fallback** | 證交所 OpenAPI | 異常時手動拉 | rate limit，不常用 |

**v1 不接付費即時源**。若未來體驗不夠，再評估升級 FinMind 付費或切換其他即時源。

---

## 6. 帳戶與起始參數

| 參數 | 預設值 | 說明 |
|---|---|---|
| 起始模擬資金 | **NTD 1,000,000** | 100 萬台幣 |
| 期貨保證金（v2）| 按官方原始 / 維持保證金 | 大台原始 ~167,000 |
| 計價幣別 | TWD | v1 不處理多幣別 |
| 是否融資融券 | ❌ 否 | v1 純現股 |
| 是否放空 | ❌ 否（v1）｜ ✅ 是（v2 期貨多空雙向） | |

---

## 7. 儲存結構

```
automation/sim/
├── REQUIREMENTS.md          # 本檔
├── M1_PLAN.md               # M1 工作拆解
├── __init__.py              # 模組入口
├── models.py                # 資料模型：Order / Fill / Position / Account
├── engine.py                # 撮合引擎
├── feed.py                  # 報價拉取（yfinance + FinMind）
├── commands.py              # 飛書 bot 指令解析
├── reports.py               # 日報 / 週報 / 即時通知產生器
├── data_provider/           # 從 dsa 抄過來的必要邏輯（解耦）
│   └── __init__.py
└── tests/
    ├── test_engine.py
    ├── test_models.py
    └── test_commands.py

config/
└── sim_config.yaml          # 起始資金、滑價、費率、撮合頻率、watchlist

logs/sim/                    # 已被 .gitignore 排除（logs/）
├── orders.jsonl             # append-only 訂單流
├── fills.jsonl              # 成交流
├── positions/
│   └── YYYY-MM-DD.json      # 每日持倉快照
└── equity_curve.jsonl       # 權益曲線
```

---

## 8. 互動命令（飛書 bot）

### 結構化下單

```
# 股票 / ETF（v1）
/buy <symbol> <qty>                    # 市價買
/buy <symbol> <qty> limit <price>      # 限價買
/buy <symbol> <qty> stop <trigger>     # 停損買
/sell <symbol> <qty> [limit|stop ...]  # 賣
/cancel <order_id>                     # 取消委託
/close_all                             # 平倉所有持倉

# 期貨（v2）
/buy_fut <contract> <lots>             # 買 N 口
/sell_fut <contract> <lots>            # 賣 N 口
```

### 結構化查詢

```
/portfolio   或 /p          # 持倉摘要（檔別、均價、現價、未實現損益）
/orders                     # 待成交委託
/pnl                        # 今日損益
/equity                     # 權益曲線（近 30 日）
/quote <symbol>             # 即時報價
```

### 自然語言（透過 ClaudeHelper）

範例：
- 「我這個月勝率多少」
- 「2330 我的持倉成本是多少」
- 「上週的成交記錄」
- 「我目前最大未實現虧損是哪一檔」

實作方式：bot 收到非 `/` 開頭訊息 → 帶上模擬盤狀態 snapshot → 呼叫 ClaudeHelper → 回覆。

---

## 9. 通知與報告

| 類型 | 觸發 | 通道 | 內容 |
|---|---|---|---|
| **即時通知** | 成交、停損觸發、達標出場 | 飛書（**不發微信**，避免踩 Ping） | 訂單號、成交價、損益 |
| **日報** | 每日 17:00（盤後） | 飛書 | 持倉、損益、當日成交清單 |
| **週報** | 每週五盤後 | 飛書 | 週績效、簡化勝率 |

**簡化勝率**：成交筆數中盈利筆數佔比。**不做策略沙盒級分析**（不算夏普、不算最大回撤、不算 alpha）。

---

## 10. 邊界

- ❌ **不接真實券商 API**：純模擬，永遠不對接券商下單
- ❌ **不擅自吃 `stock_screener` 訊號自動下單**：避免變策略沙盒（C 路線）
- ❌ **不接微信通知**：避免踩 ret=-2 ping 問題
- ✅ **data_provider 解耦**：抄一份必要邏輯到 `automation/sim/data_provider/`，不 import dsa
- ✅ **走既有 lark_notifier**：通知對齊既有飛書群

### 10.2 advisor 邊界調整（M6 2026-05-06）

- ✅ **`sim/advisor/` 模組可吃觀察池訊號自動下單**：條件為 (1) 通過 `risk_guard` 三層閘 (2) 每筆下單 + 成交即時 DM 通知 (3) 每日盤後四件套指標審計 + `daily_brief` 落檔
- ❌ **不直接讀 `stock_screener.py` 內部 state**：advisor 只讀 `logs/watchlist/current.json`（screener 對外 contract）
- ❌ **advisor 不再決策風控**：三層閘寫死在 `risk_guard.py`，LLM 不可繞過

---

## 11. 工程拆解

### v1（台股 + ETF）

| M | 內容 | 天 |
|---|---|---|
| **M1** | 訂單 / 持倉 / 撮合引擎（市價、限價） | 3 |
| **M2** | 完整訂單類型（停損 / 停利 / OCO / IFD） | 2 |
| **M3** | 飛書 bot 結構化指令 | 2 |
| **M4** | 自然語言查詢（接 ClaudeHelper） | 1 |
| **M5** | 通知 + 日報 + 週報 | 2 |
| **v1 合計** | | **10 工作日** |

### v2（含 Claude 操盤層）

| M | 內容 | 天 |
|---|---|---|
| **M6** | Claude 操盤層 advisor + 四件套指標 + daily_brief（詳見 M6_PLAN.md / §13） | +5 |
| **M7（待規劃）** | 期貨支援（保證金、結算、多空、轉倉）— 原 M6 範疇後推 | +5 |
| **v2 合計** | | **20 工作日** |

---

## 12. 後續決策點（v1 完成後再考慮）

- 是否接 `stock_screener` 訊號做半自動建議單（不下單，只通知）
- 是否做策略回測（用 FinMind EOD 跑歷史模擬）
- 是否升級 FinMind 付費 plan 拿真即時報價
- 是否加 `apps/dsa-web` 風格的 Web UI（看 K 線、權益曲線圖表）

---

## 13. advisor 操盤層（M6 2026-05-06）

### 13.1 模組分層

- 11 檔 / ~2,260 行 / 80 測試（`sim/advisor/`）
- `client` / `signals` / `decision` / `risk_guard` / `runner` / `metrics` / `daily_brief` / `__main__` / `__init__` / `_audit` / `_types`

### 13.2 決策節奏

- 盤中 09:00–13:30 每 5 分鐘 launchd 觸發 `python3 -m sim.advisor decide`
- advisor 內部 `should_run_now()` 過濾盤外 / 假日 / 週末
- launchd 用 `StartInterval=300`（每 5 分鐘）+ 進程內過濾，不在 plist 列 60 個時間點

### 13.3 風控閘三層（`risk_guard.py`）

1. 單筆風險 ≤ `account.equity × 2%`
2. 持倉集中度 ≤ 20%、同產業 ≤ 30%
3. 日內熔斷 ≤ `-account.initial_cash × 3%` → `paper_only` 至次日 09:00
- 熔斷狀態落檔 `logs/sim/advisor_state.json`（跨進程持久化）

### 13.4 四件套指標 + 0050 baseline

- 勝率 / 累積已實現 PnL / 最大回撤 / 平均盈虧比
- baseline = 同期 `0050.TW` buy-and-hold 報酬率
- 盤後 13:35 launchd 觸發 `python3 -m sim.advisor metrics`，DM 推 W

### 13.5 daily_brief（純資料聚合，零 LLM 依賴）

- 盤後 13:40 launchd 觸發 `python3 -m sim.advisor brief`
- 7 區塊 markdown：Header / 四件套 / 帳戶快照 / 今日 fills / Equity 變化 / Advisor 決策 / 風險觀察
- 落 `logs/sim/daily_brief/brief_YYYY-MM-DD.md`
- W 晚上自己用 cc-switch GUI 切到任意 LLM 讀檔評估

### 13.6 cc-switch 跟隨模式

- `client.py:_bootstrap_cc_switch_env()` 從 `config/cc_switch_route_provider.py:resolve_routes()` 拉當前 provider 的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` 注入 process env
- W 在 cc-switch GUI 切 provider，下一輪 launchd 觸發 advisor 自動跟著切，**不需重啟**
- sticky beta header：首次 1m hint 失敗後標記，後續每次帶（對抗 Calcium-Ion/new-api 後端）
- cc-switch 無可用 provider → ping 失敗 → 進程退出 → launchd 下一輪重試

---

## 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| v0.1 | 2026-05-04 | 草稿（4 叉口問題） |
| v1.0 | 2026-05-04 | 拍板（B 路線、台股+ETF+期貨、混合互動、automation/sim、5a 分階段、FinMind 免費 + yfinance、100 萬資金、30 秒撮合、滑價 0.1%、期貨每口 50 元、資料源策略 A） |
| v1.1 | 2026-05-06 | M6 落地：§10.2 advisor 邊界調整 + §13 advisor 章節 6 小節 + §11 校準（M6 改 advisor、原期貨後推 M7） |
