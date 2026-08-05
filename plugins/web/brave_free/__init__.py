"""Brave Search plugin — bundled, auto-loaded.

Mirrors the ``plugins/image_gen/openai/`` layout: ``provider.py`` holds the
provider class, ``__init__.py::register(ctx)`` registers an instance.
"""

from __future__ import annotations

from plugins.web.brave_free.provider import BraveFreeWebSearchProvider


def register(ctx) -> None:
    """Register the Brave provider with the plugin context."""
    ctx.register_web_search_provider(BraveFreeWebSearchProvider())
