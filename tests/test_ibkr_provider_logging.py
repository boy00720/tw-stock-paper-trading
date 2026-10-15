from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_ibkr_provider_silences_ib_insync_info_logs(monkeypatch):
    """ib_insync INFO logs can include full account ids and positions."""
    fake_ib_insync = types.ModuleType("ib_insync")
    fake_ib_insync.IB = type("IB", (), {})
    fake_ib_insync.Stock = type("Stock", (), {})
    fake_ib_insync.ContFuture = type("ContFuture", (), {})
    fake_ib_insync.ScannerSubscription = type("ScannerSubscription", (), {})
    monkeypatch.setitem(sys.modules, "ib_insync", fake_ib_insync)

    original_module = sys.modules.pop("utils.ibkr_provider", None)
    logger_names = (
        "ib_insync",
        "ib_insync.client",
        "ib_insync.ib",
        "ib_insync.wrapper",
    )
    for name in logger_names:
        logging.getLogger(name).setLevel(logging.INFO)

    try:
        module = importlib.import_module("utils.ibkr_provider")
        for name in module._IBKR_LOGGER_NAMES:
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        sys.modules.pop("utils.ibkr_provider", None)
        # 重 import 會在 utils 套件留下 stale 的 ibkr_provider 屬性（指向 fake-bound
        # 模組）。只還原 sys.modules 不夠——後續測試的 monkeypatch 會透過套件屬性
        # patch 到 fake 模組、與實際走 sys.modules 的 import 不一致而落空（誤真連）。
        # 故套件屬性也一併還原/清除。
        import utils as _utils_pkg
        if original_module is not None:
            sys.modules["utils.ibkr_provider"] = original_module
            _utils_pkg.ibkr_provider = original_module
        elif hasattr(_utils_pkg, "ibkr_provider"):
            del _utils_pkg.ibkr_provider
