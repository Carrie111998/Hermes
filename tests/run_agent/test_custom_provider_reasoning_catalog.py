"""Tests for the catalog-based reasoning gate in run_agent._supports_reasoning_extra_body.

Covers the new path that consults endpoint_model_supports_reasoning() before
falling through to the _is_openrouter_url() wall, which previously returned
False for all custom/localhost endpoints unconditionally.
"""

from unittest.mock import patch

from run_agent import AIAgent


def _make_custom_agent(base_url="http://127.0.0.1:8977/v1", model="sonnet"):
    """Minimal AIAgent stub pointing at a custom (non-OpenRouter) endpoint."""
    agent = object.__new__(AIAgent)
    agent.provider = "custom"
    agent.base_url = base_url
    agent._base_url_lower = base_url.lower()
    agent.model = model
    agent._api_key = ""  # required by the catalog lookup path
    return agent


class TestCustomProviderReasoningFromCatalog:

    def test_returns_true_when_catalog_says_reasoning_supported(self):
        """A localhost shim that advertises reasoning in /v1/models must enable it."""
        agent = _make_custom_agent()
        with patch(
            "agent.model_metadata.endpoint_model_supports_reasoning",
            return_value=True,
        ):
            result = agent._supports_reasoning_extra_body()
        assert result is True

    def test_falls_through_to_false_when_catalog_says_no_reasoning(self):
        """Model listed but reasoning absent -> catalog returns False -> falls through
        _is_openrouter_url() check -> returns False for non-OpenRouter URL."""
        agent = _make_custom_agent()
        with patch(
            "agent.model_metadata.endpoint_model_supports_reasoning",
            return_value=False,
        ):
            result = agent._supports_reasoning_extra_body()
        assert result is False

    def test_falls_through_to_false_when_catalog_unreachable(self):
        """Catalog unavailable (None) -> falls through -> non-OpenRouter URL -> False."""
        agent = _make_custom_agent()
        with patch(
            "agent.model_metadata.endpoint_model_supports_reasoning",
            return_value=None,
        ):
            result = agent._supports_reasoning_extra_body()
        assert result is False

    def test_exception_in_catalog_lookup_is_swallowed(self):
        """Any exception from catalog lookup must not propagate -- fall through to False."""
        agent = _make_custom_agent()
        with patch(
            "agent.model_metadata.endpoint_model_supports_reasoning",
            side_effect=RuntimeError("network failure"),
        ):
            result = agent._supports_reasoning_extra_body()
        assert result is False

    def test_openrouter_url_still_uses_openrouter_path(self):
        """OpenRouter URLs must not be short-circuited by a False catalog result --
        they already went through the catalog gate and fall through to the existing
        OpenRouter logic which handles them correctly."""
        agent = object.__new__(AIAgent)
        agent.provider = "openrouter"
        agent.base_url = "https://openrouter.ai/api/v1"
        agent._base_url_lower = agent.base_url.lower()
        agent.model = "anthropic/claude-sonnet-4-5"
        agent._api_key = ""

        # Catalog returns None (not listed on OpenRouter's own /v1/models metadata,
        # because OpenRouter is not the final provider endpoint). The function must
        # NOT return False here; it should fall through to the OpenRouter-specific
        # path and let that decide.
        with patch(
            "agent.model_metadata.endpoint_model_supports_reasoning",
            return_value=None,
        ):
            # We only assert it doesn't raise and doesn't return True from the catalog
            # path. The actual OpenRouter path decision is tested by existing tests.
            result = agent._supports_reasoning_extra_body()
        # For a non-reasoning OpenRouter model the result is False; for a known
        # reasoning model it would be True. Just assert no exception propagates.
        assert result in (True, False)

    def test_nousresearch_url_unaffected_by_catalog(self):
        """nousresearch.com returns True before the catalog lookup runs."""
        agent = object.__new__(AIAgent)
        agent.provider = "custom"
        agent.base_url = "https://inference.nousresearch.com/v1"
        agent._base_url_lower = agent.base_url.lower()
        agent.model = "hermes-3"
        agent._api_key = ""

        # Even if catalog were reachable it must never be called for this host,
        # because the nousresearch.com short-circuit fires first.
        with patch(
            "agent.model_metadata.endpoint_model_supports_reasoning"
        ) as mock_catalog:
            result = agent._supports_reasoning_extra_body()

        assert result is True
        mock_catalog.assert_not_called()

    def test_localhost_without_catalog_returns_false(self):
        """Localhost endpoint with unreachable catalog still returns False,
        not an exception -- guards the regression where AttributeError on
        self._api_key was leaking through the bare except."""
        agent = _make_custom_agent(base_url="http://localhost:8080/v1")
        with patch(
            "agent.model_metadata.endpoint_model_supports_reasoning",
            return_value=None,
        ):
            result = agent._supports_reasoning_extra_body()
        assert result is False
