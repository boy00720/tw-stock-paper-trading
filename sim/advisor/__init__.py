"""automation/sim/advisor/ — Claude 操盤層（M6）

詳細規格見 ../M6_PLAN.md 與 ../REQUIREMENTS.md §13。

模組分層：
- client.py：cc-switch 跟隨模式 + Anthropic SDK 包裝
- signals.py：訊號組裝（觀察池四池 + 持倉 + 報價 + recent fills + 0050 baseline）
- decision.py：system prompt + Claude 呼叫 + JSON 解析 → Decision dataclass
- risk_guard.py：三層風控閘（單筆 ≤ 2% / 集中度 ≤ 20% / 日內熔斷 -3%）
- runner.py：執行循環 → 串 signals → decision → risk_guard → engine.submit_order
- metrics.py（M6 階段二）：四件套指標 + 0050 baseline
- daily_brief.py（M6 階段二）：純資料聚合 markdown 日報（零 LLM 依賴）
- _audit.py / _types.py：審計與型別 helper
- __main__.py：CLI（decide / metrics / brief）

階段二完成：metrics + daily_brief + launchd 部署（~/Library/LaunchAgents/com.w.sim-advisor-{decide,metrics,brief}.plist）。
"""

__version__ = "0.2.0-m6-stage2"
