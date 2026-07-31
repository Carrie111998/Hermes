"""Tests for MCP dynamic tool discovery (notifications/tools/list_changed)."""

import asyncio
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp_tool import MCPServerTask, _register_server_tools
from tools.registry import ToolRegistry


def _make_mcp_tool(name: str, desc: str = "", *, annotations=None):
    return SimpleNamespace(
        name=name,
        description=desc,
        inputSchema=None,
        annotations=annotations,
    )


class TestRegisterServerTools:
    """Tests for the extracted _register_server_tools helper."""

    @pytest.fixture
    def mock_registry(self):
        return ToolRegistry()

    def test_exposes_live_server_aliases(self, mock_registry):
        """Registered MCP tools are reachable via live raw-server aliases."""
        server = MCPServerTask("my_srv")
        server._tools = [_make_mcp_tool("my_tool", "desc")]
        server.session = MagicMock()
        from toolsets import resolve_toolset, validate_toolset

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools("my_srv", server, {})
            assert "mcp__my_srv__my_tool" in registered
            assert "mcp__my_srv__my_tool" in mock_registry.get_all_tool_names()
            assert validate_toolset("my_srv") is True
            assert "mcp__my_srv__my_tool" in resolve_toolset("my_srv")

    @pytest.mark.parametrize("read_only_hint", [True, False, None])
    def test_external_tool_cannot_self_declare_read_only_during_investigation(
        self,
        mock_registry,
        tmp_path,
        read_only_hint,
    ):
        """Server-owned MCP annotations never grant investigation authority."""

        from agent.request_phase import (
            activate_turn_policy,
            clear_turn_policy,
        )
        from tools.registry import ToolEffect

        annotations = SimpleNamespace(
            readOnlyHint=read_only_hint,
            destructiveHint=False,
        )
        server = MCPServerTask("untrusted")
        server._tools = [
            _make_mcp_tool(
                "claimed_read",
                "Claims to be observational.",
                annotations=annotations,
            )
        ]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools(
                "untrusted",
                server,
                {"tools": {"resources": False, "prompts": False}},
            )
            tool_name = registered[0]
            assert (
                mock_registry.get_effect(tool_name, {})
                is ToolEffect.UNKNOWN
            )
            activate_turn_policy(
                "Analyze the current system and report what you find.",
                cwd=tmp_path,
            )
            try:
                result = mock_registry.dispatch(tool_name, {})
            finally:
                clear_turn_policy()

        assert "only registered read-only tools" in result
        server.session.call_tool.assert_not_called()

    def test_hash_pinned_owner_policy_registers_one_exact_read_tool(
        self,
        mock_registry,
        tmp_path,
    ):
        from agent.request_phase import (
            activate_turn_policy,
            clear_turn_policy,
        )
        from tools.registry import ToolEffect

        source = tmp_path / "trusted_server.py"
        source.write_text("# exact reviewed local MCP source\n", encoding="utf-8")
        server = MCPServerTask("owner_read")
        server._tools = [
            _make_mcp_tool("find_clients"),
            _make_mcp_tool("create_client"),
        ]
        server.session = MagicMock()
        config = {
            "command": sys.executable,
            "args": [str(source)],
            "trusted_read_only": {
                "source": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "tools": ["find_clients"],
            },
            "tools": {"resources": False, "prompts": False},
        }

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools("owner_read", server, config)
            read_tool, write_tool = registered
            read_handler = MagicMock(return_value="exact source readback")
            write_handler = MagicMock(return_value="unexpected write")
            mock_registry.get_entry(read_tool).handler = read_handler
            mock_registry.get_entry(write_tool).handler = write_handler
            assert (
                mock_registry.get_effect(read_tool, {})
                is ToolEffect.READ_ONLY
            )
            assert (
                mock_registry.get_effect(write_tool, {})
                is ToolEffect.UNKNOWN
            )
            activate_turn_policy(
                "Tell me the exact current client match.",
                cwd=tmp_path,
            )
            try:
                result = mock_registry.dispatch(
                    read_tool,
                    {"query": "Mary Dzaugis"},
                )
                block = mock_registry.dispatch(write_tool, {})
            finally:
                clear_turn_policy()

        assert result == "exact source readback"
        read_handler.assert_called_once()
        assert "only registered read-only tools" in block
        write_handler.assert_not_called()

    def test_source_change_after_registration_blocks_before_handler(
        self,
        mock_registry,
        tmp_path,
    ):
        from agent.request_phase import (
            activate_turn_policy,
            clear_turn_policy,
        )

        source = tmp_path / "trusted_server.py"
        source.write_text("# reviewed source\n", encoding="utf-8")
        config = {
            "command": sys.executable,
            "args": [str(source)],
            "trusted_read_only": {
                "source": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "tools": ["find_clients"],
            },
            "tools": {"resources": False, "prompts": False},
        }
        server = MCPServerTask("owner_read")
        server._tools = [_make_mcp_tool("find_clients")]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            tool_name = _register_server_tools(
                "owner_read",
                server,
                config,
            )[0]
            handler = MagicMock(return_value="exact source readback")
            mock_registry.get_entry(tool_name).handler = handler
            activate_turn_policy(
                "Analyze the exact current quote history.",
                cwd=tmp_path,
            )
            try:
                assert (
                    mock_registry.dispatch(tool_name, {})
                    == "exact source readback"
                )
                source.write_text("# changed after startup\n", encoding="utf-8")
                blocked = mock_registry.dispatch(tool_name, {})
            finally:
                clear_turn_policy()

        assert "only registered read-only tools" in blocked
        handler.assert_called_once()

    @pytest.mark.parametrize(
        "policy_change",
        [
            {"sha256": "0" * 64},
            {"source": "relative-server.py"},
            {"tools": ["find_*"]},
            {"tools": []},
            {"tools": ["find_clients", "find_clients"]},
            {"tools": [" find_clients"]},
            {"tools": ["find-clients", "find_clients"]},
        ],
    )
    def test_trusted_read_policy_fails_closed_when_not_exact(
        self,
        mock_registry,
        tmp_path,
        policy_change,
    ):
        from tools.registry import ToolEffect

        source = tmp_path / "trusted_server.py"
        source.write_text("# exact reviewed local MCP source\n", encoding="utf-8")
        policy = {
            "source": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "tools": ["find_clients"],
        }
        policy.update(policy_change)
        server = MCPServerTask("owner_read")
        server._tools = [_make_mcp_tool("find_clients")]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools(
                "owner_read",
                server,
                {
                    "command": sys.executable,
                    "args": [str(source)],
                    "trusted_read_only": policy,
                    "tools": {"resources": False, "prompts": False},
                },
            )
            assert (
                mock_registry.get_effect(registered[0], {})
                is ToolEffect.UNKNOWN
            )

    @pytest.mark.parametrize(
        "config_change",
        [
            {"url": "https://remote.example/mcp"},
            {"args": []},
            {"args": "not-a-list"},
            {"args": ["/tmp/evil_server.py", "SOURCE"]},
            {"command": "/bin/sh", "args": ["-c", "evil", "SOURCE"]},
            {"args": ["-m", "evil", "SOURCE"]},
            {"args": ["SOURCE", "--load-unpinned-plugin"]},
            {
                "command": "python",
                "env": {"PATH": "/tmp/attacker-controlled-bin"},
            },
        ],
    )
    def test_trusted_read_policy_rejects_remote_or_unbound_transport(
        self,
        mock_registry,
        tmp_path,
        config_change,
    ):
        from tools.registry import ToolEffect

        source = tmp_path / "trusted_server.py"
        source.write_text("# exact reviewed local MCP source\n", encoding="utf-8")
        config = {
            "command": sys.executable,
            "args": [str(source)],
            "trusted_read_only": {
                "source": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "tools": ["find_clients"],
            },
            "tools": {"resources": False, "prompts": False},
        }
        if isinstance(config_change.get("args"), list):
            config_change = {
                **config_change,
                "args": [
                    str(source) if value == "SOURCE" else value
                    for value in config_change["args"]
                ],
            }
        config.update(config_change)
        server = MCPServerTask("owner_read")
        server._tools = [_make_mcp_tool("find_clients")]
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools(
                "owner_read",
                server,
                config,
            )
            assert (
                mock_registry.get_effect(registered[0], {})
                is ToolEffect.UNKNOWN
            )

    def test_untrusted_resource_and_prompt_utilities_are_not_read_only(
        self,
        mock_registry,
        tmp_path,
    ):
        from agent.request_phase import (
            activate_turn_policy,
            clear_turn_policy,
        )
        from tools.registry import ToolEffect

        server = MCPServerTask("untrusted")
        server._tools = []
        server.session = MagicMock()

        with patch("tools.registry.registry", mock_registry):
            registered = _register_server_tools(
                "untrusted",
                server,
                {"command": "npx", "args": []},
            )
            assert registered
            for tool_name in registered:
                assert (
                    mock_registry.get_effect(tool_name, {})
                    is ToolEffect.UNKNOWN
                )
            tool_name = registered[0]
            handler = MagicMock(return_value="unexpected external result")
            mock_registry.get_entry(tool_name).handler = handler
            activate_turn_policy(
                "Analyze the current external resources.",
                cwd=tmp_path,
            )
            try:
                blocked = mock_registry.dispatch(tool_name, {})
            finally:
                clear_turn_policy()

        assert "only registered read-only tools" in blocked
        handler.assert_not_called()


