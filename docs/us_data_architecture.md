# 美股研究資料架構

## 目前資料流

- IBKR：read-only 行情、期貨、全市場 scanner、歷史 K 線。
- yfinance：新聞、財報日期、分析師評級與部分基本面補充。
- SEC：10-K、10-Q、8-K、20-F、6-K；需要合法 User-Agent。
- 本機 TTL cache：避免同一股票在同一研究時段重複請求。

## 建議環境設定

```bash
# SEC 要求可識別的 User-Agent，請使用自己的聯絡信箱
export SEC_USER_AGENT="research-bot/1.0 your-email@example.com"

# 研究事件快取，預設 15 分鐘
export US_EQUITY_EVENTS_CACHE_TTL_SECONDS=900

# 快取位置；可放到持久化磁碟
export MARKET_DATA_CACHE_DIR="/Users/w/Cursor/automation/logs/cache"

# IBKR 同一 Gateway port 的跨程序互斥等待秒數
export IBKR_SESSION_LOCK_TIMEOUT=20
```

## 資料新鮮度規則

- 盤前/盤後研究：事件快取 15 分鐘即可。
- 收盤 Research Pack：可將事件快取調到 1 小時，降低限流。
- 發生突發新聞時：先刪除對應的 `logs/cache/us_equity_events_<SYMBOL>.json` 再重抓。
- SEC ticker map 每 7 天更新一次，不應在每次股票研究時重新下載。

## 安全邊界

- 所有模組仍為 read-only，不含下單 API。
- SEC 與 yfinance 失敗只會產生 warning，禁止模型把缺資料補成事實。
- `change_metric` 必須保留 `basis`、`source`、`asof` 與 `is_realtime`。
