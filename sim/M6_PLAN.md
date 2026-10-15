# M6 計畫：Claude 操盤層（advisor）+ 四件套指標 + 風控閘

> 預估：4–5 個工作日
> 目標：在 sim 模擬盤之上加一層 `sim/advisor/`，由 Claude API 常駐進程吃 4 池觀察池訊號 + 持倉 + 即時報價 → 出 JSON 訂單 → 過風控閘 → 進 `engine.submit_order`。盤後算「四件套指標」（勝率 / 累積 PnL / 最大回撤 / 平均盈虧比）落 `advisor_metrics.jsonl`，連續觀察四件套同向往上爬，視為框架站住、再評估是否落地真實券商。
> 完成標準：盤中 09:00–13:30 每 5 分鐘 advisor 自動產生決策（含 hold）；下單通過風控閘 + 即時 DM 通知；盤後 13:35 落一筆 advisor_metrics；既有 M1–M5 全綠零回歸；連續 5 個交易日跑下來四件套有可解讀的趨勢線。

---

## 設計決策

> 規劃日期：2026-05-06（M5 收尾後同 session；W 確認「Claude 直接操盤模擬盤、後期再評估落地」路線後展開）

### 決策 1：Claude 進駐方式 — A2 新建 `sim/advisor/` 子模組 + 獨立 launchd

advisor 跟 commands.py 的飛書即時下單共存，但職責切開：
- `commands.py` 處理飛書解析 + 人工下單，不該知道 LLM
- `runner.py` 處理撮合，不該知道訊號
- **新建 `sim/advisor/`** 處理「訊號 → LLM → 下單」這一段，跟 commands.py 共用同一個 `MatchEngine` 實例（透過 `_ensure_sim_initialised` 暴露的 singleton，或 advisor 自己起一個讀同一份 jsonl）

advisor 走獨立 launchd plist（不寄生在 feishu_bot 進程），原因：
- bot 重啟頻率高（debug、改 prompt、改 LLM），advisor 不應跟著重啟漏決策節點
- launchd 觸發不依賴 bot 健康，排程確定性高
- advisor 跟 bot 是兩個進程 → 共用 engine 必須**走檔案層**（讀同一份 fills.jsonl + orders.jsonl），不能共享記憶體 state

### 決策 2：決策節奏 — 5 分鐘一次，跟撮合節奏（30 秒）解耦

W 想要「秒級行情驅動」，但 yfinance ~15 分鐘延遲是硬傷（REQUIREMENTS §3、§5），sim v1 撮合也是 30 秒。M6 採折衷：
- **advisor 決策**：盤中（09:00–13:30）每 5 分鐘觸發；盤前 / 盤後不跑（避免空轉燒 token）
- **runner 撮合**：維持 30 秒，不變
- 訊號–成交誤差上限 = 5 分鐘 + 15 分鐘延遲 + 30 秒撮合 ≈ 21 分鐘，跟 v1 「重點是流程驗證」對齊

秒級 / 真即時源（FinMind 付費 / 群益）放 M7+，等 M6 框架站住才投錢。

### 決策 3：訊號來源 — `logs/watchlist/current.json` 四池 + 持倉 + 即時報價，不重做技術分析

K 線 / 籌碼 / SEPA / 價值五項已被四個選股 agent 跑過，advisor 直接吃**結論層**（current.json 已有 entry_zone / stop_loss / take_profit / score / breakdown）。advisor 的工作是：
- **跨池融合**：同一檔在多池命中怎麼決策（正式跨池 / 輔助跨池 / 純日內，WORKSPACE.md 已定義）
- **進出時點**：是否在 entry_zone 內、是否觸停損、是否該分批
- **資金配置**：100 萬資金怎麼分到候選池

advisor 不該重做技術指標，否則跟選股 agent 衝突且燒 token。

### 決策 4：訊號 → 訂單 — JSON Schema 強制、temperature=0、多重 fallback

