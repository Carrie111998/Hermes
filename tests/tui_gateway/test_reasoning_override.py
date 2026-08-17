"""Session reasoning overrides must not ship provider-invalid efforts (#87036)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import tui_gateway.server as server
from tui_gateway.reasoning_override import (
    parse_create_reasoning_override,
    supported_session_reasoning_override,
)
from tui_gateway.server import _session_info


def test_codex_alias_still_drops_ultra(monkeypatch):
    import tui_gateway.reasoning_override as ro

    monkeypatch.setattr(ro, "_canonical_provider", lambda _provider: "openai-codex")
    parsed = parse_create_reasoning_override(
        "ultra", provider="chatgpt-codex", model="gpt-5.4"
    )
    assert parsed is None


def test_codex_drops_ultra_override():
    parsed = parse_create_reasoning_override(
        "ultra", provider="openai-codex", model="gpt-5.4"
    )
    assert parsed is None


def test_codex_keeps_xhigh_override():
    parsed = parse_create_reasoning_override(
        "xhigh", provider="openai-codex", model="gpt-5.4"
    )
    assert parsed == {"enabled": True, "effort": "xhigh"}


def test_unknown_provider_keeps_ultra():
    # Custom OpenAI-compat (GLM/ARK) documents max/xhigh; do not guess.
    parsed = parse_create_reasoning_override(
        "ultra", provider="custom:my-proxy", model="glm-5.2"
    )
    assert parsed == {"enabled": True, "effort": "ultra"}


def test_disabled_override_always_kept():
    parsed = parse_create_reasoning_override(
        "none", provider="openai-codex", model="gpt-5.4"
    )
    assert parsed == {"enabled": False}


def test_supported_override_passthrough_none_dict():
    assert supported_session_reasoning_override(None, provider="openai-codex") is None


def test_session_create_drops_codex_ultra(monkeypatch):
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    try:
        resp = server._methods["session.create"](
            "r1",
            {
                "cols": 80,
                "model": "gpt-5.4",
                "provider": "openai-codex",
                "reasoning_effort": "ultra",
            },
        )
        sid = resp["result"]["session_id"]
        assert server._sessions[sid]["create_reasoning_override"] is None
        assert resp["result"]["info"].get("reasoning_override") is False
    finally:
        server._sessions.clear()


def test_session_create_keeps_anthropic_high(monkeypatch):
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    try:
        resp = server._methods["session.create"](
            "r1",
            {
                "cols": 80,
                "model": "claude-sonnet-4.6",
                "provider": "anthropic",
                "reasoning_effort": "high",
            },
        )
        sid = resp["result"]["session_id"]
        assert server._sessions[sid]["create_reasoning_override"] == {
            "enabled": True,
            "effort": "high",
        }
        assert resp["result"]["info"]["reasoning_override"] is True
        assert resp["result"]["info"]["reasoning_effort"] == "high"
    finally:
        server._sessions.clear()


def test_config_set_default_clears_session_override():
    agent = SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "ultra"},
        service_tier=None,
        model="gpt-5.4",
        provider="openai-codex",
        session_id="k1",
    )
    session = {
        "session_key": "k1",
        "agent": agent,
        "create_reasoning_override": {"enabled": True, "effort": "ultra"},
    }
    with patch.dict(server._sessions, {"s1": session}, clear=False), \
            patch.object(server, "_load_reasoning_config", return_value={"enabled": True, "effort": "xhigh"}), \
            patch.object(server, "_persist_live_session_runtime"), \
            patch.object(server, "_emit"):
        resp = server._methods["config.set"](
            "rid-1", {"key": "reasoning", "session_id": "s1", "value": "default"}
        )
    assert "error" not in resp
    assert "create_reasoning_override" not in session
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
    assert resp["result"]["value"] == "xhigh"


def test_config_set_rejects_codex_ultra():
    agent = SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "high"},
        service_tier=None,
        model="gpt-5.4",
        provider="openai-codex",
        session_id="k1",
    )
    session = {"session_key": "k1", "agent": agent}
    with patch.dict(server._sessions, {"s1": session}, clear=False):
        resp = server._methods["config.set"](
            "rid-1", {"key": "reasoning", "session_id": "s1", "value": "ultra"}
        )
    assert "error" in resp
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_make_agent_drop_clears_session_override():
    session = {
        "create_reasoning_override": {"enabled": True, "effort": "ultra"},
    }
    with patch.dict(server._sessions, {"s-drop": session}, clear=False), \
            patch.object(server, "_load_reasoning_config", return_value={"enabled": True, "effort": "xhigh"}):
        got = server._resolve_agent_reasoning_config(
            {"enabled": True, "effort": "ultra"},
            provider="openai-codex",
            model="gpt-5.4",
            sid="s-drop",
        )
    assert got == {"enabled": True, "effort": "xhigh"}
    assert "create_reasoning_override" not in session


def test_session_info_reports_override_flag():
    agent = SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "high"},
        service_tier=None,
        model="glm-5",
        provider="zai",
        session_id="sess-key",
    )
    info = _session_info(
        agent,
        {"create_reasoning_override": {"enabled": True, "effort": "high"}, "agent": agent},
    )
    assert info["reasoning_override"] is True
    assert info["reasoning_effort"] == "high"
