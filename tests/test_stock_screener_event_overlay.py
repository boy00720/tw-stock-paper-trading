from pathlib import Path

from scripts.stock_screener import compute_event_overlay, load_institutional_snapshot


def test_compute_event_overlay_applies_sector_penalty_and_greed_penalty():
    risk_radar = {
        "sector_risks": {"半導體": "出口管制風險升高"},
        "sentiment_hint": "greed",
        "blacklist": [],
    }
    rules = {
        "event_overlay": {
            "enabled": True,
            "momentum": {
                "catalyst_bonus": 6,
                "sector_risk_penalty": 4,
                "fear_bonus": 0,
                "greed_penalty": 3,
            }
        }
    }
    inst = {"total_net": 3500}

    score, warning, note = compute_event_overlay(
        symbol="2330.TW",
        sector="半導體",
        inst=inst,
        risk_radar=risk_radar,
        rules=rules,
        engine="momentum",
    )

    assert score == -1
    assert "出口管制風險升高" in warning
    assert "法人強買 +6" in note
    assert "產業風險 -4" in note
    assert "情緒貪婪 -3" in note


def test_compute_event_overlay_applies_fear_bonus_to_rebound():
    risk_radar = {
        "sector_risks": {},
        "sentiment_hint": "fear",
        "blacklist": [],
    }
    rules = {
        "event_overlay": {
            "enabled": True,
            "rebound": {
                "catalyst_bonus": 3,
                "sector_risk_penalty": 2,
                "fear_bonus": 5,
                "greed_penalty": 0,
            }
        }
    }
    inst = {"total_net": 1200}

    score, warning, note = compute_event_overlay(
        symbol="2317.TW",
        sector="電子",
        inst=inst,
        risk_radar=risk_radar,
        rules=rules,
        engine="rebound",
    )

    assert score == 5
    assert warning == ""
    assert "情緒恐慌 +5" in note
    assert "法人強買 +3" not in note


def test_load_institutional_snapshot_reads_current_json(tmp_path, monkeypatch):
    current = tmp_path / "logs" / "institutional_snapshots"
    current.mkdir(parents=True)
    payload = {
        "stocks": {
            "2330.TW": {"total_net": 1234},
            "2317.TW": {"total_net": -4321},
        }
    }
    (current / "current.json").write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")

    import scripts.stock_screener as stock_screener

    monkeypatch.setattr(stock_screener, "_BASE_DIR", tmp_path)
    data = load_institutional_snapshot()

    assert data["2330.TW"]["total_net"] == 1234
    assert data["2317.TW"]["total_net"] == -4321
