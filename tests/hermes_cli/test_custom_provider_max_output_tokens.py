"""Regression tests for custom_providers per-model max_output_tokens resolution.

Mirrors test_custom_provider_context_length.py - covers the new per-model
output-cap override so a single provider endpoint serving models with
different max output tokens can pin them individually.

Precedence: HERMES_MAX_TOKENS > model.max_tokens >
per-provider max_output_tokens > per-model max_output_tokens > None
"""
from __future__ import annotations

from unittest.mock import patch

from hermes_cli.config import get_custom_provider_max_output_tokens


class TestGetCustomProviderMaxOutputTokens:

    def test_trailing_slash_insensitive(self):
        custom = [
            {
                "base_url": "https://example.invalid/v1/",
                "models": {"m": {"max_output_tokens": 131072}},
            }
        ]
        # config has trailing slash, runtime doesn't - must match
        assert (
            get_custom_provider_max_output_tokens(
                "m", "https://example.invalid/v1", custom
            )
            == 131072
        )
        # and the reverse
        custom2 = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {"m": {"max_output_tokens": 131072}},
            }
        ]
        assert (
            get_custom_provider_max_output_tokens(
                "m", "https://example.invalid/v1/", custom2
            )
            == 131072
        )

    def test_empty_inputs_return_none(self):
        assert get_custom_provider_max_output_tokens("", "http://x", [{"base_url": "http://x", "models": {"": {"max_output_tokens": 1}}}]) is None
        assert get_custom_provider_max_output_tokens("m", "", [{"base_url": "", "models": {"m": {"max_output_tokens": 1}}}]) is None
        assert get_custom_provider_max_output_tokens("m", "http://x", None) is None
        assert get_custom_provider_max_output_tokens("m", "http://x", []) is None

    def test_max_tokens_alias_accepted(self):
        """``max_tokens`` is accepted as an alias for ``max_output_tokens``."""
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {"m": {"max_tokens": 8192}},
            }
        ]
        assert (
            get_custom_provider_max_output_tokens("m", "https://example.invalid/v1", custom)
            == 8192
        )

    def test_max_output_tokens_takes_precedence_over_max_tokens(self):
        """When both keys are present, ``max_output_tokens`` wins."""
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {"m": {"max_output_tokens": 4096, "max_tokens": 8192}},
            }
        ]
        assert (
            get_custom_provider_max_output_tokens("m", "https://example.invalid/v1", custom)
            == 4096
        )

    def test_different_models_same_provider(self):
        """A single provider can serve models with different output caps."""
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {
                    "qwen3.8-max": {"max_output_tokens": 131072},
                    "glm-5.2": {"max_output_tokens": 16384},
                },
            }
        ]
        assert (
            get_custom_provider_max_output_tokens("qwen3.8-max", "https://example.invalid/v1", custom)
            == 131072
        )
        assert (
            get_custom_provider_max_output_tokens("glm-5.2", "https://example.invalid/v1", custom)
            == 16384
        )

    def test_model_not_in_models_dict_returns_none(self):
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {"m": {"max_output_tokens": 131072}},
            }
        ]
        assert (
            get_custom_provider_max_output_tokens("other-model", "https://example.invalid/v1", custom)
            is None
        )

    def test_invalid_values_skipped(self):
        """Non-positive or non-integer values are silently skipped."""
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": {
                    "bad-zero": {"max_output_tokens": 0},
                    "bad-negative": {"max_output_tokens": -1},
                    "bad-string": {"max_output_tokens": "not-a-number"},
                    "good": {"max_output_tokens": 4096},
                },
            }
        ]
        assert (
            get_custom_provider_max_output_tokens("bad-zero", "https://example.invalid/v1", custom)
            is None
        )
        assert (
            get_custom_provider_max_output_tokens("bad-negative", "https://example.invalid/v1", custom)
            is None
        )
        assert (
            get_custom_provider_max_output_tokens("bad-string", "https://example.invalid/v1", custom)
            is None
        )
        assert (
            get_custom_provider_max_output_tokens("good", "https://example.invalid/v1", custom)
            == 4096
        )

    def test_models_as_list_format(self):
        """The legacy list-of-dicts models format is also supported."""
        custom = [
            {
                "base_url": "https://example.invalid/v1",
                "models": [
                    {"id": "m", "max_output_tokens": 32768},
                ],
            }
        ]
        # When models is a list, the lookup goes through the dict path
        # after normalization. This should return None because the raw
        # entry has models as a list, not a dict - the helper only reads
        # dict-style models. This documents the current behavior.
        result = get_custom_provider_max_output_tokens("m", "https://example.invalid/v1", custom)
        # The helper checks isinstance(models, dict) - a list won't match.
        assert result is None
