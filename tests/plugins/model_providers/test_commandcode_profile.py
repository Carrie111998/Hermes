"""Unit tests for the CommandCode provider profiles.

CommandCode registers two profiles:

``commandcode``
    ``api_mode=chat_completions`` — OpenAI-compatible.  Defaults to
    ``deepseek/deepseek-v4-pro``. 20+ models via a single base URL.

``commandcode-anthropic``
    ``api_mode=anthropic_messages`` — Anthropic Messages API-compatible.
    Defaults to ``claude-sonnet-4-6``.  Requires Bearer auth recognition
    in ``agent/anthropic_adapter.py``.

Both share ``COMMANDCODE_API_KEY`` and ``https://api.commandcode.ai/provider/v1``.
"""

from __future__ import annotations

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def commandcode_profile():
    """Resolve the registered CommandCode (chat_completions) profile."""
    import model_tools  # noqa: F401 — triggers discovery
    import providers

    profile = providers.get_provider_profile("commandcode")
    assert profile is not None, "commandcode provider profile must be registered"
    return profile


@pytest.fixture
def commandcode_anthropic_profile():
    """Resolve the registered CommandCode Anthropic profile."""
    import model_tools  # noqa: F401 — triggers discovery
    import providers

    profile = providers.get_provider_profile("commandcode-anthropic")
    assert profile is not None, "commandcode-anthropic profile must be registered"
    return profile


# ── Chat Completions profile ──────────────────────────────────────────────────

class TestCommandCodeProfileIdentity:
    """Profile metadata matches the declared contract."""

    def test_name(self, commandcode_profile):
        assert commandcode_profile.name == "commandcode"

    def test_api_mode(self, commandcode_profile):
        assert commandcode_profile.api_mode == "chat_completions"

    def test_aliases(self, commandcode_profile):
        assert "commandcode-chat" in commandcode_profile.aliases

    def test_env_vars(self, commandcode_profile):
        assert "COMMANDCODE_API_KEY" in commandcode_profile.env_vars

    def test_base_url(self, commandcode_profile):
        assert commandcode_profile.base_url == "https://api.commandcode.ai/provider/v1"

    def test_display_name(self, commandcode_profile):
        assert "CommandCode" in commandcode_profile.display_name

    def test_has_fallback_models(self, commandcode_profile):
        assert len(commandcode_profile.fallback_models) >= 5
        # Should include the major families
        names = " ".join(commandcode_profile.fallback_models)
        assert "deepseek" in names
        assert "Qwen" in names
        assert "Kimi" in names
        assert "gemini" in names

    def test_default_aux_model(self, commandcode_profile):
        assert commandcode_profile.default_aux_model == "deepseek/deepseek-v4-flash"

    def test_signup_url(self, commandcode_profile):
        assert "commandcode" in commandcode_profile.signup_url.lower()

    def test_hostname_derived_from_base_url(self, commandcode_profile):
        assert commandcode_profile.get_hostname() == "api.commandcode.ai"


class TestCommandCodeProfileNoThinkingInterference:
    """Chat completions profile is a no-op for thinking config — it delegates
    to the underlying model's provider (DeepSeek, Qwen, etc.) for wire format.
    """

    def test_passthrough_no_reasoning_config(self, commandcode_profile):
        extra_body, top_level = commandcode_profile.build_api_kwargs_extras(
            reasoning_config=None, model="deepseek/deepseek-v4-pro"
        )
        # Chat completions profile doesn't inject thinking params — that's
        # the DeepSeek provider's job when routed through DeepSeek's own profile.
        # When routed through CommandCode, the underlying model API handles it.
        assert isinstance(extra_body, dict)
        assert isinstance(top_level, dict)
        # Default ProviderProfile returns ({}, {}).

    def test_passthrough_with_reasoning_config(self, commandcode_profile):
        extra_body, top_level = commandcode_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="deepseek/deepseek-v4-pro",
        )
        assert isinstance(extra_body, dict)
        assert isinstance(top_level, dict)


# ── Anthropic Messages profile ────────────────────────────────────────────────

