"""Focused MCP server for safe Hermes dashboard page discovery.

This server deliberately has no gateway/event bridge. It is intended to be
configured as a local stdio MCP in Hermes chat and exposes only read-only
page discovery and link construction tools.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from hermes_cli.dashboard_pages import (
    build_configured_dashboard_link,
    list_dashboard_pages,
)


def create_dashboard_mcp_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "hermes-dashboard",
        instructions=(
            "Discover Hermes dashboard pages and create safe local links that "
            "the dashboard chat can route without a page reload."
        ),
    )

    @mcp.tool()
    def dashboard_pages_list(query: Optional[str] = None) -> str:
        """List canonical Hermes dashboard pages that can be opened from chat.

        Args:
            query: Optional text matched against page id, label, description,
                group, and route path.
        """
        pages = list_dashboard_pages(query)
        return json.dumps({"count": len(pages), "pages": pages}, indent=2)

    @mcp.tool()
    def dashboard_link_get(page_id: str) -> str:
        """Return a safe clickable link to one Hermes dashboard page.

        Args:
            page_id: Canonical page id, such as ``sessions`` or ``models``.
        """
        try:
            return json.dumps(build_configured_dashboard_link(page_id), indent=2)
        except KeyError:
            return json.dumps(
                {"error": "Unknown dashboard page", "page": page_id}, indent=2
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, indent=2)

    return mcp


def run_dashboard_mcp_server() -> None:
    server = create_dashboard_mcp_server()
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    run_dashboard_mcp_server()
