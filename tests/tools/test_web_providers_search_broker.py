from __future__ import annotations

import json
from unittest.mock import Mock, patch

import httpx

from agent.web_search_provider import WebSearchProvider
from plugins.web.search_broker.provider import SearchBrokerWebSearchProvider


def test_provider_contract_and_availability(monkeypatch):
    provider = SearchBrokerWebSearchProvider()
    assert isinstance(provider, WebSearchProvider)
    assert provider.name == "search-broker"
    assert provider.supports_search() is True
    assert provider.supports_extract() is False
    monkeypatch.delenv("BROKER_CALLER_HERMES_CANARY_TOKEN", raising=False)
    assert provider.is_available() is False
    monkeypatch.setenv("BROKER_CALLER_HERMES_CANARY_TOKEN", "token")
    assert provider.is_available() is True


def test_search_maps_broker_response(monkeypatch):
    monkeypatch.setenv("BROKER_CALLER_HERMES_CANARY_TOKEN", "token")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "results": [
            {
                "canonical_url": "https://example.com",
                "title": "Example",
                "snippet": "Evidence",
                "provider_rank": 1,
            }
        ]
    }
    with patch("plugins.web.search_broker.provider.httpx.post", return_value=response) as post:
        result = SearchBrokerWebSearchProvider().search("query", limit=25)

    assert result == {
        "success": True,
        "data": {
            "web": [
                {
                    "title": "Example",
                    "url": "https://example.com",
                    "description": "Evidence",
                    "position": 1,
                }
            ]
        },
    }
    kwargs = post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer token"
    assert kwargs["json"] == {
        "query": "query",
        "capability": "web.semantic",
        "max_results": 20,
    }


def test_search_failure_never_leaks_token(monkeypatch):
    token = "super-secret-token"
    monkeypatch.setenv("BROKER_CALLER_HERMES_CANARY_TOKEN", token)
    with patch(
        "plugins.web.search_broker.provider.httpx.post",
        side_effect=httpx.ConnectError("failed"),
    ):
        result = SearchBrokerWebSearchProvider().search("query")
    assert result["success"] is False
    assert token not in json.dumps(result)
