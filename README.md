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

## 專案定位與開源來源
- 本專案是個人維護的台股自動化分析系統，用於台股市場的數據分析與**模擬交易**（不連接真實下單）。
- 台股適配：數據源以 FinMind 等台股資料為主（如 `utils/finmind_helper.py`），並針對台股調整了選股／監控策略與輸出（如 `scripts/strategy_scanner.py`、`scripts/stock_screener.py`）。模擬盤位於 `sim/`。
- 開源來源：大盤複盤步驟透過子進程呼叫同層目錄的開源專案 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)（MIT 授權，見 `orchestrator.py`）；依 `sim/REQUIREMENTS.md` 的設計，模擬盤的部分數據抓取邏輯也參考自該專案。這些部分的原創工作屬於原作者。

## 依賴安全維護
定期以 [pip-audit](https://github.com/pypa/pip-audit) 掃描依賴的已知漏洞（PyPI / OSV 資料庫），並調高 `requirements.txt` 的版本下限，避免新安裝的環境裝到已知有漏洞的版本。

```bash
# 1) 掃描目前運行環境（launchd 任務使用的直譯器）
~/.homebrew/bin/python3 -m pip install -U pip-audit
~/.homebrew/bin/python3 -m pip_audit

# 2) 依 requirements.txt 的新下限升級
~/.homebrew/bin/python3 -m pip install -U -r requirements.txt

# 3) 升級後跑離線測試，確認台股抓取、策略與模擬盤邏輯不受影響
~/.homebrew/bin/python3 -m pytest tests sim/tests -m "not network"
```

維護原則：
- 只調整依賴版本下限，不順手改業務代碼；可能跨大版本的套件（torch、transformers、sentence-transformers）同時設上限 `<下一大版本`，避免意外破壞。
- 每次更新為獨立提交，提交訊息以 `chore(deps):` 開頭，寫明涉及的套件與驗證方式。
- 已知未修復：`chromadb`（記憶管家 W 使用）截至 2026-10-15 仍有已知漏洞，上游尚無修復版，暫時保留並持續追蹤。

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
