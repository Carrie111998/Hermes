"""Runtime transport precedence: declared provider transport is the fallback.

The Coatue data-residency report (2026-07): pointing ``openai-api`` at
``us.api.openai.com`` silently fell back to ``chat_completions`` — every
tool-calling turn 400'd — because the runtime resolvers defaulted to
``chat_completions`` and consulted URL detection only, never the transport
the provider overlay itself declares.

Contract pinned here: when URL detection has no opinion, the runtime falls
back to ``providers.determine_api_mode(provider, base_url, model)`` (the
provider's declared transport), and only lands on ``chat_completions`` for
genuinely unknown providers/endpoints. Covers the explicit-runtime path and
the API-key-provider path; the pool-entry path shares the same helper.
"""

from __future__ import annotations

from unittest.mock import patch as mock_patch

import pytest

from hermes_cli.runtime_provider import _fallback_api_mode


class TestFallbackApiMode:
    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.openai.com/v1",
            "https://us.api.openai.com/v1",
            "https://eu.api.openai.com/v1",
        ],
    )
    def test_openai_api_official_hosts_resolve_codex_responses(self, base_url):
        assert _fallback_api_mode("openai-api", base_url) == "codex_responses"

    def test_openai_api_unknown_custom_proxy_still_uses_declared_transport(self):
        # Explicitly selected openai-api against a custom proxy keeps the
        # provider's declared transport (mirrors determine_api_mode semantics;
        # host identity is a separate question from provider selection).
        assert (
            _fallback_api_mode("openai-api", "https://proxy.corp.test/v1")
            == "codex_responses"
        )

    def test_lookalike_host_is_not_treated_as_official(self):
        # The spoof host must not be detected AS OpenAI by the URL lane —
        # the provider-declared transport may still apply, but host-derived
        # detection must return None for it.
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        assert _detect_api_mode_for_url("https://api.openai.com.attacker.test/v1") is None

    def test_openrouter_stays_chat_completions(self):
        assert _fallback_api_mode("openrouter", "https://openrouter.ai/api/v1") == "chat_completions"

    def test_minimax_declared_anthropic_transport_honored(self):
        # Same latent bug class: minimax declares an Anthropic-compatible
        # transport but previously fell back to chat_completions when the
        # URL carried no /anthropic hint.
        from hermes_cli.providers import determine_api_mode

        expected = determine_api_mode("minimax", "https://api.minimax.io")
        assert _fallback_api_mode("minimax", "https://api.minimax.io") == expected
        assert expected != "chat_completions" or expected == determine_api_mode("minimax", "")

    def test_unknown_provider_defaults_chat_completions(self):
        assert _fallback_api_mode("some-unknown", "https://example.test/v1") == "chat_completions"

    def test_url_detection_wins_over_provider_declaration(self):
        # /anthropic suffix on any provider routes anthropic_messages —
        # URL detection stays the higher-priority signal.
        assert (
            _fallback_api_mode("openai-api", "https://gateway.test/anthropic")
            == "anthropic_messages"
        )


class TestMiniMaxOpenAiCompatRoute:
    """Regression: MiniMax overlays default to anthropic_messages but the
    /v1 endpoint is OpenAI-compatible. A user setting
    ``MINIMAX_CN_BASE_URL=https://api.minimaxi.com/v1`` (or pointing
    ``model.base_url`` at the same path) used to land on
    ``anthropic_messages`` because the overlay won outright and the URL
    was not recognised as a wire-format mandate. That forced the user to
    hand-roll a custom provider. Hosts recognised here:

      - api.minimaxi.com   (China, /v1 OpenAI-compat)
      - api.minimax.io    (global, /v1 OpenAI-compat)

    The /anthropic path on the same hosts is NOT affected — it's caught
    by the ``endswith("/anthropic")`` branch and stays on
    ``anthropic_messages`` as before.
    """

    @pytest.mark.parametrize(
        "provider,base_url",
        [
            ("minimax-cn", "https://api.minimaxi.com/v1"),
            ("minimax-cn", "https://api.minimaxi.com/v1/"),
            ("minimax", "https://api.minimax.io/v1"),
            ("minimax", "https://api.minimax.io/v1/"),
            ("minimax-oauth", "https://api.minimax.io/v1"),
        ],
    )
    def test_minimax_v1_openai_compat_routes_to_chat_completions(
        self, provider, base_url
    ):
        # Both URL detection and the full determine_api_mode path must
        # agree, because runtime_provider._fallback_api_mode consults
        # URL detection first, but model_switch / persisted config paths
        # consult determine_api_mode directly.
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        assert _detect_api_mode_for_url(base_url) == "chat_completions"
        assert _fallback_api_mode(provider, base_url) == "chat_completions"
        assert (
            __import__("hermes_cli.providers", fromlist=["determine_api_mode"])
            .determine_api_mode(provider, base_url)
            == "chat_completions"
        )

    @pytest.mark.parametrize(
        "provider,base_url",
        [
            ("minimax-cn", "https://api.minimaxi.com/anthropic"),
            ("minimax", "https://api.minimax.io/anthropic"),
            ("minimax-oauth", "https://api.minimax.io/anthropic"),
        ],
    )
    def test_minimax_anthropic_path_still_anthropic_messages(
        self, provider, base_url
    ):
        # The OpenAI-compat carve-out must NOT regress the working
        # /anthropic routes — those are the default and stay
        # anthropic_messages.
        from hermes_cli.providers import determine_api_mode

        assert determine_api_mode(provider, base_url) == "anthropic_messages"

    def test_minimax_v1_spoofed_subdomain_is_not_anthropic(self):
        # The hostname match is exact — a lookalike subdomain
        # (api.minimaxi.com.attacker.test/v1) must NOT inherit the
        # MiniMax carve-out. Same spoof-rejection contract as
        # is_official_openai_host (#32243).
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        spoofed = "https://api.minimaxi.com.attacker.test/v1"
        assert _detect_api_mode_for_url(spoofed) is None


class TestExplicitRuntimeIntegration:
    """The explicit-runtime path resolves regional OpenAI to codex_responses."""

    def test_explicit_openai_api_regional_host(self):
        from hermes_cli.runtime_provider import _resolve_explicit_runtime

        with mock_patch(
            "hermes_cli.runtime_provider._get_model_config",
            return_value={"provider": "openai-api", "default": "gpt-5.6-terra"},
        ):
            result = _resolve_explicit_runtime(
                provider="openai-api",
                requested_provider="openai-api",
                explicit_api_key="sk-test",
                explicit_base_url="https://us.api.openai.com/v1",
                model_cfg={"provider": "openai-api", "default": "gpt-5.6-terra"},
            )
        assert result is not None
        assert result["api_mode"] == "codex_responses"
        assert result["base_url"] == "https://us.api.openai.com/v1"
