"""Dedicated tests for the LAH Discovery Platform provider's extract()
behavior — mocked HTTP responses only, no real lah-discovery-platform
instance, no network, no Docker.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from plugins.web.lah_discovery.provider import (
    LahDiscoveryWebSearchProvider,
    _normalize_lah_discovery_documents,
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
            "404 Not Found", request=MagicMock(), response=resp
        )
    return resp


class TestNormalizeLahDiscoveryDocuments:
    def test_maps_items_to_legacy_document_shape(self):
        raw = {
            "items": [
                {
                    "item": {
                        "source_id": "github",
                        "url": "https://github.com",
                        "title": "",
                        "content": "# GitHub\n\nTrending repos.",
                        "metadata": {},
                    },
                    "score": 0.9,
                }
            ]
        }
        docs = _normalize_lah_discovery_documents(raw, "github")
        assert len(docs) == 1
        assert docs[0]["url"] == "https://github.com"
        assert docs[0]["content"] == "# GitHub\n\nTrending repos."
        assert docs[0]["raw_content"] == docs[0]["content"]
        assert docs[0]["metadata"]["source_id"] == "github"
        assert docs[0]["metadata"]["score"] == 0.9

    def test_empty_items_returns_empty_list(self):
        assert _normalize_lah_discovery_documents({"items": []}, "github") == []


class TestExtract:
    def test_missing_base_url_returns_error_entries(self):
        provider = LahDiscoveryWebSearchProvider()
        result = provider.extract(["github"])
        assert len(result) == 1
        assert "LAH_DISCOVERY_BASE_URL" in result[0]["error"]

    def test_successful_extract_returns_normalized_documents(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        mock_resp = _mock_response(
            {
                "items": [
                    {
                        "item": {
                            "source_id": "github",
                            "url": "https://github.com",
                            "title": "",
                            "content": "# GitHub",
                            "metadata": {},
                        },
                        "score": 1.0,
                    }
                ]
            }
        )
        with patch("httpx.post", return_value=mock_resp) as mock_post:
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["github"])

        mock_post.assert_called_once()
        called_url = mock_post.call_args.args[0]
        assert called_url == "http://localhost:8000/discover/github"
        assert len(result) == 1
        assert result[0]["url"] == "https://github.com"
        assert "error" not in result[0]

    def test_unknown_source_id_fails_closed_with_error_entry(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        mock_resp = _mock_response({"error": "unknown source_id 'reddit'"}, status_ok=False)
        with patch("httpx.post", return_value=mock_resp):
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["reddit"])

        assert len(result) == 1
        assert "error" in result[0]
        assert result[0]["url"] == "reddit"

    def test_connection_failure_fails_closed_not_a_crash(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        import httpx

        with patch("httpx.post", side_effect=httpx.ConnectError("connection refused")):
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["github"])

        assert len(result) == 1
        assert "error" in result[0]
        assert "lah-discovery extract failed" in result[0]["error"]

    def test_no_items_discovered_is_an_error_entry_not_silent_empty_success(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        mock_resp = _mock_response({"items": []})
        with patch("httpx.post", return_value=mock_resp):
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["github"])

        assert len(result) == 1
        assert "no items discovered" in result[0]["error"]

    def test_multiple_source_ids_processed_independently(self, monkeypatch):
        monkeypatch.setenv("LAH_DISCOVERY_BASE_URL", "http://localhost:8000")
        ok_resp = _mock_response(
            {
                "items": [
                    {
                        "item": {
                            "source_id": "github",
                            "url": "https://github.com",
                            "title": "",
                            "content": "# GitHub",
                            "metadata": {},
                        },
                        "score": 1.0,
                    }
                ]
            }
        )
        bad_resp = _mock_response({"error": "unknown"}, status_ok=False)

        with patch("httpx.post", side_effect=[ok_resp, bad_resp]):
            provider = LahDiscoveryWebSearchProvider()
            result = provider.extract(["github", "reddit"])

        assert len(result) == 2
        assert "error" not in result[0]
        assert "error" in result[1]


class TestProviderMetadata:
    def test_name_is_lah_discovery(self):
        assert LahDiscoveryWebSearchProvider().name == "lah-discovery"

    def test_supports_extract_only(self):
        provider = LahDiscoveryWebSearchProvider()
        assert provider.supports_extract() is True
        assert provider.supports_search() is False

    def test_get_setup_schema_documents_the_source_id_boundary(self):
        schema = LahDiscoveryWebSearchProvider().get_setup_schema()
        assert "source_id" in schema["tag"]
        assert schema["env_vars"][0]["key"] == "LAH_DISCOVERY_BASE_URL"
