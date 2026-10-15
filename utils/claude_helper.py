#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claude_helper.py — Claude API 呼叫統一封裝

統一處理：
1. cc-switch route resolution
2. 錯誤序列化
3. Response text 抽取
4. Route fallback 呼叫

被呼叫方：stock_monitor.py、researcher.py、daily_report.py
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse

import anthropic

logger = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_CC_SWITCH_PROVIDER_MODULE = None
_CC_SWITCH_PROVIDER_MODULE_PATH: Optional[str] = None

RETRYABLE_ERROR_MARKERS = (
    "no available accounts",
    "overloaded_error",
    "resource_exhausted",
    "quota_exhausted",
    "rate limit",
    "rate_limit",
    "503",
    "502",
    "429",
    "504",
    "401",
    "403",
    "blocked",
    "forbidden",
    "invalid_api_key",
    "authentication",
    "auth_token",
    "api key",
    "api_key",
    "authorization",
    "could not resolve authentication method",
)

ROUTE_FALLBACK_ERROR_MARKERS = (
    *RETRYABLE_ERROR_MARKERS,
    "1m 上下文已经全量可用",
    "請啟用 1m",
    "请启用 1m",
    "[1m]",
)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_PATH, override=True)
    except Exception:
        pass


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _cc_switch_route_provider_path() -> str:
    return _safe_str(os.getenv("CC_SWITCH_ROUTE_PROVIDER_PATH")) or "/Users/w/Documents/Playground/shared/cc_switch_route_provider.py"


def _cc_switch_route_enabled() -> bool:
    raw = _safe_str(os.getenv("CC_SWITCH_ROUTE_ENABLED"))
    return raw.lower() != "false" if raw else True


def _has_usable_route_env(env: dict) -> bool:
    if not isinstance(env, dict):
        return False
    return bool(
        _safe_str(env.get("ANTHROPIC_BASE_URL"))
        and (
            _safe_str(env.get("ANTHROPIC_AUTH_TOKEN"))
            or _safe_str(env.get("ANTHROPIC_API_KEY"))
        )
    )


def _load_cc_switch_provider():
    global _CC_SWITCH_PROVIDER_MODULE, _CC_SWITCH_PROVIDER_MODULE_PATH
    _load_env()
    provider_path = _cc_switch_route_provider_path()
    if (
        _CC_SWITCH_PROVIDER_MODULE is not None
        and _CC_SWITCH_PROVIDER_MODULE_PATH == provider_path
    ):
        return _CC_SWITCH_PROVIDER_MODULE
    if not _cc_switch_route_enabled() or not os.path.exists(provider_path):
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            "cc_switch_route_provider",
            provider_path,
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CC_SWITCH_PROVIDER_MODULE = module
        _CC_SWITCH_PROVIDER_MODULE_PATH = provider_path
        return module
    except Exception:
        logger.exception("ClaudeHelper 載入 cc-switch route provider 失敗")
        return None


def _normalize_provider_route(route: dict[str, Any], source: Any) -> Optional[dict[str, Any]]:
    env = route.get("env") or {}
    model = _safe_str(route.get("model"))
    label = _safe_str(route.get("label")) or "primary"
    if not model or not _has_usable_route_env(env):
        return None
    return {
        "label": label,
        "model": model,
        "env": env,
        "source": source or "cc_switch",
        "provider_id": _safe_str(route.get("provider_id")) or None,
        "app_type": _safe_str(route.get("app_type")) or None,
    }


def _dedupe_routes(routes: List[dict[str, Any]]) -> List[dict[str, Any]]:
    result: List[dict[str, Any]] = []
    seen_provider_ids = set()
    seen_route_keys = set()
    for route in routes:
        provider_id = _safe_str(route.get("provider_id"))
        if provider_id:
            if provider_id in seen_provider_ids:
                continue
            seen_provider_ids.add(provider_id)
            result.append(route)
            continue

        env = route.get("env") or {}
        route_key = (
            _safe_str(route.get("label")),
            _safe_str(route.get("model")),
            _safe_str(env.get("ANTHROPIC_BASE_URL")),
        )
        if route_key in seen_route_keys:
            continue
        seen_route_keys.add(route_key)
        result.append(route)
    return result