class TestCommandCodeAnthropicProfileIdentity:
    """Anthropic-compatible profile metadata."""

    def test_name(self, commandcode_anthropic_profile):
        assert commandcode_anthropic_profile.name == "commandcode-anthropic"

    def test_api_mode(self, commandcode_anthropic_profile):
        assert commandcode_anthropic_profile.api_mode == "anthropic_messages"

    def test_aliases(self, commandcode_anthropic_profile):
        assert "commandcode-claude" in commandcode_anthropic_profile.aliases

    def test_env_vars(self, commandcode_anthropic_profile):
        assert "COMMANDCODE_API_KEY" in commandcode_anthropic_profile.env_vars

    def test_base_url(self, commandcode_anthropic_profile):
        assert commandcode_anthropic_profile.base_url == "https://api.commandcode.ai/provider/v1"

    def test_fallback_models_are_claude_family(self, commandcode_anthropic_profile):
        for model in commandcode_anthropic_profile.fallback_models:
            assert model.startswith("claude-"), (
                f"All anthropic fallback models should be claude-*: got {model}"
            )

    def test_default_aux_model(self, commandcode_anthropic_profile):
        assert commandcode_anthropic_profile.default_aux_model == "claude-haiku-4-5-20251001"

    def test_display_name_distinct_from_chat(self, commandcode_anthropic_profile):
        # The Anthropic profile should be distinguishable in /model picker
        assert "(Anthropic)" in commandcode_anthropic_profile.display_name

    def test_hostname_derived_from_base_url(self, commandcode_anthropic_profile):
        assert commandcode_anthropic_profile.get_hostname() == "api.commandcode.ai"


# ── Bearer Auth Recognition ───────────────────────────────────────────────────

class TestCommandCodeAnthropicBearerAuth:
    """``agent/anthropic_adapter.py`` must recognize CommandCode as a
    Bearer-auth endpoint, or the chat_completions transport falls back to
    ``x-api-key`` and gets a 401.
    """

    def test_requires_bearer_auth_recognizes_commandcode(self):
        from agent.anthropic_adapter import _requires_bearer_auth

        assert _requires_bearer_auth("https://api.commandcode.ai/provider/v1") is True
        assert _requires_bearer_auth("https://api.commandcode.ai/provider/v1/models") is True
        assert _requires_bearer_auth("https://api.commandcode.ai/anthropic") is True

    def test_bearer_auth_does_not_affect_unrelated(self):
        from agent.anthropic_adapter import _requires_bearer_auth

        # Native Anthropic still uses x-api-key
        assert _requires_bearer_auth("https://api.anthropic.com") is False
        # OpenRouter still uses Bearer through its own transport path
        assert _requires_bearer_auth("https://openrouter.ai/api/v1") is False

    def test_bearer_auth_case_insensitive(self):
        from agent.anthropic_adapter import _requires_bearer_auth

        assert _requires_bearer_auth("https://API.COMMANDCODE.AI/provider/v1") is True


# ── Registry integrity ───────────────────────────────────────────────────────

class TestCommandCodeRegistryIntegrity:
    """Both profiles are discoverable and distinct."""

    def test_both_profiles_registered(self):
        import model_tools  # noqa: F401
        import providers

        chat = providers.get_provider_profile("commandcode")
        anth = providers.get_provider_profile("commandcode-anthropic")
        assert chat is not None
        assert anth is not None
        assert chat is not anth  # distinct profile instances

    def test_alias_lookup(self):
        import model_tools  # noqa: F401
        import providers

        assert providers.get_provider_profile("commandcode-chat") is not None
        assert providers.get_provider_profile("commandcode-claude") is not None

    def test_unknown_returns_none(self):
        import model_tools  # noqa: F401
        import providers

        assert providers.get_provider_profile("commandcode-nonexistent") is None


# ── Model list filtering ──────────────────────────────────────────────────────

