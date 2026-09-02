"""Regression tests for custom_providers per-model context_length resolution.

Covers the fix for #15779 — mid-session /model switch to a named custom
provider must honor ``custom_providers[].models.<id>.context_length`` the
same way startup already does.
"""
from __future__ import annotations

from unittest.mock import patch

from hermes_cli.config import (
    get_custom_provider_context_length,
    get_custom_provider_model_capability,
)


class TestGetCustomProviderContextLength:

    def test_trailing_slash_insensitive(self):
        custom = [
            {
                "base_url": "https://example.invalid/v1/",
                "models": {"m": {"context_length": 500_000}},
            }
        ]
        # config has trailing slash, runtime doesn't — must match
        assert (
            get_custom_provider_context_length(
                "m", "https://example.invalid/v1", custom
            )
            == 500_000
        )
        # and the reverse
        custom2 = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {"m": {"context_length": 500_000}},
            }
        ]
        assert (
            get_custom_provider_context_length(
                "m", "https://example.invalid/v1/", custom2
            )
            == 500_000
        )


    def test_empty_inputs_return_none(self):
        assert get_custom_provider_context_length("", "http://x", [{"base_url": "http://x", "models": {"": {"context_length": 1}}}]) is None
        assert get_custom_provider_context_length("m", "", [{"base_url": "", "models": {"m": {"context_length": 1}}}]) is None
        assert get_custom_provider_context_length("m", "http://x", None) is None
        assert get_custom_provider_context_length("m", "http://x", []) is None


class TestGetCustomProviderModelCapability:
    def test_matches_exact_model_on_normalized_route(self):
        custom = [
            {
                "base_url": "https://example.invalid/anthropic/",
                "models": {"fable": {"prompt_caching": True}},
            }
        ]

        assert get_custom_provider_model_capability(
            "fable",
            "https://example.invalid/anthropic",
            "prompt_caching",
            custom,
        ) is True
        assert get_custom_provider_model_capability(
            "opus",
            "https://example.invalid/anthropic",
            "prompt_caching",
            custom,
        ) is None

    def test_false_is_preserved_and_non_boolean_is_ignored(self):
        custom = [
            {
                "base_url": "https://example.invalid/anthropic",
                "models": {
                    "disabled": {"prompt_caching": False},
                    "invalid": {"prompt_caching": "true"},
                },
            }
        ]

        assert get_custom_provider_model_capability(
            "disabled",
            "https://example.invalid/anthropic",
            "prompt_caching",
            custom,
        ) is False
        assert get_custom_provider_model_capability(
            "invalid",
            "https://example.invalid/anthropic",
            "prompt_caching",
            custom,
        ) is None

    def test_capability_is_route_isolated(self):
        """A declaration for one route must not apply to another route.

        Guards normalize_route_base_url matching: if the URL comparison ever
        regresses to a model-only (or hostname-only) shortcut, this pins the
        failure.
        """
        custom = [
            {
                "base_url": "https://other.example.invalid/anthropic",
                "models": {"fable": {"prompt_caching": True}},
            }
        ]

        assert get_custom_provider_model_capability(
            "fable",
            "https://example.invalid/anthropic",
            "prompt_caching",
            custom,
        ) is None



class TestProviderScopedContextLength:
    """Multiple providers may share one base_url (e.g. an OpenAI-compatible
    aggregator such as new-api/one-api) while serving identically named models
    with different context windows. The lookup must scope by provider when one
    is supplied instead of resolving whichever entry appears first in config
    order.
    """

    CUSTOM = [
        {
            "provider_key": "coding-plan-gpt",
            "name": "coding-plan-gpt",
            "base_url": "http://127.0.0.1:3000/v1",
            "models": {"gpt-5.6-terra": {"context_length": 500_000}},
        },
        {
            "provider_key": "market-aigw",
            "name": "market-aigw",
            "base_url": "http://127.0.0.1:3000/v1",
            "models": {"gpt-5.6-terra": {"context_length": 1_050_000}},
        },
    ]

    def test_same_model_on_shared_base_url_resolves_per_provider(self):
        assert (
            get_custom_provider_context_length(
                "gpt-5.6-terra",
                "http://127.0.0.1:3000/v1",
                self.CUSTOM,
                provider="market-aigw",
            )
            == 1_050_000
        )
        assert (
            get_custom_provider_context_length(
                "gpt-5.6-terra",
                "http://127.0.0.1:3000/v1",
                self.CUSTOM,
                provider="coding-plan-gpt",
            )
            == 500_000
        )

    def test_provider_lookup_is_case_insensitive(self):
        assert (
            get_custom_provider_context_length(
                "gpt-5.6-terra",
                "http://127.0.0.1:3000/v1",
                self.CUSTOM,
                provider="Market-AIGW",
            )
            == 1_050_000
        )

    def test_unknown_provider_does_not_match_any_entry(self):
        assert (
            get_custom_provider_context_length(
                "gpt-5.6-terra",
                "http://127.0.0.1:3000/v1",
                self.CUSTOM,
                provider="no-such-provider",
            )
            is None
        )

    def test_no_provider_keeps_first_match_order(self):
        """Backward compatibility: legacy callers without a provider keep the
        historical first-match-in-config-order behaviour."""
        assert (
            get_custom_provider_context_length(
                "gpt-5.6-terra", "http://127.0.0.1:3000/v1", self.CUSTOM
            )
            == 500_000
        )

    def test_identityless_entry_is_fallback_when_no_identified_entry_matches(self):
        """Legacy custom_providers entries carry no name/provider_key; under a
        scoped lookup they remain reachable, but only as a fallback after no
        identified entry matched."""
        legacy_only = [
            {
                "base_url": "http://127.0.0.1:3000/v1",
                "models": {"m": {"context_length": 128_000}},
            }
        ]
        assert (
            get_custom_provider_context_length(
                "m", "http://127.0.0.1:3000/v1", legacy_only, provider="anything"
            )
            == 128_000
        )

    def test_identityless_entry_does_not_shadow_named_entry(self):
        """An identity-less legacy entry placed before a correctly named one
        must NOT win the scoped lookup just because it appears first in config
        order — that is exactly the misresolution provider scoping exists to
        prevent."""
        mixed = [
            {
                "base_url": "http://127.0.0.1:3000/v1",
                "models": {"gpt-5.6-terra": {"context_length": 128_000}},
            },
            {
                "provider_key": "market-aigw",
                "name": "market-aigw",
                "base_url": "http://127.0.0.1:3000/v1",
                "models": {"gpt-5.6-terra": {"context_length": 1_050_000}},
            },
        ]
        assert (
            get_custom_provider_context_length(
                "gpt-5.6-terra",
                "http://127.0.0.1:3000/v1",
                mixed,
                provider="market-aigw",
            )
            == 1_050_000
        )


