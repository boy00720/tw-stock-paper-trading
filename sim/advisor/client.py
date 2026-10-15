"""automation/sim/advisor/client.py — Claude API 客戶端（M6 區塊 A）

走 cc-switch **跟隨模式**：每次進程啟動時 call `resolve_routes()` 拉當前
選中 provider 的 ANTHROPIC_BASE_URL / AUTH_TOKEN / MODEL 注入 process env，
`anthropic.Anthropic()` 自動讀環境變數連線。W 在 cc-switch GUI 切 provider
不需重啟 advisor，下一輪 launchd 觸發新 process 就跟著切。

cc-switch 後端可能是 Anthropic 原生 API（api.anthropic.com），也可能是
相容包裝（Any Router、其他聚合路由）。後者對 cache_control / beta headers
行為不一致，本模組做兩個 sticky 開關：
- cache_control：第一次失敗（API 錯誤訊息含 'cache'）→ 標 _cache_works=False、
  本進程後續不再嘗試
- beta headers：第一次失敗（錯誤訊息含 '1m' / 'context-1m' / 'beta'）→ 標
  _beta_headers=['context-1m-2025-08-07']、本進程後續每次都帶
- tools schema：本階段**不使用**，prompt 明確要求純 JSON output、解析 content text

對外只有兩個類別：AdvisorClient、AdvisorClientError。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AdvisorClientError(RuntimeError):
    """advisor 客戶端類錯誤（cc-switch 無 provider / API 失敗 / JSON 無解）"""


def _bootstrap_cc_switch_env() -> tuple[str, str]:
    """從 cc-switch.db 拉 currentProviderClaude 的 env 注入 process。

    若設了 SIM_PROVIDER_OVERRIDE 環境變數，直接用該 provider_id（與 cc-switch
    GUI 主路由解耦）；否則跟 cc-switch primary 走。
    回傳 (model, provider_id)。**無可用 provider 時 raise** —— 不靜默 fallback，
    讓 advisor 進程退出、launchd 下一輪重試。
    """
    try:
        from config.cc_switch_route_provider import (
            resolve_routes,
            _load_cc_switch_provider_by_id,
            CC_SWITCH_DB_PATH,
        )
    except ImportError as exc:
        raise AdvisorClientError(f"cc-switch 路由模組無法 import: {exc}") from exc

    override_id = os.environ.get("SIM_PROVIDER_OVERRIDE", "").strip()
    if override_id:
        import sqlite3
        if not CC_SWITCH_DB_PATH.exists():
            raise AdvisorClientError(
                f"SIM_PROVIDER_OVERRIDE={override_id} 但 cc-switch db 不存在: {CC_SWITCH_DB_PATH}"
            )
        conn = sqlite3.connect(str(CC_SWITCH_DB_PATH))
        try:
            primary = _load_cc_switch_provider_by_id(conn, override_id)
        finally:
            conn.close()
        if not primary or not primary.get("env"):
            raise AdvisorClientError(
                f"SIM_PROVIDER_OVERRIDE={override_id} 在 cc-switch db 找不到或 env 不完整"
            )
        logger.info("[advisor.client] 啟用 SIM_PROVIDER_OVERRIDE=%s（與 cc-switch 主路由解耦）", override_id)
    else:
        payload = resolve_routes()
        primary = payload.get("primary")
        if not primary or not primary.get("env"):
            raise AdvisorClientError(
                f"cc-switch 無可用 provider；source={payload.get('source')}"
                f" warnings={payload.get('warnings')}"
            )

    env = primary["env"]
    for key in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
    ):
        value = env.get(key)
        if value:
            os.environ[key] = value

    model = primary.get("model") or os.environ.get("ANTHROPIC_MODEL") or ""
    provider_id = primary.get("provider_id") or "?"
    if not model:
        raise AdvisorClientError(
            f"cc-switch primary 沒帶 model；env_keys={sorted(env.keys())}"
        )

    base_url_log = (env.get("ANTHROPIC_BASE_URL") or "<missing>")[:50]
    logger.info(
        "[advisor.client] cc-switch 引導完成: provider=%s model=%s base_url=%s",
        provider_id, model, base_url_log,
    )
    return model, provider_id


class _CacheNotSupported(RuntimeError):
    """internal sentinel：provider 不支援 cache_control，由 decide() 降級重試"""


class _BetaHeaderRequired(RuntimeError):
    """internal sentinel：provider 強制要求 beta headers（如 Any Router 要 1m context），
    由 ping/decide() 加上 header 重試"""


# Any Router 等 cc-switch 後端常見強制要求；首次失敗時自動套用
_DEFAULT_BETA_HEADERS = ["context-1m-2025-08-07"]
_BETA_HINT_TOKENS = ("1m 上下文", "context-1m", "1m context", "anthropic-beta")


class AdvisorClient:
    """Anthropic SDK 包裝，自動接 cc-switch 路由。"""

    def __init__(self):
        # 引導 → 注入 env → 建 SDK client
        self.model, self.provider_id = _bootstrap_cc_switch_env()
        try:
            import httpx
            from anthropic import Anthropic
        except ImportError as exc:
            raise AdvisorClientError(f"anthropic SDK 未安裝: {exc}") from exc
        # cc-switch 後端若是本地 Docker published port（如 sub2api 的
        # 127.0.0.1:8080），httpx 預設 transport 對該 port 的 POST 會穩定踩
        # Docker Desktop vpnkit 502：請求未進容器就被轉發層回 connection:close
        # 的空 502（curl / requests / urllib 因連接行為不同皆不受影響）。顯式
        # 注入一個獨立的 HTTPTransport(http1=True) 即可穩定繞過。實證 2026-06-08：
        # 預設 transport 連打 5/5 全 502，注入後 SDK 連打 4/4 正常。
        # 詳見 AUTOMATION_ARCHIVE.md「sub2api / httpx vpnkit 502」條目。
        http_client = httpx.Client(transport=httpx.HTTPTransport(http1=True))
        self.client = Anthropic(http_client=http_client)  # 自動讀 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
        # cache_control 是否被 provider 支援；None=未測 / True=支援 / False=不支援
        self._cache_works: Optional[bool] = None
        # beta headers sticky enable；None=不帶 / list=帶
        self._beta_headers: Optional[list[str]] = None

    def ping(self, *, timeout: int = 10) -> None:
        """啟動 smoke：最小 request 確認 provider 可用。

        若 provider 強制要求 beta headers（如 Any Router 要 1m context），
        第一次失敗會自動加 header 重試一次；仍失敗才 raise。
        """
        try:
            self._raw_ping(timeout=timeout)
            return
        except _BetaHeaderRequired as exc:
            self._beta_headers = list(_DEFAULT_BETA_HEADERS)
            logger.warning(
                "[advisor.client] provider=%s 要求 beta headers（%s），"
                "本進程後續每次帶 %s",
                self.provider_id, exc, self._beta_headers,
            )
            try:
                self._raw_ping(timeout=timeout)
                return
            except Exception as exc2:
                raise AdvisorClientError(
                    f"cc-switch provider {self.provider_id} 加 beta headers 後仍 ping 失敗: {exc2}"
                ) from exc2
        except Exception as exc:
            raise AdvisorClientError(
                f"cc-switch provider {self.provider_id} ping 失敗: {exc}"
            ) from exc

    def _raw_ping(self, *, timeout: int) -> None:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4,
            "messages": [{"role": "user", "content": "ok"}],
            "timeout": timeout,
        }
        if self._beta_headers:
            kwargs["extra_headers"] = {"anthropic-beta": ",".join(self._beta_headers)}
        try:
            self.client.messages.create(**kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            if not self._beta_headers and any(t.lower() in msg for t in _BETA_HINT_TOKENS):
                raise _BetaHeaderRequired(str(exc)) from exc
            raise

    def decide(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        *,
        max_tokens: int = 2048,
        timeout: int = 90,
    ) -> dict[str, Any]:
        """呼叫 Claude 出決策；回傳解析後的 dict。

        timeout 預設 90 秒：advisor 用例 prompt 較大（5K system + 18K+ user payload），
        且 cc-switch 後端可能是 OpenAI 相容包裝（如 Sub2API 走 GPT 系列），推論
        速度可能慢於 Anthropic 原生。launchd 週期 5 分鐘（300s），90s 足夠且
        即使遇到極慢回應也不會疊到下一輪。

        重試策略：
        - 第一次：若 self._cache_works != False，system 帶 cache_control
        - cache_control 失敗（API 錯誤訊息含 'cache'）→ 標 _cache_works=False、
          降級重試不帶 cache_control
        - JSON 解析失敗 → 重試 1 次（強調 "JSON only"）
        - 仍失敗 → raise AdvisorClientError，caller 決定本輪 skip
        """
        user_text = (
            "以下是當前模擬盤狀態，請依 system 規則出決策。"
            "**嚴格輸出單一 JSON 物件**：不要 markdown fence、不要解釋文字。\n\n"
            + json.dumps(user_payload, ensure_ascii=False, indent=2)
        )

        try:
            text = self._call_messages(
                system_prompt, user_text,
                max_tokens=max_tokens, timeout=timeout,
                use_cache=(self._cache_works is not False),
            )
        except _CacheNotSupported as exc:
            self._cache_works = False
            logger.warning(
                "[advisor.client] provider=%s 不支援 cache_control（%s），"
                "本進程後續不再嘗試",
                self.provider_id, exc,
            )
            text = self._call_messages(
                system_prompt, user_text,
                max_tokens=max_tokens, timeout=timeout,
                use_cache=False,
            )

        try:
            return _extract_json(text)
        except ValueError as exc:
            logger.warning(
                "[advisor.client] 第一次 JSON 解析失敗 (%s)，重試（強調 JSON only）",
                exc,
            )
            text = self._call_messages(
                system_prompt,
                user_text + "\n\n（注意：上一次回覆無法解析為 JSON，請只輸出一個 JSON 物件）",
                max_tokens=max_tokens, timeout=timeout, use_cache=False,
            )
            try:
                return _extract_json(text)
            except ValueError as exc2:
                raise AdvisorClientError(
                    f"Claude 回覆連兩次無法解析為 JSON: {exc2}; "
                    f"raw[:200]={text[:200]!r}"
                ) from exc2

    def _call_messages(
        self,
        system_prompt: str,
        user_text: str,
        *,
        max_tokens: int,
        timeout: int,
        use_cache: bool,
    ) -> str:
        """內部呼叫；回傳 content 第一段 text。

        cache_control 不支援會 raise _CacheNotSupported（caller 降級）。
        其他 API 失敗 raise AdvisorClientError。
        """
        if use_cache:
            system_param: Any = [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_param = system_prompt

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_param,
            "messages": [{"role": "user", "content": user_text}],
            "timeout": timeout,
        }
        if self._beta_headers:
            kwargs["extra_headers"] = {"anthropic-beta": ",".join(self._beta_headers)}

        try:
            response = self.client.messages.create(**kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            if use_cache and ("cache" in msg or "cache_control" in msg):
                raise _CacheNotSupported(str(exc)) from exc
            raise AdvisorClientError(
                f"Anthropic API 失敗（provider={self.provider_id}, "
                f"model={self.model}）: {exc}"
            ) from exc

        # content 是 list[ContentBlock]，取第一個 text block
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", "") or ""
        raise AdvisorClientError(
            f"Claude 回覆無 text block；blocks="
            f"{[type(b).__name__ for b in response.content]}"
        )


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """從文字抓 JSON 物件。處理 markdown fence、leading/trailing 雜訊。"""
    if not text:
        raise ValueError("empty text")
    text = text.strip()

    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"json fragment parse failed: {exc}") from exc

    raise ValueError(f"no JSON object found in text[:100]={text[:100]!r}")
