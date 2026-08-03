"""Tests for the SearXNG web search provider.

Covers:
- SearXNGWebSearchProvider.is_available() env var gating
- SearXNGWebSearchProvider.search() — happy path, HTTP error, request error, bad JSON
- Result normalization (title, url, description, position)
- Score-based sorting and limit truncation
- _is_backend_available("searxng") integration
- _get_backend() recognizes "searxng" as a valid configured backend
- check_web_api_key() includes searxng in availability check
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.tools.conftest import register_all_web_providers


# ---------------------------------------------------------------------------
# SearXNGWebSearchProvider unit tests
# ---------------------------------------------------------------------------


class TestSearXNGSearchProviderIsConfigured:
    def test_configured_when_url_set(self, monkeypatch):
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        assert SearXNGWebSearchProvider().is_available() is True

    def test_not_configured_when_url_missing(self, monkeypatch):
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        assert SearXNGWebSearchProvider().is_available() is False

    def test_not_configured_when_url_empty_string(self, monkeypatch):
        monkeypatch.setenv("SEARXNG_URL", "   ")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        assert SearXNGWebSearchProvider().is_available() is False

    def test_provider_name(self):
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        assert SearXNGWebSearchProvider().name == "searxng"

    def test_implements_web_search_provider(self):
        from agent.web_search_provider import WebSearchProvider
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        assert issubclass(SearXNGWebSearchProvider, WebSearchProvider)


class TestSearXNGSearchProviderSearch:
    """Happy path and error handling for SearXNGWebSearchProvider.search()."""

    _SAMPLE_RESPONSE = {
        "results": [
            {"title": "Result A", "url": "https://a.example.com", "content": "Desc A", "score": 0.9},
            {"title": "Result B", "url": "https://b.example.com", "content": "Desc B", "score": 0.7},
            {"title": "Result C", "url": "https://c.example.com", "content": "Desc C", "score": 0.5},
        ]
    }

    def _make_mock_response(self, json_data, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_happy_path_returns_normalized_results(self, monkeypatch):
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response(self._SAMPLE_RESPONSE)

        with patch("httpx.get", return_value=mock_resp):
            result = SearXNGWebSearchProvider().search("test query", limit=5)

        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 3
        assert web[0]["title"] == "Result A"
        assert web[0]["url"] == "https://a.example.com"
        assert web[0]["description"] == "Desc A"
        assert web[0]["position"] == 1

    def test_results_sorted_by_score_descending(self, monkeypatch):
        """Results should be sorted by score before limit is applied."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        unordered = {
            "results": [
                {"title": "Low",  "url": "https://low.example.com",  "content": "", "score": 0.1},
                {"title": "High", "url": "https://high.example.com", "content": "", "score": 0.99},
                {"title": "Mid",  "url": "https://mid.example.com",  "content": "", "score": 0.5},
            ]
        }
        mock_resp = self._make_mock_response(unordered)

        with patch("httpx.get", return_value=mock_resp):
            result = SearXNGWebSearchProvider().search("query", limit=5)

        assert result["success"] is True
        assert result["data"]["web"][0]["title"] == "High"
        assert result["data"]["web"][1]["title"] == "Mid"
        assert result["data"]["web"][2]["title"] == "Low"

    def test_limit_is_respected(self, monkeypatch):
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response(self._SAMPLE_RESPONSE)

        with patch("httpx.get", return_value=mock_resp):
            result = SearXNGWebSearchProvider().search("query", limit=2)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 2

    def test_position_is_one_indexed(self, monkeypatch):
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response(self._SAMPLE_RESPONSE)

        with patch("httpx.get", return_value=mock_resp):
            result = SearXNGWebSearchProvider().search("query", limit=5)

        positions = [r["position"] for r in result["data"]["web"]]
        assert positions == [1, 2, 3]

    def test_empty_results(self, monkeypatch):
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        with patch("httpx.get", return_value=mock_resp):
            result = SearXNGWebSearchProvider().search("nothing", limit=5)

        assert result["success"] is True
        assert result["data"]["web"] == []

    def test_missing_score_falls_back_to_zero(self, monkeypatch):
        """Results without a score field should sort to the bottom."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        data = {
            "results": [
                {"title": "No score", "url": "https://noscore.example.com", "content": ""},
                {"title": "Has score", "url": "https://scored.example.com", "content": "", "score": 0.8},
            ]
        }
        mock_resp = self._make_mock_response(data)

        with patch("httpx.get", return_value=mock_resp):
            result = SearXNGWebSearchProvider().search("query", limit=5)

        assert result["success"] is True
        # Has score should sort first (0.8 > 0)
        assert result["data"]["web"][0]["title"] == "Has score"

    def test_http_error_returns_failure(self, monkeypatch):
        import httpx
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        http_err = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)

        with patch("httpx.get", side_effect=http_err):
            result = SearXNGWebSearchProvider().search("query", limit=5)

        assert result["success"] is False
        assert "500" in result["error"]

    def test_request_error_returns_failure(self, monkeypatch):
        import httpx
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider

        with patch("httpx.get", side_effect=httpx.RequestError("connection refused")):
            result = SearXNGWebSearchProvider().search("query", limit=5)

        assert result["success"] is False
        assert "localhost:8080" in result["error"] or "connection" in result["error"].lower()

    def test_missing_url_returns_failure(self, monkeypatch):
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        from plugins.web.searxng.provider import SearXNGWebSearchProvider

        result = SearXNGWebSearchProvider().search("query", limit=5)
        assert result["success"] is False
        assert "SEARXNG_URL" in result["error"]

    def test_trailing_slash_stripped_from_url(self, monkeypatch):
        """Base URL trailing slash should not produce double-slash in endpoint."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080/")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        calls = []
        def capture_get(url, **kwargs):
            calls.append(url)
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)

        assert calls[0] == "http://localhost:8080/search", f"Got: {calls[0]}"

    def test_query_params_in_url_are_passed_to_search(self, monkeypatch):
        """Query params in SEARXNG_URL (e.g. p_token) should be merged into
        every search request so reverse-proxy-authenticated instances work."""
        monkeypatch.setenv("SEARXNG_URL", "https://search.example.com/?p_token=abc123")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        captured_params = {}
        def capture_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("test query", limit=5)

        # The p_token should be present in the request params
        assert captured_params.get("p_token") == "abc123"
        # Standard search params should still be present
        assert captured_params.get("q") == "test query"
        assert captured_params.get("format") == "json"

    def test_url_with_query_params_strips_them_from_base_url(self, monkeypatch):
        """The base URL used for the endpoint should not include query params."""
        monkeypatch.setenv("SEARXNG_URL", "https://search.example.com/?p_token=abc123")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        calls = []
        def capture_get(url, **kwargs):
            calls.append(url)
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)

        assert calls[0] == "https://search.example.com/search", f"Got: {calls[0]}"

    def test_multiple_query_params_in_url(self, monkeypatch):
        """Multiple query params should all be passed through."""
        monkeypatch.setenv("SEARXNG_URL", "https://search.example.com/?p_token=abc&lang=en")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        captured_params = {}
        def capture_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)

        assert captured_params.get("p_token") == "abc"
        assert captured_params.get("lang") == "en"

    def test_url_without_query_params_works_unchanged(self, monkeypatch):
        """URLs without query params should behave exactly as before."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        captured_params = {}
        def capture_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)

        # No extra params should be present
        assert "p_token" not in captured_params
        assert captured_params.get("q") == "query"
        assert captured_params.get("format") == "json"

    def test_reserved_params_not_overridden_by_url(self, monkeypatch):
        """URL query params must not override Hermes-owned fields (q, format, pageno).

        Regression test: if SEARXNG_URL contains ?q=stale&format=html, the
        provider must still use the actual query and JSON format.
        """
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080/?q=stale&format=html&pageno=99&p_token=abc")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        captured_params = {}
        def capture_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("real query", limit=5)

        # Hermes-owned fields must win
        assert captured_params["q"] == "real query"
        assert captured_params["format"] == "json"
        assert captured_params["pageno"] == 1
        # Non-reserved URL params should still pass through
        assert captured_params["p_token"] == "abc"


    def test_repeated_query_params_preserved_as_list(self, monkeypatch):
        """Repeated query params (e.g. ?a=1&a=2) should be preserved as a list
        so httpx emits them as repeated fields in the request."""
        monkeypatch.setenv("SEARXNG_URL", "https://search.example.com/?category=it&category=general&token=abc")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        captured_params = {}
        def capture_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)

        assert captured_params["category"] == ["it", "general"]
        assert captured_params["token"] == "abc"

    def test_blank_query_param_preserved(self, monkeypatch):
        """Blank query params (e.g. ?empty=) should be preserved, not dropped."""
        monkeypatch.setenv("SEARXNG_URL", "https://search.example.com/?empty=&p_token=abc")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})

        captured_params = {}
        def capture_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return mock_resp

        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)

        assert captured_params.get("empty") == ""
        assert captured_params.get("p_token") == "abc"

    def test_search_suffix_url_does_not_double_append(self, monkeypatch):
        """SEARXNG_URL pointed directly at /search must not produce /search/search."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080/search")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})
        calls = []
        def capture_get(url, **kwargs):
            calls.append(url)
            return mock_resp
        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)
        assert calls[0] == "http://localhost:8080/search", f"Got: {calls[0]}"

    def test_search_suffix_with_trailing_slash(self, monkeypatch):
        """SEARXNG_URL ending in /search/ must not produce /search/search."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080/search/")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})
        calls = []
        def capture_get(url, **kwargs):
            calls.append(url)
            return mock_resp
        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)
        assert calls[0] == "http://localhost:8080/search", f"Got: {calls[0]}"

    def test_search_suffix_with_query_params(self, monkeypatch):
        """SEARXNG_URL with /search path and query params must strip the path
        suffix AND merge the query params."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080/search?p_token=abc")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})
        calls = []
        captured_params = {}
        def capture_get(url, **kwargs):
            calls.append(url)
            captured_params.update(kwargs.get("params", {}))
            return mock_resp
        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("query", limit=5)
        assert calls[0] == "http://localhost:8080/search", f"Got: {calls[0]}"
        assert captured_params.get("p_token") == "abc"

    def test_reserved_keys_case_insensitive(self, monkeypatch):
        """Reserved keys must be matched case-insensitively."""
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080/?Q=evil&FORMAT=html&PageNo=99")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        mock_resp = self._make_mock_response({"results": []})
        captured_params = {}
        def capture_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return mock_resp
        with patch("httpx.get", side_effect=capture_get):
            SearXNGWebSearchProvider().search("real query", limit=5)
        assert "Q" not in captured_params
        assert "FORMAT" not in captured_params
        assert "PageNo" not in captured_params
        assert captured_params["q"] == "real query"
        assert captured_params["format"] == "json"
        assert captured_params["pageno"] == 1

    def test_http_error_log_does_not_leak_token(self, monkeypatch, caplog):
        """When SearXNG returns HTTP error, the log line must not contain
        the p_token value (httpx stringifies the full URL in the exception)."""
        import logging, httpx
        monkeypatch.setenv("SEARXNG_URL", "https://search.example.com/?p_token=secret123")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        real_resp = httpx.Response(
            403,
            request=httpx.Request("GET", "https://search.example.com/search?q=x&p_token=secret123"),
        )
        with patch("httpx.get", return_value=real_resp):
            with caplog.at_level(logging.WARNING):
                SearXNGWebSearchProvider().search("query", limit=5)
        assert "secret123" not in caplog.text

    def test_request_error_log_does_not_leak_token(self, monkeypatch, caplog):
        """When httpx.RequestError fires, the log line must not contain the
        p_token value (httpx includes the full URL in the exception message)."""
        import logging, httpx
        monkeypatch.setenv("SEARXNG_URL", "https://search.example.com/?p_token=secret123")
        from plugins.web.searxng.provider import SearXNGWebSearchProvider
        def raise_conn_error(url, **kwargs):
            raise httpx.ConnectError("connection refused")
        with patch("httpx.get", side_effect=raise_conn_error):
            with caplog.at_level(logging.WARNING):
                SearXNGWebSearchProvider().search("query", limit=5)
        assert "secret123" not in caplog.text