class TestGetModelContextLengthHonorsOverride:
    """agent.model_metadata.get_model_context_length must honor the
    custom_providers override at step 0b — before any probe, cache hit,
    or models.dev lookup can override it.
    """

    def _mock_all_probes(self):
        """Context manager that disables every downstream resolution step."""
        from agent import model_metadata as _mm
        return [
            patch.object(_mm, "get_cached_context_length", return_value=None),
            patch.object(_mm, "fetch_endpoint_model_metadata", return_value={}),
            patch.object(_mm, "fetch_model_metadata", return_value={}),
            patch.object(_mm, "is_local_endpoint", return_value=False),
            patch.object(_mm, "_is_known_provider_base_url", return_value=False),
        ]

    def test_custom_providers_override_wins_over_default_fallback(self):
        from agent.model_metadata import get_model_context_length
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {"gpt-5.5": {"context_length": 1_050_000}},
            }
        ]
        patches = self._mock_all_probes()
        for p in patches:
            p.start()
        try:
            ctx = get_model_context_length(
                "gpt-5.5",
                base_url="https://example.invalid/v1",
                provider="custom",
                custom_providers=custom,
            )
        finally:
            for p in patches:
                p.stop()
        assert ctx == 1_050_000

    def test_explicit_config_context_length_still_wins(self):
        """Top-level model.context_length (step 0) outranks custom_providers (step 0b).

        Users who set both should see the top-level value — that's the
        documented precedence and matches the long-standing step-0 behavior.
        """
        from agent.model_metadata import get_model_context_length
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {"m": {"context_length": 1_050_000}},
            }
        ]
        ctx = get_model_context_length(
            "m",
            base_url="https://example.invalid/v1",
            provider="custom",
            config_context_length=500_000,  # explicit top-level wins
            custom_providers=custom,
        )
        assert ctx == 500_000

    def test_no_override_falls_through_to_default(self):
        """With custom_providers=None and all probes disabled, resolver
        returns DEFAULT_FALLBACK_CONTEXT (256K after the stepdown bump).
        """
        from agent.model_metadata import get_model_context_length, DEFAULT_FALLBACK_CONTEXT
        patches = self._mock_all_probes()
        for p in patches:
            p.start()
        try:
            ctx = get_model_context_length(
                "unknown-model",
                base_url="https://example.invalid/v1",
                provider="custom",
                custom_providers=None,
            )
        finally:
            for p in patches:
                p.stop()
        assert ctx == DEFAULT_FALLBACK_CONTEXT


class TestContextProbeTiers:
    def test_256k_is_top_tier_and_default(self):
        """The stepdown probe starts at 256K and 256K is the new default."""
        from agent.model_metadata import CONTEXT_PROBE_TIERS, DEFAULT_FALLBACK_CONTEXT

        assert CONTEXT_PROBE_TIERS[0] == 256_000
        assert DEFAULT_FALLBACK_CONTEXT == 256_000
        # Tiers still descend monotonically
        for a, b in zip(CONTEXT_PROBE_TIERS, CONTEXT_PROBE_TIERS[1:]):
            assert a > b, f"tiers must strictly descend, got {a} then {b}"
        # 128K is still a tier (users relying on it probe-down get there)
        assert 128_000 in CONTEXT_PROBE_TIERS
