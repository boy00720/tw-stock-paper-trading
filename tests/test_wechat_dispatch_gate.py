# -*- coding: utf-8 -*-

from pathlib import Path

import orchestrator
from scripts.news_crawler import NewsCrawler
from utils import stock_skill_audit


class DummyLarkNotifier:
    def __init__(self, *_args, **_kwargs):
        self.sent = []

    def send_message_chunked(self, *_args, **_kwargs):
        self.sent.append(("chunked", _args, _kwargs))
        return True

    def send_message(self, *_args, **_kwargs):
        self.sent.append(("single", _args, _kwargs))
        return True

    def upload_file(self, *_args, **_kwargs):
        return "file_key"

    def send_file(self, *_args, **_kwargs):
        return True


class DummyWechatNotifier:
    next_send_results = []
    next_health_results = []
    send_calls = []
    file_calls = []

    def __init__(self, *_args, **_kwargs):
        self.last_error = ""
        self.last_result = {
            "accepted": False,
            "delivered": False,
            "queued": False,
            "pending_repair_count": 0,
            "delivery_state": "init",
        }

    @classmethod
    def reset(cls):
        cls.next_send_results = []
        cls.next_health_results = []
        cls.send_calls = []
        cls.file_calls = []

    def get_bridge_health(self, to_user=None):
        if self.next_health_results:
            return dict(self.next_health_results.pop(0))
        return {
            "status": "ok",
            "ready_for_push": True,
            "requires_user_refresh": False,
            "context_invalid": False,
            "pending_failed_count": 0,
            "summary_status": "ready",
            "summary_text": "ready",
            "target_user": to_user,
        }

    def send_message(self, text, to_user=None, typing_seconds=0, context_token=None, allow_unready_queue=False, metadata=None):
        self.send_calls.append({
            "text": text,
            "to_user": to_user,
            "typing_seconds": typing_seconds,
            "context_token": context_token,
            "allow_unready_queue": allow_unready_queue,
            "metadata": metadata,
        })
        result = dict(self.next_send_results.pop(0))
        self.last_result = result
        self.last_error = str(result.get("reason") or "")
        return bool(result.get("return_value", result.get("accepted") or result.get("delivered")))

    def send_file(self, file_path, to_user=None, context_token=None):
        self.file_calls.append({
            "file_path": file_path,
            "to_user": to_user,
            "context_token": context_token,
        })
        return True


class StubProcessor:
    def format_category_news(self, batch, category, offset=0):
        return f"{category}:{offset + 1}-{offset + len(batch)}"


class StubCrawler:
    _iter_notification_batches = NewsCrawler._iter_notification_batches
    _build_wechat_text = staticmethod(NewsCrawler._build_wechat_text)
    _emit_master_payloads = NewsCrawler._emit_master_payloads
    _safe_step_component = staticmethod(NewsCrawler._safe_step_component)

    def __init__(self):
        self.processor = StubProcessor()


def _patch_notifiers(monkeypatch):
    monkeypatch.setattr("utils.lark_notifier.LarkNotifier", DummyLarkNotifier)
    monkeypatch.setattr("utils.wechat_notifier.WechatNotifier", DummyWechatNotifier)


def test_send_payload_notification_marks_queued_as_not_sent(monkeypatch):
    _patch_notifiers(monkeypatch)
    DummyWechatNotifier.reset()
    DummyWechatNotifier.next_send_results = [
        {
            "accepted": True,
            "delivered": False,
            "queued": True,
            "pending_repair_count": 2,
            "delivery_state": "queued_for_repair",
            "trace_id": "wx-1",
            "queue_id": "repair-1",
            "return_value": True,
        }
    ]

    payload = {
        "run_id": "run-1",
        "step_name": "market_review",
        "agent": "market_review",
        "notification": {
            "wechat": {
                "text": "hello",
                "typing_seconds": 5,
                "attach_pdf": False,
            }
        },
        "control": {},
    }

    result = orchestrator._send_payload_notification(payload)

    assert result["wechat_sent"] is False
    assert result["errors"] == []
    assert result["channels"]["wechat"]["sent"] is False
    assert result["channels"]["wechat"]["accepted"] is True
    assert result["channels"]["wechat"]["queued"] is True
    assert result["channels"]["wechat"]["delivery_state"] == "queued_for_repair"
    assert result["channels"]["wechat"]["queue_id"] == "repair-1"


