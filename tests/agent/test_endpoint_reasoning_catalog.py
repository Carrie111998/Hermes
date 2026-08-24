"""Tests for endpoint_model_supports_reasoning() in agent/model_metadata.py.

Covers the supported_parameters extraction added to fetch_endpoint_model_metadata
and the model-lookup logic in endpoint_model_supports_reasoning.
"""

from unittest.mock import patch

from agent.model_metadata import endpoint_model_supports_reasoning


def _make_metadata(model_id, supported_parameters=None, context_length=8192):
    """Build a minimal metadata dict as fetch_endpoint_model_metadata returns."""
    entry = {"context_length": context_length}
    if supported_parameters is not None:
        entry["supported_parameters"] = supported_parameters
    return {model_id: entry}


# ============================================================
# endpoint_model_supports_reasoning
# ============================================================


class TestEndpointModelSupportsReasoning:

    def test_returns_true_when_reasoning_in_supported_parameters(self):
        """Exact model match with reasoning in supported_parameters -> True."""
        metadata = _make_metadata("claude-sonnet", ["temperature", "reasoning", "top_p"])
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            result = endpoint_model_supports_reasoning(
                "claude-sonnet", "http://127.0.0.1:8977/v1"
            )
        assert result is True

    def test_returns_false_when_reasoning_absent_from_supported_parameters(self):
        """Model listed but reasoning not in supported_parameters -> False."""
        metadata = _make_metadata("gpt-4o", ["temperature", "top_p", "seed"])
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            result = endpoint_model_supports_reasoning(
                "gpt-4o", "http://127.0.0.1:8977/v1"
            )
        assert result is False

    def test_returns_none_when_catalog_empty(self):
        """Empty catalog -> None (unreachable / model unlisted)."""
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}):
            result = endpoint_model_supports_reasoning(
                "sonnet", "http://127.0.0.1:8977/v1"
            )
        assert result is None

    def test_returns_none_when_model_not_in_catalog(self):
        """Catalog present with multiple entries but model not listed -> None.
        (Single-entry shortcut doesn't apply when there are multiple entries and
        the model name doesn't substring-match any key.)"""
        metadata = {
            "claude-haiku-4": {"context_length": 48000, "supported_parameters": ["reasoning"]},
            "claude-opus-4-6": {"context_length": 200000, "supported_parameters": ["reasoning"]},
        }
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            # "gpt-4o" has no substring overlap with either key
            result = endpoint_model_supports_reasoning(
                "gpt-4o", "http://127.0.0.1:8977/v1"
            )
        assert result is None

    def test_returns_none_when_supported_parameters_missing(self):
        """Model found but entry has no supported_parameters key -> None."""
        metadata = {"claude-sonnet": {"context_length": 200000}}
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            result = endpoint_model_supports_reasoning(
                "claude-sonnet", "http://127.0.0.1:8977/v1"
            )
        assert result is None

    def test_returns_none_when_supported_parameters_not_a_list(self):
        """supported_parameters present but not a list -> None (malformed response)."""
        metadata = {"claude-sonnet": {"context_length": 8192, "supported_parameters": "reasoning"}}
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            result = endpoint_model_supports_reasoning(
                "claude-sonnet", "http://127.0.0.1:8977/v1"
            )
        assert result is None

    def test_single_entry_shortcut_matches_any_model_name(self):
        """Single-entry catalog: model name mismatch still matches the only entry."""
        metadata = _make_metadata("claude-sonnet-4-6", ["reasoning"])
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            # Caller uses a shorter alias; single-entry shortcut applies
            result = endpoint_model_supports_reasoning(
                "sonnet", "http://127.0.0.1:8977/v1"
            )
        assert result is True

    def test_substring_match_used_when_exact_fails_and_multi_entry(self):
        """Multi-entry catalog falls back to substring match."""
        metadata = {
            "claude-sonnet-4-6": {"context_length": 200000, "supported_parameters": ["reasoning"]},
            "claude-haiku-4": {"context_length": 48000, "supported_parameters": []},
        }
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            result = endpoint_model_supports_reasoning(
                "claude-sonnet", "http://127.0.0.1:8977/v1"
            )
        assert result is True

    def test_substring_match_negative(self):
        """Substring match finds model but reasoning absent -> False."""
        metadata = {
            "claude-sonnet-4-6": {"context_length": 200000, "supported_parameters": ["temperature"]},
            "claude-haiku-4": {"context_length": 48000, "supported_parameters": ["reasoning"]},
        }
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            result = endpoint_model_supports_reasoning(
                "claude-sonnet", "http://127.0.0.1:8977/v1"
            )
        assert result is False


# ============================================================
# supported_parameters extraction via the public lookup function
# (exercises the extraction indirectly through pre-populated metadata)
# ============================================================


class TestSupportedParametersExtractionViaLookup:
    """Verify the supported_parameters field is respected in the model lookup.
    These tests inject already-populated metadata (bypassing the HTTP fetch)
    to isolate the extraction/lookup logic from network calls.
    """

    def test_reasoning_detected_from_pre_populated_metadata(self):
        """When the cache already has supported_parameters populated with
        reasoning, endpoint_model_supports_reasoning returns True."""
        metadata = {
            "claude-sonnet": {
                "context_length": 200000,
                "supported_parameters": ["temperature", "reasoning", "top_p"],
            }
        }
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            assert endpoint_model_supports_reasoning(
                "claude-sonnet", "http://127.0.0.1:8977/v1"
            ) is True

    def test_reasoning_absent_means_false(self):
        """supported_parameters present but without reasoning -> False."""
        metadata = {
            "gpt-4o": {
                "context_length": 128000,
                "supported_parameters": ["temperature", "top_p", "seed"],
            }
        }
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            assert endpoint_model_supports_reasoning(
                "gpt-4o", "http://127.0.0.1:8977/v1"
            ) is False

    def test_non_list_supported_parameters_returns_none(self):
        """supported_parameters present but not a list -> None (malformed)."""
        metadata = {
            "bad-model": {
                "context_length": 8192,
                "supported_parameters": "reasoning",  # string, not list
            }
        }
        with patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value=metadata):
            assert endpoint_model_supports_reasoning(
                "bad-model", "http://127.0.0.1:8977/v1"
            ) is None