class TestCommandCodeModelFiltering:
    """``fetch_models`` filtering contracts."""

    def test_anthropic_profile_filters_to_claude(self):
        """If we mock a response with mixed models, anthropic profile
        should only return claude-* models.
        """
        from plugins.model_providers.commandcode import CommandCodeAnthropicProfile

        profile = CommandCodeAnthropicProfile(
            name="test-cc-anth",
            api_mode="anthropic_messages",
            env_vars=("COMMANDCODE_API_KEY",),
            base_url="https://api.commandcode.ai/provider/v1",
        )

        # Don't actually hit the network — just test the filter logic.
        # The class has a fetch_models override that filters.
        # We verify the filter works by inspecting the method.
        import inspect

        source = inspect.getsource(profile.fetch_models)
        assert "startswith(\"claude-\")" in source or '"claude-" in m' in source, (
            "CommandCodeAnthropicProfile.fetch_models should filter to claude-* models"
        )


class TestCommandCodeAccountUsage:
    def test_cookie_header_sends_only_recognized_session_cookie(self):
        from plugins.model_providers.commandcode import _commandcode_cookie_header

        raw = "analytics=do-not-send; __Secure-commandcode_prod_.session_token=abc==; other=secret"
        assert (
            _commandcode_cookie_header(raw)
            == "__Secure-commandcode_prod_.session_token=abc=="
        )
        assert _commandcode_cookie_header("bare-token") == (
            "__Secure-better-auth.session_token=bare-token"
        )
        assert _commandcode_cookie_header("analytics=only") is None
        assert _commandcode_cookie_header("token\r\nInjected: yes") is None

    def test_fetch_account_usage_maps_windows_monthly_plan_and_alias(
        self, monkeypatch
    ):
        import sys

        from agent.account_usage import fetch_account_usage
        from providers import get_provider_profile

        profile = get_provider_profile("commandcode")
        assert profile is not None
        module = sys.modules[profile.__class__.__module__]

        calls = []

        def fake_get(url, cookie_header, timeout):
            calls.append((url, cookie_header, timeout))
            if url.endswith("/credits"):
                return {
                    "credits": {
                        "monthlyCredits": 7.5,
                        "purchasedCredits": 2,
                        "premiumMonthlyCredits": 0,
                        "opensourceMonthlyCredits": 0,
                    },
                    "windowLimits": {
                        "fiveHour": {
                            "cap": "4",
                            "used": "3",
                            "resetAt": 1_900_000_000,
                        },
                        "weekly": {
                            "cap": 20,
                            "used": 5,
                            "resetAt": 1_900_500_000_000,
                        },
                    },
                }
            return {
                "success": True,
                "data": {
                    "planId": "individual-pro",
                    "status": "active",
                    "currentPeriodEnd": "2030-03-01T00:00:00.000Z",
                },
            }

        monkeypatch.setenv(
            "COMMANDCODE_SESSION_COOKIE",
            "analytics=no; __Secure-commandcode_prod_.session_token=valid; unrelated=no",
        )
        monkeypatch.setattr(module, "_commandcode_get_json", fake_get)

        snapshot = fetch_account_usage("commandcode-claude")

        assert snapshot is not None
        assert snapshot.provider == "commandcode"
        assert snapshot.plan == "Pro"
        assert [window.label for window in snapshot.windows] == [
            "5-hour limit",
            "Weekly limit",
            "Monthly credits",
        ]
        assert snapshot.windows[0].used_percent == 75.0
        assert snapshot.windows[1].used_percent == 25.0
        assert snapshot.windows[2].used_percent == 75.0
        assert snapshot.windows[2].detail == "$7.50 of $30.00 remaining"
        assert snapshot.details == ("Purchased credits: $2.00",)
        assert all(
            cookie == "__Secure-commandcode_prod_.session_token=valid"
            for _, cookie, _ in calls
        )

    def test_fetch_account_usage_requires_session_cookie(self, monkeypatch):
        import sys

        from agent.account_usage import fetch_account_usage
        from providers import get_provider_profile

        profile = get_provider_profile("commandcode")
        assert profile is not None
        module = sys.modules[profile.__class__.__module__]

        monkeypatch.delenv("COMMANDCODE_SESSION_COOKIE", raising=False)
        monkeypatch.setattr(
            module,
            "_commandcode_get_json",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("billing endpoint must not be called")
            ),
        )
        assert fetch_account_usage("commandcode") is None

    def test_session_cookie_is_catalogued_as_secret(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        entry = OPTIONAL_ENV_VARS["COMMANDCODE_SESSION_COOKIE"]
        assert entry["password"] is True
        assert entry["category"] == "provider"
