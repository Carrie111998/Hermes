"""Regression coverage for cua-driver MCP stderr ownership on Windows."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import sys
from types import SimpleNamespace

from tools.computer_use import cua_backend


class _DeniedStderr:
    """Model prompt_toolkit's non-inheritable Windows output proxy."""

    def fileno(self) -> int:
        raise PermissionError(5, "Access is denied")


class _FakeClientSession:
    def __init__(self, _read, _write) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=[])


async def _run_lifecycle(session: cua_backend._CuaDriverSession) -> None:
    task = asyncio.create_task(session._lifecycle_coro())
    while session._shutdown_event is None:
        await asyncio.sleep(0)
    session._shutdown_event.set()
    await task


def test_stdio_spawn_uses_owned_real_stderr_when_process_stderr_is_denied(
    monkeypatch,
) -> None:
    captured = {}

    @asynccontextmanager
    async def fake_stdio_client(_params, *, errlog):
        captured["errlog"] = errlog
        assert errlog.fileno() >= 0
        yield object(), object()

    monkeypatch.setattr(cua_backend, "resolve_cua_driver_cmd", lambda: "cua-driver")
    monkeypatch.setattr(
        cua_backend,
        "_resolve_mcp_invocation",
        lambda _driver: ("cua-driver", ["mcp"]),
    )
    monkeypatch.setattr(
        cua_backend,
        "_standard_runtime_launch_args",
        lambda args, **_kwargs: (args, None),
    )

    import mcp
    import mcp.client.stdio

    monkeypatch.setattr(mcp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", fake_stdio_client)

    session = cua_backend._CuaDriverSession(cua_backend._AsyncBridge())
    with monkeypatch.context() as stderr_patch:
        stderr_patch.setattr(sys, "stderr", _DeniedStderr())
        asyncio.run(_run_lifecycle(session))

    assert captured["errlog"] is not sys.stderr
    assert captured["errlog"].closed
