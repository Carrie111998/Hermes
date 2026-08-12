"""Compatibility and behavior seams for the extracted MCP pagination helper."""

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

from tools import mcp_pagination, mcp_tool


@dataclass
class _Page:
    tools: list
    nextCursor: object = None


def test_pagination_names_are_identity_preserving_reexports():
    assert mcp_tool._MCP_LIST_MAX_PAGES is mcp_pagination._MCP_LIST_MAX_PAGES
    assert mcp_tool._paginate_full_list is mcp_pagination._paginate_full_list


def test_pagination_reexport_remains_patchable_at_original_namespace():
    original = mcp_tool._paginate_full_list
    sentinel = object()
    with patch.object(mcp_tool, "_paginate_full_list", sentinel):
        assert mcp_tool._paginate_full_list is sentinel
        assert mcp_pagination._paginate_full_list is original


def test_pagination_combines_pages_and_passes_opaque_cursors():
    calls = []
    pages = {
        None: _Page(["first"], "opaque-1"),
        "opaque-1": _Page(["second"], "opaque-2"),
        "opaque-2": _Page(["third"], None),
    }

    async def list_method(**kwargs):
        cursor = kwargs.get("cursor")
        calls.append(cursor)
        return pages[cursor]

    items = asyncio.run(
        mcp_pagination._paginate_full_list(list_method, "tools", "server")
    )

    assert items == ["first", "second", "third"]
    assert calls == [None, "opaque-1", "opaque-2"]


def test_pagination_stops_on_non_string_cursor_and_missing_items():
    calls = []

    async def list_method(**kwargs):
        calls.append(kwargs)
        return _Page([], 123)

    items = asyncio.run(
        mcp_pagination._paginate_full_list(list_method, "tools", "server")
    )

    assert items == []
    assert calls == [{}]
