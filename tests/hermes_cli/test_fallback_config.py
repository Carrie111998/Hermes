"""Tests for hermes_cli/fallback_config.py."""

from __future__ import annotations

from hermes_cli.fallback_config import (
    get_configured_fallback_chain,
    get_fallback_chain,
    get_fallback_policy,
    resolve_entry_api_key,
)


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"

    def test_key_env_resolves_from_environment(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"

    def test_api_key_env_alias(self, monkeypatch):
        monkeypatch.setenv("FB_ALIAS_KEY", "alias-key")
        assert resolve_entry_api_key({"api_key_env": "FB_ALIAS_KEY"}) == "alias-key"

    def test_unset_env_var_returns_none(self, monkeypatch):
        monkeypatch.delenv("FB_MISSING", raising=False)
        # None (not "") lets resolve_runtime_provider fall through to the
        # provider's standard credential resolution.
        assert resolve_entry_api_key({"key_env": "FB_MISSING"}) is None

    def test_empty_env_var_returns_none(self, monkeypatch):
        monkeypatch.setenv("FB_EMPTY", "   ")
        assert resolve_entry_api_key({"key_env": "FB_EMPTY"}) is None

    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None

    def test_non_dict_returns_none(self):
        assert resolve_entry_api_key(None) is None
        assert resolve_entry_api_key("nope") is None  # type: ignore[arg-type]

    def test_whitespace_inline_key_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"api_key": "   ", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "env-key"


def test_missing_policy_preserves_legacy_any_behavior():
    cfg = {
        "fallback_providers": [
            {"provider": "openrouter", "model": "remote"},
            {
                "provider": "custom",
                "model": "local",
                "base_url": "http://127.0.0.1:8000/v1",
            },
        ]
    }

    assert get_fallback_policy(cfg) == "any"
    assert get_fallback_chain(cfg) == cfg["fallback_providers"]


def test_off_keeps_configured_order_but_has_no_eligible_routes():
    cfg = {
        "fallback_policy": "off",
        "fallback_providers": [
            {"provider": "openrouter", "model": "first"},
            {"provider": "anthropic", "model": "second"},
        ],
    }

    assert get_configured_fallback_chain(cfg) == cfg["fallback_providers"]
    assert get_fallback_chain(cfg) == []


def test_local_only_uses_endpoint_metadata_not_model_names(monkeypatch):
    monkeypatch.setenv("LM_BASE_URL", "http://10.55.0.3:1234/v1")
    cfg = {
        "fallback_policy": "local-only",
        "fallback_providers": [
            {"provider": "opencode-zen", "model": "local-looking-name"},
            {"provider": "lmstudio", "model": "cloud-looking-name"},
            {"provider": "mystery", "model": "definitely-local"},
        ],
    }

    assert get_fallback_chain(cfg) == [
        {"provider": "lmstudio", "model": "cloud-looking-name"}
    ]


def test_local_only_does_not_reclassify_builtin_cloud_provider_via_env(
    monkeypatch,
):
    monkeypatch.setenv("OPENCODE_ZEN_BASE_URL", "http://127.0.0.1:9999/v1")
    cfg = {
        "fallback_policy": "local-only",
        "fallback_providers": [
            {"provider": "opencode-zen", "model": "remote-model"},
        ],
    }

    assert get_fallback_chain(cfg) == []


def test_local_only_rejects_builtin_anthropic_even_with_explicit_local_url():
    cfg = {
        "fallback_policy": "local-only",
        "fallback_providers": [
            {
                "provider": "anthropic",
                "model": "claude",
                "base_url": "http://localhost:9000/v1",
            }
        ],
    }

    assert get_fallback_chain(cfg) == []


def test_local_only_allows_explicit_user_provider_redefinition():
    cfg = {
        "fallback_policy": "local-only",
        "providers": {
            "anthropic": {
                "base_url": "http://10.55.0.3:9000/v1",
            }
        },
        "fallback_providers": [
            {"provider": "anthropic", "model": "local-compatible"}
        ],
    }

    assert get_fallback_chain(cfg) == [
        {"provider": "anthropic", "model": "local-compatible"}
    ]


def test_invalid_explicit_policy_fails_closed():
    cfg = {
        "fallback_policy": "ANY",
        "fallback_providers": [{"provider": "openrouter", "model": "remote"}],
    }

    assert get_fallback_policy(cfg) == "off"
    assert get_fallback_chain(cfg) == []
