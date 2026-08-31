"""Tests for hermes_cli/fallback_config.py — fallback entry API-key resolution."""

from agent.secret_scope import reset_secret_scope, set_secret_scope
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

    def test_key_env_resolves_from_active_secret_scope_not_raw_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "fake-other-profile-key")
        token = set_secret_scope({"FB_KEY": "fake-active-profile-key"})
        try:
            assert (
                resolve_entry_api_key({"key_env": "FB_KEY"})
                == "fake-active-profile-key"
            )
        finally:
            reset_secret_scope(token)

    def test_key_env_falls_back_to_env_when_no_active_scope(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        assert resolve_entry_api_key({"key_env": "FB_KEY"}) == "env-key"


class TestFallbackChainIdentity:
    def test_distinct_entry_credentials_are_not_deduplicated(self):
        config = {
            "fallback_providers": [
                {
                    "provider": "custom",
                    "model": "m",
                    "base_url": "https://gw/v1",
                    "api_key": "key-a",
                },
                {
                    "provider": "custom",
                    "model": "m",
                    "base_url": "https://gw/v1",
                    "api_key": "key-b",
                },
            ]
        }
        chain = get_fallback_chain(config)
        assert len(chain) == 2

    def test_identical_entry_credentials_are_deduplicated(self):
        entry = {
            "provider": "custom",
            "model": "m",
            "base_url": "https://gw/v1",
            "api_key": "same",
        }
        assert (
            len(get_fallback_chain({"fallback_providers": [entry, dict(entry)]})) == 1
        )

    def test_distinct_unresolved_key_env_entries_stay_distinct(self, monkeypatch):
        """Credential-source identity must survive when env values are absent."""
        monkeypatch.delenv("FB_KEY_A", raising=False)
        monkeypatch.delenv("FB_KEY_B", raising=False)
        common = {
            "provider": "custom",
            "model": "m",
            "base_url": "https://gw/v1",
        }
        entries = [
            {**common, "key_env": "FB_KEY_A"},
            {**common, "key_env": "FB_KEY_B"},
            {**common, "api_key": "inline-key"},
            {**common, "credential_pool": "pool-a"},
        ]
        # Even without resolving either env var, all four credential surfaces
        # remain selectable; collapsing the two key_env rows strands one.
        assert len(get_fallback_chain({"fallback_providers": entries})) == 4

    def test_path_case_is_preserved_for_deduplication(self):
        config = {
            "fallback_providers": [
                {
                    "provider": "custom",
                    "model": "m",
                    "base_url": "https://gw/v1/Models",
                },
                {
                    "provider": "custom",
                    "model": "m",
                    "base_url": "https://gw/v1/models",
                },
            ]
        }
        assert len(get_fallback_chain(config)) == 2
