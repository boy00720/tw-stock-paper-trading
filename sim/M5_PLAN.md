# M5 計畫：通知 + 日報 + 週報 + equity_curve

> 預估：2 個工作日
> 目標：sim 模擬盤從「被動查詢」進入「主動推播 + 定時報告」。即時成交飛書通知、每日 17:00 日報、每週五盤後週報、`equity_curve.jsonl` 每 30 秒一筆持久化權益曲線。
> 完成標準：飛書即時收到成交通知（推 W 個人 DM）；launchd 17:00 觸發日報送達 DM；週五盤後送達週報；`equity_curve.jsonl` 隨 runner 同節奏增長；既有 M1–M4 全綠零回歸。

---

## 設計決策

> 規劃日期：2026-05-05（M4 收尾後同 session 對話收斂；W 確認四叉口全收）

### 決策 1：觸發機制 + 持久化 — A3 即時 runner thread + 日報週報 launchd CLI

REQUIREMENTS §9 沒寫 M5 跟既有 launchd / orchestrator 怎麼整合。三案折衷：

- **即時通知**：在 `runner.run_loop` 每輪 `engine.match()` + `apply_fills` 之後，對新 fills 呼叫注入的 `on_fill` callback。runner 不依賴飛書（純參數注入），保持 sim 模組可獨立測試。
- **日報 / 週報**：走 launchd cron CLI（`python3 -m sim.reports daily|weekly`），bot 重啟不影響定時觸發；新 process 從 `fills.jsonl` + `equity_curve.jsonl` 讀資料。
- **附帶代價**：M5 必須加「reports CLI 從 `fills.jsonl` 重建當日持倉」的 replay 邏輯（**不改**主流程「bot 重啟記憶體歸零」A 案，只給 reports CLI 在獨立進程裡用）。

### 決策 2：報告產生地 — B1 新建 `sim/reports.py` 集中

新建 `sim/reports.py` 涵蓋資料層 + 格式化層 + CLI entry。`commands.py` 職責是「飛書指令解析+分派」，不混塞「定時報告」。

### 決策 3：通知通道 — C3 即時 `feishu_bot.send_message` + 日報週報 `utils/lark_notifier`

- **即時**：runner thread 在 bot 進程內，用 bot 既有 `send_message(chat_id, text)`（lark_oapi client 已建好）。
- **日報 / 週報**：CLI 是獨立進程，必須用 webhook-based 的 `utils/lark_notifier`（CLI 沒 bot 的 lark client）。

### 決策 4：`equity_curve.jsonl` append 時機 — D2 runner 每輪 match 後

每 30 秒一筆，跟 runner 同節奏。沒成交時也寫（避免「持倉浮動但曲線不動」的鋸齒）。

### 收件人

**sim 全面走個人 DM（`FEISHU_USER_OPEN_ID`），不走群組**。即時通知、日報、週報都推 W 個人。

理由：sim 是個人模擬盤的私有交易記錄，跟群組裡其他自動化推送（晨報 / 新聞 / 股市 W / 跨境電商週報 / 反思 W 等）混在一起會洗版且破壞群組主題（沒人需要看你昨天 2330 賺了 552 元）。memory `feedback_notification_channels`「Automation 通知：飛書一律群組」原意是針對**跨 agent / 多人可看的定時推送**，不適用於個人交易記錄。

副作用：M5 完工後 memory 條目本身需要校準成「**自動化定時 / 跨 agent 推送一律群組；個人記錄 / 高頻交易事件走 DM**」（不在本 PLAN 範圍）。

---

## 任務拆解

### 區塊 A：`sim/reports.py` 資料層（純函數）

- [ ] `_iter_fills(fills_path)` → `Iterator[dict]`：streaming 讀 jsonl，避免一次載入
- [ ] `_iter_fills_in_period(fills_path, start_dt, end_dt)` → `list[dict]`：時間範圍過濾（含 start、不含 end，便於「今日 = [00:00, 隔日 00:00)」）
- [ ] `today_realized_pnl(fills_path)` → `float`：今日 0:00 起 sum realized_pnl
- [ ] `last_n_days_realized_pnl(fills_path, n_days)` → `float`：近 N 日同上
- [ ] `win_rate(fills)` → `dict{total, win, rate}`：對齊 `nl_query._compute_lifetime_stats` 規則
- [ ] `replay_positions_from_fills(fills_path, until=None)` → `dict[symbol, dict]`：從 fills 重建持倉（FIFO 或加權均價，跟 `engine.account.positions` 同一套規則 — **規則必須抽出來共用，避免兩處實作飄移**）
- [ ] `equity_curve_append(path, *, time, cash, position_value, equity, total_realized_pnl)`：append 一行 jsonl

### 區塊 B：`sim/reports.py` 格式化層

- [ ] `fmt_realtime_fill(fill, account_after)` → `str`：3 行（成交概要 / 累積損益 / 剩餘現金），對齊 M3 `_do_order` 風格（✅ + emoji）
- [ ] `fmt_daily_report(date, fills_today, positions, account)` → `str`：5 區塊（持倉、今日成交清單、今日損益、未實現、簡化勝率）
- [ ] `fmt_weekly_report(week_range, fills_week, equity_curve_week, lifetime_stats)` → `str`：5 區塊（週成交筆數、週實現損益、週勝率、最大盈虧檔、月累計）