def test_send_payload_notification_passes_news_metadata_and_marks_partial(monkeypatch):
    _patch_notifiers(monkeypatch)
    DummyWechatNotifier.reset()
    DummyWechatNotifier.next_send_results = [
        {
            "accepted": True,
            "delivered": True,
            "queued": False,
            "pending_repair_count": 0,
            "delivery_state": "sent",
            "trace_id": "wx-partial",
            "logical_id": "wxmsg-123",
            "chunk_count": 2,
            "details": [],
            "meta": {
                "category": "economy",
                "sequence_index": 2,
                "sequence_total": 3,
                "range_start": 11,
                "range_end": 20,
            },
            "return_value": True,
        }
    ]

    payload = {
        "run_id": "run-news",
        "step_name": "news_economy_11_20",
        "agent": "news_crawler",
        "meta": {
            "category": "economy",
            "sequence_index": 2,
            "sequence_total": 3,
            "range_start": 11,
            "range_end": 20,
            "news_count": 25,
        },
        "notification": {
            "wechat": {
                "text": "📰 新聞 W [2/3] economy 11-20/25\n\nbody",
                "typing_seconds": 6,
                "attach_pdf": False,
            }
        },
        "control": {},
    }

    result = orchestrator._send_payload_notification(payload)

    assert DummyWechatNotifier.send_calls[0]["metadata"] == {
        "category": "economy",
        "sequence_index": 2,
        "sequence_total": 3,
        "range_start": 11,
        "range_end": 20,
        "news_count": 25,
        "step_name": "news_economy_11_20",
        "agent": "news_crawler",
    }
    assert result["wechat_sent"] is False
    assert result["channels"]["wechat"]["partial_suspected"] is True
    assert result["channels"]["wechat"]["delivery_state"] == "partial_suspected"
    assert "missing_chunk_details" in result["channels"]["wechat"]["partial_reasons"]
    assert "wechat_partial_suspected" in result["errors"]


def test_dispatch_master_payloads_gates_later_wechat_after_queue(monkeypatch):
    _patch_notifiers(monkeypatch)
    DummyWechatNotifier.reset()
    DummyWechatNotifier.next_health_results = [
        {
            "status": "ok",
            "ready_for_push": True,
            "requires_user_refresh": False,
            "context_invalid": False,
            "pending_failed_count": 0,
            "summary_status": "ready",
            "summary_text": "ready",
        }
    ]
    DummyWechatNotifier.next_send_results = [
        {
            "accepted": True,
            "delivered": False,
            "queued": True,
            "pending_repair_count": 1,
            "delivery_state": "queued_waiting_context",
            "trace_id": "wx-queue",
            "queue_id": "repair-q",
            "return_value": True,
        }
    ]

    payloads = {
        "first": {
            "run_id": "run-2",
            "step_name": "first",
            "agent": "stock_afternoon",
            "notification": {
                "feishu": {"title": "F1", "content": "feishu one"},
                "wechat": {"text": "wechat one", "typing_seconds": 0, "attach_pdf": False},
            },
            "control": {},
        },
        "second": {
            "run_id": "run-2",
            "step_name": "second",
            "agent": "stock_screener",
            "notification": {
                "feishu": {"title": "F2", "content": "feishu two"},
                "wechat": {"text": "wechat two", "typing_seconds": 0, "attach_pdf": False},
            },
            "control": {},
        },
    }
    recorded = []

    monkeypatch.setattr(orchestrator, "load_payload", lambda run_id, step_name: payloads.get(step_name))
    monkeypatch.setattr(orchestrator, "record_master_event", lambda *args, **kwargs: recorded.append((args, kwargs)))
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    result = orchestrator._dispatch_master_payloads("run-2", ["first", "second"])

    assert result.success is True
    assert result.data["wechat_queued"] == 1
    assert result.data["wechat_gated"] == 1
    assert result.data["wechat_gate_reason"] == "queued_waiting_context"
    assert len(DummyWechatNotifier.send_calls) == 1

    second_event = recorded[-1][1]["data"]["channels"]["wechat"]
    assert second_event["gated"] is True
    assert second_event["attempted"] is False
    assert second_event["delivery_state"] == "gated"
    assert second_event["gate_reason"] == "queued_waiting_context"