class TestRefreshTools:
    """Tests for MCPServerTask._refresh_tools nuke-and-repave cycle."""

    @pytest.fixture
    def mock_registry(self):
        return ToolRegistry()

    @pytest.mark.asyncio
    async def test_nuke_and_repave(self, mock_registry):
        """Old tools are removed and new tools registered on refresh."""
        server = MCPServerTask("live_srv")
        server._refresh_lock = asyncio.Lock()
        server._config = {}
        from toolsets import resolve_toolset

        # Seed initial state: one old tool registered
        mock_registry.register(
            name="mcp__live_srv__old_tool", toolset="mcp-live_srv", schema={},
            handler=lambda x: x, check_fn=lambda: True, is_async=False,
            description="", emoji="",
        )
        server._registered_tool_names = ["mcp__live_srv__old_tool"]

        # New tool list from server
        new_tool = _make_mcp_tool("new_tool", "new behavior")
        server.session = SimpleNamespace(
            list_tools=AsyncMock(
                return_value=SimpleNamespace(tools=[new_tool])
            )
        )

        with patch("tools.registry.registry", mock_registry):
            await server._refresh_tools()
            assert "mcp__live_srv__old_tool" not in mock_registry.get_all_tool_names()
            assert "mcp__live_srv__old_tool" not in resolve_toolset("live_srv")
            assert "mcp__live_srv__new_tool" in mock_registry.get_all_tool_names()
            assert "mcp__live_srv__new_tool" in resolve_toolset("live_srv")
            assert server._registered_tool_names == ["mcp__live_srv__new_tool"]

    @pytest.mark.asyncio
    async def test_refresh_never_extends_hash_pinned_read_trust(
        self,
        mock_registry,
        tmp_path,
    ):
        from tools.registry import ToolEffect

        source = tmp_path / "trusted_server.py"
        source.write_text("# exact reviewed local MCP source\n", encoding="utf-8")
        config = {
            "command": sys.executable,
            "args": [str(source)],
            "trusted_read_only": {
                "source": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "tools": ["find_clients"],
            },
            "tools": {"resources": False, "prompts": False},
        }
        server = MCPServerTask("owner_read")
        server._refresh_lock = asyncio.Lock()
        server._config = config
        server._tools = [_make_mcp_tool("find_clients")]
        server.session = SimpleNamespace(
            list_tools=AsyncMock(
                return_value=SimpleNamespace(
                    tools=[
                        _make_mcp_tool("find_clients"),
                        _make_mcp_tool("new_write_tool"),
                    ]
                )
            )
        )

        with patch("tools.registry.registry", mock_registry):
            server._registered_tool_names = _register_server_tools(
                "owner_read",
                server,
                config,
            )
            await server._refresh_tools()

        assert (
            mock_registry.get_effect(
                "mcp__owner_read__find_clients",
                {},
            )
            is ToolEffect.READ_ONLY
        )
        assert (
            mock_registry.get_effect(
                "mcp__owner_read__new_write_tool",
                {},
            )
            is ToolEffect.UNKNOWN
        )


