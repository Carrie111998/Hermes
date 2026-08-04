"""Unit tests for the AgentRouter provider profiles.

AgentRouter registers two profiles from one plugin because it serves two wire
protocols on the same host:

  - ``agentrouter``            → OpenAI-compatible Chat Completions at /v1
  - ``agentrouter-anthropic``  → Anthropic Messages at the bare host (no /v1)

The invariants below are the ones downstream layers (auth, models, doctor,
runtime_provider, transport, anthropic_adapter) actually read.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def openai_profile():
    """Resolve the OpenAI-compatible AgentRouter profile via the registry.

    Importing ``model_tools`` triggers plugin discovery. Going through
    ``get_provider_profile`` keeps the test honest about the real registration
    path (name + alias resolution) rather than importing the plugin directly.
    """
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("agentrouter")
    assert profile is not None, "agentrouter provider profile must be registered"
    return profile


@pytest.fixture
def anthropic_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("agentrouter-anthropic")
    assert profile is not None, (
        "agentrouter-anthropic provider profile must be registered"
    )
    return profile


class TestOpenAICompatibleProfile:
    def test_identity_and_endpoint(self, openai_profile):
        assert openai_profile.name == "agentrouter"
        assert openai_profile.api_mode == "chat_completions"
        assert openai_profile.auth_type == "api_key"
        assert openai_profile.base_url == "https://agentrouter.org/v1"
        assert openai_profile.get_hostname() == "agentrouter.org"

    def test_env_vars(self, openai_profile):
        # API key first, optional base-url override second (priority order).
        assert openai_profile.env_vars == (
            "AGENTROUTER_API_KEY",
            "AGENTROUTER_BASE_URL",
        )

    def test_aliases_resolve_to_same_object(self, openai_profile):
        import providers

        for alias in ("agent-router", "agentrouter-openai"):
            assert providers.get_provider_profile(alias) is openai_profile

    def test_fallback_catalog_is_the_documented_openai_family(self, openai_profile):
        # The live /v1/models catalog needs auth, so the offline picker falls
        # back to this list. entry[0] is the setup default.
        assert openai_profile.fallback_models[0] == "gpt-5.5"
        assert set(openai_profile.fallback_models) == {"gpt-5.5", "gpt-5.6", "glm-5.2"}

    def test_no_pinned_aux_model(self, openai_profile):
        # AgentRouter publishes no cheap/small model, so auxiliary tasks
        # (compression, titles, vision) must reuse the main model.
        assert openai_profile.default_aux_model == ""


class TestAnthropicMessagesProfile:
    def test_identity_and_endpoint(self, anthropic_profile):
        assert anthropic_profile.name == "agentrouter-anthropic"
        assert anthropic_profile.api_mode == "anthropic_messages"
        assert anthropic_profile.auth_type == "api_key"
        # No /v1 — the Anthropic SDK appends /v1/messages to this base.
        assert anthropic_profile.base_url == "https://agentrouter.org"
        assert anthropic_profile.get_hostname() == "agentrouter.org"

    def test_alias_resolves(self, anthropic_profile):
        import providers

        assert providers.get_provider_profile("agentrouter-claude") is anthropic_profile

    def test_shares_the_openai_api_key(self, anthropic_profile):
        assert anthropic_profile.env_vars[0] == "AGENTROUTER_API_KEY"

    def test_models_url_points_at_the_v1_catalog(self, anthropic_profile):
        # The bare Anthropic host has no /models route; without the explicit
        # models_url the default {base_url}/models probe (and the doctor health
        # check that reuses it) would hit https://agentrouter.org/models.
        assert anthropic_profile.models_url == "https://agentrouter.org/v1/models"

    def test_fallback_catalog_is_the_documented_claude_family(self, anthropic_profile):
        assert anthropic_profile.fallback_models[0] == "claude-opus-4-6"
        assert set(anthropic_profile.fallback_models) == {
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-opus-4-8",
        }

    def test_is_a_distinct_profile_from_the_openai_route(
        self, anthropic_profile, openai_profile
    ):
        assert anthropic_profile is not openai_profile


class TestAnthropicBearerAuth:
    """AgentRouter keys are ``ak-…`` and must go out as Authorization: Bearer.

    Without the ``_requires_bearer_auth`` entry the adapter would fall through
    to OAuth-shape detection and then to Anthropic's native ``x-api-key``,
    which the relay rejects with 401.
    """

    def test_relay_host_requires_bearer(self):
        from agent.anthropic_adapter import _requires_bearer_auth

        assert _requires_bearer_auth("https://agentrouter.org") is True
        assert _requires_bearer_auth("https://agentrouter.org/") is True

    def test_native_anthropic_still_uses_x_api_key(self):
        from agent.anthropic_adapter import _requires_bearer_auth

        assert _requires_bearer_auth("https://api.anthropic.com") is False

    def test_lookalike_hosts_do_not_opt_into_bearer(self):
        from agent.anthropic_adapter import _requires_bearer_auth

        # Hostname match, never bare substring: neither a suffix-spoof host nor
        # a path segment may borrow AgentRouter's auth scheme.
        assert _requires_bearer_auth("https://agentrouter.org.attacker.test") is False
        assert _requires_bearer_auth("https://proxy.test/agentrouter.org/v1") is False
