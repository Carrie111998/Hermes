"""Regression tests for the web_extract configurable fallback chain.

Covers the automated-review findings on the fallback-chain dispatch loop in
``tools.web_tools.web_extract_tool`` (``web.extract_backends``):

  1. Plugin discovery must run BEFORE the chain is resolved/filtered, so a
     custom fallback provider that only becomes registered at discovery time
     (cold start — subprocess agent runs, delegate children, standalone
     scripts) is not dropped from the chain by the availability filter.
  2. An explicit chain entry that fails to resolve to a registered provider
     must be skipped (recorded as an error) — never silently replaced by the
     scalar "active" provider, which resolves independently from
     ``web.extract_backend`` / ``web.backend`` and may not even be a member
     of the configured chain.
  3. Duplicate entries in the configured chain (e.g. ``[a, b, a]``) must not
     short-circuit the "is this the last attempt" check by comparing names —
     every distinct backend in the chain is still attempted.
  4. All-error / empty-response / exception outcomes from a backend fall
     through to the next chain entry, and the last attempt's outcome is
     surfaced when nothing in the chain succeeds.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent import web_search_registry
from agent.web_search_provider import WebSearchProvider
from tools import web_tools


class _FakeExtractProvider(WebSearchProvider):
    """Minimal configurable extract-only provider for dispatch tests."""

    def __init__(self, name, *, available=True, respond=None, raises=None, empty=False):
        self._name = name
        self._available = available
        self._respond = respond  # callable(urls) -> list[dict] | None
        self._raises = raises
        self._empty = empty
        self.calls = 0

    @property
    def name(self):
        return self._name

    @property
    def display_name(self):
        return self._name

    def is_available(self):
        return self._available

    def supports_extract(self):
        return True

    async def extract(self, urls, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        if self._empty:
            return []
        if self._respond is not None:
            return self._respond(urls)
        return [
            {
                "url": u, "title": "", "content": f"ok-from-{self._name}",
                "raw_content": f"ok-from-{self._name}",
            }
            for u in urls
        ]


def _error_results(name):
    """Build a ``respond`` callable whose results all carry an error."""
    def _respond(urls):
        return [
            {"url": u, "title": "", "content": "", "raw_content": "",
             "error": f"{name} failed"}
            for u in urls
        ]
    return _respond


@pytest.fixture
def clean_registry():
    """Snapshot/restore the web provider registry around a test."""
    with web_search_registry._lock:
        previous = dict(web_search_registry._providers)
        web_search_registry._providers.clear()
    yield
    with web_search_registry._lock:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(previous)


@pytest.fixture
def safe_urls(monkeypatch):
    """Bypass the SSRF probe so plain https:// test URLs dispatch normally."""
    async def _safe(_url):
        return True
    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)


# ─── Finding 1: discovery must precede chain resolution/filtering ───────────


class TestColdStartPluginDiscoveryOrdering:
    @pytest.mark.asyncio
    async def test_custom_plugin_registered_at_discovery_is_not_dropped_from_chain(
        self, clean_registry, safe_urls, monkeypatch
    ):
        # "already-loaded" simulates a provider registered before this call
        # (e.g. a built-in loaded earlier in process lifetime) — available
        # without needing discovery. It fails every extraction, so the
        # dispatcher must fall through to the next configured entry.
        already_loaded = _FakeExtractProvider(
            "already-loaded", respond=_error_results("already-loaded"),
        )
        web_search_registry.register_provider(already_loaded)

        # "cold-start-plugin" is NOT registered until discovery runs — it
        # represents a custom fallback plugin whose registration only
        # happens via _ensure_web_plugins_loaded() in a fresh process.
        cold_start_plugin = _FakeExtractProvider("cold-start-plugin")

        def _discover():
            if web_search_registry.get_provider("cold-start-plugin") is None:
                web_search_registry.register_provider(cold_start_plugin)

        mock_hook = MagicMock(wraps=_discover)
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", mock_hook)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["already-loaded", "cold-start-plugin"]},
        )

        # Sanity: the custom plugin genuinely isn't registered pre-discovery.
        assert web_search_registry.get_provider("cold-start-plugin") is None

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert mock_hook.called
        assert already_loaded.calls == 1
        assert cold_start_plugin.calls == 1, (
            "cold-start-plugin must be attempted after already-loaded fails. "
            "If the chain is resolved/filtered BEFORE discovery, this entry "
            "looks unavailable at resolution time and is silently dropped."
        )
        assert result["results"][0]["content"] == "ok-from-cold-start-plugin"


# ─── Finding 2: unregistered explicit entry must not fall back to the ──────
# ─── scalar active provider ─────────────────────────────────────────────────


class TestExplicitUnregisteredEntryNeverSubstitutesActiveProvider:
    @pytest.mark.asyncio
    async def test_unregistered_explicit_entry_is_skipped_not_replaced(
        self, clean_registry, safe_urls, monkeypatch
    ):
        # A fully valid, resolvable provider that ``get_active_extract_provider()``
        # would hand back (it reads web.extract_backend / web.backend, a
        # completely separate resolution path from web.extract_backends).
        # It must never be dispatched for a chain entry that itself fails
        # to resolve.
        wrong_active_provider = _FakeExtractProvider("wrong-active-provider")
        mock_active = MagicMock(return_value=wrong_active_provider)
        monkeypatch.setattr(web_search_registry, "get_active_extract_provider", mock_active)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["totally-unregistered-name"]},
        )

        raw = await web_tools.web_extract_tool(["https://example.com"])
        result = json.loads(raw)

        mock_active.assert_not_called()
        assert wrong_active_provider.calls == 0
        assert "wrong-active-provider" not in raw
        assert result.get("success") is False
        assert result.get("error")


