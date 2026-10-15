# 自動化功能架構

## 目錄結構
```
automation/
├── scripts/           # 自動化腳本
│   ├── news_crawler.py   # 新聞爬蟲與推送
│   ├── daily_report.py   # 日報生成與推送
│   ├── stock_monitor.py  # 股市監控
│   ├── ecommerce.py      # 電商管理
│   └── reflection.py     # 反思導入
├── config/             # 配置文件
│   ├── news_sources.yaml # 新聞源配置
│   ├── stocks.yaml      # 股票配置
│   └── lark_config.yaml # 飛書配置
├── templates/          # 模板文件
├── logs/               # 日誌文件
└── utils/              # 工具函數
```

## 核心功能
1. **新聞資訊自動化**：自動爬取、整理、推送新聞
2. **日報自動化**：每日日誌生成與存儲
3. **股市監控**：實時股價監控與風險提醒
4. **電商管理**：平台數據同步與分析
5. **反思導入**：自動化思想錄引導

## 技術架構
- Python 3.8+
- 依賴管理：pipenv 或 poetry
- 日誌系統：structlog
- 配置管理：PyYAML
- 任務調度：APScheduler
- 通知機制：HTTP API

## 安裝說明
```bash
pip install -r requirements.txt
```

## 使用方法
建議統一走 `orchestrator.py`，避免舊腳本入口行為不一致。

```bash
cd /Users/w/Cursor/automation

# 單模式
python3 orchestrator.py --mode morning

# 全架構（晨間、盤後、晚間、週一、週日）
python3 orchestrator.py --mode all
```

## Claude 設定同步（一鍵）
跟隨你在 Cursor / Claude Code 當前選擇，同步以下三項到 `.env`：
- `ANTHROPIC_MODEL`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`

```bash
cd /Users/w/Cursor/automation
./sync_cursor_model.sh
```

手動指定模型（例如額度切換時）：

```bash
cd /Users/w/Cursor/automation
./sync_cursor_model.sh claude-sonnet-4-6
```

手動指定模型 + 路由 + 金鑰：

```bash
cd /Users/w/Cursor/automation
./sync_cursor_model.sh claude-sonnet-4-6 https://api.anthropic.com sk-ant-xxxx
```

## 擴展功能
- 支持多種通知渠道
- 可定製的數據處理流程
- 模塊化設計，易於維護和擴展
- 完善的錯誤處理和日誌記錄

## 下一步
1. 建立具體的腳本實現
2. 設定定時任務
3. 實現通知機制
4. 添加測試用例和文檔
