"""Tests for fallback config and invocation-scoped overrides."""

import pytest

from hermes_cli.fallback_config import get_fallback_chain, resolve_entry_api_key


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"


    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None


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
