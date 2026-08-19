"""Tests for MCP quick-reconnect tool refresh.

When an MCP server loses its connection briefly and reconnects within the
retry budget (no parking), ``_register_discovered_tools_if_needed()``
returns early — it sees ``_registered_tool_names`` already populated and
skips re-registration.  The tool registry becomes stale until a manual
``/mcp refresh`` or until the server exhausts the retry budget and goes
through the parked-reconnect path (which deregisters and re-registers).

This is distinct from the parked-reconnect fix in #68659 / #67187.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp_tool import MCPServerTask, _register_server_tools
from tools.registry import ToolRegistry


def _make_mcp_tool(name: str, desc: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=desc, inputSchema=None)


def _make_task(server_name: str = "test") -> MCPServerTask:
    """Create an MCPServerTask pre-wired for unit tests."""
    task = MCPServerTask.__new__(MCPServerTask)
    task.name = server_name
    task.session = MagicMock()
    task._tools = []
    task._registered_tool_names = []
    task._ready = asyncio.Event()
    task._ready.set()
    task._refresh_lock = asyncio.Lock()
    task._rpc_lock = asyncio.Lock()
    task._config = {}
    task._ping_unsupported = False
    task._reconnect_retries = 0
    task._reconnect_event = asyncio.Event()
    task._shutdown_event = asyncio.Event()
    task._session_proven = True
    task._was_parked = False
    task._error = None
    task._task = None
    task._sampling = None
    task._elicitation = None
    task._auth_type = ""
    task._pending_refresh_tasks = set()
    task.initialize_result = None  # → _advertises_tools() returns True
    task.tool_timeout = 30
    task._list_cache_meta = {}
    return task


@pytest.fixture(autouse=True)
def _patch_advertises_tools(monkeypatch):
    """Force _advertises_tools() to always return True for tests.

    MCPServerTask uses __slots__, so monkeypatching on the *instance*
    raises AttributeError.  Patching on the class works because the
    descriptor lookup finds our replacement before __slots__.
    """
    monkeypatch.setattr(
        MCPServerTask, "_advertises_tools",
        lambda self: True,
        raising=False,
    )


@pytest.fixture
def isolated_registry(monkeypatch):
    """A fresh ToolRegistry per test to prevent cross-test pollution."""
    reg = ToolRegistry()
    import tools.registry
    monkeypatch.setattr(tools.registry, "registry", reg)
    return reg


# ---------------------------------------------------------------------------
# RED tests — reproduce the quick-reconnect bug
# ---------------------------------------------------------------------------

class TestQuickReconnect:

    @pytest.mark.asyncio
    async def test_tool_added_on_reconnect(self, isolated_registry):
        """A tool added during a quick reconnect must appear in the registry."""
        task = _make_task(server_name="srv")
        # Simulate state after initial connect: one tool registered
        task._registered_tool_names = ["mcp__srv__tool_a"]
        isolated_registry.register(
            name="mcp__srv__tool_a", toolset="mcp-srv", schema={},
            handler=lambda x: x, check_fn=lambda: True, is_async=False,
            description="", emoji="",
        )
        # Simulate server restart: now the server has a different tool
        task._tools = [_make_mcp_tool("tool_b")]
        # _refresh_tools() needs a session to list tools
        task.session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[_make_mcp_tool("tool_b")])
        )
        task.session.get_server_version = MagicMock(return_value="1.0")

        await task._register_discovered_tools_if_needed()

        # GREEN: tool_a should be gone and tool_b should be present.
        assert "mcp__srv__tool_b" in isolated_registry.get_all_tool_names(), (
            "New tool must be registered after quick reconnect"
        )

    @pytest.mark.asyncio
    async def test_tool_removed_on_reconnect(self, isolated_registry):
        """A tool removed during a quick reconnect must be deregistered."""
        task = _make_task(server_name="srv")
        task._registered_tool_names = ["mcp__srv__tool_a", "mcp__srv__tool_b"]
        for name in ["mcp__srv__tool_a", "mcp__srv__tool_b"]:
            isolated_registry.register(
                name=name, toolset="mcp-srv", schema={},
                handler=lambda x: x, check_fn=lambda: True, is_async=False,
                description="", emoji="",
            )
        # Server no longer offers tool_a
        task._tools = [_make_mcp_tool("tool_b")]
        # _refresh_tools() needs a session to list tools
        task.session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[_make_mcp_tool("tool_b")])
        )
        task.session.get_server_version = MagicMock(return_value="1.0")

        await task._register_discovered_tools_if_needed()

        # GREEN: tool_a must be deregistered.
        assert "mcp__srv__tool_a" not in isolated_registry.get_all_tool_names(), (
            "Removed tool must be deregistered after quick reconnect"
        )

    @pytest.mark.asyncio
    async def test_identical_tools_noop(self, isolated_registry):
        """Reconnect with identical tools must not re-register."""
        task = _make_task(server_name="srv")
        task._registered_tool_names = ["mcp__srv__tool_a"]
        isolated_registry.register(
            name="mcp__srv__tool_a", toolset="mcp-srv", schema={},
            handler=lambda x: x, check_fn=lambda: True, is_async=False,
            description="", emoji="",
        )
        task._tools = [_make_mcp_tool("tool_a")]

        reg_count_before = len(isolated_registry.get_all_tool_names())
        await task._register_discovered_tools_if_needed()
        reg_count_after = len(isolated_registry.get_all_tool_names())

        assert reg_count_after == reg_count_before, (
            "Identical tool set must not cause duplicate registration"
        )

    @pytest.mark.asyncio
    async def test_first_discovery_still_works(self, isolated_registry):
        """First-time discovery (empty _registered_tool_names) must still work."""
        task = _make_task(server_name="srv")
        task._tools = [_make_mcp_tool("tool_a")]

        await task._register_discovered_tools_if_needed()

        assert "mcp__srv__tool_a" in isolated_registry.get_all_tool_names(), (
            "First-time discovery must register tools"
        )


# ---------------------------------------------------------------------------
# Regression tests — existing behaviour remains intact
# ---------------------------------------------------------------------------

class TestRefreshToolsPreserved:

    @pytest.mark.asyncio
    async def test_refresh_tools_still_works(self, isolated_registry):
        """_refresh_tools (notification path) must still function."""
        task = _make_task(server_name="srv")
        task._registered_tool_names = ["mcp__srv__old_tool"]
        isolated_registry.register(
            name="mcp__srv__old_tool", toolset="mcp-srv", schema={},
            handler=lambda x: x, check_fn=lambda: True, is_async=False,
            description="", emoji="",
        )
        new_tool = _make_mcp_tool("new_tool")
        task.session = MagicMock()
        task.session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[new_tool])
        )
        task.session.get_server_version = MagicMock(return_value="1.0")

        await task._refresh_tools()

        assert "mcp__srv__old_tool" not in isolated_registry.get_all_tool_names()
        assert "mcp__srv__new_tool" in isolated_registry.get_all_tool_names()


class TestMultipleServers:

    @pytest.mark.asyncio
    async def test_servers_dont_interfere(self, isolated_registry):
        """Multiple servers must not interfere during reconnect."""
        # Server A
        task_a = _make_task(server_name="srv_a")
        task_a._registered_tool_names = ["mcp__srv_a__tool_x"]
        isolated_registry.register(
            name="mcp__srv_a__tool_x", toolset="mcp-srv_a", schema={},
            handler=lambda x: x, check_fn=lambda: True, is_async=False,
            description="", emoji="",
        )

        # Server B
        task_b = _make_task(server_name="srv_b")
        task_b._tools = [_make_mcp_tool("tool_y")]
        await task_b._register_discovered_tools_if_needed()
        task_b._registered_tool_names = ["mcp__srv_b__tool_y"]
        # Give srv_b a session so _refresh_tools can work if called
        task_b.session = MagicMock()
        task_b.session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[_make_mcp_tool("tool_y")])
        )
        task_b.session.get_server_version = MagicMock(return_value="1.0")

        # Quick reconnect on server A — tools changed
        task_a._tools = [_make_mcp_tool("tool_z")]
        # _refresh_tools() needs a session to list tools
        task_a.session.list_tools = AsyncMock(
            return_value=SimpleNamespace(tools=[_make_mcp_tool("tool_z")])
        )
        task_a.session.get_server_version = MagicMock(return_value="1.0")
        await task_a._register_discovered_tools_if_needed()

        all_tools = isolated_registry.get_all_tool_names()
        assert "mcp__srv_b__tool_y" in all_tools, (
            "Server B's tools must survive Server A's reconnect"
        )
        assert "mcp__srv_a__tool_z" in all_tools, (
            "Server A's new tool must be registered"
        )