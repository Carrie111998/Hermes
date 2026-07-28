"""Failure fallback helpers for web search.

The normal web provider remains unchanged. Call ``search_after_failure`` only
when the selected provider returned an error or raised. The automatic fallback
is CloakBrowser; the final browser-use/ComputerUse step is intentionally
manual because it may open or reuse a user's browser session.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def search_after_failure(query: str, limit: int = 5) -> Dict[str, Any]:
    """Retry a failed search through the local CloakBrowser provider."""
    try:
        from plugins.web.cloakbrowser.session import search_duckduckgo_sync

        rows = search_duckduckgo_sync(query, limit)
        if rows:
            return {
                "success": True,
                "data": {"web": rows},
                "fallback": {"provider": "cloakbrowser", "automatic": True},
            }
        return {
            "success": False,
            "error": "CloakBrowser returned no results",
            "fallback": {"provider": "cloakbrowser", "automatic": True},
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("CloakBrowser fallback failed: %s", exc)
        return {
            "success": False,
            "error": f"CloakBrowser fallback failed: {exc}",
            "fallback": {"provider": "cloakbrowser", "automatic": True},
        }


def manual_browser_hint(query: str) -> str:
    """Return a safe, non-secret instruction for the final manual fallback."""
    return (
        "Automatic web search and CloakBrowser fallback failed. "
        "Use browser-use or ComputerUse manually with this query: "
        f"{query!r}. Do not enter credentials or bypass a verification challenge."
    )
