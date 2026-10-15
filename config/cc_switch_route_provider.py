#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared cc-switch route provider for local agent services.

Reads ~/.cc-switch/cc-switch.db and exposes a stable JSON shape so individual
services do not need to know the database schema.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from typing import Any, Dict, List, Optional


CC_SWITCH_DB_PATH = pathlib.Path.home() / ".cc-switch" / "cc-switch.db"
CC_SWITCH_SETTINGS_PATH = pathlib.Path.home() / ".cc-switch" / "settings.json"
CLAUDE_SETTINGS_PATH = pathlib.Path.home() / ".claude" / "settings.json"


def _has_usable_route_env(env: Dict[str, str]) -> bool:
    has_base_url = bool(str(env.get("ANTHROPIC_BASE_URL") or "").strip())
    has_auth = bool(
        str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
        or str(env.get("ANTHROPIC_API_KEY") or "").strip()
    )
    return has_base_url and has_auth


def _parse_route_env(
    raw: str,
    label: str,
    *,
    provider_id: Optional[str] = None,
    app_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except Exception:
        return None

    env_raw = parsed.get("env") or {}
    if not isinstance(env_raw, dict):
        return None

    env = {
        key: value
        for key, value in env_raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    if not _has_usable_route_env(env):
        return None
    model = str(env.get("ANTHROPIC_MODEL") or "").strip()
    if not model:
        # cc-switch GUI 中部分 provider（如 Any Router 類聚合路由）可能不填
        # ANTHROPIC_MODEL，由 server 端自行決定。為避免該 provider 被視為
        # unusable 進而觸發跨 provider fallback，這裡用環境變數提供的預設值補上。
        import os as _os
        model = _os.environ.get("CC_SWITCH_ROUTE_FALLBACK_MODEL", "claude-sonnet-4-6").strip() or "claude-sonnet-4-6"
        env = {**env, "ANTHROPIC_MODEL": model}

    return {
        "label": label,
        "model": model,
        "env": env,
        "provider_id": provider_id,
        "app_type": app_type,
    }


def _read_json_file(path: pathlib.Path) -> Dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_cc_switch_settings() -> Dict[str, Any]:
    return _read_json_file(CC_SWITCH_SETTINGS_PATH)


def _read_cc_switch_current_provider_id() -> str:
    current_provider_id = _read_cc_switch_settings().get("currentProviderClaude")
    return current_provider_id.strip() if isinstance(current_provider_id, str) else ""


def _provider_id(route: Optional[Dict[str, Any]]) -> str:
    if not isinstance(route, dict):
        return ""
    provider_id = route.get("provider_id")
    return provider_id.strip() if isinstance(provider_id, str) else ""


def _same_provider(left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> bool:
    left_id = _provider_id(left)
    right_id = _provider_id(right)
    return bool(left_id and right_id and left_id == right_id)


def _query_one(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    row = cur.execute(sql, params).fetchone()
    return row


def _load_cc_switch_primary(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = _query_one(
        conn,
        """
        select id, name, settings_config
        from providers
        where app_type = 'claude' and is_current = 1
        order by sort_index asc
        limit 1
        """,
    )
    if not row:
        return None
    return _parse_route_env(row[2], row[1] or "Primary", provider_id=row[0], app_type="claude")


def _load_cc_switch_provider_by_id(
    conn: sqlite3.Connection,
    provider_id: str,
) -> Optional[Dict[str, Any]]:
    provider_id = provider_id.strip()
    if not provider_id:
        return None
    row = _query_one(
        conn,
        """
        select id, name, settings_config
        from providers
        where app_type = 'claude' and id = ?
        limit 1
        """,
        (provider_id,),
    )
    if not row:
        return None
    return _parse_route_env(row[2], row[1] or provider_id, provider_id=row[0], app_type="claude")


def _load_cc_switch_all_candidates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        select id, name, settings_config
        from providers
        where app_type = 'claude'
        order by is_current desc, sort_index asc
        """
    ).fetchall()
    candidates: List[Dict[str, Any]] = []
    for row in rows:
        route = _parse_route_env(row[2], row[1] or row[0], provider_id=row[0], app_type="claude")
        if route:
            candidates.append(route)
    return candidates


def _merge_unique_routes(*groups: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen_provider_ids = set()
    for group in groups:
        for route in group or []:
            provider_id = _provider_id(route)
            if provider_id and provider_id in seen_provider_ids:
                continue
            if provider_id:
                seen_provider_ids.add(provider_id)
            merged.append(route)
    return merged


def resolve_routes(prefer_cc_switch: bool = True) -> Dict[str, Any]:
    del prefer_cc_switch

    primary = None
    source = "unresolved"
    warnings = []
    routes: List[Dict[str, Any]] = []
    cc_switch_settings_provider_id = _read_cc_switch_current_provider_id()

    if not cc_switch_settings_provider_id:
        warnings.append("cc_switch_settings_provider_missing")

    if CC_SWITCH_DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(CC_SWITCH_DB_PATH))
            db_primary = _load_cc_switch_primary(conn)
            settings_primary = _load_cc_switch_provider_by_id(conn, cc_switch_settings_provider_id)
            all_candidates = _load_cc_switch_all_candidates(conn)

            if settings_primary:
                primary = settings_primary
                source = "cc_switch_settings"
                if db_primary and not _same_provider(settings_primary, db_primary):
                    warnings.append(
                        "cc_switch_primary_mismatch="
                        f"settings:{_provider_id(settings_primary)}"
                        f" db:{_provider_id(db_primary)}"
                    )
            elif cc_switch_settings_provider_id:
                warnings.append(
                    "cc_switch_settings_provider_unusable="
                    f"{cc_switch_settings_provider_id}"
                )
                if db_primary:
                    warnings.append(
                        "cc_switch_db_primary_present="
                        f"{_provider_id(db_primary)}"
                    )
                    primary = db_primary
                    source = "cc_switch_db"

            if not primary and db_primary:
                primary = db_primary
                source = "cc_switch_db"

            # 設計 C：routes 只回傳 cc-switch 當前選中那個 provider，
            # 不附帶其他 candidates，避免 helper 在主路由失敗時自動 fallback
            # 到使用者沒有主動選的 provider（含已停用、已過期者）。
            # 嚴格遵循「cc-switch 自由選擇 + 動態跟隨」語義：使用者選什麼就用什麼。
            routes = [primary] if primary else []
            conn.close()
        except Exception as exc:
            warnings.append(f"cc_switch_db_error={exc}")
    else:
        warnings.append(f"cc_switch_db_missing={CC_SWITCH_DB_PATH}")

    return {
        "source": source,
        "primary": primary,
        "routes": routes,
        "warnings": warnings,
        "paths": {
            "cc_switch_db": str(CC_SWITCH_DB_PATH),
            "cc_switch_settings": str(CC_SWITCH_SETTINGS_PATH),
            "claude_settings": str(CLAUDE_SETTINGS_PATH),
        },
    }



def main() -> int:
    parser = argparse.ArgumentParser(description="Shared cc-switch route provider")
    parser.add_argument("--json", action="store_true", help="Print resolved routes as JSON")
    parser.add_argument(
        "--no-prefer-cc-switch",
        action="store_true",
        help="Deprecated compatibility flag; cc-switch is always authoritative",
    )
    args = parser.parse_args()

    payload = resolve_routes(prefer_cc_switch=not args.no_prefer_cc_switch)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