Claude 回傳格式：
```json
{
  "decisions": [
    {
      "action": "buy" | "sell" | "hold",
      "symbol": "2330.TW",
      "qty": 1000,
      "type": "MKT" | "LMT" | "STOP" | "TP",
      "price": 920.0,
      "reason": "短中跨池命中、entry zone 內、剩餘倉位 17%"
    }
  ],
  "summary": "本輪 hold 為主，僅 2330 加倉 1000 股",
  "metrics_self_eval": "上週四件套 ↑↑↓↑，盈虧比仍偏低，本輪緊縮停利目標"
}
```
- 用 SDK `tools` 機制強制 JSON Schema，比 prompt 約束穩
- `temperature=0`、`max_tokens=2048`
- 解析失敗 → 重試 1 次 → 仍失敗 → log + skip 本輪（不下單比下錯單安全）
- prompt cache：system prompt（風控規則 + 四件套定義）走 cache、user prompt（觀察池 + 持倉）每輪重算

### 決策 5：風控閘三層 — 在 advisor → engine 之間，過不了的 decision 標 reject 並 log

1. **單筆風險上限**：`(entry_price - stop_loss) × qty ≤ account.equity × 2%`，沒帶 stop 的單視同最大風險 = 5% 強制 reject
2. **持倉集中度**：同檔不超過 `account.equity × 20%`、同產業不超過 30%（產業表用 `config/stock_universe.yaml` 既有資料）
3. **日內熔斷**：當日已實現損益 ≤ `-account.initial_cash × 3%`（即 -3 萬）→ advisor 進入 `paper_only=True` 模式（只 log 不下單）至次日 09:00 解除

這三層寫死在 `sim/advisor/risk_guard.py`，**不丟給 Claude 自己判斷**（LLM 對風險的判斷不可信）。Claude 出 decision 後 risk_guard 把不過的拍掉、過的才 submit_order。

### 決策 6：四件套指標 — `sim/advisor/metrics.py` + 每日盤後 13:35 落 `logs/sim/advisor_metrics.jsonl`

四件套同時看才不會被勝率單指標騙：
| 指標 | 定義 | 來源 |
|---|---|---|
| 勝率 | `reports.win_rate(fills)`（已存在）| fills.jsonl |
| 累積 PnL | `account.total_realized_pnl + 未實現` | engine snapshot |
| 最大回撤 | 從 equity_curve.jsonl 算 peak-to-trough | equity_curve.jsonl |
| 平均盈虧比 | 平均盈利筆 / 平均虧損筆（絕對值）| fills.jsonl per_fill_realized |

**baseline 對照**：每筆 advisor_metrics 同時記同期 0050.TW 的買入持有報酬率，避免「四件套爬其實是大盤好」的假信號。

每日 13:35 launchd 觸發 `python3 -m sim.advisor metrics`，append 一筆 jsonl + DM 推 W（簡短版「今日勝率 X% / 累積 PnL Y / MDD Z% / 盈虧比 W；vs 0050 大盤 +N%」）。

### 決策 7：sim §10 邊界調整 — 明確開放 advisor 路徑

REQUIREMENTS §10 現寫：「不擅自吃 stock_screener 訊號自動下單：避免變策略沙盒（C 路線）」。

M6 動這條，REQUIREMENTS 加 §10.2：
> ✅ **`sim/advisor/` 模組可吃觀察池訊號自動下單**，但需通過 `risk_guard` 三層閘 + 即時 DM 通知 + 每日四件套指標審計。**advisor 不直接讀 stock_screener.py 內部 state**（避免緊耦合），只讀 `logs/watchlist/current.json`（已是 screener 的對外 contract）。

memory feedback 對應校準：
- 既有「Daily stock report formal PDF only」「Daily stock report output folder」不衝突（advisor 跟報告路徑不交叉）
- 既有「General permissions fully automatic with rollback」適用於 advisor 的「下單 → DM 通知 → W 看到不對可手動 `/cancel <order_id>`」撤回路徑

### 決策 8：watchlist 動態合併 — 順手補 REQUIREMENTS §7 既有缺口

`sim_config.yaml` 現在 watchlist 硬編 `[2330.TW, 0050.TW]`，但觀察池有 31 短線 + 12 長線。advisor 啟動時把候選 symbols（entry_zone 內 + 持倉中）合併進 engine 的記憶體 watchlist，`runner.run_loop` 撮合範圍跟著擴大。**只改記憶體不寫回 yaml**（重啟回到原始）。

