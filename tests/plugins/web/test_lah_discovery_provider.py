"""Dedicated tests for the LAH Discovery Platform provider's extract()
behavior — mocked HTTP responses only, no real lah-discovery-platform
instance, no network, no Docker.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugins.web.lah_discovery.provider import (
    LahDiscoveryWebSearchProvider,
    _normalize_lah_discovery_extract_results,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAH_DISCOVERY_BASE_URL", raising=False)


def _mock_response(json_body: dict, status_ok: bool = True) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_body
    if status_ok:
        resp.raise_for_status = MagicMock()
    else:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error", request=MagicMock(), response=resp
        )
    return resp


class TestNormalizeLahDiscoveryExtractResults:
    def test_maps_successful_results_to_legacy_document_shape(self):
        raw = {
            "results": [
                {
                    "url": "https://github.com",
                    "success": True,
                    "item": {
                        "url": "https://github.com",
                        "title": "",
                        "content": "# GitHub\n\nTrending repos.",
                        "metadata": {},
                    },
                    "error": None,
                }
            ]
        }
        docs = _normalize_lah_discovery_extract_results(raw)
        assert len(docs) == 1
        assert docs[0]["url"] == "https://github.com"
        assert docs[0]["content"] == "# GitHub\n\nTrending repos."
        assert docs[0]["raw_content"] == docs[0]["content"]
        assert "error" not in docs[0]

    def test_maps_failed_results_to_error_documents(self):
        raw = {
            "results": [
                {"url": "https://bad.example", "success": False, "item": None, "error": "boom"}
            ]
        }
        docs = _normalize_lah_discovery_extract_results(raw)
        assert len(docs) == 1
        assert docs[0]["url"] == "https://bad.example"
        assert docs[0]["error"] == "boom"

    def test_empty_results_returns_empty_list(self):
        assert _normalize_lah_discovery_extract_results({"results": []}) == []


class TestExtract:
    def test_missing_base_url_returns_error_entries(self):
        provider = LahDiscoveryWebSearchProvider()
        result = provider.extract(["https://github.com"])
        assert len(result) == 1
        assert "LAH_DISCOVERY_BASE_URL" in result[0]["error"]

    def test_successful_extract_returns_normalized_documents(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        mock_resp = _mock_response(
            {
                "results": [
                    {
                        "url": "https://github.com",
                        "success": True,
                        "item": {
                            "url": "https://github.com",
                            "title": "",
                            "content": "# GitHub",
                            "metadata": {},
                        },
                        "error": None,
                    }
                ]
            }
        )
        with patch("httpx.post", return_value=mock_resp) as mock_post:
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["https://github.com"])

        mock_post.assert_called_once()
        called_url = mock_post.call_args.args[0]
        assert called_url == "http://localhost:8000/extract"
        called_json = mock_post.call_args.kwargs["json"]
        assert called_json == {"urls": ["https://github.com"]}
        assert len(result) == 1
        assert result[0]["url"] == "https://github.com"
        assert "error" not in result[0]

    def test_per_url_failure_within_a_successful_batch_fails_closed(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        mock_resp = _mock_response(
            {
                "results": [
                    {
                        "url": "https://good.example",
                        "success": True,
                        "item": {
                            "url": "https://good.example",
                            "title": "",
                            "content": "# ok",
                            "metadata": {},
                        },
                        "error": None,
                    },
                    {
                        "url": "https://bad.example",
                        "success": False,
                        "item": None,
                        "error": "fetch failed for 'https://bad.example'",
                    },
                ]
            }
        )
        with patch("httpx.post", return_value=mock_resp):
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["https://good.example", "https://bad.example"])

        assert len(result) == 2
        assert "error" not in result[0]
        assert "error" in result[1]
        assert result[1]["url"] == "https://bad.example"

    def test_unreachable_api_fails_every_requested_url_closed(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        mock_resp = _mock_response({"error": "boom"}, status_ok=False)
        with patch("httpx.post", return_value=mock_resp):
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["https://a.example", "https://b.example"])

        assert len(result) == 2
        assert all("error" in doc for doc in result)

    def test_connection_failure_fails_closed_not_a_crash(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        import httpx

        with patch("httpx.post", side_effect=httpx.ConnectError("connection refused")):
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["https://github.com"])

        assert len(result) == 1
        assert "error" in result[0]
        assert "lah-discovery extract failed" in result[0]["error"]


class TestProviderMetadata:
    def test_name_is_lah_discovery(self):
        assert LahDiscoveryWebSearchProvider().name == "lah-discovery"

    def test_supports_extract_only(self):
        provider = LahDiscoveryWebSearchProvider()
        assert provider.supports_extract() is True
        assert provider.supports_search() is False

    def test_get_setup_schema_documents_the_extract_endpoint(self):
        schema = LahDiscoveryWebSearchProvider().get_setup_schema()
        assert "/extract" in schema["tag"]
        assert schema["env_vars"][0]["key"] == "LAH_DISCOVERY_BASE_URL"
