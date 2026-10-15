from utils.ttl_cache import read_json, write_json


def test_ttl_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_CACHE_DIR", str(tmp_path))
    write_json("sample", {"ok": True})
    assert read_json("sample", ttl_seconds=60) == {"ok": True}


def test_ttl_cache_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_CACHE_DIR", str(tmp_path))
    assert read_json("missing", ttl_seconds=60) is None
