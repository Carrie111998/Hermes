"""Regression coverage for RPC ownership across MCP transport rebuilds."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_invalidating_session_cancels_owned_rpc() -> None:
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("generation-unit")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingSession:
        async def call_tool(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    old_session = HangingSession()
    server.session = old_session
    call = asyncio.create_task(
        server._call_session_rpc(
            "tools/call probe", lambda session: session.call_tool()
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    server._invalidate_session()

    with pytest.raises(RuntimeError, match="restarted while tools/call probe"):
        await asyncio.wait_for(call, timeout=1)
    assert cancelled.is_set()
    assert server.session is None

    replacement = object()
    server.session = replacement
    assert (
        await server._call_session_rpc(
            "tools/call probe", lambda session: asyncio.sleep(0, result=session)
        )
        is replacement
    )


@pytest.mark.asyncio
async def test_dynamic_tool_refresh_uses_session_invalidation() -> None:
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("generation-refresh")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingSession:
        async def list_tools(self, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    server.session = HangingSession()
    refresh = asyncio.create_task(server._refresh_tools())
    await asyncio.wait_for(started.wait(), timeout=1)

    server._invalidate_session()

    with pytest.raises(RuntimeError, match="restarted while tools/list refresh"):
        await asyncio.wait_for(refresh, timeout=1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_invalidated_session_rejects_new_rpc_before_transport_clears() -> None:
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("generation-teardown-window")
    session = object()
    invoked = False
    server.session = session
    server._invalidate_session(clear=False)

    async def call(_session):
        nonlocal invoked
        invoked = True

    with pytest.raises(
        RuntimeError, match="restarted while resources/read was starting"
    ):
        await server._call_session_rpc("resources/read", call)
    assert server.session is session
    assert invoked is False


def test_tool_handler_fails_fast_when_lifecycle_replaces_session() -> None:
    from tools import mcp_tool

    mcp_tool._ensure_mcp_loop()
    initial_ready = threading.Event()
    call_started = threading.Event()
    call_cancelled = threading.Event()
    replacement_ready = threading.Event()
    transport_count = 0

    class HangingSession:
        async def call_tool(self, *_args, **_kwargs):
            call_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                call_cancelled.set()

    class HealthySession:
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="text", text="healthy")],
                structuredContent=None,
            )

    class LifecycleServer(mcp_tool.MCPServerTask):
        async def _run_stdio(self, config: dict):
            nonlocal transport_count
            assert config["command"] == "unused"
            transport_count += 1
            self.session = (
                HangingSession() if transport_count == 1 else HealthySession()
            )
            self._ready.set()
            if transport_count == 1:
                initial_ready.set()
            else:
                replacement_ready.set()
            return await self._wait_for_lifecycle_event()

    server = LifecycleServer("generation-integration")
    mcp_tool._servers[server.name] = server
    mcp_tool._server_error_counts.pop(server.name, None)
    mcp_tool._server_breaker_opened_at.pop(server.name, None)
    loop = mcp_tool._mcp_loop
    assert loop is not None
    run_future = asyncio.run_coroutine_threadsafe(
        server.run({"command": "unused"}), loop
    )

    def reconnect_after_call_starts() -> None:
        assert call_started.wait(2)
        assert mcp_tool._signal_reconnect(server)

    reconnect_thread = threading.Thread(target=reconnect_after_call_starts)
    try:
        assert initial_ready.wait(2)
        reconnect_thread.start()
        handler = mcp_tool._make_tool_handler(server.name, "probe", 5.0)
        started_at = time.monotonic()
        result = json.loads(handler({}))
        elapsed = time.monotonic() - started_at
        reconnect_thread.join(timeout=2)

        assert elapsed < 2
        assert "restarted while tools/call probe" in result["error"]
        assert call_cancelled.wait(1)
        assert replacement_ready.wait(2)
        assert json.loads(handler({})) == {"result": "healthy"}
    finally:
        if reconnect_thread.is_alive():
            reconnect_thread.join(timeout=2)
        loop.call_soon_threadsafe(server._shutdown_event.set)
        run_future.result(timeout=5)
        mcp_tool._servers.pop(server.name, None)
        mcp_tool._server_error_counts.pop(server.name, None)
        mcp_tool._server_breaker_opened_at.pop(server.name, None)


@pytest.mark.parametrize(
    ("factory_name", "arguments", "session_method", "operation"),
    [
        ("_make_list_resources_handler", {}, "list_resources", "resources/list"),
        (
            "_make_read_resource_handler",
            {"uri": "file:///report.txt"},
            "read_resource",
            "resources/read",
        ),
        ("_make_list_prompts_handler", {}, "list_prompts", "prompts/list"),
        (
            "_make_get_prompt_handler",
            {"name": "review"},
            "get_prompt",
            "prompts/get",
        ),
    ],
)
def test_utility_handler_fails_fast_when_session_is_invalidated(
    factory_name: str,
    arguments: dict,
    session_method: str,
    operation: str,
) -> None:
    from tools import mcp_tool

    mcp_tool._ensure_mcp_loop()
    call_started = threading.Event()

    class HangingSession:
        pass

    async def hang(*_args, **_kwargs):
        call_started.set()
        await asyncio.Event().wait()

    session = HangingSession()
    setattr(session, session_method, hang)
    server = mcp_tool.MCPServerTask(f"generation-{session_method}")
    server.session = session
    server._ready.set()
    mcp_tool._servers[server.name] = server
    loop = mcp_tool._mcp_loop
    assert loop is not None

    def invalidate_after_call_starts() -> None:
        assert call_started.wait(2)
        loop.call_soon_threadsafe(server._invalidate_session)

    invalidator = threading.Thread(target=invalidate_after_call_starts)
    try:
        invalidator.start()
        handler = getattr(mcp_tool, factory_name)(server.name, 5.0)
        started_at = time.monotonic()
        result = json.loads(handler(arguments))
        elapsed = time.monotonic() - started_at
        invalidator.join(timeout=2)

        assert elapsed < 2
        assert f"restarted while {operation}" in result["error"]
    finally:
        if invalidator.is_alive():
            invalidator.join(timeout=2)
        mcp_tool._servers.pop(server.name, None)
