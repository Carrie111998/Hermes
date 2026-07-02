"""LAH Discovery Platform — plugin form, opt-in web extract backend.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. HTTP-only
client against a self-hosted ``lah-discovery-platform`` REST API instance —
this plugin never imports the ``lah_discovery`` Python package and never
touches ``crawl4ai`` directly. It is disabled (``is_available()`` returns
False) unless ``LAH_DISCOVERY_BASE_URL`` is explicitly set.

As of lah-discovery-platform's ``POST /extract`` endpoint, this provider's
:meth:`extract` sends the ``urls`` list through unchanged — each entry is a
real URL, fetched directly via the platform's Discovery Facade (no Source
Registry lookup, no curated multi-source pipeline). This replaces the
earlier stopgap where each entry in ``urls`` had to be one of six
registered ``source_id`` strings (``github``, ``hackernews``, ``blogs``,
``documentation``, ``forums``, ``producthunt``) POSTed to
``/discover/<source_id>`` — that capability gap is now closed. A failed
fetch for one URL never fails the whole batch: the response carries a
per-URL ``success``/``error`` result, and an unreachable API or malformed
response fails every requested URL closed (a per-item error entry), never
a crash and never a fabricated result.

Only ``supports_extract()`` is implemented — there is no free-text search
capability behind ``/extract`` or ``/discover/<source_id>``, so
``supports_search()`` is False and :meth:`search` is left at the ABC's
NotImplementedError default.

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


def _lah_discovery_extract_request(base_url: str, urls: List[str]) -> Dict[str, Any]:
    """POST the full ``urls`` batch to ``lah-discovery-platform``'s
    ``POST /extract`` and return the parsed JSON response.

    Raises on transport/HTTP errors; the caller turns those into a
    per-URL error entry for every requested URL (the whole batch shares
    one request, so a transport failure is not attributable to a single
    URL).
    """
    import httpx

    url = f"{base_url.rstrip('/')}/extract"
    logger.info("lah-discovery extract request to %s for %d url(s)", url, len(urls))

    response = httpx.post(url, json={"urls": urls}, timeout=60)
    response.raise_for_status()
    return response.json()


def _normalize_lah_discovery_extract_results(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map a ``/extract`` response to the legacy document shape.

    Successful entries follow the same ``{"url", "title", "content",
    "raw_content", "metadata"}`` contract every other provider in this
    tree returns. Failed entries become ``{"url", "title", "content",
    "error"}`` instead, matching the fail-closed per-item contract.
    """
    documents: List[Dict[str, Any]] = []
    for entry in response.get("results", []):
        if entry.get("success"):
            item = entry.get("item") or {}
            content = item.get("content", "")
            documents.append(
                {
                    "url": item.get("url", entry.get("url", "")),
                    "title": item.get("title", ""),
                    "content": content,
                    "raw_content": content,
                    "metadata": item.get("metadata", {}),
                }
            )
        else:
            documents.append(
                {
                    "url": entry.get("url", ""),
                    "title": "",
                    "content": "",
                    "error": entry.get("error") or "lah-discovery extract failed",
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
        """Fetch and extract each of ``urls`` via ``POST /extract``.

        Fails closed: a missing base URL, an unreachable REST API, or a
        malformed response all become an ``{"error": ...}`` document for
        every requested URL rather than raising or fabricating a result.
        A failure for one URL within a successful batch response is
        reported only for that URL (see
        :func:`_normalize_lah_discovery_extract_results`).
        """
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]

        base_url = os.getenv("LAH_DISCOVERY_BASE_URL", "").strip()
        if not base_url:
            message = (
                "LAH_DISCOVERY_BASE_URL environment variable not set. "
                "Point it at a running lah-discovery-platform REST API instance "
                "(e.g. http://localhost:8000)."
            )
            return [{"url": u, "title": "", "content": "", "error": message} for u in urls]

        try:
            raw = _lah_discovery_extract_request(base_url, urls)
        except Exception as exc:  # noqa: BLE001 - httpx errors, 4xx/5xx, timeouts, etc.
            logger.warning("lah-discovery extract request failed: %s", exc)
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "error": f"lah-discovery extract failed: {exc}",
                }
                for u in urls
            ]

        return _normalize_lah_discovery_extract_results(raw)

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "LAH Discovery Platform",
            "badge": "internal",
            "tag": (
                "Direct URL extraction via a self-hosted "
                "lah-discovery-platform REST API's POST /extract endpoint."
            ),
            "env_vars": [
                {
                    "key": "LAH_DISCOVERY_BASE_URL",
                    "prompt": "lah-discovery-platform REST API base URL (e.g. http://localhost:8000)",
                    "url": "",
                },
            ],
        }