# ---------------------------------------------------------------------------
# Integration: _is_backend_available recognizes "searxng"
# ---------------------------------------------------------------------------


class TestIsBackendAvailable:
    def test_searxng_available_when_url_set(self, monkeypatch):
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        from tools.web_tools import _is_backend_available
        assert _is_backend_available("searxng") is True

    def test_searxng_unavailable_when_url_missing(self, monkeypatch):
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        from tools.web_tools import _is_backend_available
        assert _is_backend_available("searxng") is False

    def test_unknown_backend_still_false(self):
        from tools.web_tools import _is_backend_available
        assert _is_backend_available("unknownbackend") is False


# ---------------------------------------------------------------------------
# Integration: _get_backend() accepts "searxng" as configured value
# ---------------------------------------------------------------------------


class TestGetBackendSearXNG:
    def test_configured_searxng_returns_searxng(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "searxng"})
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        assert web_tools._get_backend() == "searxng"

    def test_auto_detect_picks_searxng_when_only_url_set(self, monkeypatch):
        """When no backend is configured but SEARXNG_URL is set, auto-detect returns it."""
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
        monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        # Suppress tool gateway
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        assert web_tools._get_backend() == "searxng"

    def test_searxng_does_not_override_higher_priority_provider(self, monkeypatch):
        """Tavily (higher priority than searxng) should win in auto-detect."""
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
        monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        assert web_tools._get_backend() == "tavily"

    def test_auto_detect_picks_searxng_when_url_only_in_hermes_config(self, monkeypatch):
        """#34290 follow-up: a config-only SEARXNG_URL (absent from process env)
        must still drive auto-detect via the now config-aware ``_has_env``."""
        from hermes_cli import config as hermes_config
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
        monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        monkeypatch.setattr(
            hermes_config,
            "get_env_value",
            lambda key: "http://config-only:8080" if key == "SEARXNG_URL" else None,
        )
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        assert web_tools._get_backend() == "searxng"


