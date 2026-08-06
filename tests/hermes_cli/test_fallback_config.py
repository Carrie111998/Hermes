"""Tests for hermes_cli/fallback_config.py."""

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.fallback_config import get_fallback_chain, resolve_entry_api_key


class TestGetFallbackChain:
    def test_json_string_fallback_providers_is_parsed_as_chain(self):
        config = {
            "fallback_providers": '[{"provider":"openai-api","model":"gpt-5.5"}]'
        }

        assert get_fallback_chain(config) == [
            {"provider": "openai-api", "model": "gpt-5.5"}
        ]

    def test_json_string_fallback_model_dict_is_parsed_as_legacy_entry(self):
        config = {
            "fallback_model": '{"provider":"openrouter","model":"anthropic/claude-sonnet-4"}'
        }

        assert get_fallback_chain(config) == [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}
        ]

    def test_non_json_string_fallback_is_ignored(self):
        assert get_fallback_chain({"fallback_providers": "openai-api/gpt-5.5"}) == []


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

    def test_key_env_resolves_from_active_secret_scope_not_raw_env(self, monkeypatch):
        # Multiplexed gateway: os.environ holds another profile's key, but the
        # active per-turn secret scope holds this profile's key. The scoped
        # value must win — a raw os.getenv() would leak the other profile's
        # credential (issue #74311).
        monkeypatch.setenv("FB_KEY", "fake-other-profile-key")
        token = set_secret_scope({"FB_KEY": "fake-active-profile-key"})
        try:
            assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "fake-active-profile-key"
        finally:
            reset_secret_scope(token)

    def test_key_env_falls_back_to_env_when_no_active_scope(self, monkeypatch):
        # Non-multiplexed / single-profile behavior must be unchanged: with no
        # secret scope installed, resolution still reads os.environ.
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"
