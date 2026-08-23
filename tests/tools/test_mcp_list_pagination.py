"""Tests for MCP list_* pagination (nextCursor draining).

The MCP spec allows servers to paginate ``tools/list``, ``resources/list``,
and ``prompts/list`` via an opaque ``nextCursor`` token. The Python SDK
fetches one page per call, so hermes must follow the cursor to see items
past page 1. Port of the invariant behind anomalyco/opencode#35439/#35500.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.mcp_tool import _MCP_LIST_MAX_PAGES, _paginate_full_list


def _tool(name):
    t = MagicMock()
    t.name = name
    return t


class TestPaginateFullList:
    def test_single_page_no_cursor(self):
        """A result without nextCursor returns just that page."""
        list_method = AsyncMock(
            return_value=SimpleNamespace(tools=[_tool("a"), _tool("b")])
        )
        items = asyncio.run(_paginate_full_list(list_method, "tools", "srv"))
        assert [t.name for t in items] == ["a", "b"]
        list_method.assert_called_once_with()


    def test_runaway_cursor_capped(self):
        """A server that returns a cursor forever is bounded by the page cap."""
        calls = {"n": 0}

        async def evil_list(cursor=None):
            calls["n"] += 1
            return SimpleNamespace(
                tools=[_tool(f"t{calls['n']}")], nextCursor=f"c{calls['n']}"
            )

        items = asyncio.run(_paginate_full_list(evil_list, "tools", "srv"))
        assert calls["n"] == _MCP_LIST_MAX_PAGES
        assert len(items) == _MCP_LIST_MAX_PAGES

    def test_same_ttl_on_every_modern_page(self):
        metadata = {}
        pages = [
            SimpleNamespace(tools=[_tool("a")], nextCursor="next", ttlMs=100, cacheScope="private"),
            SimpleNamespace(tools=[_tool("b")], ttlMs=100, cacheScope="private"),
        ]
        method = AsyncMock(side_effect=pages)
        asyncio.run(
            _paginate_full_list(
                method,
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="modern",
            )
        )
        assert metadata == {
            "protocol_era": "modern",
            "ttl_ms": 100.0,
            "cache_scope": "private",
            "metadata_complete": True,
        }

    def test_shortest_ttl_wins_across_pages(self):
        metadata = {}
        pages = [
            SimpleNamespace(tools=[], nextCursor="next", ttlMs=500, cacheScope="private"),
            SimpleNamespace(tools=[], ttlMs=25, cacheScope="private"),
        ]
        asyncio.run(
            _paginate_full_list(
                AsyncMock(side_effect=pages),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="modern",
            )
        )
        assert metadata["ttl_ms"] == 25.0

    def test_zero_ttl_on_one_page_wins(self):
        metadata = {}
        pages = [
            SimpleNamespace(tools=[], nextCursor="next", ttlMs=100, cacheScope="private"),
            SimpleNamespace(tools=[], ttlMs=0, cacheScope="private"),
        ]
        asyncio.run(
            _paginate_full_list(
                AsyncMock(side_effect=pages),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="modern",
            )
        )
        assert metadata["ttl_ms"] == 0.0

    def test_scope_disagreement_fails_closed_to_private(self):
        metadata = {}
        pages = [
            SimpleNamespace(tools=[], nextCursor="next", ttlMs=100, cacheScope="public"),
            SimpleNamespace(tools=[], ttlMs=100, cacheScope="private"),
        ]
        asyncio.run(
            _paginate_full_list(
                AsyncMock(side_effect=pages),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="modern",
            )
        )
        assert metadata["cache_scope"] == "private"
        assert metadata["scope_conflict"] is True

    def test_missing_modern_metadata_is_immediately_stale(self):
        metadata = {}
        pages = [
            SimpleNamespace(tools=[], nextCursor="next", ttlMs=100, cacheScope="private"),
            SimpleNamespace(tools=[]),
        ]
        asyncio.run(
            _paginate_full_list(
                AsyncMock(side_effect=pages),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="modern",
            )
        )
        assert metadata["ttl_ms"] == 0.0
        assert metadata["cache_scope"] == "private"
        assert metadata["metadata_complete"] is False

    def test_legacy_missing_metadata_remains_hintless(self):
        metadata = {}
        asyncio.run(
            _paginate_full_list(
                AsyncMock(return_value=SimpleNamespace(tools=[])),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="legacy",
            )
        )
        assert metadata == {
            "protocol_era": "legacy",
            "metadata_complete": False,
        }

    def test_real_legacy_result_without_ttl_hint_remains_hintless(self):
        mcp_types = pytest.importorskip("mcp.types")
        result = mcp_types.ListToolsResult.model_validate(
            {"tools": [], "cacheScope": "private"}
        )
        metadata = {}

        asyncio.run(
            _paginate_full_list(
                AsyncMock(return_value=result),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="legacy",
            )
        )

        assert "ttl_ms" not in metadata
        assert metadata["cache_scope"] == "private"

    def test_real_modern_result_without_ttl_hint_fails_closed(self):
        mcp_types = pytest.importorskip("mcp.types")
        result = mcp_types.ListToolsResult.model_validate(
            {"tools": [], "cacheScope": "private"}
        )
        metadata = {}

        asyncio.run(
            _paginate_full_list(
                AsyncMock(return_value=result),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="modern",
            )
        )

        assert metadata["ttl_ms"] == 0.0
        assert metadata["cache_scope"] == "private"
        assert metadata["metadata_complete"] is False

    def test_real_legacy_result_preserves_explicit_zero_ttl(self):
        mcp_types = pytest.importorskip("mcp.types")
        result = mcp_types.ListToolsResult.model_validate(
            {"tools": [], "ttlMs": 0}
        )
        metadata = {}

        asyncio.run(
            _paginate_full_list(
                AsyncMock(return_value=result),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="legacy",
            )
        )

        assert metadata["ttl_ms"] == 0.0

    def test_real_legacy_page_without_ttl_keeps_aggregate_hintless(self):
        mcp_types = pytest.importorskip("mcp.types")
        pages = [
            mcp_types.ListToolsResult.model_validate(
                {
                    "tools": [],
                    "nextCursor": "next",
                    "ttlMs": 500,
                    "cacheScope": "private",
                }
            ),
            mcp_types.ListToolsResult.model_validate(
                {"tools": [], "cacheScope": "private"}
            ),
        ]
        metadata = {}

        asyncio.run(
            _paginate_full_list(
                AsyncMock(side_effect=pages),
                "tools",
                "srv",
                cache_meta_out=metadata,
                protocol_era="legacy",
            )
        )

        assert "ttl_ms" not in metadata
        assert metadata["cache_scope"] == "private"
        assert metadata["metadata_complete"] is False


class TestDiscoveryUsesPagination:
    def test_discover_tools_drains_all_pages(self):
        """MCPServerTask._discover_tools registers tools from every page."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("pag_srv")
        server._config = {"command": "test"}
        pages = {
            None: SimpleNamespace(tools=[_tool("first")], nextCursor="page-2"),
            "page-2": SimpleNamespace(tools=[_tool("second")]),
        }

        async def fake_list(cursor=None):
            return pages[cursor]

        server.session = MagicMock()
        server.session.list_tools = fake_list
        # capability gate: _advertises_tools() returns True when no
        # capability info was captured (legacy fallback), so no override
        # is needed here.

        asyncio.run(server._discover_tools())
        assert [t.name for t in server._tools] == ["first", "second"]
