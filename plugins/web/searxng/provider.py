"""SearXNG search — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Same JSON
API call (``/search?format=json``), same result normalization. The legacy
in-tree module ``tools.web_providers.searxng`` was removed in the same
commit that moved this code under ``plugins/``; this file is now the
canonical implementation.

Search-only — SearXNG aggregates results from upstream engines but does not
fetch/extract arbitrary URLs. ``supports_extract()`` returns False.

Config keys this provider responds to::

    web:
      search_backend: "searxng"     # explicit per-capability
      backend: "searxng"            # shared fallback

Env var::

    SEARXNG_URL=http://localhost:8080

``SEARXNG_URL`` may include query parameters (e.g. for reverse-proxy
authentication tokens like Pangolin's ``p_token``).  Any query params
present in the URL are merged into every search request.  Reserved
fields (``q``, ``format``, ``pageno``) are never overridden::

    SEARXNG_URL=https://search.example.com/?p_token=abc123
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _searxng_url() -> str:
    """Return SEARXNG_URL from Hermes config-aware env, falling back to process env."""
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value("SEARXNG_URL")
    except Exception:
        val = None
    if val is None:
        val = os.getenv("SEARXNG_URL", "")
    return (val or "").strip()


class SearXNGWebSearchProvider(WebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def display_name(self) -> str:
        return "SearXNG"

    def is_available(self) -> bool:
        """Return True when ``SEARXNG_URL`` is set."""
        return bool(_searxng_url())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a search against the configured SearXNG instance."""
        import httpx

        raw_url = _searxng_url().strip()
        if not raw_url:
            return {"success": False, "error": "SEARXNG_URL is not set"}

        # Support query params in SEARXNG_URL (e.g. reverse-proxy auth tokens
        # like Pangolin's p_token).  Parse and merge them into every request.
        from urllib.parse import urlparse, parse_qsl
        parsed = urlparse(raw_url)
        # Strip a trailing /search or /search/ from the path — users often
        # point SEARXNG_URL directly at the endpoint and we always append
        # /search ourselves.  Without this the final URL becomes /search/search.
        path = parsed.path.rstrip("/")
        if path.endswith("/search"):
            path = path[: -len("/search")]
        base_url = f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")
        # parse_qsl preserves order, blank values, and repeated keys (as
        # separate tuples).  Group repeated keys into lists so httpx emits
        # them as repeated query params (e.g. ?a=1&a=2).
        extra_params: Dict[str, Any] = {}
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key in extra_params:
                existing = extra_params[key]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    extra_params[key] = [existing, value]
            else:
                extra_params[key] = value

        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "pageno": 1,
        }
        # Merge extra params from the URL, but never let them override
        # Hermes-owned request fields (q, format, pageno).  Case-insensitive
        # comparison — HTTP query param names are case-sensitive on the wire,
        # but a malicious or sloppy ?Q=evil should not hijack the search query.
        _reserved = {"q", "format", "pageno"}
        for key, value in extra_params.items():
            if key.lower() not in _reserved:
                params[key] = value

        try:
            resp = httpx.get(
                f"{base_url}/search",
                params=params,
                timeout=15,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Don't log the exception directly — httpx stringifies the full
            # URL including any auth query params (p_token).
            logger.warning(
                "SearXNG HTTP error: %s %s",
                exc.response.status_code,
                exc.request.url.path if exc.request else "<no path>",
            )
            return {
                "success": False,
                "error": f"SearXNG returned HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            # Scrub query params from the log line; they may contain tokens.
            logger.warning(
                "SearXNG request error reaching %s: %s",
                base_url,
                type(exc).__name__,
            )
            return {
                "success": False,
                "error": f"Could not reach SearXNG at {base_url}: {type(exc).__name__}",
            }

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearXNG response parse error: %s", exc)
            return {
                "success": False,
                "error": "Could not parse SearXNG response as JSON",
            }

        raw_results = data.get("results", [])

        # SearXNG may return a score field; sort descending and cap to limit.
        sorted_results = sorted(
            raw_results,
            key=lambda r: float(r.get("score", 0)),
            reverse=True,
        )[:limit]

        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("content", "")),
                "position": i + 1,
            }
            for i, r in enumerate(sorted_results)
        ]

        logger.info(
            "SearXNG search '%s': %d results (from %d raw, limit %d)",
            query,
            len(web_results),
            len(raw_results),
            limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "SearXNG",
            "badge": "free · self-hosted",
            "tag": "Free, privacy-respecting metasearch. Point SEARXNG_URL at your instance.",
            "env_vars": [
                {
                    "key": "SEARXNG_URL",
                    "prompt": "SearXNG instance URL (e.g. http://localhost:8080)",
                    "url": "https://searx.space/",
                },
            ],
        }
