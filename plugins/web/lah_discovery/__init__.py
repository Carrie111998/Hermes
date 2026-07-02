"""LAH Discovery Platform web plugin — bundled, auto-loaded, opt-in."""

from __future__ import annotations

from plugins.web.lah_discovery.provider import LahDiscoveryWebSearchProvider


def register(ctx) -> None:
    """Register the LAH Discovery Platform provider with the plugin context."""
    ctx.register_web_search_provider(LahDiscoveryWebSearchProvider())
