"""Tests for MCP tool-handler stale-connection auto-reconnect (#90166).

A CDN / load balancer / proxy can close an idle keep-alive connection
out from under the MCP streamable-HTTP transport.  The SDK then surfaces
``MCPError: Connection closed`` or ``RemoteProtocolError: Server
disconnected without sending a response`` on the next tool call.  The
server itself is healthy — only the pooled connection is dead — so the
tool handler rebuilds the transport and retries once instead of
surfacing the transient failure to the model.

Before this fix, that error class fell through both the auth and
session-expired recovery paths (it is neither a credential problem nor a
server-side session GC) and landed as a plain tool error, so an idle
CDN kill turned into a visible ``MCPError: Connection closed`` that
self-healed only minutes later after parking.
"""

import asyncio
import json
import threading

import pytest


# ---------------------------------------------------------------------------
# _is_stale_connection_error — unit coverage
# ---------------------------------------------------------------------------


def _make_remote_protocol_error():
    """Build the exact exception class the SDK's httpx2 stack raises."""
    try:
        import httpx2
    except ImportError:  # pragma: no cover - dev extra always installs it
        pytest.skip("httpx2 not installed")
    return httpx2.RemoteProtocolError(
        "Server disconnected without sending a response"
    )


def test_is_stale_connection_detects_remote_protocol_error():
    """Reporter's exact traceback error (#90166)."""
    from tools.mcp_tool import _is_stale_connection_error
    assert _is_stale_connection_error(_make_remote_protocol_error()) is True


def test_is_stale_connection_detects_httpcore_remote_protocol_error():
    """The SDK may leak the underlying httpcore2 exception un-wrapped."""
    from tools.mcp_tool import _is_stale_connection_error
    try:
        import httpcore2
    except ImportError:  # pragma: no cover
        pytest.skip("httpcore2 not installed")
    exc = httpcore2.RemoteProtocolError(
        "Server disconnected without sending a response"
    )
    assert _is_stale_connection_error(exc) is True


def test_is_stale_connection_detects_connection_closed_message():
    """``MCPError: Connection closed`` — the SDK's user-facing wording."""
    from tools.mcp_tool import _is_stale_connection_error
    assert _is_stale_connection_error(RuntimeError("MCPError: Connection closed")) is True


def test_is_stale_connection_detects_wrapped_exception_group():
    """post_writer raises inside an anyio TaskGroup (issue traceback)."""
    from tools.mcp_tool import _is_stale_connection_error
    inner = _make_remote_protocol_error()
    eg = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    assert _is_stale_connection_error(eg) is True


def test_is_stale_connection_detects_chained_cause():
    """SDK wrappers raise a generic error *from* the transport error."""
    from tools.mcp_tool import _is_stale_connection_error
    root = _make_remote_protocol_error()
    wrapper = RuntimeError("MCP call failed")
    wrapper.__cause__ = root
    assert _is_stale_connection_error(wrapper) is True


def test_is_stale_connection_rejects_unrelated_errors():
    from tools.mcp_tool import _is_stale_connection_error
    assert _is_stale_connection_error(RuntimeError("Invalid params")) is False
    assert _is_stale_connection_error(ValueError("tool returned bad JSON")) is False
    # auth failures stay on the OAuth recovery path
    assert _is_stale_connection_error(RuntimeError("401 Unauthorized")) is False


def test_is_stale_connection_interrupt_override():
    from tools.mcp_tool import _is_stale_connection_error
    assert _is_stale_connection_error(InterruptedError("user stop")) is False


def test_is_stale_connection_traversal_is_budget_bounded():
    """Pathologically long chains stop at the node budget without spinning."""
    import tools.mcp_tool as mcp_mod
    from tools.mcp_tool import _is_stale_connection_error

    exc: BaseException = RuntimeError("leaf")
    for i in range(mcp_mod._EXC_TRAVERSAL_MAX_NODES * 2):
        wrapper = RuntimeError(f"layer {i}")
        wrapper.__cause__ = exc
        exc = wrapper
    assert _is_stale_connection_error(exc) is False


# ---------------------------------------------------------------------------
# Handler integration — recovery plumbing wires end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transport_config, expected_route",
    [
        ({"command": "cdr-mcp"}, "stdio"),
        ({"url": "https://qcc.example.test/mcp", "skip_preflight": True}, "http"),
    ],
    ids=["stdio", "http"],
)
def test_call_tool_handler_rebuilds_transport_on_stale_connection(
    monkeypatch, tmp_path, transport_config, expected_route
):
    """First call hits a dead keep-alive connection; the handler rebuilds the
    transport and retries once instead of surfacing the error."""
    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask, _make_tool_handler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mcp_tool._ensure_mcp_loop()
    transport_ready = threading.Event()
    routes = []
    sessions = []
    call_count = {"n": 0}

    class _Session:
        async def call_tool(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _make_remote_protocol_error()
            result = type("R", (), {})()
            result.is_error = False
            result.content = [type("C", (), {})()]
            result.content[0].type = "text"
            result.content[0].text = "reconnected"
            result.structured_content = None
            return result

    class _LifecycleTask(MCPServerTask):
        async def _serve_transport(self, route, config):
            routes.append(route)
            self.session = _Session()
            sessions.append(self.session)
            self._ready.set()
            transport_ready.set()
            return await self._wait_for_lifecycle_event()

        async def _run_stdio(self, config):
            return await self._serve_transport("stdio", config)

        async def _run_http(self, config):
            return await self._serve_transport("http", config)

    server = _LifecycleTask("staleconn")
    mcp_tool._servers["staleconn"] = server
    mcp_tool._server_error_counts.pop("staleconn", None)
    mcp_tool._server_breaker_opened_at.pop("staleconn", None)
    loop = mcp_tool._mcp_loop
    assert loop is not None
    run_future = asyncio.run_coroutine_threadsafe(
        server.run(transport_config), loop
    )

    try:
        assert transport_ready.wait(3), "server lifecycle did not establish transport"
        handler = _make_tool_handler("staleconn", "lookup", 10.0)
        parsed = json.loads(handler({}))

        assert parsed == {"result": "reconnected"}
        assert call_count["n"] == 2
        assert routes == [expected_route, expected_route]
        assert len(sessions) == 2
    finally:
        loop.call_soon_threadsafe(server._shutdown_event.set)
        run_future.result(timeout=10)
        mcp_tool._servers.pop("staleconn", None)