# ─── Finding 3: duplicate chain entries must not block a remaining ─────────
# ─── distinct fallback ───────────────────────────────────────────────────────


class TestDuplicateChainEntriesStillAttemptRemainingFallbacks:
    @pytest.mark.asyncio
    async def test_duplicate_first_and_last_entry_does_not_skip_middle_fallback(
        self, clean_registry, safe_urls, monkeypatch
    ):
        # Mirrors the reported [firecrawl, tavily, firecrawl] shape with
        # neutral names so the test doesn't depend on real backend env vars.
        chain_a = _FakeExtractProvider("chain-a", respond=_error_results("chain-a"))
        chain_b = _FakeExtractProvider("chain-b")
        web_search_registry.register_provider(chain_a)
        web_search_registry.register_provider(chain_b)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["chain-a", "chain-b", "chain-a"]},
        )

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert chain_a.calls == 1, (
            "chain-a (index 0) should be attempted once before falling "
            "through — comparing by value against the final entry ('chain-a' "
            "again) must not make index 0 look like the last attempt"
        )
        assert chain_b.calls == 1, "chain-b is the distinct remaining fallback and must be attempted"
        assert result["results"][0]["content"] == "ok-from-chain-b"


# ─── Finding 4 support: all-error / empty / exception outcomes ─────────────


class TestAllErrorEmptyExceptionOutcomes:
    @pytest.mark.asyncio
    async def test_single_backend_all_error_falls_through_to_next(
        self, clean_registry, safe_urls, monkeypatch
    ):
        chain_a = _FakeExtractProvider("chain-a", respond=_error_results("chain-a"))
        chain_b = _FakeExtractProvider("chain-b")
        web_search_registry.register_provider(chain_a)
        web_search_registry.register_provider(chain_b)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["chain-a", "chain-b"]},
        )

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert chain_a.calls == 1
        assert chain_b.calls == 1
        assert result["results"][0]["content"] == "ok-from-chain-b"

    @pytest.mark.asyncio
    async def test_all_backends_error_surfaces_last_attempted_results(
        self, clean_registry, safe_urls, monkeypatch
    ):
        chain_a = _FakeExtractProvider("chain-a", respond=_error_results("chain-a"))
        chain_b = _FakeExtractProvider("chain-b", respond=_error_results("chain-b"))
        web_search_registry.register_provider(chain_a)
        web_search_registry.register_provider(chain_b)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["chain-a", "chain-b"]},
        )

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert chain_a.calls == 1
        assert chain_b.calls == 1
        assert result["results"][0]["error"] == "chain-b failed"

    @pytest.mark.asyncio
    async def test_empty_response_falls_through_to_next_backend(
        self, clean_registry, safe_urls, monkeypatch
    ):
        chain_a = _FakeExtractProvider("chain-a", empty=True)
        chain_b = _FakeExtractProvider("chain-b")
        web_search_registry.register_provider(chain_a)
        web_search_registry.register_provider(chain_b)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["chain-a", "chain-b"]},
        )

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert chain_a.calls == 1
        assert chain_b.calls == 1
        assert result["results"][0]["content"] == "ok-from-chain-b"

    @pytest.mark.asyncio
    async def test_all_backends_empty_surfaces_last_empty_error(
        self, clean_registry, safe_urls, monkeypatch
    ):
        chain_a = _FakeExtractProvider("chain-a", empty=True)
        chain_b = _FakeExtractProvider("chain-b", empty=True)
        web_search_registry.register_provider(chain_a)
        web_search_registry.register_provider(chain_b)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["chain-a", "chain-b"]},
        )

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert chain_a.calls == 1
        assert chain_b.calls == 1
        assert result.get("success") is False
        assert "chain-b" in result["error"]

    @pytest.mark.asyncio
    async def test_exception_falls_through_to_next_backend(
        self, clean_registry, safe_urls, monkeypatch
    ):
        chain_a = _FakeExtractProvider("chain-a", raises=RuntimeError("boom"))
        chain_b = _FakeExtractProvider("chain-b")
        web_search_registry.register_provider(chain_a)
        web_search_registry.register_provider(chain_b)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["chain-a", "chain-b"]},
        )

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert chain_a.calls == 1
        assert chain_b.calls == 1
        assert result["results"][0]["content"] == "ok-from-chain-b"

    @pytest.mark.asyncio
    async def test_all_backends_raise_surfaces_last_exception_error(
        self, clean_registry, safe_urls, monkeypatch
    ):
        chain_a = _FakeExtractProvider("chain-a", raises=RuntimeError("first boom"))
        chain_b = _FakeExtractProvider("chain-b", raises=RuntimeError("second boom"))
        web_search_registry.register_provider(chain_a)
        web_search_registry.register_provider(chain_b)

        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"extract_backends": ["chain-a", "chain-b"]},
        )

        result = json.loads(await web_tools.web_extract_tool(["https://example.com"]))

        assert chain_a.calls == 1
        assert chain_b.calls == 1
        assert result.get("success") is False
        assert "second boom" in result["error"]