# ---------------------------------------------------------------------------
# Integration: check_web_api_key includes searxng
# ---------------------------------------------------------------------------


class TestCheckWebApiKey:
    def test_searxng_satisfies_check_web_api_key(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "searxng"})
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        assert web_tools.check_web_api_key() is True

    def test_searxng_config_only_satisfies_check_web_api_key(self, monkeypatch):
        """#34290 follow-up: config-only SEARXNG_URL satisfies the credential check."""
        from hermes_cli import config as hermes_config
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "searxng"})
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        monkeypatch.setattr(
            hermes_config,
            "get_env_value",
            lambda key: "http://config-only:8080" if key == "SEARXNG_URL" else None,
        )
        assert web_tools.check_web_api_key() is True

    def test_no_credentials_fails(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
        monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "check_firecrawl_api_key", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        assert web_tools.check_web_api_key() is False


# ---------------------------------------------------------------------------
# searxng-only: web_extract returns a clear error
# ---------------------------------------------------------------------------


class TestSearXNGOnlyExtractCrawlErrors:
    """When searxng is the active backend, extract/crawl must return clear errors."""

    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_web_extract_searxng_returns_clear_error(self, monkeypatch):
        import asyncio
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "searxng"})
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        async def _allow_ssrf(_url: str) -> bool:
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_ssrf)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        result_str = asyncio.get_event_loop().run_until_complete(
            web_tools.web_extract_tool(["https://example.com"])
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "search-only" in result["error"].lower() or "SearXNG" in result["error"]
