#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
BRIDGE_LOG="$LOG_DIR/wechat_bridge.log"
DELIVERY_LOG="$LOG_DIR/wechat_delivery.jsonl"
QUEUE_JSON="$LOG_DIR/wechat_failed_queue.json"
HEALTH_URL="http://127.0.0.1:3001/health"
MARKERS_REGEX='repair_context_invalid 暫不告警|context_invalid 告警節流|repair 已在進行中，略過重複觸發'
DELIVERY_REGEX='replayed_success|replayed_error|send_context_invalid|repair_context_invalid'
MARKER_LINES="${MARKER_LINES:-20}"
DELIVERY_LINES="${DELIVERY_LINES:-20}"
QUEUE_TAIL="${QUEUE_TAIL:-5}"
MODE="${1:-full}"

section() {
    printf '\n=== %s ===\n' "$1"
}

summary_mode() {
    HEALTH_URL="$HEALTH_URL" BRIDGE_LOG="$BRIDGE_LOG" DELIVERY_LOG="$DELIVERY_LOG" QUEUE_JSON="$QUEUE_JSON" MARKERS_REGEX="$MARKERS_REGEX" DELIVERY_REGEX="$DELIVERY_REGEX" python3 - <<'PY'
import json
import os
import re
import urllib.request
from pathlib import Path

health_url = os.environ["HEALTH_URL"]
bridge_log = Path(os.environ["BRIDGE_LOG"])
delivery_log = Path(os.environ["DELIVERY_LOG"])
queue_json = Path(os.environ["QUEUE_JSON"])
markers_regex = re.compile(os.environ["MARKERS_REGEX"])
delivery_regex = re.compile(os.environ["DELIVERY_REGEX"])

ready = False
pending = None
context_invalid = None
summary_status = "unknown"
health_error = None

try:
    with urllib.request.urlopen(health_url, timeout=5) as resp:
        health = json.loads(resp.read().decode("utf-8"))
    ready = bool(health.get("ready_for_push"))
    pending = int(health.get("pending_failed_count") or 0)
    context_invalid = bool(health.get("context_invalid"))
    summary_status = health.get("summary_status") or "unknown"
except Exception as exc:
    health_error = str(exc)

queue_len = None
queue_error = None
try:
    queue = json.loads(queue_json.read_text() or "[]")
    queue_len = len(queue)
except Exception as exc:
    queue_error = str(exc)

marker_hits = 0
if bridge_log.exists():
    with bridge_log.open() as fh:
        for line in fh:
            if markers_regex.search(line):
                marker_hits += 1

recent_delivery = "none"
delivery_hits = 0
if delivery_log.exists():
    lines = delivery_log.read_text().splitlines()
    matched = [line for line in lines if delivery_regex.search(line)]
    delivery_hits = len(matched)
    if matched:
        recent_delivery = matched[-1]

issues = []
warns = []

if health_error:
    issues.append(f"health 失敗：{health_error}")
else:
    if not ready:
        issues.append(f"bridge 未 ready（summary_status={summary_status}）")
    if context_invalid:
        issues.append("context_invalid=true")
    if pending and pending > 0:
        warns.append(f"pending_failed_count={pending}")

if queue_error:
    issues.append(f"queue 解析失敗：{queue_error}")
elif queue_len and queue_len > 0:
    warns.append(f"pending_queue={queue_len}")

if marker_hits > 0:
    warns.append(f"noise markers 命中 {marker_hits} 次")

if recent_delivery != "none":
    if '"status":"replayed_error"' in recent_delivery or 'send_context_invalid' in recent_delivery or 'repair_context_invalid' in recent_delivery:
        warns.append("delivery 尾端含 error/context_invalid 事件")

if issues:
    color = "RED"
elif warns:
    color = "YELLOW"
else:
    color = "GREEN"

print(f"{color} | ready={ready} pending_failed_count={pending if pending is not None else '-'} pending_queue={queue_len if queue_len is not None else '-'} marker_hits={marker_hits} delivery_hits={delivery_hits}")
if health_error:
    print(f"- health: {health_error}")
else:
    print(f"- summary_status: {summary_status}")
    print(f"- context_invalid: {context_invalid}")
if issues:
    for item in issues:
        print(f"- issue: {item}")
if warns:
    for item in warns:
        print(f"- warn: {item}")
PY
}

if [ "$MODE" = "summary" ]; then
    summary_mode
    exit 0
fi

section "wechat health"
bash "$ROOT_DIR/wechat_health_check.sh" || true

section "noise markers"
if [ -f "$BRIDGE_LOG" ]; then
    grep -nE "$MARKERS_REGEX" "$BRIDGE_LOG" | tail -n "$MARKER_LINES" || echo "(no marker matches)"
else
    echo "missing log: $BRIDGE_LOG"
fi

section "pending queue"
if [ -f "$QUEUE_JSON" ]; then
    QUEUE_JSON="$QUEUE_JSON" QUEUE_TAIL="$QUEUE_TAIL" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["QUEUE_JSON"])
queue_tail = int(os.environ.get("QUEUE_TAIL", "5"))
try:
    data = json.loads(path.read_text() or "[]")
except Exception as exc:
    print(f"queue parse error: {exc}")
    raise SystemExit(1)

print(f"pending_queue = {len(data)}")
for item in data[-queue_tail:]:
    print(
        "- target={target} logical_id={logical_id} invalid_reason={invalid_reason} created_at={created_at}".format(
            target=item.get("target") or "-",
            logical_id=item.get("logical_id") or "-",
            invalid_reason=item.get("invalid_reason") or "-",
            created_at=item.get("created_at") or "-",
        )
    )
PY
else
    echo "missing queue file: $QUEUE_JSON"
fi

section "delivery tail"
if [ -f "$DELIVERY_LOG" ]; then
    grep -nE "$DELIVERY_REGEX" "$DELIVERY_LOG" | tail -n "$DELIVERY_LINES" || echo "(no delivery matches)"
else
    echo "missing log: $DELIVERY_LOG"
fi
