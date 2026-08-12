"""Bing-via-browser web provider plugin — bundled, auto-loaded.

Mirrors the ``plugins/web/brave_free/`` layout: ``provider.py`` holds the
provider class, ``__init__.py::register(ctx)`` registers an instance under
the explicit opt-in name ``bing-browser`` (web config must name it; no
credentials, no auto-activation).
"""

from __future__ import annotations

from plugins.web.bing_browser.provider import BingBrowserWebSearchProvider


def register(ctx) -> None:
    """Register the Bing-browser provider with the plugin context."""
    ctx.register_web_search_provider(BingBrowserWebSearchProvider())
