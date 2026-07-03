"""SkyAI customer-facing plugin for Hermes.

This plugin is intentionally an edge capability: it registers only public-safe
SkyVision tools and does not touch Hermes core, Muncho memory, DevOps tools, or
customer/admin systems.
"""

from __future__ import annotations

from plugins.skyai_customer.public_tools import (
    SKYAI_CATALOG_SEARCH_SCHEMA,
    SKYAI_CAMPAIGN_KNOWLEDGE_SCHEMA,
    SKYAI_EVENT_LOG_APPEND_SCHEMA,
    SKYAI_PRODUCT_DETAIL_SCHEMA,
    SKYAI_PRODUCT_SLOTS_SCHEMA,
    handle_skyai_catalog_search,
    handle_skyai_campaign_knowledge,
    handle_skyai_event_log_append,
    handle_skyai_product_detail,
    handle_skyai_product_slots,
)


_TOOLS = (
    (
        "skyai_catalog_search",
        SKYAI_CATALOG_SEARCH_SCHEMA,
        handle_skyai_catalog_search,
    ),
    (
        "skyai_product_detail",
        SKYAI_PRODUCT_DETAIL_SCHEMA,
        handle_skyai_product_detail,
    ),
    (
        "skyai_product_slots",
        SKYAI_PRODUCT_SLOTS_SCHEMA,
        handle_skyai_product_slots,
    ),
    (
        "skyai_campaign_knowledge",
        SKYAI_CAMPAIGN_KNOWLEDGE_SCHEMA,
        handle_skyai_campaign_knowledge,
    ),
    (
        "skyai_event_log_append",
        SKYAI_EVENT_LOG_APPEND_SCHEMA,
        handle_skyai_event_log_append,
    ),
)


def register(ctx) -> None:
    """Register SkyAI v2 public-safe tools."""
    for name, schema, handler in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="skyai_customer",
            schema=schema,
            handler=handler,
            emoji="AI",
        )
