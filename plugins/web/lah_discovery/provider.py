"""LAH Discovery Platform — plugin form, opt-in web extract backend.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. HTTP-only
client against a self-hosted ``lah-discovery-platform`` REST API instance —
this plugin never imports the ``lah_discovery`` Python package and never
touches ``crawl4ai`` directly. It is disabled (``is_available()`` returns
False) unless ``LAH_DISCOVERY_BASE_URL`` is explicitly set.

IMPORTANT — capability boundary, read before wiring this up:

``lah-discovery-platform``'s REST API (as of its ``LAH_DISCOVERY_REST_API_SERVER``
mission) exposes exactly one discovery endpoint, ``POST /discover/<source_id>``,
which runs a curated pipeline against one of six *registered* sources
(``github``, ``hackernews``, ``blogs``, ``documentation``, ``forums``,
``producthunt``) — it has no endpoint for fetching an arbitrary URL. This
plugin's :meth:`extract` therefore expects each entry in ``urls`` to be one
of those six source_id strings, NOT a real URL, despite the ABC's normal
"list of URLs" semantics. An unrecognized source_id fails closed (a per-item
error entry, matching the existing legacy contract for per-URL failures —
see :meth:`extract`), never a crash and never a fabricated result.

Only ``supports_extract()`` is implemented — there is no free-text search
capability behind ``/discover/<source_id>``, so ``supports_search()`` is
False and :meth:`search` is left at the ABC's NotImplementedError default.

Config keys this provider responds to::

    web:
      extract_backend: "lah-discovery"   # opt-in only — never a default

Env vars::

    LAH_DISCOVERY_BASE_URL=...   # e.g. http://localhost:8000 (no default —
                                  # must be explicitly set to enable this
                                  # provider at all)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _lah_discovery_request(source_id: str) -> Dict[str, Any]:
    """POST to the lah-discovery-platform REST API and return parsed JSON.

    Raises ``ValueError`` when ``LAH_DISCOVERY_BASE_URL`` is unset; the
    caller catches and surfaces as a typed per-item error, matching the
    Tavily/Firecrawl provider pattern in this same plugin tree.
    """
    import httpx

    base_url = os.getenv("LAH_DISCOVERY_BASE_URL", "").strip()
    if not base_url:
        raise ValueError(
            "LAH_DISCOVERY_BASE_URL environment variable not set. "
            "Point it at a running lah-discovery-platform REST API instance "
            "(e.g. http://localhost:8000)."
        )

    url = f"{base_url.rstrip('/')}/discover/{source_id}"
    logger.info("lah-discovery request to %s", url)

    response = httpx.post(url, timeout=60)
    response.raise_for_status()
    return response.json()


def _normalize_lah_discovery_documents(
    response: Dict[str, Any], source_id: str
) -> List[Dict[str, Any]]:
    """Map a ``/discover/<source_id>`` response to the legacy document shape.

    Documents follow the same ``{"url", "title", "content", "raw_content",
    "metadata"}`` contract every other provider in this tree returns.
    """
    documents: List[Dict[str, Any]] = []
    for entry in response.get("items", []):
        item = entry.get("item", {})
        content = item.get("content", "")
        documents.append(
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "content": content,
                "raw_content": content,
                "metadata": {
                    **item.get("metadata", {}),
                    "source_id": source_id,
                    "score": entry.get("score"),
                },
            }
        )
    return documents


class LahDiscoveryWebSearchProvider(WebSearchProvider):
    """LAH Discovery Platform extract-only provider (HTTP client)."""

    @property
    def name(self) -> str:
        return "lah-discovery"

    @property
    def display_name(self) -> str:
        return "LAH Discovery Platform"

    def is_available(self) -> bool:
        """Return True when ``LAH_DISCOVERY_BASE_URL`` is set to a non-empty
        value. Cheap check only — no network call, per the ABC contract."""
        return bool(os.getenv("LAH_DISCOVERY_BASE_URL", "").strip())

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Run the discovery pipeline for each requested source_id.

        Each entry in ``urls`` must be a registered source_id (see module
        docstring). Fails closed per-entry: a missing base URL, an
        unreachable REST API, an unknown source_id, or a source with no
        discovered items all become an ``{"error": ...}`` document rather
        than raising or fabricating a result.
        """
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]

        documents: List[Dict[str, Any]] = []
        for source_id in urls:
            try:
                raw = _lah_discovery_request(source_id)
                docs = _normalize_lah_discovery_documents(raw, source_id)
                if not docs:
                    documents.append(
                        {
                            "url": source_id,
                            "title": "",
                            "content": "",
                            "raw_content": "",
                            "error": f"no items discovered for source '{source_id}'",
                            "metadata": {"source_id": source_id},
                        }
                    )
                else:
                    documents.extend(docs)
            except ValueError as exc:
                documents.append(
                    {"url": source_id, "title": "", "content": "", "error": str(exc)}
                )
            except Exception as exc:  # noqa: BLE001 - httpx errors, 404s, timeouts, etc.
                logger.warning("lah-discovery extract error for %s: %s", source_id, exc)
                documents.append(
                    {
                        "url": source_id,
                        "title": "",
                        "content": "",
                        "error": f"lah-discovery extract failed: {exc}",
                    }
                )
        return documents

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "LAH Discovery Platform",
            "badge": "internal",
            "tag": (
                "Curated multi-source discovery via a self-hosted "
                "lah-discovery-platform REST API. Takes a source_id "
                "(github/hackernews/blogs/documentation/forums/producthunt), "
                "not an arbitrary URL."
            ),
            "env_vars": [
                {
                    "key": "LAH_DISCOVERY_BASE_URL",
                    "prompt": "lah-discovery-platform REST API base URL (e.g. http://localhost:8000)",
                    "url": "",
                },
            ],
        }
