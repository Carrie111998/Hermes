"""Tests for hermes_cli/fallback_config.py — fallback entry API-key resolution."""

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.fallback_config import (
    compose_fallback_chain,
    get_configured_default_route,
    resolve_entry_api_key,
)


def test_configured_default_precedes_fallbacks_for_session_override():
    configured_default = {
        "provider": "commandcode",
        "model": "deepseek-v4-flash",
        "base_url": "https://commandcode.ai/v1/",
    }
    chain = [
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
    ]

    effective = compose_fallback_chain(
        chain,
        primary={"provider": "openrouter", "model": "override-model"},
        configured_default=configured_default,
    )

    assert effective == [
        {
            "provider": "commandcode",
            "model": "deepseek-v4-flash",
            "base_url": "https://commandcode.ai/v1",
        },
        *chain,
    ]


def test_configured_default_is_not_duplicated_as_primary_or_fallback():
    configured_default = {
        "provider": "commandcode",
        "model": "deepseek-v4-flash",
    }
    configured_chain = [configured_default, configured_default.copy()]

    assert compose_fallback_chain(
        configured_chain,
        primary=configured_default,
        configured_default=configured_default,
    ) == []


def test_get_configured_default_route_uses_requested_runtime_provider():
    route = get_configured_default_route(
        {
            "model": {
                "default": "model-a",
                "provider": "custom:free-lane",
                "base_url": "https://free.example/v1/",
            }
        },
        runtime={
            "provider": "custom",
            "requested_provider": "custom:resolved-lane",
            "base_url": "https://resolved.example/v1",
            "api_mode": "chat_completions",
        },
    )

    assert route == {
        "provider": "custom:free-lane",
        "model": "model-a",
        "base_url": "https://free.example/v1",
        "api_mode": "chat_completions",
    }


def test_configured_default_does_not_borrow_auth_fallback_provider():
    for fallback_model in ("fallback-model", "model-a"):
        route = get_configured_default_route(
            {"model": {"default": "model-a"}},
            runtime={
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": fallback_model,
                "base_url": "https://openrouter.ai/api/v1",
            },
        )

        assert route is None


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