def _resolve_provider_routes() -> List[dict[str, Any]]:
    provider = _load_cc_switch_provider()
    if provider is None:
        return []

    override_route: Optional[dict[str, Any]] = None
    override_id = _safe_str(os.getenv("AUTOMATION_PROVIDER_OVERRIDE"))
    if override_id:
        try:
            import sqlite3
            db_path = provider.CC_SWITCH_DB_PATH
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                try:
                    raw = provider._load_cc_switch_provider_by_id(conn, override_id)
                finally:
                    conn.close()
                if raw:
                    override_route = _normalize_provider_route(raw, "cc_switch_override")
                    if override_route:
                        logger.info(
                            "[ClaudeHelper] AUTOMATION_PROVIDER_OVERRIDE=%s 優先；失敗時 fallback 到 cc-switch 主路由",
                            override_id,
                        )
                    else:
                        logger.warning(
                            "AUTOMATION_PROVIDER_OVERRIDE=%s env 不完整，略過 override 直接走 cc-switch primary",
                            override_id,
                        )
                else:
                    logger.warning(
                        "AUTOMATION_PROVIDER_OVERRIDE=%s 在 cc-switch db 找不到，略過 override",
                        override_id,
                    )
            else:
                logger.warning(
                    "AUTOMATION_PROVIDER_OVERRIDE=%s 但 cc-switch db 不存在，略過 override",
                    override_id,
                )
        except Exception:
            logger.exception("AUTOMATION_PROVIDER_OVERRIDE 載入失敗，略過 override")

    # AUTOMATION_FALLBACK_CHAIN：顯式指定 fallback 順位 provider id（逗號分隔）；
    # 設了就完全取代 cc-switch primary/routes 偵測（[override] + [chain...]），
    # cc-switch GUI 切換不影響智囊團，避免 GUI 主路由帶有 1m context 等特殊
    # 限制的 provider 拖累飛書/微信生成任務。
    chain_raw = _safe_str(os.getenv("AUTOMATION_FALLBACK_CHAIN"))
    if chain_raw:
        chain_ids = [pid.strip() for pid in chain_raw.split(",") if pid.strip()]
        candidates: List[dict[str, Any]] = []
        if override_route:
            candidates.append(override_route)
        try:
            import sqlite3
            db_path = provider.CC_SWITCH_DB_PATH
            if not db_path.exists():
                logger.warning("AUTOMATION_FALLBACK_CHAIN 設了但 cc-switch db 不存在，僅 override 生效")
                return _dedupe_routes(candidates)
            conn = sqlite3.connect(str(db_path))
            try:
                for pid in chain_ids:
                    raw = provider._load_cc_switch_provider_by_id(conn, pid)
                    if not raw:
                        logger.warning("AUTOMATION_FALLBACK_CHAIN provider=%s 找不到，略過", pid)
                        continue
                    normalized = _normalize_provider_route(raw, "cc_switch_chain")
                    if normalized:
                        candidates.append(normalized)
            finally:
                conn.close()
        except Exception:
            logger.exception("AUTOMATION_FALLBACK_CHAIN 載入失敗，僅 override 生效")
        logger.info(
            "[ClaudeHelper] 啟用 AUTOMATION_FALLBACK_CHAIN（%d providers）；跳過 cc-switch GUI primary 偵測",
            len(chain_ids),
        )
        return _dedupe_routes(candidates)

    try:
        payload = provider.resolve_routes(prefer_cc_switch=True)
        source = payload.get("source")
        candidates = []
        if override_route:
            candidates.append(override_route)

        primary = payload.get("primary")
        if isinstance(primary, dict):
            normalized = _normalize_provider_route(primary, source)
            if normalized:
                candidates.append(normalized)

        routes = payload.get("routes") or []
        if isinstance(routes, list):
            for route in routes:
                if not isinstance(route, dict):
                    continue
                normalized = _normalize_provider_route(route, source)
                if normalized:
                    candidates.append(normalized)

        return _dedupe_routes(candidates)
    except Exception:
        logger.exception("ClaudeHelper 解析 cc-switch route 失敗")
        return [override_route] if override_route else []



