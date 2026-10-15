import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.log_error_monitor import run_once


def _write_config(tmp_path, log_path, *, baseline=True, cooldown=3600, ignore_patterns=None):
    config = {
        "enabled": True,
        "baseline_on_first_run": baseline,
        "cooldown_seconds": cooldown,
        "max_events_per_run": 10,
        "max_bytes_per_file": 65536,
        "context_before": 1,
        "context_after": 1,
        "notify": {"enabled": False},
        "files": [str(log_path)],
        "ignore_patterns": ignore_patterns or [],
        "rules": [
            {
                "id": "same-price-trail",
                "severity": "warning",
                "pattern": r"trail raised: stop\s+([0-9.]+)\s*(?:→|->)\s*\1\b",
                "tags": ["sim", "regression"],
            },
            {"id": "traceback", "severity": "critical", "pattern": r"Traceback \(most recent call last\)"},
            {"id": "error", "severity": "error", "pattern": r"\bERROR\b|\[ERROR\]"},
        ],
    }
    cfg_path = tmp_path / "log_error_monitor.yaml"
    cfg_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return cfg_path


def _audit_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_first_run_baselines_without_reporting_old_errors(tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text("2026-06-04 ERROR old failure\n", encoding="utf-8")
    cfg = _write_config(tmp_path, log_path, baseline=True)
    state = tmp_path / "state.json"
    audit = tmp_path / "audit.jsonl"

    result = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)

    assert result["matched"] == 0
    assert result["selected"] == 0
    assert _audit_lines(audit) == []

    with log_path.open("a", encoding="utf-8") as f:
        f.write("2026-06-04 ERROR new failure\n")

    result2 = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)

    assert result2["matched"] == 1
    assert result2["selected"] == 1
    lines = _audit_lines(audit)
    assert len(lines) == 1
    assert lines[0]["rule_id"] == "error"
    assert "new failure" in lines[0]["line"]


def test_dedupes_same_fingerprint_in_one_run(tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "2026-06-04 10:00:00 ERROR repeated failure id=abc123\n"
        "2026-06-04 10:00:01 ERROR repeated failure id=abc123\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, log_path, baseline=False)
    state = tmp_path / "state.json"
    audit = tmp_path / "audit.jsonl"

    result = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)

    assert result["matched"] == 2
    assert result["selected"] == 1
    assert result["suppressed"] == 1
    assert len(_audit_lines(audit)) == 1


def test_known_sim_same_price_trail_pattern(tmp_path):
    log_path = tmp_path / "dispatch.err.log"
    log_path.write_text(
        "11:00:25 [INFO] sim.advisor.auto_trail: [auto_trail] "
        "2891.TW trail raised: stop 63.62 → 63.62 (gain=+16.9%)\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, log_path, baseline=False)
    state = tmp_path / "state.json"
    audit = tmp_path / "audit.jsonl"

    result = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)

    assert result["matched"] == 1
    lines = _audit_lines(audit)
    assert lines[0]["rule_id"] == "same-price-trail"
    assert "regression" in lines[0]["tags"]


def test_cooldown_suppresses_repeated_fingerprint_across_runs(tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text("ERROR same failure\n", encoding="utf-8")
    cfg = _write_config(tmp_path, log_path, baseline=False, cooldown=3600)
    state = tmp_path / "state.json"
    audit = tmp_path / "audit.jsonl"

    first = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write("ERROR same failure\n")
    second = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)

    assert first["selected"] == 1
    assert second["matched"] == 1
    assert second["selected"] == 0
    assert second["suppressed"] == 1
    assert len(_audit_lines(audit)) == 1


def test_ignore_patterns_skip_yfinance_noise(tmp_path):
    """yfinance 下載失敗噪音被 ignore_patterns 吞掉；不帶 ticker 的真錯誤仍保留。"""
    log_path = tmp_path / "orchestrator.log"
    log_path.write_text(
        "2026-06-05 16:50:46 [ERROR] ['1303.TW']: TypeError(\"'NoneType' object is not subscriptable\")\n"
        "2026-06-05 16:50:47 [ERROR] 1 Failed download:\n"
        "2026-06-05 16:50:48 [ERROR] real unrelated failure\n",
        encoding="utf-8",
    )
    cfg = _write_config(
        tmp_path, log_path, baseline=False,
        ignore_patterns=[r"\[.*\.(TW|TWO|HK).*\].*not subscriptable", r"Failed download"],
    )
    state = tmp_path / "state.json"
    audit = tmp_path / "audit.jsonl"

    result = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)

    assert result["ignored"] == 2
    assert result["matched"] == 1  # 只剩「real unrelated failure」
    lines = _audit_lines(audit)
    assert len(lines) == 1
    assert "real unrelated failure" in lines[0]["line"]


def test_ticker_normalized_dedupes_batch_symbol_errors(tmp_path):
    """不同 symbol 的同類錯誤歸成同指紋，一輪內只推一次（防批量爆指紋洗版）。"""
    log_path = tmp_path / "orchestrator.log"
    log_path.write_text(
        "2026-06-05 16:50:46 [ERROR] fetch 1303.TW done with status fail\n"
        "2026-06-05 16:50:47 [ERROR] fetch 5880.TW done with status fail\n"
        "2026-06-05 16:50:48 [ERROR] fetch 2880.TW done with status fail\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, log_path, baseline=False)
    state = tmp_path / "state.json"
    audit = tmp_path / "audit.jsonl"

    result = run_once(config_path=cfg, state_path=state, audit_path=audit, no_send=True)

    assert result["matched"] == 3
    assert result["selected"] == 1
    assert result["suppressed"] == 2
    assert len(_audit_lines(audit)) == 1
