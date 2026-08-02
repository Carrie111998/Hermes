"""Tests for the TinyFish web search provider.

Covers:
- TinyFishWebSearchProvider.is_available() — reflects TINYFISH_API_KEY
- TinyFishWebSearchProvider.search() — happy path, empty results, HTTP error,
  request error, normalization (title, url, description, position)
- TinyFishWebSearchProvider.extract() — happy path, per-URL error handling
- TinyFishWebSearchProvider.get_setup_schema()
- Registry wiring (get_provider("tinyfish") / active selection)
"""
from __future__ import annotations

import httpx
import pytest

from tests.tools.conftest import register_all_web_providers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_search_response(*results):
    """Build a fake TinyFish Search API response body."""
    return {"results": [dict(r) for r in results]}


def _make_extract_response(results=None, errors=None):
    """Build a fake TinyFish Fetch API response body."""
    body = {}
    if results is not None:
        body["results"] = [dict(r) for r in results]
    if errors is not None:
        body["errors"] = [dict(e) for e in errors]
    return body


class _FakeResponse:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://api.search.tinyfish.ai"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._json


def _stub_http(monkeypatch, *, search_response=None, fetch_response=None,
               search_raise=None, fetch_raise=None):
    """Stub httpx.get / httpx.post used by the provider's search/extract."""

    def fake_get(url, **kwargs):
        if search_raise is not None:
            raise search_raise
        return _FakeResponse(200, search_response or {})

    def fake_post(url, **kwargs):
        if fetch_raise is not None:
            raise fetch_raise
        return _FakeResponse(200, fetch_response or {})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)


def _provider():
    from plugins.web.tinyfish.provider import TinyFishWebSearchProvider
    return TinyFishWebSearchProvider()


# ---------------------------------------------------------------------------
# is_available / interface
# ---------------------------------------------------------------------------


class TestTinyFishProviderConfigured:
    def test_available_when_key_set(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        assert _provider().is_available() is True

    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
        assert _provider().is_available() is False

    def test_implements_web_search_provider(self):
        from agent.web_search_provider import WebSearchProvider
        from plugins.web.tinyfish.provider import TinyFishWebSearchProvider
        assert issubclass(TinyFishWebSearchProvider, WebSearchProvider)

    def test_supports_search_and_extract(self):
        prov = _provider()
        assert prov.supports_search() is True
        assert prov.supports_extract() is True
        assert prov.name == "tinyfish"
        assert prov.display_name == "TinyFish"


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


class TestTinyFishSearch:
    def test_happy_path_normalizes_results(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        _stub_http(monkeypatch, search_response=_make_search_response(
            {"title": "A", "url": "https://a.example.com", "snippet": "desc A", "position": 1},
            {"title": "B", "url": "https://b.example.com", "snippet": "desc B"},
            {"title": "C", "url": "https://c.example.com", "snippet": "desc C"},
        ))

        result = _provider().search("q", limit=5)

        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 3
        assert web[0] == {"title": "A", "url": "https://a.example.com",
                          "description": "desc A", "position": 1}
        assert web[1]["position"] == 2
        assert web[2]["position"] == 3

    def test_empty_results(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        _stub_http(monkeypatch, search_response=_make_search_response())

        result = _provider().search("nothing", limit=5)

        assert result["success"] is True
        assert result["data"]["web"] == []

    def test_no_key_returns_failure(self, monkeypatch):
        monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
        result = _provider().search("q", limit=5)
        assert result["success"] is False
        assert "TINYFISH_API_KEY" in result["error"]

    def test_http_error_returns_failure(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")

        def fake_get_bad(url, **kwargs):
            raise httpx.HTTPStatusError(
                "HTTP 500", request=httpx.Request("GET", url),
                response=httpx.Response(500))

        monkeypatch.setattr(httpx, "get", fake_get_bad)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        result = _provider().search("q", limit=5)
        assert result["success"] is False
        assert "500" in result["error"]

    def test_request_error_returns_failure(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        _stub_http(
            monkeypatch,
            search_raise=httpx.RequestError("boom", request=httpx.Request("GET", "x")),
        )
        result = _provider().search("q", limit=5)
        assert result["success"] is False
        assert "Could not reach" in result["error"]


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


class TestTinyFishExtract:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        _stub_http(monkeypatch, fetch_response=_make_extract_response(
            results=[{"url": "https://x.example.com", "title": "X",
                      "text": "some content", "language": "en", "format": "markdown"}],
        ))

        docs = _provider().extract(["https://x.example.com"])

        assert len(docs) == 1
        assert docs[0]["url"] == "https://x.example.com"
        assert docs[0]["title"] == "X"
        assert docs[0]["content"] == "some content"
        assert docs[0]["metadata"]["language"] == "en"

    def test_per_url_error_handling(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        _stub_http(monkeypatch, fetch_response=_make_extract_response(
            errors=[{"url": "https://bad.example.com", "error": "extraction failed"}],
        ))

        docs = _provider().extract(["https://bad.example.com"])

        assert len(docs) == 1
        assert docs[0]["url"] == "https://bad.example.com"
        assert docs[0]["error"] == "extraction failed"

    def test_http_error_returns_error_per_url(self, monkeypatch):
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        _stub_http(
            monkeypatch,
            fetch_raise=httpx.HTTPStatusError(
                "HTTP 503", request=httpx.Request("POST", "https://api.fetch.tinyfish.ai"),
                response=httpx.Response(503)),
        )
        docs = _provider().extract(["https://x.example.com"])
        assert len(docs) == 1
        assert docs[0]["error"]
        assert "503" in docs[0]["error"]


# ---------------------------------------------------------------------------
# get_setup_schema
# ---------------------------------------------------------------------------


class TestTinyFishSetupSchema:
    def test_returns_env_var_metadata(self):
        schema = _provider().get_setup_schema()
        assert schema["name"] == "TinyFish"
        assert schema["badge"] == "free"
        assert schema["env_vars"][0]["key"] == "TINYFISH_API_KEY"
        assert "tinyfish.ai" in schema["env_vars"][0]["url"]


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestTinyFishBackendWiring:
    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_provider_registered_under_tinyfish(self):
        from agent.web_search_registry import get_provider
        from plugins.web.tinyfish.provider import TinyFishWebSearchProvider
        prov = get_provider("tinyfish")
        assert isinstance(prov, TinyFishWebSearchProvider)

    def test_active_search_provider_when_configured(self, monkeypatch):
        from agent.web_search_registry import get_active_search_provider
        monkeypatch.setenv("TINYFISH_API_KEY", "tf-test-key")
        # No other provider is available (no keys), so tinyfish becomes active.
        prov = get_active_search_provider()
        assert prov is not None
        assert prov.name == "tinyfish"