如果未來要持久化，加 `sim/advisor/watchlist_sync.py` 寫回 yaml，本次不做。

### 決策 9：取消 LLM Reviewer 監督層、改做純資料 daily_brief（W 拍板 2026-05-06）

**初版設計嘗試**：階段一末做 reviewer 用獨立 cc-switch provider 走「指定模式」、預設 Any Router 後降為 Kimi。

**廢棄理由（2026-05-06 同 session 後續對話）**：
1. Any Router 後端 server panic 在 client 端證實**完全解不了**（試 7 種：標準 model 名稱 + 5 種 beta header 變體 + SDK betas 參數 + 多 beta 組合 + 自訂 model 後綴 + 非 Claude model + token 權限驗證 — 全部 400「请启用 1m」或 500 nil deref panic）。問題在 Calcium-Ion/new-api 後端代碼，需要 server 端修
2. W 對「其他能跑的 provider 能力不夠」有疑慮（reviewer 跑非 Claude 後端 = 同類 GPT 自評）
3. 真正的需求是「W 晚上回來自己用 Any Router 在 Claude Code / cc-switch GUI 評估」 — 那條路徑跟 advisor 系統解耦（W 切 cc-switch GUI、Claude Code 認、advisor 不影響）

**新設計：daily_brief**（純資料聚合、零 LLM 依賴）：
- 盤後 13:35 launchd 觸發 `python3 -m sim.advisor brief`
- 從 `advisor_audit.jsonl` + `orders.jsonl` + `fills.jsonl` + `equity_curve.jsonl` 聚合當日狀態
- 產出 `logs/sim/daily_brief/YYYY-MM-DD.md`（人類 + LLM 友善的 markdown）
- DM 推短版「今日 brief 已落 `logs/sim/daily_brief/2026-05-06.md`，回家 review」
- W 晚上自己用 Any Router（cc-switch 切過去、Claude Code 走那條）讀 markdown 自己評
- 沒有 advisor 層的 LLM 呼叫 = 沒有 server bug 卡關 = 沒有 token 燒外部成本

**Markdown 八區塊**：
1. advisor 操盤摘要（總輪數 / 出單統計）
2. 每輪 decisions 詳列（含 provider id + model + Claude summary + self_eval）
3. 實際成交 fills（時間軸）
4. 持倉變化（開盤 → 收盤）
5. 風控閘 reject 詳列（含被擋的 decision + reason）
6. 權益曲線（開盤 / 最高 / 最低 / 收盤 / 當日 MDD）
7. 四件套指標（截至今日：累積 PnL / vs 0050 / MDD / 勝率 / 盈虧比）
8. 給 reviewer LLM 看的提示（W 晚上拿給 LLM 評時的 prompt 引導）

**異質 reviewer 留作未來選項**：W 若日後想要 advisor 系統內建 LLM reviewer，再回來啟用決策 9 v1.0 的設計（cc-switch 指定模式 + 異質 provider）；但**daily_brief 的 markdown 設計同時能餵 advisor 內建 reviewer 跟 W 手動的 cc-switch GUI reviewer**，兩條路徑不衝突。

---

## 任務拆解

### 區塊 A：Anthropic SDK 接入（`sim/advisor/client.py`）— 走 cc-switch 跟隨模式

W 拍板（2026-05-06）：走 cc-switch 路由 + **跟隨模式**（GUI 當前選中那個 provider 即 advisor 用的 provider；W 在 GUI 切 provider，advisor 下一輪 launchd 觸發自動跟著切）。

利用既有 `config/cc_switch_route_provider.py:resolve_routes()`，啟動時拉當前 provider 的 `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_MODEL` 注入 process env，`anthropic.Anthropic()` 客戶端自動讀。**不需要 W 在 .env 寫死 key**。