### 區塊 C：`sim/runner.py` 改 `run_loop` 簽名

```python
def run_loop(
    engine: MatchEngine,
    feed: QuoteFeed,
    *,
    on_fill: Callable[[Fill, dict], None] | None = None,
    equity_curve_path: Path | None = None,
    interval_sec: int = 30,
    max_iterations: int | None = None,
) -> None:
    while True:
        # ... 既有 match + apply_fills 邏輯 ...
        if fills and on_fill:
            account_after = engine.snapshot(quotes)
            for fill in fills:
                try:
                    on_fill(fill, account_after)
                except Exception:
                    logger.exception("on_fill callback failed")
        if equity_curve_path:
            try:
                equity_curve_append(equity_curve_path, ...)
            except Exception:
                logger.exception("equity_curve_append failed")
```

新參數全 keyword-only + 預設 None，保 **既有測試零回歸**。

### 區塊 D：`scripts/feishu_bot.py` 注入即時通知 callback

`_ensure_sim_initialised` 啟動 runner thread 時：

```python
def _on_sim_fill(fill, account_after):
    try:
        from sim import reports as sim_reports
        text = sim_reports.fmt_realtime_fill(fill, account_after)
        # 推 W 個人 DM，不推群組（sim 是私有交易記錄）
        send_message(
            os.getenv("FEISHU_USER_OPEN_ID"),
            text,
            receive_id_type="open_id",
        )
    except Exception:
        logger.exception("[sim] 即時成交通知推送失敗")

threading.Thread(
    target=runner.run_loop,
    kwargs={
        "on_fill": _on_sim_fill,
        "equity_curve_path": Path(AUTOMATION_DIR) / "logs" / "sim" / "equity_curve.jsonl",
        "interval_sec": _SIM_ENGINE.match_interval_sec(),
    },
    daemon=True,
).start()
```

既有 `send_message(chat_id, text)` 預設 `receive_id_type="chat_id"`，DM 路徑必須顯式傳 `receive_id_type="open_id"`。**實作前先 grep 確認 `send_message` 簽名**（風險點 9）。

### 區塊 E：`sim/reports.py` CLI entry

```python
# python3 -m sim.reports daily               → 今日日報，推 DM
# python3 -m sim.reports daily --date 2026-05-04  → 重產任意日期（dry-run 用）
# python3 -m sim.reports weekly              → 本週週報，推 DM
# python3 -m sim.reports daily --no-send     → 只 print 不推（debug）
```

argparse subcommand `daily` / `weekly`、`--date`、`--no-send`。走 `utils/lark_notifier` 的 DM 推送函數（**簽名實作前先 grep 確認**，PLAN 風險點 1；推送目標一律 `FEISHU_USER_OPEN_ID` env，不推群組）。

### 區塊 F：`sim/commands.py` 既有 `/pnl` `/equity` 補切片（M3 留的 TODO）

- `_do_pnl`：既有「lifetime」訊息加上「今日 / 30 日」三行，呼叫 reports 純函數
- `_do_equity`：既有「當下值」加上「30 日報酬曲線摘要」（`equity_curve.jsonl` 30 日的最高 / 最低 / 月變化）

### 區塊 G：launchd plist（部署任務，不在程式碼範圍）

- `~/Library/LaunchAgents/com.w.sim-daily-report.plist`：每日 17:00 觸發 `python3 -m sim.reports daily`
- `~/Library/LaunchAgents/com.w.sim-weekly-report.plist`：每週五 17:30 觸發 `python3 -m sim.reports weekly`
- 兩個 plist 都設 `WorkingDirectory=/Users/w/Cursor/automation`、stdout/stderr 落 `logs/sim/`
- 部署：`launchctl load ~/Library/LaunchAgents/com.w.sim-*.plist`

### 區塊 H：測試

- [ ] 新建 `sim/tests/test_reports.py`（≥18 項）：
  - 資料層 6 項（_iter_fills、period 過濾、today_pnl、last_n_days_pnl、win_rate、replay_positions、equity_curve_append）
  - 格式化 3 項（realtime_fill / daily / weekly）
  - CLI 3 項（mock lark_notifier 驗證 daily / weekly / --no-send）
  - replay 一致性 3 項（replay_positions vs engine.account.positions 應一致）
  - 邊界 3 項（無 fills、跨日界、jsonl 損壞行）
- [ ] 改 `sim/tests/test_e2e_smoke.py`：加 on_fill + equity_curve 整合測試（mock callback 驗證 fills 觸發次數、equity_curve.jsonl 寫入內容）
- [ ] 改 `sim/tests/test_commands.py`：`/pnl` `/equity` 切片新欄位驗證（mock fills.jsonl）

---

## 驗收條件

