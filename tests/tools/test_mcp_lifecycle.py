from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import tools.mcp_tool as mcp_tool
from tools.mcp_protocol import StaleConnectionGenerationError


def _run(coro):
    return asyncio.run(coro)


def test_stale_generation_refresh_cannot_replace_catalogue(monkeypatch):
    fetched = asyncio.Event()
    release = asyncio.Event()
    registrations: list[list[str]] = []

    class Session:
        async def list_tools(self):
            fetched.set()
            await release.wait()
            return SimpleNamespace(
                tools=[SimpleNamespace(name="stale", description="", inputSchema={})],
                next_cursor=None,
            )

    async def drive() -> None:
        task = mcp_tool.MCPServerTask("stale-refresh")
        task._connection_generation = 1
        task.negotiated_era = "modern"
        task.session = Session()
        monkeypatch.setattr(mcp_tool.MCPServerTask, "_advertises_tools", lambda _self: True)
        monkeypatch.setattr(
            mcp_tool,
            "_register_server_tools",
            lambda _name, server, _config: registrations.append(
                [tool.name for tool in server._tools]
            )
            or [],
        )
        refresh = asyncio.create_task(task._refresh_tools(generation=1))
        await fetched.wait()
        task._connection_generation = 2
        task.session = SimpleNamespace()
        release.set()
        with pytest.raises(StaleConnectionGenerationError):
            await refresh
        assert task._tools == []
        assert registrations == []

    _run(drive())


def test_stale_initial_discovery_cannot_replace_catalogue(monkeypatch):
    fetched = asyncio.Event()
    release = asyncio.Event()

    class Session:
        async def list_tools(self):
            fetched.set()
            await release.wait()
            return SimpleNamespace(
                tools=[SimpleNamespace(name="stale", description="", inputSchema={})],
                next_cursor=None,
            )

    async def drive() -> None:
        task = mcp_tool.MCPServerTask("stale-discovery")
        task._connection_generation = 1
        task.negotiated_era = "modern"
        task.session = Session()
        monkeypatch.setattr(mcp_tool.MCPServerTask, "_advertises_tools", lambda _self: True)
        monkeypatch.setattr(
            mcp_tool.MCPServerTask,
            "_register_discovered_tools_if_needed",
            lambda _self: None,
        )
        discovery = asyncio.create_task(task._discover_tools(generation=1))
        await fetched.wait()
        task._connection_generation = 2
        task.session = SimpleNamespace()
        task._tools = [SimpleNamespace(name="fresh")]
        task._list_cache_meta = {"ttl_ms": 5_000}
        release.set()
        with pytest.raises(StaleConnectionGenerationError):
            await discovery
        assert [tool.name for tool in task._tools] == ["fresh"]
        assert task._list_cache_meta == {"ttl_ms": 5_000}

    _run(drive())


def test_new_generation_cancels_owned_background_tasks():
    async def drive() -> None:
        task = mcp_tool.MCPServerTask("owned-tasks")
        blocker = asyncio.Event()

        async def wait_forever():
            await blocker.wait()

        refresh = asyncio.create_task(wait_forever())
        mrtr = asyncio.create_task(wait_forever())
        listener = asyncio.create_task(wait_forever())
        task._pending_refresh_tasks.add(refresh)
        task._pending_mrtr_tasks.add(mrtr)
        task._listen_task = listener
        generation = await task._begin_connection_generation()
        assert generation == 1
        assert refresh.cancelled()
        assert mrtr.cancelled()
        assert listener.cancelled()
        assert task._pending_refresh_tasks == set()
        assert task._pending_mrtr_tasks == set()
        assert task._listen_task is None

    _run(drive())


def test_dynamic_refresh_replaces_payload_and_cache_metadata(monkeypatch):
    async def drive() -> None:
        server = mcp_tool.MCPServerTask("dynamic-metadata")
        server._connection_generation = 1
        server.negotiated_era = "modern"
        server.session = SimpleNamespace(list_tools=object())
        server.initialize_result = SimpleNamespace(
            capabilities=SimpleNamespace(tools=SimpleNamespace())
        )
        server._tools = [SimpleNamespace(name="old")]

        async def paginate(
            _method,
            _items_attr,
            _server_name,
            cache_meta_out=None,
            protocol_era=None,
        ):
            cache_meta_out.update(
                {
                    "protocol_era": protocol_era,
                    "ttl_ms": 20.0,
                    "cache_scope": "private",
                    "metadata_complete": True,
                }
            )
            return [SimpleNamespace(name="new", description="", inputSchema={})]

        monkeypatch.setattr(mcp_tool, "_paginate_full_list", paginate)
        monkeypatch.setattr(mcp_tool, "_register_server_tools", lambda *_args: [])
        await server._refresh_tools(generation=1)
        assert [tool.name for tool in server._tools] == ["new"]
        assert server._list_cache_meta == {
            "protocol_era": "modern",
            "ttl_ms": 20.0,
            "cache_scope": "private",
            "metadata_complete": True,
        }

    _run(drive())