- [ ] `pip install anthropic>=0.40` 進 `requirements.txt`
- [ ] `_bootstrap_cc_switch_env() -> str`：呼叫 `resolve_routes()`，把 `primary.env` 寫進 `os.environ`，回傳 model 名。**無可用 provider 時 raise RuntimeError**（advisor 進程退出、launchd 下一輪重試），不靜默 fallback 到別的 provider
- [ ] `AdvisorClient.__init__()`：bootstrap → `self.client = Anthropic()`、`self.model = ...`
- [ ] `decide(system_prompt, user_payload, *, tools, timeout=30) -> dict`：含重試、JSON 解析；**`cache_control` 走 try/except**（部分 cc-switch 後端 provider 不支援，失敗降級為無 cache，log warning 後繼續）
- [ ] 啟動時做一次 ping：`messages.create(max_tokens=1, ...)` 驗證 provider 可用，失敗則退出
- [ ] 失敗模式：API timeout / rate limit / 格式錯 / cc-switch 無 provider → 各自 log 唯一 reason
- [ ] **每輪 advisor decision 把當下 provider id + model 一起 log 進 `advisor_audit.jsonl`**：跟隨模式下 W 切 provider 後 advisor 跟著切，事後 retro 才能對照「這筆決策當時用的是哪個 provider」

### 區塊 B：訊號組裝（`sim/advisor/signals.py`）

- [ ] `load_watchlist_pools() -> dict`：讀 `logs/watchlist/current.json`，回傳四池 + cross_signals
- [ ] `build_decision_context(engine, feed, recent_fills_n=20) -> dict`：組成 user_payload
  - account snapshot（cash / equity / positions / total_realized_pnl）
  - pools（四池 + 跨池）
  - quotes（持倉檔 + 池內候選檔）
  - recent_fills（近 N 筆判斷自己最近的決策品質）
  - market_context（大盤 0050 當前報價 + 5 日漲跌）
- [ ] payload 大小監控：超 80K tokens 就壓縮（去掉 breakdown 細節，只保留 score）

### 區塊 C：Prompt + 解析（`sim/advisor/decision.py`）

- [ ] `SYSTEM_PROMPT`：含風控規則、四件套定義、「你被怎麼考核」、輸出 schema
- [ ] `decide_once(engine, feed) -> list[Decision]`：B + A + 解析
- [ ] `Decision` dataclass：`action / symbol / qty / type / price / reason / source_pools`

### 區塊 D：風控閘（`sim/advisor/risk_guard.py`）

- [ ] `RiskGuard.__init__(engine, sim_config)`
- [ ] `evaluate(decision) -> tuple[bool, str]`：(passed, reason_if_blocked)
- [ ] 三層：單筆風險 / 集中度 / 日內熔斷
- [ ] `is_circuit_broken_today() -> bool`：讀 fills.jsonl + initial_cash
- [ ] 所有 reject 寫 `logs/sim/advisor_audit.jsonl`（含 decision + block reason）

### 區塊 E：執行循環（`sim/advisor/runner.py`）

- [ ] `run_advisor_once(engine, feed) -> dict`：B → C → D → submit_order，回傳統計（decided / passed / submitted / blocked）
- [ ] `should_run_now() -> bool`：盤中 09:00–13:30 + 非假日（讀 `config/trading_calendar.yaml`）才跑
- [ ] 每次跑都 append `logs/sim/advisor_audit.jsonl`

### 區塊 F：四件套指標（`sim/advisor/metrics.py`）

- [ ] `compute_metrics(fills_path, equity_curve_path, *, since=None) -> dict`：四件套 + 樣本數
- [ ] `compute_baseline_return(symbol='0050.TW', since=None) -> float`：大盤對照
- [ ] `format_metrics_dm(metrics, baseline) -> str`：簡短 DM 文字
- [ ] CLI：`python3 -m sim.advisor metrics` 算當日 + 推 DM；`--since 7d` 算近 7 日

### 區塊 G：CLI entry（`sim/advisor/__main__.py`）

```
python3 -m sim.advisor decide       # 跑一次決策（給 launchd 5 分鐘觸發）
python3 -m sim.advisor metrics      # 算四件套 + 推 DM（盤後 13:35 觸發）
python3 -m sim.advisor decide --dry-run  # 出 decision 但不 submit（debug）
```

argparse subcommand。decide 內部呼叫 `should_run_now()`，週末 / 盤外直接 noop。

### 區塊 H：launchd plist + REQUIREMENTS 更新