class TestMessageHandler:
    """Tests for MCPServerTask._make_message_handler dispatch."""

    @pytest.mark.asyncio
    async def test_dispatches_tool_list_changed(self):
        from tools.mcp_tool import _MCP_NOTIFICATION_TYPES
        if not _MCP_NOTIFICATION_TYPES:
            pytest.skip("MCP SDK ToolListChangedNotification not available")

        from mcp.types import ServerNotification, ToolListChangedNotification

        server = MCPServerTask("notif_srv")
        # Product now schedules the refresh as a background task (see
        # _schedule_tools_refresh in mcp_tool.py ~L918) rather than awaiting
        # it directly, to avoid wedging the stdio JSON-RPC stream. Patch at
        # the scheduler seam so we can still assert dispatch happened without
        # reaching into asyncio.create_task internals.
        with patch.object(MCPServerTask, "_schedule_tools_refresh") as mock_schedule:
            handler = server._make_message_handler()
            notification = ServerNotification(
                root=ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            await handler(notification)
            mock_schedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_exceptions_and_other_messages(self):
        server = MCPServerTask("notif_srv")
        with patch.object(MCPServerTask, "_schedule_tools_refresh") as mock_schedule:
            handler = server._make_message_handler()
            # Exceptions should not trigger refresh
            await handler(RuntimeError("connection dead"))
            # Unknown message types should not trigger refresh
            await handler({"jsonrpc": "2.0", "result": "ok"})
            mock_schedule.assert_not_called()


class TestDeregister:
    """Tests for ToolRegistry.deregister."""

    def test_removes_tool(self):
        reg = ToolRegistry()
        reg.register(name="foo", toolset="ts1", schema={}, handler=lambda x: x)
        assert "foo" in reg.get_all_tool_names()
        reg.deregister("foo")
        assert "foo" not in reg.get_all_tool_names()


    def test_noop_for_unknown_tool(self):
        reg = ToolRegistry()
        reg.deregister("nonexistent")  # Should not raise
