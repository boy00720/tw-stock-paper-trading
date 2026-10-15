# M1 計畫：訂單 / 持倉 / 撮合引擎（市價、限價）

> 預估：3 個工作日
> 目標：跑通「下單 → 撮合 → 持倉 → 損益」最小迴圈
> 完成標準：可以從 Python REPL 下市價單和限價單，看到持倉和損益

---

## Day 1：資料模型與配置

### 任務

- [ ] **建立 `automation/sim/__init__.py`**：模組入口
- [ ] **建立 `automation/sim/models.py`**：定義以下 dataclass / Pydantic model
  - `Order`：訂單（id、symbol、side、type、qty、price、status、created_at）
  - `Fill`：成交（order_id、price、qty、fee、tax、filled_at）
  - `Position`：持倉（symbol、qty、avg_cost、realized_pnl、unrealized_pnl）
  - `Account`：帳戶（cash、positions: dict[symbol, Position]、equity）
- [ ] **建立 `config/sim_config.yaml`**：
  ```yaml
  initial_cash: 1000000          # NTD
  slippage_pct: 0.001            # 0.1%
  match_interval_sec: 30
  fees:
    stock:
      commission_pct: 0.001425
      commission_min: 20
      tax_pct: 0.003             # 賣方
    etf:
      commission_pct: 0.001425
      commission_min: 20
      tax_pct: 0.001             # 賣方
  watchlist:                     # 撮合迴圈會掃這些
    - 2330
    - 0050
  ```
- [ ] **logs/sim/ 目錄結構初始化**：在 `automation/sim/__init__.py` 載入時 `os.makedirs(exist_ok=True)`

### 驗收

- `python3 -c "from sim.models import Order, Fill, Position, Account; print('ok')"` 通過
- `config/sim_config.yaml` 可被 PyYAML 解析
- `logs/sim/` 目錄存在

---

## Day 2：撮合引擎

### 任務

- [ ] **建立 `automation/sim/engine.py`**：
  - `class MatchEngine`
    - `__init__(config_path, account_state_path)`：載入配置、復原帳戶狀態
    - `submit_order(order: Order) -> Order`：下單（寫入 orders.jsonl，回傳含 order_id 的 Order）
    - `cancel_order(order_id: str) -> bool`：取消委託
    - `match(quotes: dict[symbol, float]) -> list[Fill]`：對未成交委託撮合，回傳本輪成交
      - 市價單：立即成交，價格 = quote × (1 ± slippage)
      - 限價單：報價優於限價才成交
      - 漲跌停外限價單：標記 invalid
    - `apply_fills(fills: list[Fill])`：更新 Position、Account、寫入 fills.jsonl
    - `snapshot() -> dict`：回傳當前帳戶狀態（cash、positions、equity）
- [ ] **手續費 / 稅金計算**：
  - `calc_fee(symbol_type, side, price, qty) -> tuple[fee, tax]`
  - 台股、ETF 規則照 REQUIREMENTS.md 第 4 節
- [ ] **持倉計算**：
  - 加倉：均價加權
  - 部分平倉：減 qty、計算已實現損益、保留剩餘均價
  - 全平倉：刪除 position

### 驗收

寫單元測試 `tests/test_engine.py`：
- 市價買 100 股 2330 @ 600 → 持倉 100 股 / 均價 600 + 滑價 + 手續費
- 限價單 580 買 → 報價 590 不成交、報價 575 成交
- 部分平倉：持有 100 股 @ 600，賣 50 股 @ 620 → 剩 50 股 @ 600，已實現 +1000 - 費用
- 全平倉：賣完，position 從 dict 移除

---

## Day 3：報價接入 + 撮合迴圈 + 整合測試

### 任務

- [ ] **建立 `automation/sim/feed.py`**：
  - `class QuoteFeed`
    - `get_quotes(symbols: list[str]) -> dict[symbol, float]`：用 yfinance 拉 latest price
    - 簡單快取（5 秒 TTL）避免短時間重複拉
    - 失敗 fallback 到上一筆已知報價
- [ ] **撮合迴圈** `automation/sim/runner.py`：
  ```python
  def run_loop(engine: MatchEngine, feed: QuoteFeed, interval=30):
      while True:
          symbols = engine.watchlist()
          quotes = feed.get_quotes(symbols)
          fills = engine.match(quotes)
          if fills:
              engine.apply_fills(fills)
          time.sleep(interval)
  ```
- [ ] **整合測試 `tests/test_e2e_smoke.py`**：
  - 下市價買 → 跑一輪 match → 確認持倉
  - 下限價買 → 跑一輪 match（不成交）→ 修改報價快取（mock）→ 跑一輪（成交）→ 確認持倉

### 驗收

- 從 Python REPL 跑：
  ```python
  from sim.engine import MatchEngine
  from sim.feed import QuoteFeed
  from sim.models import Order
  
  e = MatchEngine.from_config("config/sim_config.yaml")
  f = QuoteFeed()
  
  o = e.submit_order(Order(symbol="2330", side="BUY", type="MKT", qty=100))
  quotes = f.get_quotes(["2330"])
  fills = e.match(quotes)
  e.apply_fills(fills)
  print(e.snapshot())
  # 期待：cash 減少、positions 出現 2330
  ```

---

## M1 完成後

- 進 M2：完整訂單類型（停損 / 停利 / OCO / IFD）
- 此時還**沒有飛書 bot 接入**，純 Python 操作
- 此時還**沒有定時撮合**，runner 是 manual loop
- M5 才會自動排程（cron 或常駐 daemon）

---

## 風險與不確定點

- **yfinance 對台股代號的格式**：需驗 `2330.TW` 還是 `2330`，影響 feed.py 實作
- **yfinance rate limit**：盤中每 30 秒拉 watchlist，10 檔以下應該安全；watchlist 變大再評估
- **持倉序列化**：jsonl append + 每日 snapshot 的設計需注意「程式重啟時 cash 與 positions 怎麼復原」 → Day 1 model 設計時要先想好

---

## 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| v1.0 | 2026-05-04 | M1 初版拆解 |
