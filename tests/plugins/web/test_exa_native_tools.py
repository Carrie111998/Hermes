"""Tests for native Exa advanced search + agent tools (no MCP needed).

Covers:
- The Exa plugin exposes ``advanced_search`` and ``agent_run`` capabilities.
- The ``web_search_advanced`` and ``exa_agent_run`` tools are registered in the
  tool registry and gated on the web API key check.
- Non-Exa providers do NOT expose these capabilities (ABC default raises).
"""
from __future__ import annotations

import pytest


def _ensure_plugins_loaded() -> None:
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()


def _clear_web_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "BRAVE_SEARCH_API_KEY",
        "SEARXNG_URL",
        "TAVILY_API_KEY",
        "TAVILY_BASE_URL",
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "PARALLEL_SEARCH_MODE",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
        "TOOL_GATEWAY_USER_TOKEN",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_web_env(monkeypatch)


class TestExaNativeCapabilities:
    """Exa is the only provider exposing advanced_search + agent_run."""

    def test_exa_exposes_advanced_search_and_agent_run(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        exa = get_provider("exa")
        assert exa is not None
        assert hasattr(exa, "advanced_search")
        assert hasattr(exa, "agent_run")

    def test_non_exa_provider_does_not_expose_capabilities(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        # brave-free only supports search; the ABC default for the new
        # optional capabilities must raise, not silently return.
        p = get_provider("brave-free")
        assert p is not None
        with pytest.raises(NotImplementedError):
            p.advanced_search("test")
        with pytest.raises(NotImplementedError):
            p.agent_run("test")


class TestToolRegistration:
    """The two new tools register and are gated on the web key check."""

    def test_tools_registered(self) -> None:
        import tools.web_tools  # noqa: F401  (registers on import)
        from tools.registry import registry

        for name in ("web_search_advanced", "exa_agent_run"):
            entry = registry.get_entry(name)
            assert entry is not None, f"{name} not registered"
            assert entry.toolset == "web"

    def test_tools_gated_on_web_api_key(self) -> None:
        import tools.web_tools  # noqa: F401
        from tools.registry import registry

        for name in ("web_search_advanced", "exa_agent_run"):
            entry = registry.get_entry(name)
            assert entry is not None
            # check_fn is the standard web availability gate; it must be set
            # so the tools only light up when a web backend is available.
            assert entry.check_fn is not None

    def test_exa_agent_run_schema_has_no_dead_model_param(self) -> None:
        """The model param was advertised but never sent to the API.

        The Agent API derives effort from the run payload, so exposing a
        selectable model that the handler silently drops is misleading.
        It must not appear in the tool schema.
        """
        import tools.web_tools  # noqa: F401
        from tools.registry import registry

        entry = registry.get_entry("exa_agent_run")
        assert entry is not None
        props = entry.schema["parameters"]["properties"]
        assert "model" not in props


class TestAdvancedSearchSummarySubpages:
    """advanced_search must surface summary/subpages it already pays for."""

    def test_summary_and_subpages_mapped_into_results(self, monkeypatch) -> None:
        from plugins.web.exa import provider as exa_provider

        class _Result:
            url = "https://example.com"
            title = "Example"
            highlights = ["a highlight"]
            summary = "A short summary."
            subpages = [{"url": "https://example.com/p1", "title": "p1"}]

        class _Resp:
            results = [_Result()]
            search_time = 0.5

        class _Client:
            def search(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(exa_provider, "_get_exa_client", lambda: _Client())

        out = exa_provider.ExaWebSearchProvider().advanced_search(
            "test", enable_summary=True, subpages=2
        )
        assert out["success"] is True
        item = out["data"]["web"][0]
        assert item["summary"] == "A short summary."
        assert item["subpages"] == [{"url": "https://example.com/p1", "title": "p1"}]

    def test_summary_subpages_omitted_when_not_requested(self, monkeypatch) -> None:
        from plugins.web.exa import provider as exa_provider

        class _Result:
            url = "https://example.com"
            title = "Example"
            highlights = []

        class _Resp:
            results = [_Result()]
            search_time = 0.5

        class _Client:
            def search(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(exa_provider, "_get_exa_client", lambda: _Client())

        out = exa_provider.ExaWebSearchProvider().advanced_search("test")
        item = out["data"]["web"][0]
        assert "summary" not in item
        assert "subpages" not in item
