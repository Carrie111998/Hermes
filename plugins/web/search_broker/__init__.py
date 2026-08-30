from __future__ import annotations

from plugins.web.search_broker.provider import SearchBrokerWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(SearchBrokerWebSearchProvider())
