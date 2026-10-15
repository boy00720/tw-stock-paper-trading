"""sim/tests/test_advisor_client.py — 區塊 A 測試（mock cc-switch + Anthropic SDK）"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sim.advisor import client as client_mod
from sim.advisor.client import (
    AdvisorClient,
    AdvisorClientError,
    _bootstrap_cc_switch_env,
    _extract_json,
)


# ─── _extract_json ───

def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown_fence():
    text = '```json\n{"a": 1, "b": "x"}\n```'
    assert _extract_json(text) == {"a": 1, "b": "x"}


def test_extract_json_with_prefix_text():
    text = "Sure, here is the result:\n{\"a\": 1}\nHope this helps"
    out = _extract_json(text)
    assert out == {"a": 1}


def test_extract_json_empty_raises():
    with pytest.raises(ValueError):
        _extract_json("")


def test_extract_json_no_json_raises():
    with pytest.raises(ValueError):
        _extract_json("just text, no braces here")


# ─── _bootstrap_cc_switch_env ───

def test_bootstrap_no_provider_raises(monkeypatch):
    fake_resolve = lambda: {"primary": None, "source": "unresolved", "warnings": ["x"]}
    monkeypatch.setattr(
        "config.cc_switch_route_provider.resolve_routes",
        fake_resolve,
        raising=False,
    )
    with pytest.raises(AdvisorClientError, match="cc-switch 無可用 provider"):
        _bootstrap_cc_switch_env()


def test_bootstrap_injects_env(monkeypatch):
    fake_primary = {
        "label": "Test",
        "model": "claude-sonnet-4-6",
        "provider_id": "test-provider-id",
        "env": {
            "ANTHROPIC_BASE_URL": "https://test.example.com",
            "ANTHROPIC_AUTH_TOKEN": "sk-test-token",
            "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        },
    }
    monkeypatch.setattr(
        "config.cc_switch_route_provider.resolve_routes",
        lambda: {"primary": fake_primary, "source": "cc_switch_settings", "warnings": []},
        raising=False,
    )
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    model, pid = _bootstrap_cc_switch_env()
    assert model == "claude-sonnet-4-6"
    assert pid == "test-provider-id"
    import os
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://test.example.com"
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "sk-test-token"


# ─── AdvisorClient（mock SDK）───

def _bootstrap_stub(env=None):
    """回傳一個 closure 取代 _bootstrap_cc_switch_env，避免實際 query db"""
    return lambda: ("claude-sonnet-4-6", "stub-provider")


def _make_client_with_mock_sdk(monkeypatch):
    monkeypatch.setattr(client_mod, "_bootstrap_cc_switch_env", _bootstrap_stub())
    fake_anthropic_module = MagicMock()
    fake_client_instance = MagicMock()
    fake_anthropic_module.Anthropic.return_value = fake_client_instance
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)
    c = AdvisorClient()
    return c, fake_client_instance


def test_client_init_uses_bootstrap(monkeypatch):
    c, _ = _make_client_with_mock_sdk(monkeypatch)
    assert c.model == "claude-sonnet-4-6"
    assert c.provider_id == "stub-provider"
    assert c._cache_works is None
    assert c._beta_headers is None


def test_client_decide_happy(monkeypatch):
    c, sdk = _make_client_with_mock_sdk(monkeypatch)
    # mock messages.create response
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = '{"decisions": [{"action": "hold"}], "summary": "x"}'
    sdk.messages.create.return_value = MagicMock(content=[mock_block])

    result = c.decide("system", {"k": "v"})
    assert result == {"decisions": [{"action": "hold"}], "summary": "x"}
    # cache 默認啟用
    call = sdk.messages.create.call_args
    sys_param = call.kwargs["system"]
    assert isinstance(sys_param, list)
    assert sys_param[0]["cache_control"] == {"type": "ephemeral"}


def test_client_decide_cache_unsupported_falls_back(monkeypatch):
    c, sdk = _make_client_with_mock_sdk(monkeypatch)

    # 第一次失敗 (cache 不支援)、第二次成功
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = '{"decisions": [{"action": "hold"}]}'

    side_effects = [
        Exception("cache_control not supported by this provider"),
        MagicMock(content=[mock_block]),
    ]
    sdk.messages.create.side_effect = side_effects

    result = c.decide("system", {"k": "v"})
    assert result["decisions"][0]["action"] == "hold"
    assert c._cache_works is False
    # 第二次 system 變成純 string
    second_call = sdk.messages.create.call_args_list[1]
    assert isinstance(second_call.kwargs["system"], str)


def test_client_decide_json_parse_failure_retry(monkeypatch):
    c, sdk = _make_client_with_mock_sdk(monkeypatch)

    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "garbage no json"
    good_block = MagicMock(); good_block.type = "text"; good_block.text = '{"ok": 1}'

    sdk.messages.create.side_effect = [
        MagicMock(content=[bad_block]),
        MagicMock(content=[good_block]),
    ]
    result = c.decide("system", {"k": "v"})
    assert result == {"ok": 1}


def test_client_decide_double_failure_raises(monkeypatch):
    c, sdk = _make_client_with_mock_sdk(monkeypatch)
    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "garbage"
    sdk.messages.create.return_value = MagicMock(content=[bad_block])

    with pytest.raises(AdvisorClientError, match="連兩次無法解析"):
        c.decide("system", {"k": "v"})
