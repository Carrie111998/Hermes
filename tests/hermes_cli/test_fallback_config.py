"""Tests for fallback config and invocation-scoped overrides."""

import pytest

from hermes_cli.fallback_config import get_fallback_chain, resolve_entry_api_key


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


class TestInvocationFallbackOverrides:
    def test_cli_overrides_replace_profile_chain_in_exact_order(self):
        config = {
            "fallback_providers": [
                {"provider": "openai-codex", "model": "gpt-5.4"}
            ]
        }

        assert get_fallback_chain(
            config,
            ["openai-codex/gpt-5.6-sol", "gemini/gemini-3.1-pro-preview"],
        ) == [
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
            {"provider": "gemini", "model": "gemini-3.1-pro-preview"},
        ]

    @pytest.mark.parametrize("value", ["", "missing-slash", "/model", "provider/"])
    def test_malformed_cli_override_fails_closed(self, value):
        with pytest.raises(ValueError, match="fallback"):
            get_fallback_chain({}, [value])

    def test_duplicate_cli_override_fails_closed(self):
        with pytest.raises(ValueError, match="duplicate fallback"):
            get_fallback_chain(
                {},
                ["openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-sol"],
            )