- [ ] `~/Library/LaunchAgents/com.w.sim-advisor-decide.plist`：09:00–13:30 每 5 分鐘觸發 `python3 -m sim.advisor decide`
- [ ] `~/Library/LaunchAgents/com.w.sim-advisor-metrics.plist`：每日 13:35 觸發 `python3 -m sim.advisor metrics`
- [ ] `sim/REQUIREMENTS.md` 加 §10.2（advisor 邊界調整）+ §13（M6 advisor 章節）
- [ ] `sim_config.yaml` 加 `advisor:` 區塊（model、interval_min、risk_pct、concentration_pct、circuit_pct）

### 區塊 I：測試（≥30 項）

- [ ] `sim/tests/test_advisor_signals.py`（≥6 項）：load_pools / build_context / 大 payload 壓縮
- [ ] `sim/tests/test_advisor_decision.py`（≥6 項）：mock AnthropicClient、JSON 解析、解析失敗重試
- [ ] `sim/tests/test_advisor_risk_guard.py`（≥10 項）：三層風控閘各邊界、熔斷狀態切換
- [ ] `sim/tests/test_advisor_metrics.py`（≥6 項）：四件套計算、baseline、空 fills 邊界
- [ ] `sim/tests/test_advisor_e2e.py`（≥3 項）：mock LLM 全鏈路、跨池合併、watchlist 動態擴展
- [ ] 既有 `test_e2e_smoke.py` / `test_commands.py` / `test_reports.py` 全綠不回歸

---

## 待決點（W 拍板後落本檔 v1.0）

| # | 待決問題 | 預設選項 | 影響 |
|---|---|---|---|
| **W1** | advisor 吃哪幾池 | 全四池（W 拍板 2026-05-06）| 影響 prompt 規模、訊號雜訊。**已拍板：模擬盤試水期不限風格，看四件套指標自動暴露哪個池質量好**。落地真錢時再縮池對齊 profile |
| **W2** | 啟動資金分倉策略 | 完全交給 Claude（W 拍板 2026-05-06）| Claude 自決定 vs 風控閘預設。**已拍板：advisor prompt 不寫死分倉規則，risk_guard 三層閘兜底。前 1–2 週可能因 advisor 不熟風控邊界、出單頻繁被 reject，是預期行為**，retro 時據此調 prompt 用語 |
| **W3** | 是否允許 advisor 主動平倉（非 stop / TP 觸發）| 完全自治（W 拍板 2026-05-06）| 完全自治 vs 半自治。**已拍板：buy / sell / hold 任意組合，所有訂單在 audit log 寫 reason** |
| **W4** | DM 通知頻率 | 每筆下單 + 成交 + 每日 metrics + 熔斷（W 拍板 2026-05-06）| 過度通知 vs 漏單。**已拍板：盤中 4.5 小時 × 5 分鐘 1 輪、估每天 10–20 條交易類 DM + 1 條 metrics + 0–N 條熔斷**。memory `feedback_notification_channels` 對應更新「個人交易記錄走 DM、推送密度可接受 ~20 條/天」 |
| **W5** | API model 選擇 | 由 W 在 cc-switch GUI 切（advisor 跟隨）| 成本 vs 效果。每天 ~$3–5 估算（風險點 8）。**已轉化為 cc-switch 跟隨模式，advisor 不寫死 model**，W 可隨時 GUI 切換 |
| **W6** | 跑 N 個交易日視為驗證通過 | 20 交易日 + 累積 PnL 趏 0050 +5% + MDD < 8% + 勝率 > 50%（W 拍板 2026-05-06）| **已拍板：業界回測標準**。期間 prompt 不能動（動了重起算），20 日達標進 M7 落地評估、未達標進 M6.5 retro 調 prompt 後再跑下一輪 20 日 |

---

## 驗收條件

1. `cd /Users/w/Cursor/automation && python3 -m pytest sim/tests/` → **≥180/180 通過**（M5 完工 ~150 + M6 新增 ~30）
2. `python3 -m py_compile sim/advisor/*.py` 全通過
3. 飛書端到端 smoke：
   - `python3 -m sim.advisor decide --dry-run` → 印出 decision JSON + risk_guard 結果，不 submit
   - `python3 -m sim.advisor decide` → submit 至少一筆 hold（hold 也要 log）；若市況觸發買賣 → DM 收到即時成交通知
   - `python3 -m sim.advisor metrics` → DM 收到四件套摘要 + 大盤 baseline
