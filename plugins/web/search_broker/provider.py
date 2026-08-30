from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlsplit

import httpx

from agent.web_search_provider import WebSearchProvider, get_provider_env


MAX_RESPONSE_BYTES = 1_048_576
MAX_RESULTS = 20


class SearchBrokerWebSearchProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "search-broker"

    @property
    def display_name(self) -> str:
        return "Search Capability Broker"

    def is_available(self) -> bool:
        token = get_provider_env("BROKER_CALLER_HERMES_CANARY_TOKEN")
        base_url = get_provider_env("SEARCH_BROKER_URL") or "http://127.0.0.1:8766"
        parsed = urlsplit(base_url)
        return bool(token) and parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5, **kwargs: Any) -> Dict[str, Any]:
        token = get_provider_env("BROKER_CALLER_HERMES_CANARY_TOKEN")
        if not token:
            return {"success": False, "error": "Broker caller credential is not configured"}
        base_url = (get_provider_env("SEARCH_BROKER_URL") or "http://127.0.0.1:8766").rstrip("/")
        try:
            response = httpx.post(
                f"{base_url}/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "query": query,
                    "capability": "web.semantic",
                    "max_results": max(1, min(int(limit), MAX_RESULTS)),
                    "caller": {"runtime": "hermes", "profile": "deepsearch"},
                },
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=False,
            )
            if len(response.content) > MAX_RESPONSE_BYTES:
                return {"success": False, "error": "Broker response exceeded size limit"}
            if response.status_code in {401, 403}:
                return {"success": False, "error": "Broker caller credential was rejected"}
            if response.status_code == 429:
                return {"success": False, "error": "Broker rate or budget limit reached"}
            if response.status_code >= 500:
                return {"success": False, "error": "Broker service is unavailable"}
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                return {"success": False, "error": "Broker returned an invalid response"}
            web = []
            seen_urls = set()
            for index, item in enumerate(payload["results"][:MAX_RESULTS], start=1):
                if not isinstance(item, dict) or not item.get("canonical_url"):
                    continue
                url = str(item["canonical_url"])
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                web.append(
                    {
                        "title": str(item.get("title") or ""),
                        "url": url,
                        "description": str(item.get("snippet") or ""),
                        "position": int(item.get("provider_rank") or index),
                    }
                )
            return {"success": True, "data": {"web": web}}
        except httpx.TimeoutException:
            return {"success": False, "error": "Search broker request timed out"}
        except (httpx.HTTPError, ValueError, TypeError):
            return {"success": False, "error": "Search broker request failed"}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "local",
            "tag": "Unified loopback search routing and budgets.",
            "env_vars": [
                {
                    "key": "BROKER_CALLER_HERMES_CANARY_TOKEN",
                    "prompt": "Broker caller token",
                }
            ],
        }
