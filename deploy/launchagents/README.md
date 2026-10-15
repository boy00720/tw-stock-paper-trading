# launchd plist 納管備份

這裡是 `~/Library/LaunchAgents/com.w.*.plist` 全部 26 個 job 的版本控制副本。

## 為什麼存在

2026-06-12 發生過一次事故：`~/Library/LaunchAgents/` 下 26 個 plist 被誤操作（`plutil` 寫回源檔）全削成 JSON 數組、丟失排程與註釋，導致 launchd 重載時 `EX_CONFIG`（exit 78）失效。當時靠 launchd 記憶體裡仍存活的 job 定義（`launchctl dumpstate` 的 `calendarinterval`）+ 歷史日誌 + 對話記憶三源交叉驗證重建。

這些 plist 原本**不受任何版本控制保護**（不在 git、無 Time Machine 本地快照），所以一旦損壞、註釋無法復原。此目錄即為補上這條防線。

## 恢復方法

正式檔（`~/Library/LaunchAgents/`）若再損壞：

```bash
# 1. 從這裡複製回正式位置
cp deploy/launchagents/com.w.<job>.plist ~/Library/LaunchAgents/

# 2. 校驗合法
plutil -lint ~/Library/LaunchAgents/com.w.<job>.plist

# 3. 重載（會丟該 job 記憶體定義改從磁碟讀，確認檔案沒問題再做）
launchctl bootout gui/$UID/com.w.<job>
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.w.<job>.plist
```

緊急時若正式檔損壞但**尚未重載/重啟**，launchd 記憶體仍有定義，可先 `launchctl dumpstate | grep -A30 com.w.<job>` 搶救，不要急著 bootout。

## 註釋狀態（重要）

2026-06-12 重建時，只有 5 個 us-market job 的中文排程註釋從對話記憶救回：
`premarket-us` / `open-alert` / `intraday-early` / `intraday-late` / `overnight-intraday`。

其餘 21 個（sim 系列、ai 系列、freqtrade、tw-premarket、常駐服務等）的原始註釋**已永久丟失**，功能定義完整但無說明文字。日後若補寫註釋，提交後即永久留存於此。

## 排程一覽（重建時三源驗證）

| Job | 排程 |
|---|---|
| 常駐 KeepAlive | claude-gemini-gateway / claude-nvidia-backup-proxy / dsa.server8000 / feishu-bot |
| 週期 StartInterval | sim-advisor-dispatch 30s / automation-log-error-monitor 120s / sim-advisor-decide 300s / 00631l-live-risk-monitor 300s / official-notice-monitor 600s |
| tw-premarket | 08:15 |
| sim-morning-check | 08:55 |
| freqtrade.daily-health-check | 09:00 |
| freqtrade.forward-inspect | 09:05 |
| sim-advisor-metrics | 13:35 |
| sim-advisor-brief | 13:40 |
| ai.intraday-screener | 14:30 週一~五 |
| sim-advisor-eod-cleanup | 14:35 週一~五 |
| ai.stock-report | 16:30 週一~五 |
| sim-daily-report | 17:00 |
| sim-weekly-report | 週五 17:30 |
| us-market-premarket-us | 21:10 |
| us-market-open-alert | 21:35 |
| us-market-intraday-early | 23:30 |
| us-market-overnight-intraday | 01:30 |
| us-market-intraday-late | 03:00 |
| us-market-overnight-close | 05:10 |

> 註：`ai.stock-report` 內存記錄 `runs=0`（從未靠 launchd 自己觸發成功），排程雖為 16:30 週一~五，但此 job 是否正常運作待查。