4. launchd 兩個 plist load 後 24 小時內：
   - `tail logs/sim/advisor_audit.jsonl` 看到盤中每 5 分鐘一筆 decision_round
   - `tail logs/sim/advisor_metrics.jsonl` 看到當日一筆 metrics
5. **連續 20 個交易日**人工驗收（W 拍板 W6 2026-05-06）：累積 PnL 趏 0050 同期報酬 +5% **且** MDD < 8% **且** 勝率 > 50% → 進 M7 落地評估；其中任一項未達標 → 進 M6.5 retro 調 prompt（**不調風控閘參數**，保持環境一致）後重跑下一輪 20 日。期間 prompt 不能動，動了重起算

---

## 影響檔案清單

| 檔案 | 動作 |
|---|---|
| `automation/sim/advisor/__init__.py` | **新建**（模組入口）|
| `automation/sim/advisor/client.py` | **新建**（Anthropic SDK 封裝，~150 行）|
| `automation/sim/advisor/signals.py` | **新建**（訊號組裝，~200 行）|
| `automation/sim/advisor/decision.py` | **新建**（prompt + 解析，~250 行）|
| `automation/sim/advisor/risk_guard.py` | **新建**（風控閘三層，~150 行）|
| `automation/sim/advisor/runner.py` | **新建**（執行循環，~120 行）|
| `automation/sim/advisor/metrics.py` | **新建**（四件套 + baseline，~180 行）|
| `automation/sim/advisor/__main__.py` | **新建**（CLI entry，~80 行）|
| `automation/sim/tests/test_advisor_*.py` | **新建** ×5 檔（≥30 項）|
| `automation/requirements.txt` | 改（加 `anthropic>=0.40`）|
| `automation/.env` | 改（加 `ANTHROPIC_API_KEY`，**W 自行配，不入 git**）|
| `automation/config/sim_config.yaml` | 改（加 `advisor:` 區塊）|
| `automation/sim/REQUIREMENTS.md` | 改（§10.2 邊界調整 + §13 advisor 章節）|
| `~/Library/LaunchAgents/com.w.sim-advisor-decide.plist` | **新建**（部署任務，不進 git）|
| `~/Library/LaunchAgents/com.w.sim-advisor-metrics.plist` | **新建**（部署任務，不進 git）|
| `automation/sim/M6_PLAN.md` | **本檔（新建）** |

---

## 風險與不確定點

1. **cc-switch 路由 provider 兼容性**（W 已選跟隨模式 2026-05-06）：cc-switch 後端可能是 Anthropic 原生 API，也可能是 OpenAI 相容包裝（聚合路由）。後者對 SDK 的 `tools` JSON Schema 與 `cache_control` 行為不一致 — `tools` 失敗會 raise，`cache_control` 失敗只是少 cache。**M6 區塊 A 必須對 `cache_control` 走 try/except 降級、`tools` 走 ping smoke 驗證**。若 W 在 GUI 切到不支援 tools 的 provider，advisor 啟動 ping 就退出，下一輪 launchd 重試 + log 寫清楚 provider id 給 W 看（提示要切回支援 tools 的 provider）

   **實作中發現的 cc-switch 後端 bug（2026-05-06 階段一首次端到端）**：當 cc-switch 當前 provider 是「Any Router」（base_url 走 fcapp.run 阿里雲代理、後端是 Calcium-Ion/new-api 開源 fork）時，server 強制要求 client 帶 `anthropic-beta: context-1m-2025-08-07` header（不帶回 400「请启用 1m 上下文」），但帶了會觸發 server-side `runtime error: invalid memory address or nil pointer dereference` 500 panic。client 端兩條路都堵死，無解。已在 `client.py` 加 sticky beta header enable 機制（首次 1m hint 失敗 → 標 `_beta_headers=['context-1m-2025-08-07']` 後續每次帶），但對 server panic 完全無解 — 只能 friendly error message 引導 W 切到別的 provider（如 Claude Official、Sub2API、ANYONE.AI 等不要求強制 1m 的 provider）。**W 切 provider 後 advisor 跟隨模式自動讀新 provider，不需重啟 launchd**
