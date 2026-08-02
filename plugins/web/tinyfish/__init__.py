"""TinyFish web search + fetch plugin — bundled, auto-loaded.

Register the TinyFish provider with Hermes' plugin context on load.
"""

from __future__ import annotations

from .provider import TinyFishWebSearchProvider


def register(ctx) -> None:
    """Register the TinyFish provider with the plugin context."""
    ctx.register_web_search_provider(TinyFishWebSearchProvider())