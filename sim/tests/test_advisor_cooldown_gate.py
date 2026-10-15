"""sim/tests/test_advisor_cooldown_gate.py — 過度交易防火牆 cooldown_gate 驗證

驗證 `_gate_recent_cooldown`：
- 60 分內有 SELL → buy 降級 hold（cooldown_after_sell）
- 30 分內有 BUY → buy 降級 hold（cooldown_between_buys）
- 超過冷卻期 → 放行
- sell / hold 一律不動
- 缺 fills_path / 解析失敗 → fail-open（不擋）
- 多個 fills 取最新一筆比對
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor._types import Decision
from sim.advisor.runner import (
    _COOLDOWN_AFTER_SELL_MINUTES,
    _COOLDOWN_BETWEEN_BUYS_MINUTES,
    _gate_recent_cooldown,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _write_fills(path: Path, fills: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in fills:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _buy(sym: str, qty: int = 1000, score_hint: str = "") -> Decision:
    return Decision(
        action="buy", symbol=sym, qty=qty, type="MKT",
        stop_loss=95.0, reason=f"test buy {score_hint}",
    )


# ─── cooldown_after_sell ───

def test_buy_blocked_30min_after_sell(tmp_dir):
    now = datetime(2026, 5, 14, 10, 0)
    sell_at = now - timedelta(minutes=30)
    fills = [{
        "order_id": "ord-1", "symbol": "9921.TW", "side": "SELL",
        "qty": 1000, "price": 75.0, "filled_at": sell_at.isoformat(),
    }]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    out, gated = _gate_recent_cooldown([_buy("9921.TW")], p, now=now)
    assert out[0].action == "hold"
    assert "cooldown_after_sell" in out[0].reason
    assert len(gated) == 1
    assert gated[0]["rule"] == "cooldown_after_sell"
    assert gated[0]["elapsed_minutes"] == 30.0
    assert gated[0]["cooldown_minutes"] == _COOLDOWN_AFTER_SELL_MINUTES


def test_buy_passes_61min_after_sell(tmp_dir):
    """超過 60 分鐘 cooldown，buy 放行。"""
    now = datetime(2026, 5, 14, 10, 0)
    sell_at = now - timedelta(minutes=61)
    fills = [{
        "symbol": "9921.TW", "side": "SELL",
        "qty": 1000, "price": 75.0, "filled_at": sell_at.isoformat(),
    }]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    out, gated = _gate_recent_cooldown([_buy("9921.TW")], p, now=now)
    assert out[0].action == "buy"
    assert gated == []


# ─── cooldown_between_buys ───

def test_buy_blocked_10min_after_buy(tmp_dir):
    now = datetime(2026, 5, 14, 10, 0)
    buy_at = now - timedelta(minutes=10)
    fills = [{
        "symbol": "3044.TW", "side": "BUY",
        "qty": 100, "price": 529.5, "filled_at": buy_at.isoformat(),
    }]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    out, gated = _gate_recent_cooldown([_buy("3044.TW", qty=100)], p, now=now)
    assert out[0].action == "hold"
    assert "cooldown_between_buys" in out[0].reason
    assert gated[0]["rule"] == "cooldown_between_buys"


def test_buy_passes_31min_after_buy(tmp_dir):
    now = datetime(2026, 5, 14, 10, 0)
    buy_at = now - timedelta(minutes=31)
    fills = [{
        "symbol": "3044.TW", "side": "BUY",
        "qty": 100, "price": 529.5, "filled_at": buy_at.isoformat(),
    }]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    out, gated = _gate_recent_cooldown([_buy("3044.TW", qty=100)], p, now=now)
    assert out[0].action == "buy"
    assert gated == []


# ─── 優先順序與互動 ───

def test_recent_sell_outranks_old_buy(tmp_dir):
    """若 10 分前有 SELL、35 分前有 BUY，cooldown_after_sell 優先（雖然 buy cooldown 已過）。"""
    now = datetime(2026, 5, 14, 10, 0)
    fills = [
        {"symbol": "6415.TW", "side": "BUY", "qty": 100, "price": 510.0,
         "filled_at": (now - timedelta(minutes=35)).isoformat()},
        {"symbol": "6415.TW", "side": "SELL", "qty": 100, "price": 494.0,
         "filled_at": (now - timedelta(minutes=10)).isoformat()},
    ]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    out, gated = _gate_recent_cooldown([_buy("6415.TW", qty=100)], p, now=now)
    assert out[0].action == "hold"
    assert gated[0]["rule"] == "cooldown_after_sell"


def test_sell_decision_untouched_even_in_cooldown(tmp_dir):
    """cooldown 只 gate buy；同檔 sell 不受影響。"""
    now = datetime(2026, 5, 14, 10, 0)
    fills = [{"symbol": "9921.TW", "side": "SELL", "qty": 1000, "price": 75.0,
              "filled_at": (now - timedelta(minutes=10)).isoformat()}]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    decs = [
        Decision(action="sell", symbol="9921.TW", qty=1000, type="MKT"),
        Decision(action="hold", reason="watching"),
    ]
    out, gated = _gate_recent_cooldown(decs, p, now=now)
    assert [d.action for d in out] == ["sell", "hold"]
    assert gated == []


def test_different_symbol_not_blocked(tmp_dir):
    now = datetime(2026, 5, 14, 10, 0)
    fills = [{"symbol": "9921.TW", "side": "SELL", "qty": 1000, "price": 75.0,
              "filled_at": (now - timedelta(minutes=10)).isoformat()}]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    out, gated = _gate_recent_cooldown([_buy("3034.TW")], p, now=now)
    assert out[0].action == "buy"
    assert gated == []


# ─── fail-open ───

def test_no_fills_path_passes_through(tmp_dir):
    out, gated = _gate_recent_cooldown([_buy("9921.TW")], None, now=datetime.now())
    assert out[0].action == "buy"
    assert gated == []


def test_missing_fills_file_passes_through(tmp_dir):
    p = tmp_dir / "does_not_exist.jsonl"
    out, gated = _gate_recent_cooldown([_buy("9921.TW")], p, now=datetime.now())
    assert out[0].action == "buy"
    assert gated == []


def test_corrupt_fills_lines_skipped(tmp_dir):
    p = tmp_dir / "fills.jsonl"
    p.write_text("{ not valid json\n{\"foo\":\"no_filled_at\"}\n", encoding="utf-8")
    out, gated = _gate_recent_cooldown([_buy("9921.TW")], p, now=datetime.now())
    assert out[0].action == "buy"  # fail-open，不擋
    assert gated == []


def test_old_fills_outside_window_ignored(tmp_dir):
    """超過 max(after_sell, between_buys) window 的 fills 完全忽略。"""
    now = datetime(2026, 5, 14, 10, 0)
    fills = [{
        "symbol": "9921.TW", "side": "SELL", "qty": 1000, "price": 75.0,
        "filled_at": (now - timedelta(hours=3)).isoformat(),
    }]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)
    out, gated = _gate_recent_cooldown([_buy("9921.TW")], p, now=now)
    assert out[0].action == "buy"
    assert gated == []


# ─── 多筆 fills 取最新 ───

def test_latest_sell_wins_when_multiple(tmp_dir):
    """同檔多筆 SELL，取最新一筆計算 elapsed。"""
    now = datetime(2026, 5, 14, 10, 0)
    fills = [
        # 舊 SELL（55 分前）+ 新 SELL（5 分前）
        {"symbol": "9921.TW", "side": "SELL", "qty": 500, "price": 75.0,
         "filled_at": (now - timedelta(minutes=55)).isoformat()},
        {"symbol": "9921.TW", "side": "SELL", "qty": 500, "price": 74.9,
         "filled_at": (now - timedelta(minutes=5)).isoformat()},
    ]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)
    out, gated = _gate_recent_cooldown([_buy("9921.TW")], p, now=now)
    assert out[0].action == "hold"
    assert gated[0]["elapsed_minutes"] == 5.0  # 最新一筆


def test_mixed_batch(tmp_dir):
    """同輪三筆 buy：一檔被 sell cooldown 擋、一檔被 buy cooldown 擋、一檔放行。"""
    now = datetime(2026, 5, 14, 10, 0)
    fills = [
        {"symbol": "9921.TW", "side": "SELL", "qty": 1000, "price": 75.0,
         "filled_at": (now - timedelta(minutes=20)).isoformat()},
        {"symbol": "3044.TW", "side": "BUY", "qty": 100, "price": 530.0,
         "filled_at": (now - timedelta(minutes=15)).isoformat()},
        # 3481 沒最近 fills → 放行
    ]
    p = tmp_dir / "fills.jsonl"
    _write_fills(p, fills)

    decs = [_buy("9921.TW"), _buy("3044.TW", qty=100), _buy("3481.TW")]
    out, gated = _gate_recent_cooldown(decs, p, now=now)
    assert [d.action for d in out] == ["hold", "hold", "buy"]
    assert {g["symbol"] for g in gated} == {"9921.TW", "3044.TW"}
    rules = {g["symbol"]: g["rule"] for g in gated}
    assert rules["9921.TW"] == "cooldown_after_sell"
    assert rules["3044.TW"] == "cooldown_between_buys"
