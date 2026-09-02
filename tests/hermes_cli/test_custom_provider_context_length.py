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


class TestEntryLevelContextLengthFallback:
    """The provider-level ``context_length`` key must be honored when the
    per-model ``models.<id>.context_length`` lookup misses (#98387).

    The ``/model`` switch re-derivation consults this helper instead of the
    top-level ``model.context_length``, so dropping the entry-level key
    made every switch to a relay-hosted model fall through to the hardcoded
    catalog even though the user configured an explicit override.
    """

    def test_entry_level_used_when_model_not_in_models_dict(self):
        """The reporter's shape: entry-level override set, but ``models:``
        only lists *other* models — the queried id must still resolve."""
        custom = [
            {
                "base_url": "https://relay.example.invalid/v1",
                "model": "glm-5.3-flash",
                "context_length": 1_000_000,
                "models": {"other/model": {"context_length": 1_000_000}},
            }
        ]
        assert (
            get_custom_provider_context_length(
                "glm-5.3-flash", "https://relay.example.invalid/v1", custom
            )
            == 1_000_000
        )

    def test_entry_level_used_when_models_dict_absent(self):
        """Entries without a ``models`` mapping at all used to be skipped
        entirely, hiding their entry-level override."""
        custom = [{"base_url": "https://x.invalid/v1", "context_length": 200_000}]
        assert (
            get_custom_provider_context_length(
                "any-model", "https://x.invalid/v1", custom
            )
            == 200_000
        )

    def test_per_model_override_wins_over_entry_level(self):
        custom = [
            {
                "base_url": "https://x.invalid/v1",
                "context_length": 111_111,
                "models": {"m": {"context_length": 222_222}},
            }
        ]
        assert (
            get_custom_provider_context_length("m", "https://x.invalid/v1", custom)
            == 222_222
        )

    def test_invalid_entry_level_value_falls_through(self):
        """Zero/negative/non-int/boolean entry-level values stay ignored
        instead of poisoning resolution — resolver falls back to the catalog
        chain. ``True``/``False`` must be rejected explicitly: bool is an int
        subclass in Python, so a true/false typo would otherwise parse as a
        1-token (or 0) window instead of falling through."""
        for bad in (0, -5, "huge", True, False):
            custom = [{"base_url": "https://x.invalid/v1", "context_length": bad}]
            assert (
                get_custom_provider_context_length(
                    "m", "https://x.invalid/v1", custom
                )
                is None
            ), bad

    def test_boolean_per_model_value_falls_through(self):
        """The strict non-boolean integer contract applies to per-model
        overrides too — the two levels share one validation chain."""
        custom = [
            {
                "base_url": "https://x.invalid/v1",
                "models": {"m": {"context_length": True}},
            }
        ]
        assert (
            get_custom_provider_context_length("m", "https://x.invalid/v1", custom)
            is None
        )

    def test_entry_level_is_route_isolated(self):
        """The entry-level override must not leak across routes."""
        custom = [
            {
                "base_url": "https://other.example.invalid/v1",
                "context_length": 1_000_000,
            }
        ]
        assert (
            get_custom_provider_context_length(
                "m", "https://example.invalid/v1", custom
            )
            is None
        )

    def test_same_route_pinned_entries_are_model_isolated(self):
        """Two entries sharing one normalized route, each pinning a different
        ``model`` with its own entry-level ``context_length``: each id must
        resolve to its own entry's value, never the sibling's — otherwise the
        first matching entry leaks its context limit into the other model
        during a /model switch."""
        def resolve(entries, model):
            return get_custom_provider_context_length(
                model, "https://relay.example.invalid/v1", entries
            )

        forward = [
            {
                "base_url": "https://relay.example.invalid/v1",
                "model": "model-a",
                "context_length": 100_000,
            },
            {
                "base_url": "https://relay.example.invalid/v1",
                "model": "model-b",
                "context_length": 200_000,
            },
        ]
        assert resolve(forward, "model-a") == 100_000
        assert resolve(forward, "model-b") == 200_000

        reversed_order = list(reversed(forward))
        assert resolve(reversed_order, "model-a") == 100_000
        assert resolve(reversed_order, "model-b") == 200_000

    def test_unpinned_entry_level_applies_route_wide(self):
        """Documented counterpart of the isolation rule: an entry with no
        ``model`` pin makes its entry-level value apply to every model on
        that route, because the entry claims the whole route."""
        custom = [{"base_url": "https://x.invalid/v1", "context_length": 150_000}]
        assert (
            get_custom_provider_context_length(
                "model-a", "https://x.invalid/v1", custom
            )
            == 150_000
        )
        assert (
            get_custom_provider_context_length(
                "model-b", "https://x.invalid/v1", custom
            )
            == 150_000
        )

    def test_pinned_sibling_does_not_shadow_unpinned_entry(self):
        """A pinned sibling must not consume the lookup for other models:
        resolution continues to a later unpinned entry on the same route."""
        custom = [
            {
                "base_url": "https://x.invalid/v1",
                "model": "model-a",
                "context_length": 100_000,
            },
            {"base_url": "https://x.invalid/v1", "context_length": 300_000},
        ]
        assert (
            get_custom_provider_context_length(
                "model-b", "https://x.invalid/v1", custom
            )
            == 300_000
        )


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

    def test_entry_level_override_wins_over_default_fallback(self):
        """Step 0b must also honor the entry-level context_length when the
        per-model lookup misses — the /model-switch path (which passes
        ``config_context_length=None``) depends on it (#98387)."""
        from agent.model_metadata import get_model_context_length
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "context_length": 1_000_000,
            }
        ]
        patches = self._mock_all_probes()
        for p in patches:
            p.start()
        try:
            ctx = get_model_context_length(
                "glm-5.3-flash",
                base_url="https://example.invalid/v1",
                provider="custom",
                config_context_length=None,
                custom_providers=custom,
            )
        finally:
            for p in patches:
                p.stop()
        assert ctx == 1_000_000

    def test_same_route_entry_level_keeps_model_identity_through_resolver(self):
        """End-to-end for the same-route fixture: with ``config_context_length``
        unset (the /model-switch shape) each model's entry-level override must
        survive the full resolver chain — with every catalog probe mocked —
        proving the caller keeps model identity, not just the helper's local
        return value. An explicit top-level value (cold-startup shape) still
        wins per the documented precedence."""
        from agent.model_metadata import get_model_context_length
        custom = [
            {
                "base_url": "https://relay.example.invalid/v1",
                "model": "model-a",
                "context_length": 100_000,
            },
            {
                "base_url": "https://relay.example.invalid/v1",
                "model": "model-b",
                "context_length": 200_000,
            },
        ]
        patches = self._mock_all_probes()
        for p in patches:
            p.start()
        try:
            for model_id, expected in (("model-a", 100_000), ("model-b", 200_000)):
                ctx = get_model_context_length(
                    model_id,
                    base_url="https://relay.example.invalid/v1",
                    provider="custom",
                    config_context_length=None,
                    custom_providers=custom,
                )
                assert ctx == expected, model_id
        finally:
            for p in patches:
                p.stop()

        # Cold-startup shape: an explicit top-level model.context_length
        # still outranks the entry-level override (documented precedence).
        assert (
            get_model_context_length(
                "model-b",
                base_url="https://relay.example.invalid/v1",
                provider="custom",
                config_context_length=500_000,
                custom_providers=custom,
            )
            == 500_000
        )

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
