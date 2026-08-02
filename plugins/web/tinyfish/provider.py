"""TinyFish web search + content extraction — bundled plugin.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Two
capabilities advertised:

- ``supports_search()``  -> True (TinyFish Search API)
- ``supports_extract()`` -> True (TinyFish Fetch API)

Both APIs are free (no credits consumed) and return structured JSON.

Config keys this provider responds to::

    web:
      search_backend: "tinyfish"     # explicit per-capability
      extract_backend: "tinyfish"    # explicit per-capability
      backend: "tinyfish"            # shared fallback for both

Env vars::

    TINYFISH_API_KEY=...          # https://agent.tinyfish.ai/api-keys (required)

REST API endpoints::

    Search:  GET https://api.search.tinyfish.ai?query=...
    Fetch:   POST https://api.fetch.tinyfish.ai  {"urls": [...]}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_SEARCH_ENDPOINT = "https://api.search.tinyfish.ai"
_FETCH_ENDPOINT = "https://api.fetch.tinyfish.ai"


class TinyFishWebSearchProvider(WebSearchProvider):
    """TinyFish search + extract provider.

    Uses the free REST APIs:
    - Search: GET https://api.search.tinyfish.ai
    - Fetch:  POST https://api.fetch.tinyfish.ai

    Both require the ``X-API-Key`` header.
    """

    @property
    def name(self) -> str:
        return "tinyfish"

    @property
    def display_name(self) -> str:
        return "TinyFish"

    def is_available(self) -> bool:
        """Return True when ``TINYFISH_API_KEY`` is set to a non-empty value."""
        from agent.web_search_provider import get_provider_env

        return bool(get_provider_env("TINYFISH_API_KEY"))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a web search via the TinyFish Search API.

        Returns ``{"success": True, "data": {"web": [{"title", "url", "description", "position"}]}}``
        on success, or ``{"success": False, "error": str}`` on failure.
        """
        import httpx

        from agent.web_search_provider import get_provider_env

        api_key = get_provider_env("TINYFISH_API_KEY")
        if not api_key:
            return {"success": False, "error": "TINYFISH_API_KEY is not set"}

        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}
        except Exception:  # noqa: BLE001
            pass

        try:
            logger.info("TinyFish search: '%s' (limit=%d)", query, limit)
            resp = httpx.get(
                _SEARCH_ENDPOINT,
                params={"query": query, "num": min(limit, 20)},
                headers={
                    "X-API-Key": api_key,
                    "Accept": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("TinyFish Search HTTP error: %s", exc)
            return {
                "success": False,
                "error": f"TinyFish Search returned HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            logger.warning("TinyFish Search request error: %s", exc)
            return {"success": False, "error": f"Could not reach TinyFish Search: {exc}"}

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("TinyFish Search response parse error: %s", exc)
            return {"success": False, "error": "Could not parse TinyFish Search response as JSON"}

        raw_results = data.get("results", []) or []
        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("snippet", "")),
                "position": r.get("position", i + 1),
            }
            for i, r in enumerate(raw_results[:limit])
        ]

        logger.info(
            "TinyFish search '%s': %d results (from %d raw, limit %d)",
            query,
            len(web_results),
            len(raw_results),
            limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via the TinyFish Fetch API.

        Returns a list of result dicts with ``url``, ``title``, ``content``,
        ``raw_content``, and ``metadata``. Per-URL failures get an ``error`` field.
        """
        import httpx

        from agent.web_search_provider import get_provider_env

        api_key = get_provider_env("TINYFISH_API_KEY")
        if not api_key:
            return [
                {"url": u, "title": "", "content": "", "error": "TINYFISH_API_KEY is not set"}
                for u in urls
            ]

        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": u, "error": "Interrupted", "title": ""} for u in urls
                ]
        except Exception:  # noqa: BLE001
            pass

        # Request markdown format for clean LLM consumption
        payload: Dict[str, Any] = {
            "urls": urls,
            "format": "markdown",
        }

        # Pass through any format hint from kwargs
        if "format" in kwargs:
            payload["format"] = kwargs["format"]

        try:
            logger.info("TinyFish fetch: %d URL(s)", len(urls))
            resp = httpx.post(
                _FETCH_ENDPOINT,
                json=payload,
                headers={
                    "X-API-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("TinyFish Fetch HTTP error: %s", exc)
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "error": f"TinyFish Fetch returned HTTP {exc.response.status_code}",
                }
                for u in urls
            ]
        except httpx.RequestError as exc:
            logger.warning("TinyFish Fetch request error: %s", exc)
            return [
                {"url": u, "title": "", "content": "", "error": f"Could not reach TinyFish Fetch: {exc}"}
                for u in urls
            ]

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("TinyFish Fetch response parse error: %s", exc)
            return [
                {"url": u, "title": "", "content": "", "error": "Could not parse TinyFish Fetch response"}
                for u in urls
            ]

        documents: List[Dict[str, Any]] = []

        for result in data.get("results", []):
            url = result.get("url", "")
            content = result.get("text", "") or ""
            title = result.get("title", "")
            documents.append(
                {
                    "url": url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": {
                        "sourceURL": url,
                        "title": title,
                        "language": result.get("language", ""),
                        "format": result.get("format", "markdown"),
                        "final_url": result.get("final_url", url),
                    },
                }
            )

        for error in data.get("errors", []):
            err_url = error.get("url", "") if isinstance(error, dict) else str(error)
            err_msg = error.get("error", "extraction failed") if isinstance(error, dict) else str(error)
            documents.append(
                {
                    "url": err_url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": err_msg,
                    "metadata": {"sourceURL": err_url},
                }
            )

        logger.info("TinyFish fetch: %d results, %d errors", len(documents), len(data.get("errors", [])))

        return documents

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "TinyFish",
            "badge": "free",
            "tag": "Free Search + Fetch APIs. No credits consumed.",
            "env_vars": [
                {
                    "key": "TINYFISH_API_KEY",
                    "prompt": "TinyFish API key",
                    "url": "https://agent.tinyfish.ai/api-keys",
                },
            ],
        }