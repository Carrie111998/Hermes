"""Tests for the Agent Reach (free) web search plugin.

Verifies that the AgentReachWebSearchProvider:
- Registers correctly via the plugin system
- Performs search via Jina Reader + DuckDuckGo fallback
- Performs extraction via Jina Reader
- Returns properly shaped responses
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from plugins.web.agentreach.provider import AgentReachWebSearchProvider


class TestAgentReachProviderUnit:
    """Unit tests with mocked HTTP responses."""

    def test_name(self):
        p = AgentReachWebSearchProvider()
        assert p.name == "agentreach"

    def test_display_name(self):
        p = AgentReachWebSearchProvider()
        assert p.display_name == "Agent Reach (Free)"

    def test_is_available(self):
        p = AgentReachWebSearchProvider()
        assert p.is_available() is True

    def test_supports_search(self):
        p = AgentReachWebSearchProvider()
        assert p.supports_search() is True

    def test_supports_extract(self):
        p = AgentReachWebSearchProvider()
        assert p.supports_extract() is True

    def test_get_setup_schema(self):
        p = AgentReachWebSearchProvider()
        schema = p.get_setup_schema()
        assert schema["name"] == "Agent Reach (Free)"
        assert schema["badge"] == "free"
        assert schema["env_vars"] == []

    def test_extract_invalid_url(self):
        """Invalid URLs should return error results, not raise."""
        p = AgentReachWebSearchProvider()
        results = p.extract(["not-a-url"])
        assert len(results) == 1
        assert results[0]["error"] == "Invalid URL (must be http/https)"

    def test_extract_empty_list(self):
        """Empty URL list should return empty results."""
        p = AgentReachWebSearchProvider()
        results = p.extract([])
        assert results == []


class TestAgentReachProviderIntegration:
    """Integration tests that hit real endpoints (may be flaky)."""

    @pytest.mark.integration
    def test_real_jina_extract(self):
        """Test real Jina Reader extraction from example.com."""
        p = AgentReachWebSearchProvider()
        results = p.extract(["https://example.com"])
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com"
        # error key only present on failure
        assert results[0].get("error") is None
        assert len(results[0]["content"]) > 0

    @pytest.mark.integration
    def test_real_jina_search(self):
        """Test real Jina Reader + DuckDuckGo search."""
        p = AgentReachWebSearchProvider()
        result = p.search("Python programming", limit=3)
        assert result["success"] is True
        assert "data" in result
        assert "web" in result["data"]
        assert len(result["data"]["web"]) > 0
        # Verify result shape
        for item in result["data"]["web"]:
            assert "title" in item
            assert "url" in item
            assert "description" in item
            assert "position" in item

    @pytest.mark.integration
    def test_search_result_shape(self):
        """Verify search results match Hermes's expected contract."""
        p = AgentReachWebSearchProvider()
        result = p.search("test query", limit=2)
        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) <= 2
        for i, item in enumerate(web):
            assert item["position"] == i + 1


class TestAgentReachPluginRegistration:
    """Test plugin registration via the plugin system."""

    def test_plugin_registers(self):
        """Verify the plugin registers correctly."""
        from hermes_cli.plugins import discover_plugins
        from agent.web_search_registry import list_providers

        discover_plugins()
        providers = list_providers()
        names = [p.name for p in providers]
        assert "agentreach" in names

    def test_provider_instance(self):
        """Verify the registered provider is the correct class."""
        from hermes_cli.plugins import discover_plugins
        from agent.web_search_registry import get_provider

        discover_plugins()
        provider = get_provider("agentreach")
        assert provider is not None
        assert isinstance(provider, AgentReachWebSearchProvider)