def test_dispatch_master_payloads_gates_when_health_not_ready(monkeypatch):
    _patch_notifiers(monkeypatch)
    DummyWechatNotifier.reset()
    DummyWechatNotifier.next_health_results = [
        {
            "status": "ok",
            "ready_for_push": False,
            "requires_user_refresh": True,
            "context_invalid": True,
            "pending_failed_count": 2,
            "summary_status": "context_invalid",
            "summary_text": "need ping",
        }
    ]

    payload = {
        "run_id": "run-3",
        "step_name": "only",
        "agent": "market_review",
        "notification": {
            "feishu": {"title": "F", "content": "feishu"},
            "wechat": {"text": "wechat", "typing_seconds": 0, "attach_pdf": False},
        },
        "control": {},
    }
    monkeypatch.setattr(orchestrator, "load_payload", lambda _run_id, _step_name: payload)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    result = orchestrator._dispatch_master_payloads("run-3", ["only"])

    assert result.success is True
    assert result.data["wechat_gated"] == 1
    assert result.data["wechat_gate_reason"] == "context_invalid"
    assert DummyWechatNotifier.send_calls == []


def test_news_crawler_emits_sequence_metadata_and_header(monkeypatch):
    captured = []
    crawler = StubCrawler()
    categorized_news = {"economy": [{"id": idx} for idx in range(25)]}

    monkeypatch.setattr("scripts.news_crawler.write_payload", lambda payload, step_name: captured.append((step_name, payload)) or f"/tmp/{step_name}.json")

    steps = crawler._emit_master_payloads(categorized_news, "news.json", "news")

    assert steps == ["news_economy_1_10", "news_economy_11_20", "news_economy_21_25"]
    first_payload = captured[0][1]
    second_payload = captured[1][1]
    third_payload = captured[2][1]

    assert first_payload["meta"] == {
        "category": "economy",
        "news_count": 25,
        "range_start": 1,
        "range_end": 10,
        "sequence_index": 1,
        "sequence_total": 3,
    }
    assert second_payload["meta"]["sequence_index"] == 2
    assert third_payload["meta"]["sequence_index"] == 3
    assert third_payload["meta"]["range_end"] == 25
    assert first_payload["notification"]["wechat"]["text"].startswith("📰 新聞 W [1/3] economy 1-10/25")
    assert second_payload["notification"]["wechat"]["text"].startswith("📰 新聞 W [2/3] economy 11-20/25")


def test_audit_master_events_accepts_event_type(tmp_path, monkeypatch):
    audit_dir = tmp_path / "logs" / "master_agent_audit"
    audit_dir.mkdir(parents=True)
    event_path = audit_dir / "events_2026-04-28.jsonl"
    event_path.write_text(
        '{"time":"2026-04-28T20:41:29","event_type":"payload_dispatch","step_name":"market_review","data":{"success":false,"errors":["wechat_failed"],"agent":"market_review","channels":{"wechat":{"sent":false}}}}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(stock_skill_audit, "LOG_DIR", tmp_path / "logs")

    findings, checked = stock_skill_audit._audit_master_events("2026-04-28")

    assert checked == 1
    assert len(findings) == 1
    assert findings[0].source == "master_payload_dispatch"
    assert findings[0].details["step_name"] == "market_review"
