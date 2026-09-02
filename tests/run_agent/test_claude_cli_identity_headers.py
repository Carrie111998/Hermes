from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.agent_runtime_helpers import create_openai_client


def test_rebuilt_claude_client_keeps_identity_header_for_any_provider(monkeypatch):
    agent = SimpleNamespace(
        model="claude-opus-4-8",
        provider="custom",
        requested_provider="relay-a",
        base_url="http://proxy.example/v1",
        session_id=None,
        _build_keepalive_http_client=lambda *_a, **_k: None,
        _client_log_context=lambda: "test",
    )
    kwargs = {
        "api_key": "test-key",
        "base_url": "http://proxy.example/v1",
        "default_headers": {"X-Existing": "kept"},
    }
    captured = {}

    def fake_openai(**client_kwargs):
        captured.update(client_kwargs)
        return MagicMock()

    monkeypatch.setattr("agent.ssl_verify.resolve_httpx_verify", lambda *_a, **_k: None)
    monkeypatch.setattr("agent.auxiliary_client._validate_base_url", lambda *_a, **_k: None)
    monkeypatch.setattr("agent.auxiliary_client._validate_proxy_env_urls", lambda: None)
    with patch("openai.OpenAI", side_effect=fake_openai):
        create_openai_client(agent, kwargs, reason="test", shared=False)

    assert captured["default_headers"] == {
        "X-Existing": "kept",
        "User-Agent": "claude-cli/2.1.233 (external, sdk-cli)",
    }
    assert kwargs["default_headers"] == {"X-Existing": "kept"}


def test_rebuilt_non_claude_client_does_not_use_claude_identity(monkeypatch):
    agent = SimpleNamespace(
        model="gpt-5.6-sol",
        provider="custom",
        requested_provider="relay-a",
        base_url="http://proxy.example/v1",
        session_id=None,
        _build_keepalive_http_client=lambda *_a, **_k: None,
        _client_log_context=lambda: "test",
    )
    kwargs = {"api_key": "test-key", "base_url": "http://proxy.example/v1"}
    captured = {}

    def fake_openai(**client_kwargs):
        captured.update(client_kwargs)
        return MagicMock()

    monkeypatch.setattr("agent.ssl_verify.resolve_httpx_verify", lambda *_a, **_k: None)
    monkeypatch.setattr("agent.auxiliary_client._validate_base_url", lambda *_a, **_k: None)
    monkeypatch.setattr("agent.auxiliary_client._validate_proxy_env_urls", lambda: None)
    with patch("openai.OpenAI", side_effect=fake_openai):
        create_openai_client(agent, kwargs, reason="test", shared=False)

    assert "User-Agent" not in (captured.get("default_headers") or {})