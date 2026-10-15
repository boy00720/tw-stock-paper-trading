#!/usr/bin/env python3
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import urlopen

ROOT = Path("/Users/w/Cursor/automation")
DELIVERY_LOG = ROOT / "logs" / "wechat_delivery.jsonl"
HEALTH_URL = "http://127.0.0.1:3001/health"


def fetch_health():
    with urlopen(HEALTH_URL, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_recent_events(limit=400):
    if not DELIVERY_LOG.exists():
        return []
    lines = DELIVERY_LOG.read_text(encoding="utf-8").splitlines()
    events = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def latest_matching(events, predicate):
    for event in reversed(events):
        if predicate(event):
            return event
    return None


def ok(label, detail):
    print(f"[OK] {label}：{detail}")


def warn(label, detail):
    print(f"[WARN] {label}：{detail}")


def fail(label, detail):
    print(f"[FAIL] {label}：{detail}")


def find_replay_for_queue(events, queue_id, target):
    if not queue_id:
        return None
    return latest_matching(
        events,
        lambda e: e.get("status") == "replayed_success"
        and e.get("target") == target
        and e.get("queue_id") == queue_id,
    )


def _is_news_event(event, target):
    if event.get("target") != target:
        return False
    meta = event.get("meta") or {}
    if meta.get("category") == "economy":
        return True
    if event.get("category") == "economy":
        return True
    sequence_total = meta.get("sequence_total") if isinstance(meta, dict) else None
    return bool(sequence_total)


def latest_news_sequence_events(events, target):
    news_events = [
        event for event in events
        if event.get("status") in {"success", "replayed_success"} and _is_news_event(event, target)
    ]
    if not news_events:
        return []

    def _event_meta(event):
        meta = event.get("meta") or {}
        return meta if isinstance(meta, dict) else {}

    def _event_time(event):
        raw = str(event.get("time") or event.get("timestamp") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    latest_event = max(news_events, key=lambda event: str(event.get("time") or event.get("timestamp") or ""))
    latest_meta = _event_meta(latest_event)
    expected_total = int(latest_meta.get("sequence_total") or latest_event.get("news_sequence_total") or 0)
    latest_category = latest_meta.get("category") or latest_event.get("category")
    latest_agent = latest_meta.get("agent") or ""
    latest_time = _event_time(latest_event)

    if expected_total <= 0:
        return [latest_event]

    collected = {}
    for event in reversed(news_events):
        meta = _event_meta(event)
        seq_total = int(meta.get("sequence_total") or event.get("news_sequence_total") or 0)
        seq_index = int(meta.get("sequence_index") or event.get("news_sequence_index") or 0)
        category = meta.get("category") or event.get("category")
        agent = meta.get("agent") or ""

        if seq_total != expected_total or seq_index <= 0:
            continue
        if latest_category and category != latest_category:
            continue
        if latest_agent and agent and agent != latest_agent:
            continue

        event_time = _event_time(event)
        if latest_time and event_time:
            if abs((latest_time - event_time).total_seconds()) > 600:
                continue

        collected.setdefault(seq_index, event)
        if len(collected) >= expected_total:
            break

    latest_group = [collected[idx] for idx in sorted(collected)]
    return latest_group


def audit_news_sequence(events, target):
    latest_group = latest_news_sequence_events(events, target)
    if not latest_group:
        return "warn", "最近審計中沒有新聞 W logical message"

    expected_total = int((latest_group[0].get("meta") or {}).get("sequence_total") or 0)
    seen_indexes = []
    missing_manifest = []
    missing_details = []

    for event in latest_group:
        meta = event.get("meta") or {}
        seq_index = int(meta.get("sequence_index") or 0)
        seen_indexes.append(seq_index)
        chunk_count = event.get("chunk_count")
        details = event.get("details") or []
        if not event.get("logical_id") or chunk_count in (None, ""):
            missing_manifest.append(seq_index)
        if not details:
            missing_details.append(seq_index)

    missing_indexes = [idx for idx in range(1, expected_total + 1) if idx not in seen_indexes] if expected_total else []
    if missing_indexes:
        return "fail", f"新聞 W 序列缺號：missing={missing_indexes} seen={seen_indexes}/{expected_total}"
    if missing_manifest:
        return "fail", f"新聞 W manifest 缺欄位：sequence={missing_manifest}"
    if missing_details:
        return "warn", f"新聞 W sender ack 沒有 chunk details：sequence={missing_details}"
    return "ok", f"新聞 W 序列完整：seen={seen_indexes}/{expected_total}"


def main():
    print("微信 Bridge 一鍵自測")
    try:
        health = fetch_health()
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        fail("/health", f"無法連線：{exc}")
        return 2

    events = read_recent_events()
    target = health.get("target_user")
    ready = bool(health.get("ready_for_push"))
    summary_status = health.get("summary_status") or "unknown"
    recommended_action = health.get("recommended_action") or "none"
    pending = int(health.get("pending_failed_count") or 0)

    ok("/health", f"summary_status={summary_status}, recommended_action={recommended_action}")

    if ready:
        ok("推送狀態", "目前可直接推送")
    else:
        warn("推送狀態", f"目前未 ready，建議動作={recommended_action}")

    if target:
        ok("目標用戶", target)
    else:
        fail("目標用戶", "未設定 target_user")
        return 1

    direct = health.get("latest_direct_success") or {}
    replay = health.get("latest_replayed_success") or {}
    refresh = health.get("last_context_refresh") or {}

    if direct.get("trace_id"):
        ok("最近直接送達", f"trace_id={direct.get('trace_id')}, time={direct.get('time')}")
    else:
        warn("最近直接送達", "尚未找到 direct success")

    if refresh.get("updated_at"):
        ok("最近 context 刷新", f"updated_at={refresh.get('updated_at')}")
    else:
        warn("最近 context 刷新", "尚未找到 context refresh")

    if replay.get("queue_id"):
        ok("最近補發成功", f"queue_id={replay.get('queue_id')}, trigger={replay.get('trigger_source')}")
    else:
        warn("最近補發成功", "尚未找到 replayed_success")

    latest_direct_event = latest_matching(
        events,
        lambda e: e.get("status") == "success"
        and e.get("source") != "chat_reply"
        and e.get("target") == target,
    )
    latest_queue_event = latest_matching(
        events,
        lambda e: e.get("status") in {"queued_for_repair", "queued_for_repair_duplicate"}
        and e.get("target") == target
        and e.get("queue_id"),
    )
    latest_waiting_context_event = latest_matching(
        events,
        lambda e: e.get("status") == "queued_waiting_context" and e.get("target") == target,
    )
    latest_replay_event = latest_matching(
        events,
        lambda e: e.get("status") == "replayed_success" and e.get("target") == target,
    )
    matched_replay_event = find_replay_for_queue(events, latest_queue_event.get("queue_id") if latest_queue_event else None, target)

    if latest_direct_event:
        ok("審計 direct success", f"trace_id={latest_direct_event.get('trace_id')}, text_len={latest_direct_event.get('text_len')}")
    else:
        warn("審計 direct success", "最近審計中沒有 direct success")

    if latest_queue_event:
        ok(
            "審計 repair queue",
            f"status={latest_queue_event.get('status')}, queue_id={latest_queue_event.get('queue_id')}, time={latest_queue_event.get('time')}",
        )
    elif latest_waiting_context_event:
        warn(
            "審計 repair queue",
            f"只看到 queued_waiting_context，尚未抓到帶 queue_id 的排隊事件，time={latest_waiting_context_event.get('time')}",
        )
    else:
        warn("審計 repair queue", "最近審計中沒有 repair queue 事件")

    if matched_replay_event:
        ok(
            "queue→replay 關聯",
            f"queue_id={matched_replay_event.get('queue_id')} 已對上 replayed_success，time={matched_replay_event.get('time')}",
        )
    elif latest_queue_event:
        warn(
            "queue→replay 關聯",
            f"queue_id={latest_queue_event.get('queue_id')} 尚未對到 replayed_success",
        )

    if latest_replay_event:
        ok(
            "審計 replayed_success",
            f"queue_id={latest_replay_event.get('queue_id')}, trigger={latest_replay_event.get('trigger_source')}, time={latest_replay_event.get('time')}",
        )
    else:
        warn("審計 replayed_success", "最近審計中沒有 replayed_success")

    sequence_status, sequence_detail = audit_news_sequence(events, target)
    if sequence_status == "ok":
        ok("新聞 W 序列", sequence_detail)
    elif sequence_status == "warn":
        warn("新聞 W 序列", sequence_detail)
    else:
        fail("新聞 W 序列", sequence_detail)

    if pending == 0:
        ok("待補發佇列", "目前為 0")
    else:
        warn("待補發佇列", f"目前仍有 {pending} 筆")

    print("結論")
    if ready and direct.get("trace_id"):
        print("- Bridge 主鏈路正常，至少已觀測到 /health 與直接送達成功。")
    elif not ready:
        print(f"- Bridge 目前未 ready，請先依 recommended_action={recommended_action} 處理。")
    else:
        print("- Bridge 可用，但 direct success 審計仍待補觀測。")

    if matched_replay_event:
        print("- repair / replay 鏈路近期有實際事件，而且 queue_id 已完成關聯驗證。")
    elif latest_queue_event and latest_replay_event:
        print("- repair / replay 鏈路近期有實際事件，但最新 queue_id 尚未直接對上 replayed_success。")
    else:
        print("- repair / replay 鏈路近期沒有完整事件；若要驗證補發流程，需先製造一筆 queued_for_repair 再刷新 context。")

    if sequence_status == "fail":
        print("- 新聞 W logical sequence 仍不完整，需回看 wechat_delivery.jsonl 的 sequence / chunk manifest。")
    elif sequence_status == "warn":
        print("- 新聞 W 已有事件，但 manifest 觀測仍不完整，建議再跑一次受控長訊息驗證。")
    else:
        print("- 新聞 W logical sequence 與 chunk manifest 已可直接從審計比對。")

    return 0 if sequence_status != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
