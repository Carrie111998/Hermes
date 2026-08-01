"""Wigolo search plugin — bundled, auto-loaded.

Backed by the local-first `wigolo` CLI (Node): multi-engine search with
on-device ML rerank, no API key, zero marginal cost. The heavy runtime
(browser engine + models, ~1.5GB) is installed once via `npx wigolo init`;
`is_available()` gates on that, so the plugin registers either way and
`hermes tools` can tell the user what to run.
"""

from __future__ import annotations

from plugins.web.wigolo.provider import WigoloWebSearchProvider


def register(ctx) -> None:
    """Register the Wigolo provider with the plugin context."""
    ctx.register_web_search_provider(WigoloWebSearchProvider())
