import pytest


@pytest.fixture(autouse=True)
def _isolate_production_flags(monkeypatch):
    """測試隔離：清掉生產 .env 注入的功能 flag。

    多支腳本在 import 時 `load_dotenv()`，會把 `automation/.env` 的 flag 灌進
    `os.environ`。一旦生產啟用（如 `OVERNIGHT_FUTURES_ENABLED=1`），未明確 mock
    取數的 `run()` 類測試就會真連 IBKR Gateway、拖慢並依賴外部服務。

    這裡在每個測試前統一清除；需要啟用的測試自行 `monkeypatch.setenv` 覆蓋。
    """
    for key in (
        "OVERNIGHT_FUTURES_ENABLED",
        "RESEARCH_PACK_US_CLOSE_ENABLED",
        "US_STOCK_SCREENER_ENABLED",
        "US_STOCK_SCREENER_CARD_ENABLED",
        "US_FIVE_FACE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