2. **yfinance 15 分鐘延遲 + 5 分鐘決策 = 訊號–成交誤差最差 21 分鐘**：M6 仍是「流程驗證」非「績效驗證」。四件套同向爬只說明「框架閉環沒崩」，不證明「真錢能用」。落地評估必須在 M7 升級即時源後重新驗證
3. **Claude JSON 不穩定**：即使 `tools` schema + `temperature=0`，仍可能吐缺欄位。M6 需要 JSON 解析失敗的降級（重試 1 次 + skip 本輪）
4. **prompt 上下文超量**：四池總共 ~45 檔 × breakdown 詳細欄位 ~20 行 = 單輪 prompt 可能 > 50K tokens，加上 system + 持倉，~80K。M6 區塊 B 必須做壓縮（去 breakdown 細節、只留 score + entry_zone）
5. **W profile 衝突**：穩健保守 vs 短線池 31 檔。**待決點 W1 必須在實作前定**，否則 advisor 不知道該偏向哪池
6. **風控參數可能跟 100 萬資金 + 台股最小單位 1000 股不 fit**：例如 2330 股價 ~920，1000 股 = 92 萬 = 92% 集中度，遠超 20% 上限 → advisor 要嘛買 100 股零股、要嘛永遠不能買 2330。風控閘要支援零股，或對「最小可交易單位 > 集中度上限」的標的給特殊處理
7. **四件套同向爬不必然 = 框架站住**：可能是大盤好。M6 用 0050 baseline 對照，但 baseline 也是粗糙指標（沒考慮波動度調整）。M6 完工後若四件套漂亮但 baseline-adjusted 後沒 alpha，仍要老實說「不證明」
8. **token 成本**：5 分鐘決策一次、盤中 4.5 小時 = 54 次 / 天，單次 prompt + output ~5K tokens，每天 ~270K tokens（Sonnet 4.6 約 $1–2 / 天，連跑 30 天 ~$30–60）。**先報准預算，超 $100 / 月要降頻或換 Haiku**
9. **advisor 與 runner 跨進程共用 engine state**：advisor 是獨立 process（launchd），bot 內 runner 也是 process。兩者修改 fills.jsonl 是 OS-level append-safe，但 advisor submit_order 後 engine 記憶體 state 不會傳給 bot 的 runner — **advisor 必須自己 init 一個 MatchEngine 實例 + replay fills.jsonl 重建持倉**（reports.py 已有 replay_positions_from_fills 可重用）。這條工程量比想像大，區塊 E 要顯式設計
10. **launchd 5 分鐘觸發跟 advisor 真正 reload state 的順序**：每次 launchd 觸發 = 全新 process = 從 fills.jsonl 重建 → 跑 decide → 退出。每輪冷啟動成本 ~2 秒（讀 jsonl、解析、init engine），可接受。**但若 advisor 卡在 Anthropic API 等回應 > 5 分鐘**，下一輪會疊上來 — plist 要設 `LowPriorityIO`、advisor 內加 30 秒 timeout
11. **熔斷狀態的跨進程持久化**：日內熔斷觸發後 paper_only 標記必須持久化（不能只記憶體），否則下一輪 launchd 觸發 advisor 不知道熔斷了。寫 `logs/sim/advisor_state.json`（atomic 寫）
12. **測試難 mock**：advisor e2e 測試要同時 mock Anthropic SDK + feed.get_quotes + watchlist json + clock（trading hours 判斷）。組合空間大，建議先寫純函數層測試（signals / risk_guard / metrics 各自 mock 上游），e2e 只跑 1–2 個 happy path

---

## 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0 草稿 | 2026-05-06 | 八決策（進駐方式 / 節奏 / 訊號源 / 訂單格式 / 風控閘 / 四件套 / §10 邊界 / watchlist）+ 任務拆解 9 區塊 + 6 個待決點 + 12 風險（W 拍板待決點後進實作）|
