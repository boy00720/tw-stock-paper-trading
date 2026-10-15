# Stock Screener 短線選股子代理

> 建置日期：2026-04-10  
> 狀態：Shadow Mode（測試中）

## 概述

股市 W 的分支代理，專注於 3-7 日波段的短線選股。每日盤後從 150+ 檔高流動性候選池中，用雙引擎評分篩出：

- **量價突破 10 檔**（右側交易）
- **均值回歸 10 檔**（左側交易）

## 檔案結構

```text
automation/
├── scripts/
│   ├── stock_screener.py      # 主程式
│   └── screener_review.py     # 週五復盤（待建置）
├── config/
│   ├── stock_universe.yaml    # 候選池（~150 檔）
│   ├── screener_rules.yaml    # 評分權重、門檻、停損規則
│   └── risk_keywords.yaml     # 風險關鍵字
├── logs/
│   ├── screener/              # 每日輸出
│   │   ├── YYYY-MM-DD.json    # 結構化候選清單
│   │   └── YYYY-MM-DD_narrative.md  # Claude 敘事
│   ├── positions/
│   │   └── holdings.yaml      # 追蹤中部位（待建置）
│   ├── screener_review/
│   │   └── YYYY-WXX.md        # 週五復盤報告
│   └── risk_radar.json        # News W 產出的風險雷達
```

## 雙引擎評分

### 量價突破引擎 — 總分 100

| 指標 | 權重 | 說明 |
|-----|-----|-----|
| ret_5d | 20 | 近 5 日報酬率 |
| vol_ratio | 15 | 當日量 / 20 日均量 ≥ 1.5 |
| break_20dh | 15 | 突破 20 日新高 |
| ma_stack | 10 | MA5 > MA10 > MA20 多頭排列 |
| rsi_5 | 10 | RSI(5) 落在 55-80 |
| macd_hist | 10 | MACD 柱狀轉正或擴張 |
| inst_buy | 15 | 三大法人近 5 日同買 |
| sector_rps | 5 | 類股相對強度 |

**入選門檻**：≥ 65 分（情緒過熱時 +10）

### 均值回歸引擎 — 總分 100

| 指標 | 權重 | 說明 |
|-----|-----|-----|
| neg_bias_20 | 20 | 負乖離率 ≤ -8% |
| rsi_5_oversold | 15 | RSI(5) < 30 且上翹 |
| bullish_reversal | 15 | 長下影 / 吞噬 / 晨星 |
| vol_shrink_then_up | 10 | 量縮打底後出量 |
| inst_stop_selling | 15 | 法人止跌轉買 |
| distance_to_ma60 | 10 | 仍在 MA60 之上 |
| support_bounce | 10 | 接近支撐位 |
| sentiment_bonus | 5 | 大盤極度恐慌時加分 |

**入選門檻**：≥ 60 分（情緒恐慌時 -5）

## 三維過濾

### 1. 三大法人（FinMind）

```python
institutional:
  foreign_weight: 0.5
  trust_weight: 0.3
  dealer_weight: 0.2
  strong_buy_threshold_shares: 1000
  consecutive_days: 3
```

### 2. 市場情緒（0-100，高=恐慌）

來源：VIX、DXY、TWD=X、^TNX、^TWII 量能

| 分數 | Regime | 影響 |
|-----|--------|-----|
| ≥80 | extreme_fear | 均值回歸加分、量價突破減碼 |
| 60-79 | fear | 稍微保守 |
| 40-59 | neutral | 正常 |
| 20-39 | greed | 稍微積極 |
| <20 | extreme_greed | 量價突破觀望 |

### 3. 國際風險雷達

由 `news_crawler.py` 在每日爬蟲後用 Claude 抽取，存入 `logs/risk_radar.json`：

```json
{
  "international": ["關稅", "Fed"],
  "sector_risks": {"半導體": "..."},
  "blacklist": ["2317.TW"],
  "sentiment_hint": "fear",
  "summary": "..."
}
```

## 停損 / 停利 / 時間停損

| 引擎 | 停損 | 停利 | 時間停損 |
|-----|-----|-----|---------|
| 量價突破 | -5% | +10% | 7 日 |
| 均值回歸 | -4% | +7% | 5 日 |

## CLI 用法

```bash
# 盤後掃描（完整流程）
python3 scripts/stock_screener.py

# 乾跑（不推播）
python3 scripts/stock_screener.py --dry-run

# 單檔評分除錯
python3 scripts/stock_screener.py --symbol 2330.TW

# 只看情緒分數
python3 scripts/stock_screener.py --debug-sentiment
```

## Orchestrator 整合

### 每日盤後（自動）

`run_afternoon()` 末尾會自動觸發 `stock_screener.run_screener()`。

### 週五復盤（手動或排程）

```bash
python3 orchestrator.py --mode weekly_fri
```

## 記憶整合

`memory_agent.py` 已擴充：

- 每日吸收 `logs/screener/{date}_narrative.md` → source: `screener`
- 週五吸收 `logs/screener_review/{year}-W{week}.md` → source: `screener_review`

微信 RAG 可查詢：「上週推薦的量價突破標的表現如何」

## 驗證 Checklist

- [ ] `python3 scripts/stock_screener.py --debug-sentiment` 正常顯示情緒分數
- [ ] `python3 scripts/stock_screener.py --symbol 2330.TW` 正常顯示評分明細
- [ ] `python3 scripts/stock_screener.py --dry-run` 產出 `logs/screener/{today}.json`
- [ ] `python3 scripts/news_crawler.py` 產出 `logs/risk_radar.json`
- [ ] 連續 5 交易日 dry-run，人工檢視噪音
- [ ] 4 週 shadow mode 後統計勝率 ≥ 55%

## 變更記錄

| 日期 | 變更 |
|-----|-----|
| 2026-04-10 | 初版建置：stock_screener.py + 三個設定檔 + orchestrator 整合 |
