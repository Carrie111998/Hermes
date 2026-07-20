"""Scrapling extract plugin — bundled, auto-loaded.

Backed by the ``scrapling`` package (BSD-3), a self-hosted scraper. No API
key required; the package + its ``[fetchers]`` extra must be installed (lazy
dep ``search.scrapling``, gated via :meth:`is_available`).
"""

from __future__ import annotations

from plugins.web.scrapling.provider import ScraplingWebSearchProvider


def register(ctx) -> None:
    """Register the Scrapling provider with the plugin context."""
    ctx.register_web_search_provider(ScraplingWebSearchProvider())
