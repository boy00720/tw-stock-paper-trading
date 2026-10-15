"""
automation/sim/ — 模擬盤模組

詳細規格見 REQUIREMENTS.md，工作拆解見 M1_PLAN.md。

模組於 import 時自動建立必要的 logs/sim/ 目錄結構，
避免後續寫 jsonl 時出現 FileNotFoundError。
"""

from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
_LOGS_SIM_DIR = _BASE_DIR / "logs" / "sim"
_LOGS_SIM_POSITIONS_DIR = _LOGS_SIM_DIR / "positions"

# 確保 logs/sim/ 與 logs/sim/positions/ 存在
for _d in (_LOGS_SIM_DIR, _LOGS_SIM_POSITIONS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

__version__ = "1.0.0-m1-day1"