def resolve_llm_routes() -> List[dict[str, Any]]:
    _load_env()
    routes = _resolve_provider_routes()
    if routes:
        return routes
    raise RuntimeError("No usable Claude routes resolved from cc-switch provider")



def route_debug_summary(route: dict[str, Any]) -> str:
    env = (route or {}).get("env") or {}
    base_url = _safe_str(env.get("ANTHROPIC_BASE_URL")) or "https://api.anthropic.com"
    base_host = base_url
    try:
        parsed = urlparse(base_url)
        if parsed.netloc:
            base_host = parsed.netloc
    except Exception:
        pass
    return (
        f"label={_safe_str(route.get('label')) or 'unknown'} "
        f"source={_safe_str(route.get('source')) or 'unknown'} "
        f"app_type={_safe_str(route.get('app_type')) or 'unknown'} "
        f"provider_id={_safe_str(route.get('provider_id')) or 'unknown'} "
        f"model={_safe_str(route.get('model')) or 'unknown'} "
        f"base_host={base_host}"
    )


class ClaudeHelper:
    """Claude API 呼叫的統一封裝。支援 route fallback。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        max_retries: int = 0,
    ) -> None:
        _load_env()
        self.api_key = api_key or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
        self.base_url = base_url or os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self.max_retries = max_retries
        self._explicit_route = bool(api_key or base_url)
        self.client = anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=max_retries,
        )

    @staticmethod
    def build_candidates() -> List[str]:
        """保留相容介面，但只返回 primary 模型。"""
        _load_env()
        primary_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()
        return [primary_model] if primary_model else []

    @staticmethod
    def serialize_error(exc: Exception) -> str:
        """把 anthropic exception 序列化成可分析的字串。"""
        parts = [str(exc)]
        body = getattr(exc, "body", None)
        if body is not None:
            try:
                parts.append(json.dumps(body, ensure_ascii=False))
            except TypeError:
                parts.append(str(body))
        return " | ".join(parts)

    @classmethod
    def should_try_fallback(cls, exc: Exception) -> bool:
        """相容舊 caller；現在僅代表是否屬於可重試型 primary 錯誤。"""
        if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if status in {401, 403, 429, 500, 502, 503, 504}:
                return True
        text = cls.serialize_error(exc).lower()
        return any(marker in text for marker in RETRYABLE_ERROR_MARKERS)

    @classmethod
    def should_try_route_fallback(cls, exc: Exception) -> bool:
        if cls.should_try_fallback(exc):
            return True
        text = cls.serialize_error(exc).lower()
        return any(marker.lower() in text for marker in ROUTE_FALLBACK_ERROR_MARKERS)

    @staticmethod
    def extract_text(resp: Any) -> str:
        """從 anthropic response 裡抽出所有文字區塊。"""
        text_blocks: List[str] = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                text_blocks.append(block.text.strip())
        if text_blocks:
            return "\n".join(text_blocks).strip()
        first_block = resp.content[0] if getattr(resp, "content", None) else None
        if first_block is not None and getattr(first_block, "text", None):
            return first_block.text.strip()
        raise ValueError("Claude response did not contain text content.")

    def resolve_routes(self) -> List[dict[str, Any]]:
        if self._explicit_route:
            model = _safe_str(os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
            return [
                {
                    "label": "explicit",
                    "model": model,
                    "env": {
                        "ANTHROPIC_BASE_URL": self.base_url,
                        "ANTHROPIC_AUTH_TOKEN": self.api_key,
                    },
                    "source": "explicit",
                }
            ]
        return resolve_llm_routes()

    def create_client(self, route: Optional[dict[str, Any]] = None) -> anthropic.Anthropic:
        route_env = (route or {}).get("env", {}) if isinstance(route, dict) else {}
        api_key = (
            _safe_str(route_env.get("ANTHROPIC_AUTH_TOKEN"))
            or _safe_str(route_env.get("ANTHROPIC_API_KEY"))
            or self.api_key
        )
        base_url = (
            _safe_str(route_env.get("ANTHROPIC_BASE_URL"))
            or self.base_url
            or "https://api.anthropic.com"
        )
        return anthropic.Anthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=self.max_retries,
        )

    def route_debug_summary(self, route: dict[str, Any]) -> str:
        return route_debug_summary(route)

    def _route_models(self, route: dict[str, Any], models: Optional[List[str]]) -> List[str]:
        route_model = _safe_str((route or {}).get("model"))
        if route_model:
            return [route_model]
        if models:
            return [model for model in models if model][:1]
        return self.build_candidates()

    def chat(
        self,
        *,
        prompt: str,
        system: str = "",
        max_tokens: int = 2500,
        models: Optional[List[str]] = None,
        on_fallback=None,
    ) -> str:
        routes = self.resolve_routes()
        if not routes:
            raise RuntimeError("No usable Claude routes resolved from cc-switch provider")

        errors: List[str] = []
        last_exc: Optional[Exception] = None

        for route_index, route in enumerate(routes):
            route_summary = self.route_debug_summary(route)
            route_models = self._route_models(route, models)
            if not route_models:
                errors.append(f"route[{route_index}] no_model {route_summary}")
                continue

            model_name = route_models[0]
            client = self.create_client(route)
            kwargs: dict[str, Any] = {
                "model": model_name,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system

            # 1M context beta header 開關（預設關閉）。
            # 背景：fcapp.run / anyrouter 等代理在沒帶 header 時會回 400
            # 「请启用 1m 上下文」；但帶上 "context-1m-2025-08-07" 後會觸發
            # new-api 後端自身的 nil pointer panic（500），故 2026-05-05 預設
            # 不啟用，避免讓行為從可預期的 400 fallback 變成 500 panic。
            # 未來 backend 修好或想試其他 header 字串時，在 .env 設：
            #   ANTHROPIC_BETA_HEADERS=context-1m-2025-08-07
            beta_headers = os.getenv("ANTHROPIC_BETA_HEADERS", "").strip()
            if beta_headers:
                kwargs.setdefault("extra_headers", {})["anthropic-beta"] = beta_headers

            logger.info(
                "ClaudeHelper calling route[%s/%s]=%s model=%s",
                route_index + 1,
                len(routes),
                route_summary,
                model_name,
            )
            try:
                resp = client.messages.create(**kwargs)
                logger.info("ClaudeHelper route success %s", route_summary)
                return self.extract_text(resp)
            except Exception as exc:
                last_exc = exc
                serialized = self.serialize_error(exc)
                retryable = self.should_try_route_fallback(exc)
                errors.append(
                    f"route[{route_index}] retryable={retryable} route={route_summary} error={serialized}"
                )
                logger.error("ClaudeHelper route failed %s error=%s", route_summary, serialized)

                if route_index + 1 < len(routes) and on_fallback:
                    next_route_models = self._route_models(routes[route_index + 1], models)
                    next_model = next_route_models[0] if next_route_models else ""
                    try:
                        on_fallback(model_name, next_model, exc)
                    except Exception:
                        logger.exception("ClaudeHelper on_fallback callback failed")

                if not retryable:
                    break

        details = "\n".join(errors)
        if last_exc is not None:
            raise RuntimeError(
                "Claude routes exhausted or unavailable.\n"
                f"{details}"
            ) from last_exc
        raise RuntimeError("Claude routes exhausted or unavailable.")
