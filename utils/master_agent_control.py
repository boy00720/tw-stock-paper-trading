#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Master agent control helpers.

目標：
1. 讓 orchestrator 成為唯一對外通知決策者
2. 子 agent 只產出結構化 payload，不直接推送
3. 支援同進程呼叫與 subprocess 呼叫共用同一套控制上下文
"""

from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-Unix fallback
    fcntl = None


_CURRENT_CONTROL: ContextVar[Optional["MasterControlContext"]] = ContextVar(
    "master_agent_control",
    default=None,
)

_ENV_CONTROL_ENABLED = "MASTER_AGENT_CONTROLLED_NOTIFICATIONS"
_ENV_RUN_ID = "MASTER_AGENT_RUN_ID"
_ENV_STEP_NAME = "MASTER_AGENT_STEP_NAME"
_ENV_SOURCE = "MASTER_AGENT_SOURCE"
_ENV_TO_USER = "WECHAT_PUSH_TO_USER"
_ENV_CONTEXT_TOKEN = "WECHAT_PUSH_CONTEXT_TOKEN"


@dataclass
class MasterControlContext:
    run_id: str
    step_name: str
    source: str = ""
    to_user: str = ""
    context_token: str = ""

    def is_enabled(self) -> bool:
        return bool(self.run_id and self.step_name)

    def to_env(self) -> Dict[str, str]:
        env = {
            _ENV_CONTROL_ENABLED: "1",
            _ENV_RUN_ID: self.run_id,
            _ENV_STEP_NAME: self.step_name,
        }
        if self.source:
            env[_ENV_SOURCE] = self.source
        if self.to_user:
            env[_ENV_TO_USER] = self.to_user
        if self.context_token:
            env[_ENV_CONTEXT_TOKEN] = self.context_token
        return env


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def build_run_id(prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{prefix}-{ts}-{os.getpid()}-{secrets.token_hex(2)}"


def build_context(
    run_id: str,
    step_name: str,
    source: str = "",
    to_user: str = "",
    context_token: str = "",
) -> MasterControlContext:
    return MasterControlContext(
        run_id=_safe_str(run_id),
        step_name=_safe_str(step_name),
        source=_safe_str(source),
        to_user=_safe_str(to_user),
        context_token=_safe_str(context_token),
    )


def get_current_control() -> Optional[MasterControlContext]:
    current = _CURRENT_CONTROL.get()
    if current and current.is_enabled():
        return current

    if _safe_str(os.getenv(_ENV_CONTROL_ENABLED)) != "1":
        return None

    run_id = _safe_str(os.getenv(_ENV_RUN_ID))
    step_name = _safe_str(os.getenv(_ENV_STEP_NAME))
    if not run_id or not step_name:
        return None

    return MasterControlContext(
        run_id=run_id,
        step_name=step_name,
        source=_safe_str(os.getenv(_ENV_SOURCE)),
        to_user=_safe_str(os.getenv(_ENV_TO_USER)),
        context_token=_safe_str(os.getenv(_ENV_CONTEXT_TOKEN)),
    )


@contextmanager
def activate_control(control: Optional[MasterControlContext]) -> Iterator[Optional[MasterControlContext]]:
    if not control or not control.is_enabled():
        yield None
        return

    token = _CURRENT_CONTROL.set(control)
    try:
        yield control
    finally:
        _CURRENT_CONTROL.reset(token)


def payload_root() -> Path:
    return Path(__file__).resolve().parent.parent / "logs" / "master_agent_payloads"


def audit_root() -> Path:
    return Path(__file__).resolve().parent.parent / "logs" / "master_agent_audit"


def _audit_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def audit_events_path(date_str: Optional[str] = None) -> Path:
    final_date = _safe_str(date_str) or _audit_date_str()
    root = audit_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"events_{final_date}.jsonl"


def audit_metrics_path(date_str: Optional[str] = None) -> Path:
    final_date = _safe_str(date_str) or _audit_date_str()
    root = audit_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"metrics_{final_date}.json"


@contextmanager
def _locked_file(path: Path, mode: str) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as handle:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        try:
            yield handle
        finally:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                pass
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _default_metrics(date_str: str) -> Dict[str, Any]:
    return {
        "date": date_str,
        "last_updated_at": "",
        "event_counts": {},
        "dispatch": {
            "attempted_steps": 0,
            "successful_steps": 0,
            "failed_steps": 0,
            "channels": {},
        },
        "interactive_exceptions": {
            "total": 0,
            "success": 0,
            "failed": 0,
            "by_name": {},
        },
        "step_last_status": {},
    }


def _update_metrics_from_event(entry: Dict[str, Any]) -> Optional[Path]:
    date_str = _safe_str(entry.get("date")) or _audit_date_str()
    path = audit_metrics_path(date_str)
    try:
        with _locked_file(path, "a+") as handle:
            handle.seek(0)
            raw = handle.read()
            metrics = _default_metrics(date_str)
            if raw.strip():
                try:
                    metrics = json.loads(raw)
                except Exception:
                    metrics = _default_metrics(date_str)

            metrics["last_updated_at"] = _safe_str(entry.get("timestamp"))
            event_type = _safe_str(entry.get("event_type")) or "unknown"
            event_counts = metrics.setdefault("event_counts", {})
            event_counts[event_type] = int(event_counts.get(event_type, 0) or 0) + 1

            data = entry.get("data") or {}
            step_name = _safe_str(entry.get("step_name"))

            if event_type == "payload_dispatch":
                dispatch = metrics.setdefault("dispatch", {})
                dispatch["attempted_steps"] = int(dispatch.get("attempted_steps", 0) or 0) + 1
                success = bool(data.get("success"))
                if success:
                    dispatch["successful_steps"] = int(dispatch.get("successful_steps", 0) or 0) + 1
                else:
                    dispatch["failed_steps"] = int(dispatch.get("failed_steps", 0) or 0) + 1

                channels_state = dispatch.setdefault("channels", {})
                for channel_name, channel_data in (data.get("channels") or {}).items():
                    if not channel_data.get("attempted"):
                        continue
                    channel_metrics = channels_state.setdefault(
                        channel_name,
                        {"attempts": 0, "sent": 0, "failed": 0},
                    )
                    channel_metrics["attempts"] = int(channel_metrics.get("attempts", 0) or 0) + 1
                    if channel_data.get("sent"):
                        channel_metrics["sent"] = int(channel_metrics.get("sent", 0) or 0) + 1
                    else:
                        channel_metrics["failed"] = int(channel_metrics.get("failed", 0) or 0) + 1

                if step_name:
                    metrics.setdefault("step_last_status", {})[step_name] = {
                        "event_type": event_type,
                        "success": success,
                        "timestamp": _safe_str(entry.get("timestamp")),
                        "run_id": _safe_str(entry.get("run_id")),
                        "agent": _safe_str(data.get("agent")),
                        "channels": data.get("channels") or {},
                        "errors": data.get("errors") or [],
                    }

            if event_type == "interactive_exception":
                interactive = metrics.setdefault("interactive_exceptions", {})
                interactive["total"] = int(interactive.get("total", 0) or 0) + 1
                success = bool(data.get("success"))
                if success:
                    interactive["success"] = int(interactive.get("success", 0) or 0) + 1
                else:
                    interactive["failed"] = int(interactive.get("failed", 0) or 0) + 1

                name = _safe_str(data.get("name")) or step_name or "interactive"
                by_name = interactive.setdefault("by_name", {})
                item = by_name.setdefault(name, {"total": 0, "success": 0, "failed": 0})
                item["total"] = int(item.get("total", 0) or 0) + 1
                if success:
                    item["success"] = int(item.get("success", 0) or 0) + 1
                else:
                    item["failed"] = int(item.get("failed", 0) or 0) + 1

                metrics.setdefault("step_last_status", {})[f"interactive:{name}"] = {
                    "event_type": event_type,
                    "success": success,
                    "timestamp": _safe_str(entry.get("timestamp")),
                    "channel": _safe_str(data.get("channel")),
                    "title": _safe_str(data.get("title")),
                    "details": _safe_str(data.get("details")),
                }

            handle.seek(0)
            handle.truncate(0)
            handle.write(json.dumps(metrics, ensure_ascii=False, indent=2))
        return path
    except Exception:
        return None


def record_master_event(
    event_type: str,
    *,
    run_id: str = "",
    step_name: str = "",
    source: str = "",
    to_user: str = "",
    control: Optional[MasterControlContext] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    current = control or get_current_control()
    event_time = datetime.now()
    entry = {
        "timestamp": event_time.isoformat(timespec="seconds"),
        "date": event_time.strftime("%Y-%m-%d"),
        "event_type": _safe_str(event_type),
        "run_id": _safe_str(run_id) or _safe_str(getattr(current, "run_id", "")),
        "step_name": _safe_str(step_name) or _safe_str(getattr(current, "step_name", "")),
        "source": _safe_str(source) or _safe_str(getattr(current, "source", "")),
        "to_user": _safe_str(to_user) or _safe_str(getattr(current, "to_user", "")),
        "has_context_token": bool(_safe_str(getattr(current, "context_token", ""))),
        "data": data or {},
    }
    path = audit_events_path(entry["date"])
    try:
        with _locked_file(path, "a+") as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        return None

    _update_metrics_from_event(entry)
    return path


def record_interactive_exception(
    name: str,
    *,
    channel: str,
    title: str,
    success: bool,
    details: str = "",
    source: str = "interactive_direct_send",
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    return record_master_event(
        "interactive_exception",
        step_name=f"interactive:{_safe_str(name) or 'direct_send'}",
        source=source,
        data={
            "name": _safe_str(name),
            "channel": _safe_str(channel),
            "title": _safe_str(title),
            "success": bool(success),
            "details": _safe_str(details),
            "metadata": metadata or {},
        },
    )


def payload_path(run_id: str, step_name: str) -> Path:
    root = payload_root() / _safe_str(run_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{_safe_str(step_name)}.json"


def write_payload(
    payload: Dict[str, Any],
    *,
    control: Optional[MasterControlContext] = None,
    step_name: Optional[str] = None,
) -> Optional[Path]:
    current = control or get_current_control()
    if not current or not current.is_enabled():
        return None

    final_step = _safe_str(step_name) or current.step_name
    path = payload_path(current.run_id, final_step)
    content = {
        "run_id": current.run_id,
        "step_name": final_step,
        "source": current.source,
        "to_user": current.to_user,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "control": asdict(current),
        **(payload or {}),
    }
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    record_master_event(
        "payload_emitted",
        run_id=current.run_id,
        step_name=final_step,
        source=current.source,
        to_user=current.to_user,
        control=current,
        data={
            "agent": _safe_str(content.get("agent")),
            "payload_path": str(path),
            "channels": sorted((content.get("notification") or {}).keys()),
            "artifacts": sorted((content.get("artifacts") or {}).keys()),
        },
    )
    return path


def load_payload(run_id: str, step_name: str) -> Optional[Dict[str, Any]]:
    path = payload_path(run_id, step_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