1. `cd /Users/w/Cursor/automation && python3 -m pytest sim/tests/` → **≥150/150 通過**（126 + M5 新增 ~24，無回歸）
2. `python3 -m py_compile sim/reports.py sim/runner.py scripts/feishu_bot.py` 全通過
3. 飛書端到端 smoke（人工，重啟 bot 後）：
   - `/buy 2330.TW 100` → **W 個人 DM** 秒收即時成交通知（含損益、剩餘現金）；群組不出現
   - `python3 -m sim.reports daily --no-send` → stdout 顯示日報
   - `python3 -m sim.reports daily` → **W 個人 DM** 收到日報；群組不出現
   - `python3 -m sim.reports weekly` → **W 個人 DM** 收到週報；群組不出現
4. `tail -f logs/sim/equity_curve.jsonl` → runner 啟動後每 30 秒新增一行
5. `/pnl` 飛書回覆含「今日 / 30 日 / lifetime」三段
6. `/equity` 飛書回覆含「30 日權益曲線摘要」

---

## 影響檔案清單

| 檔案 | 動作 |
|---|---|
| `automation/sim/reports.py` | **新建**（資料層 + 格式化 + CLI，~400 行） |
| `automation/sim/tests/test_reports.py` | **新建**（≥18 項） |
| `automation/sim/runner.py` | 改（`run_loop` 加 `on_fill` + `equity_curve_path` keyword-only 參數） |
| `automation/scripts/feishu_bot.py` | 改（`_ensure_sim_initialised` 啟 runner 時注入 callback + equity_curve_path） |
| `automation/sim/commands.py` | 改（`_do_pnl` / `_do_equity` 加切片欄位，呼叫 reports 純函數） |
| `automation/sim/tests/test_e2e_smoke.py` | 改（加 on_fill / equity_curve 整合測試） |
| `automation/sim/tests/test_commands.py` | 改（`/pnl` `/equity` 切片回歸） |
| `~/Library/LaunchAgents/com.w.sim-daily-report.plist` | **新建**（部署任務，不進 git） |
| `~/Library/LaunchAgents/com.w.sim-weekly-report.plist` | **新建**（部署任務，不進 git） |
| `automation/sim/M5_PLAN.md` | **本檔（新建）** |

---

## 風險與不確定點

1. **`utils/lark_notifier` 簽名未確認**：實作前先 `grep "def send" utils/lark_notifier.py` 確認對外 API（`send_lark_text` / `send_lark_message` / 其他）。若沒有單純 text + chat_id 的 helper 要新增包裝
2. **`replay_positions_from_fills` 與 `engine` 持倉計算規則一致性**：M5 把均價計算抽出來共用是必要的；不抽會在「engine 改了規則但 replay 沒同步」時產出錯誤日報。M5 範圍內必須加共用函數（建議放 `sim/models.py` 或 `sim/_position_math.py`）
3. **`on_fill` callback 在 thread 拋例外**：必須包 try/except（已在區塊 D 設計），但 callback 內呼叫 lark API 也可能 hang，要加 timeout 或限制重試
4. **equity_curve 增長**：30 秒一筆 → 一日 ~960 筆 ~80KB → 一年 ~30MB。M5 不處理輪轉，列為 M6+ 風險
5. **日報 17:00 觸發時 bot 正在 match**：reports CLI 只讀 jsonl 不寫，bot 寫的是 append-only，OS-level 不衝突。但若 reports 跑時 fills.jsonl 正被 fsync，可能讀到不完整最後一行 — `_iter_fills` 對 JSONDecodeError 略過該行（跟 nl_query._tail_jsonl 相同策略）
6. **launchd plist 部署不可重現**：plist 在 home 目錄、不進 git。M5 完成後 archive 條目要把 plist 完整內容寫進去，方便 W 換機重建
7. **跨日界的「今日」定義**：`_iter_fills_in_period` 用本地時區（UTC+8 台灣）；fills.jsonl 的 `time` 欄位 M3 寫的是 isoformat（含或不含時區待確認），需在 M5 區塊 A 第一個函數實作前 grep 確認格式
8. **B 問題仍在**：M5 不依賴 LLM（純資料 + 格式化），所以 ClaudeHelper 1m context 400 不影響 M5 任何路徑
9. **`send_message` DM 路徑與 memory 校準**：既有 `feishu_bot.send_message` 預設 `receive_id_type="chat_id"`（M3 從 incoming message event 拿 chat_id 用），sim 即時通知是 runner thread 主動推、沒有 chat_id 來源，必須顯式傳 `receive_id_type="open_id"` + `FEISHU_USER_OPEN_ID` env。實作前 `grep "def send_message" scripts/feishu_bot.py` 確認簽名是否原生支援這個 keyword arg，若沒支援要新增包裝（例如 `send_dm(text)` helper）。M5 完工後別忘了校準 memory 條目 `feedback_notification_channels`：「自動化定時 / 跨 agent 推送一律群組；個人記錄 / 高頻交易事件走 DM」

---

## 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0 草稿 | 2026-05-05 | 四決策（觸發 / 報告位置 / 通道 / equity 時機）+ 任務拆解 + 驗收 + 風險（W 確認後進實作） |
