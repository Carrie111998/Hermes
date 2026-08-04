"""Credential pools must never cross provider or custom-endpoint boundaries."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.credential_pool import (
    credential_pool_matches_provider,
    get_custom_provider_pool_key,
)
from hermes_cli import runtime_provider as rp


def test_provider_match_requires_exact_non_custom_identity():
    assert credential_pool_matches_provider("deepseek", "deepseek")
    assert not credential_pool_matches_provider("openai-codex", "deepseek")
    assert not credential_pool_matches_provider("", "deepseek")


def test_custom_pool_match_is_scoped_by_endpoint():
    with patch(
        "agent.credential_pool.get_custom_provider_pool_key",
        return_value="custom:lab",
    ):
        assert credential_pool_matches_provider(
            "custom:lab", "custom", base_url="https://lab.example/v1"
        )
        assert not credential_pool_matches_provider(
            "custom:other", "custom", base_url="https://lab.example/v1"
        )


def test_custom_pool_match_is_scoped_by_named_provider_when_urls_collide():
    calls = []

    def pool_key(base_url, provider_name=None):
        calls.append(provider_name)
        return "custom:provider-b" if provider_name == "provider-b" else "custom:provider-a"

    with patch(
        "agent.credential_pool.get_custom_provider_pool_key",
        side_effect=pool_key,
    ):
        assert credential_pool_matches_provider(
            "custom:provider-b",
            "custom",
            base_url="https://gateway.example/v1",
            provider_name="provider-b",
        )

    assert calls == ["provider-b"]


def test_agent_init_keeps_matching_named_custom_pool_when_urls_collide():
    pool = SimpleNamespace(provider="custom:provider-b")

    with patch(
        "agent.credential_pool.get_custom_provider_pool_key",
        side_effect=lambda _url, provider_name=None: (
            "custom:provider-b" if provider_name == "provider-b" else "custom:provider-a"
        ),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            provider="custom",
            requested_provider="provider-b",
            base_url="https://gateway.example/v1",
            api_key="test-key",
            model="test-model",
            credential_pool=pool,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent._credential_pool is pool


def test_named_pool_key_normalizes_raw_and_prefixed_identity(monkeypatch):
    config = {
        "custom_providers": [
            {
                "name": "provider-a",
                "base_url": "https://gateway.example/v1",
            },
            {
                "name": "provider-b",
                "base_url": "https://gateway.example/v1",
            },
        ]
    }
    monkeypatch.setattr("agent.credential_pool._load_config_safe", lambda: config)

    assert (
        get_custom_provider_pool_key(
            "https://gateway.example/v1/",
            provider_name="provider-b",
        )
        == "custom:provider-b"
    )
    assert (
        get_custom_provider_pool_key(
            "https://gateway.example/v1/",
            provider_name="custom:provider-b",
        )
        == "custom:provider-b"
    )


def test_explicit_named_pool_identity_fails_closed(monkeypatch):
    config = {
        "custom_providers": [
            {
                "name": "provider-a",
                "base_url": "https://gateway.example/v1",
            },
            {
                "name": "provider-b",
                "base_url": "https://gateway.example/v1",
            },
        ]
    }
    monkeypatch.setattr("agent.credential_pool._load_config_safe", lambda: config)

    assert (
        get_custom_provider_pool_key(
            "https://gateway.example/v1",
            provider_name="custom:deleted-provider",
        )
        is None
    )
    assert (
        get_custom_provider_pool_key(
            "https://other.example/v1",
            provider_name="custom:provider-b",
        )
        is None
    )


def test_identityless_custom_pool_lookup_keeps_legacy_url_fallback(monkeypatch):
    config = {
        "custom_providers": [
            {
                "name": "provider-a",
                "base_url": "https://gateway.example/v1",
            },
            {
                "name": "provider-b",
                "base_url": "https://gateway.example/v1",
            },
        ]
    }
    monkeypatch.setattr("agent.credential_pool._load_config_safe", lambda: config)

    assert (
        get_custom_provider_pool_key("https://gateway.example/v1")
        == "custom:provider-a"
    )


def test_runtime_provider_pool_match_passes_named_identity(monkeypatch):
    class _Entry:
        runtime_api_key = "account-b-key"
        base_url = "https://gateway.example/v1"

    class _Pool:
        provider = "custom:provider-b"

        def has_credentials(self):
            return True

        def select(self):
            return _Entry()

    pool = _Pool()
    seen = {}
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "custom")
    monkeypatch.setattr(rp, "_get_model_config", lambda: {})
    monkeypatch.setattr(rp, "_resolve_named_custom_runtime", lambda **kwargs: None)
    monkeypatch.setattr(rp, "load_pool", lambda _provider: pool)
    monkeypatch.setattr(
        rp,
        "credential_pool_matches_provider",
        lambda *args, **kwargs: seen.update(kwargs) or True,
    )
    monkeypatch.setattr(
        rp,
        "_resolve_runtime_from_pool_entry",
        lambda **kwargs: {"provider": "custom", "requested_provider": "custom:provider-b"},
    )

    resolved = rp.resolve_runtime_provider(requested="custom:provider-b")

    assert resolved["provider"] == "custom"
    assert seen["provider_name"] == "custom:provider-b"


def test_runtime_ignores_pool_loaded_for_different_provider(monkeypatch):
    entry = SimpleNamespace(
        provider="openai-codex",
        access_token="wrong-token",
        runtime_api_key="wrong-token",
        runtime_base_url="https://chatgpt.com/backend-api/codex",
        base_url="https://chatgpt.com/backend-api/codex",
    )
    pool = SimpleNamespace(
        provider="openai-codex",
        has_credentials=lambda: True,
        select=lambda: entry,
    )
    monkeypatch.setattr(rp, "load_pool", lambda _provider: pool)
    monkeypatch.setattr(rp, "resolve_provider", lambda *_a, **_kw: "deepseek")
    monkeypatch.setattr(
        rp,
        "_get_model_config",
        lambda: {"provider": "deepseek", "default": "deepseek-chat"},
    )
    monkeypatch.setattr(
        rp,
        "resolve_api_key_provider_credentials",
        lambda _provider: {
            "provider": "deepseek",
            "api_key": "deepseek-key",
            "base_url": "https://api.deepseek.com/v1",
            "source": "env",
        },
    )

    resolved = rp.resolve_runtime_provider(requested="deepseek")

    assert resolved["provider"] == "deepseek"
    assert resolved["api_key"] == "deepseek-key"
    assert resolved["base_url"] == "https://api.deepseek.com/v1"