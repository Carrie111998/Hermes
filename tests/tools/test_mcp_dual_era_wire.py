from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any

import psutil
import pytest

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import MCPServerTask


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_SERVER = REPO_ROOT / "tests" / "fixtures" / "mcp_legacy_server.py"
LEGACY_CLIENT = REPO_ROOT / "tests" / "fixtures" / "mcp_legacy_client.py"
MODERN_SERVER = REPO_ROOT / "tests" / "fixtures" / "mcp_modern_server.py"


class _CountingSession:
    def __init__(self, session: Any) -> None:
        self._session = session
        self.calls: list[str] = []

    @property
    def protocol_version(self) -> str | None:
        return self._session.protocol_version

    async def discover(self):
        self.calls.append("server/discover")
        return await self._session.discover()

    async def initialize(self):
        self.calls.append("initialize")
        return await self._session.initialize()


def _legacy_python() -> str:
    path = os.environ.get("MCP_LEGACY_PYTHON")
    if not path or not Path(path).is_file():
        pytest.skip("MCP_LEGACY_PYTHON must point to the isolated mcp==1.28.1 interpreter")
    return path


def _run(coro):
    return asyncio.run(coro)


def _result_text(result: Any) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(result, "content", None) or [])
    )


async def _wait_until(predicate, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def _shutdown_without_leaks(
    task: MCPServerTask,
    baseline_tasks: set[asyncio.Task],
    baseline_children: set[int],
) -> None:
    await task.shutdown()
    await asyncio.sleep(0)
    assert task._task is not None and task._task.done()
    assert task.session is None
    assert not task._pending_refresh_tasks
    assert not task._pending_mrtr_tasks
    assert task._listen_task is None or task._listen_task.done()
    current = asyncio.current_task()
    leaked_tasks = {
        pending
        for pending in asyncio.all_tasks()
        if pending not in baseline_tasks
        and pending is not current
        and not pending.done()
    }
    assert leaked_tasks == set()
    live_children = {
        child.pid
        for child in psutil.Process().children(recursive=True)
        if child.is_running()
    }
    assert live_children - baseline_children == set()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"stateless fixture exited with {process.returncode}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"stateless fixture did not listen on port {port}")


def _legacy_client_report(tool_name: str, arguments: dict, server_argv: list[str]) -> dict:
    completed = subprocess.run(
        [
            _legacy_python(),
            str(LEGACY_CLIENT),
            tool_name,
            json.dumps(arguments),
            *server_argv,
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "HERMES_QUIET": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_auto_modern_wire_sends_discover_without_initialize():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import mcp_serve; mcp_serve.run_mcp_server()"],
        env={**os.environ, "HERMES_QUIET": "1"},
    )

    async def drive() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                counted = _CountingSession(session)
                task = MCPServerTask("modern-wire")
                task._config = {"protocol": "auto", "command": sys.executable}
                result = await task._negotiate_session(counted, 30)
                assert result is not None
                tools = await session.list_tools()
                assert any(tool.name == "conversations_list" for tool in tools.tools)
                call = await session.call_tool("conversations_list", arguments={})
                assert not call.is_error
                assert counted.calls == ["server/discover"]
                assert task.negotiated_era == "modern"

    _run(drive())


def test_auto_legacy_wire_attempts_modern_then_proves_legacy_once():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=_legacy_python(),
        args=[str(LEGACY_SERVER)],
    )

    async def drive() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                counted = _CountingSession(session)
                task = MCPServerTask("legacy-wire")
                task._config = {"protocol": "auto", "command": _legacy_python()}
                result = await task._negotiate_session(counted, 30)
                assert result is not None
                tools = await session.list_tools()
                assert "ping_echo" in {tool.name for tool in tools.tools}
                call = await session.call_tool("ping_echo", arguments={})
                text = "".join(
                    getattr(block, "text", "") or "" for block in call.content
                )
                assert text.strip() == "pong"
                assert counted.calls == ["server/discover", "initialize"]
                assert task.negotiated_era == "legacy"
                assert task._protocol_state is not None
                assert task._protocol_state.legacy_proof_attempted is True

    _run(drive())


def test_strict_modern_legacy_wire_never_initializes():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import MCPError

    params = StdioServerParameters(
        command=_legacy_python(),
        args=[str(LEGACY_SERVER)],
    )

    async def drive() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                counted = _CountingSession(session)
                task = MCPServerTask("strict-modern-wire")
                task._config = {"protocol": "2026-07-28", "command": _legacy_python()}
                with pytest.raises(MCPError):
                    await task._negotiate_session(counted, 30)
                assert counted.calls == ["server/discover"]
                assert task.negotiated_era is None

    _run(drive())


def test_explicit_legacy_wire_sends_initialize_without_discover():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=_legacy_python(),
        args=[str(LEGACY_SERVER)],
    )

    async def drive() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                counted = _CountingSession(session)
                task = MCPServerTask("strict-legacy-wire")
                task._config = {"protocol": "legacy", "command": _legacy_python()}
                result = await task._negotiate_session(counted, 30)
                assert result is not None
                tools = await session.list_tools()
                assert "ping_echo" in {tool.name for tool in tools.tools}
                call = await session.call_tool("ping_echo", arguments={})
                assert _result_text(call).strip() == "pong"
                assert counted.calls == ["initialize"]
                assert task.negotiated_era == "legacy"

    _run(drive())


@pytest.mark.parametrize(
    ("server_args", "success_tool", "success_args", "error_tool", "error_args", "error_text"),
    [
        (
            ["-c", "import mcp_serve; mcp_serve.run_mcp_server()"],
            "conversations_list",
            {},
            "conversation_get",
            {"session_key": "missing-wire-session"},
            "Conversation not found",
        ),
        (
            ["-m", "agent.transports.hermes_tools_mcp_server"],
            "skills_list",
            {},
            "skill_view",
            {"name": "missing-wire-skill"},
            "not found",
        ),
    ],
    ids=["mcp-serve", "hermes-tools"],
)
def test_production_lifecycle_runs_both_first_party_server_surfaces(
    server_args,
    success_tool,
    success_args,
    error_tool,
    error_args,
    error_text,
    tmp_path,
    monkeypatch,
):
    async def drive() -> None:
        mcp_tool._ensure_mcp_sdk()
        real_session = mcp_tool.ClientSession
        calls: list[str] = []

        class RecordingSession(real_session):
            async def discover(self):
                calls.append("server/discover")
                return await super().discover()

            async def initialize(self):
                calls.append("initialize")
                return await super().initialize()

        monkeypatch.setattr(mcp_tool, "ClientSession", RecordingSession)
        baseline_tasks = set(asyncio.all_tasks())
        baseline_children = {
            child.pid for child in psutil.Process().children(recursive=True)
        }
        task = MCPServerTask(f"first-party-{success_tool}")
        try:
            await asyncio.wait_for(
                task.start(
                    {
                        "protocol": "auto",
                        "command": sys.executable,
                        "args": server_args,
                        "env": {
                            "HERMES_HOME": str(tmp_path),
                            "HERMES_QUIET": "1",
                        },
                        "connect_timeout": 45,
                    }
                ),
                timeout=60,
            )
            initial_generation = task._connection_generation
            initial_session = task.session
            assert initial_session is not None
            assert task.negotiated_era == "modern"
            assert success_tool in {tool.name for tool in task._tools}
            initial_discovery_count = calls.count("server/discover")
            assert initial_discovery_count >= 1

            async with task._rpc_lock:
                success = await initial_session.call_tool(
                    success_tool,
                    arguments=success_args,
                )
                application_error = await initial_session.call_tool(
                    error_tool,
                    arguments=error_args,
                )
                protocol_error = await initial_session.call_tool(
                    "__missing_protocol_probe__",
                    arguments={},
                )
            assert not getattr(success, "is_error", False)
            assert error_text.lower() in _result_text(application_error).lower()
            assert getattr(protocol_error, "is_error", False)

            task._reconnect_event.set()
            await _wait_until(
                lambda: task._connection_generation > initial_generation
                and task.session is not None
                and task.session is not initial_session,
                timeout=45,
            )
            async with task._rpc_lock:
                reconnected = await task.session.call_tool(
                    success_tool,
                    arguments=success_args,
            )
            assert not getattr(reconnected, "is_error", False)
            assert "initialize" not in calls
            assert calls.count("server/discover") > initial_discovery_count
        finally:
            await _shutdown_without_leaks(
                task,
                baseline_tasks,
                baseline_children,
            )

    _run(drive())


@pytest.mark.parametrize(
    ("protocol", "command", "server_path", "expected_calls", "expected_era"),
    [
        (
            "2026-07-28",
            sys.executable,
            MODERN_SERVER,
            ["server/discover", "server/discover"],
            "modern",
        ),
        (
            "auto",
            None,
            LEGACY_SERVER,
            ["server/discover", "initialize", "server/discover", "initialize"],
            "legacy",
        ),
    ],
    ids=["modern", "legacy"],
)
def test_production_lifecycle_cancellation_interruption_reconnect_and_cleanup(
    protocol,
    command,
    server_path,
    expected_calls,
    expected_era,
    tmp_path,
    monkeypatch,
):
    async def drive() -> None:
        mcp_tool._ensure_mcp_sdk()
        real_session = mcp_tool.ClientSession
        calls: list[str] = []

        class RecordingSession(real_session):
            async def discover(self):
                calls.append("server/discover")
                return await super().discover()

            async def initialize(self):
                calls.append("initialize")
                return await super().initialize()

        monkeypatch.setattr(mcp_tool, "ClientSession", RecordingSession)
        baseline_tasks = set(asyncio.all_tasks())
        baseline_children = {
            child.pid for child in psutil.Process().children(recursive=True)
        }
        task = MCPServerTask(f"lifecycle-{expected_era}")
        executable = command or _legacy_python()
        try:
            await asyncio.wait_for(
                task.start(
                    {
                        "protocol": protocol,
                        "command": executable,
                        "args": [str(server_path)],
                        "env": {
                            "HERMES_HOME": str(tmp_path),
                            "HERMES_QUIET": "1",
                        },
                        "connect_timeout": 30,
                        "keepalive_interval": 5,
                    }
                ),
                timeout=45,
            )
            assert task.negotiated_era == expected_era
            assert {"ping_echo", "application_error", "slow_echo", "crash_transport"} <= {
                tool.name for tool in task._tools
            }
            current_session = task.session
            assert current_session is not None

            application_error = await current_session.call_tool(
                "application_error",
                arguments={},
            )
            assert getattr(application_error, "is_error", False)
            assert "fixture application error" in _result_text(application_error)

            pending_call = asyncio.create_task(
                current_session.call_tool(
                    "slow_echo",
                    arguments={"delay_ms": 10_000},
                )
            )
            await asyncio.sleep(0.1)
            pending_call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending_call

            healthy = await current_session.call_tool("ping_echo", arguments={})
            assert _result_text(healthy).strip() == "pong"
            generation = task._connection_generation
            with pytest.raises(Exception):
                await current_session.call_tool("crash_transport", arguments={})
            await _wait_until(
                lambda: task._connection_generation > generation
                and task.session is not None
                and task.session is not current_session,
                timeout=45,
            )
            recovered = await task.session.call_tool("ping_echo", arguments={})
            assert _result_text(recovered).strip() == "pong"
            assert calls == expected_calls
        finally:
            await _shutdown_without_leaks(
                task,
                baseline_tasks,
                baseline_children,
            )

    _run(drive())


def test_stateless_http_wire_has_no_initialize_or_session_id(tmp_path):
    port = _free_port()
    capture = tmp_path / "wire.jsonl"
    process = subprocess.Popen(
        [
            sys.executable,
            str(MODERN_SERVER),
            "--http",
            str(port),
            "--capture",
            str(capture),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_QUIET": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port, process)

        async def drive() -> None:
            task = MCPServerTask("stateless-http-wire")
            baseline_tasks = set(asyncio.all_tasks())
            baseline_children = {
                child.pid for child in psutil.Process().children(recursive=True)
            }
            try:
                await asyncio.wait_for(
                    task.start(
                        {
                            "protocol": "2026-07-28",
                            "url": f"http://127.0.0.1:{port}/mcp",
                            "skip_preflight": True,
                            "connect_timeout": 30,
                        }
                    ),
                    timeout=45,
                )
                assert task.negotiated_era == "modern"
                assert task.liveness_strategy == "stateless-discover"
                result = await task.session.call_tool("ping_echo", arguments={})
                assert _result_text(result).strip() == "pong"
            finally:
                await _shutdown_without_leaks(
                    task,
                    baseline_tasks,
                    baseline_children,
                )

        _run(drive())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    records = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    methods = [record["method"] for record in records if record["method"]]
    assert methods[0] == "server/discover"
    assert "initialize" not in methods
    assert "tools/list" in methods
    assert "tools/call" in methods
    assert all(record["session_id"] is None for record in records)


@pytest.mark.parametrize(
    ("server_argv", "tool_name", "arguments", "expected_text"),
    [
        (
            [sys.executable, "-c", "import mcp_serve; mcp_serve.run_mcp_server()"],
            "conversations_list",
            {},
            "conversations",
        ),
        (
            [sys.executable, "-m", "agent.transports.hermes_tools_mcp_server"],
            "skills_list",
            {},
            "skills",
        ),
        (
            [None, str(LEGACY_SERVER)],
            "ping_echo",
            {},
            "pong",
        ),
    ],
    ids=["mcp-serve", "hermes-tools", "legacy-fixture"],
)
def test_legacy_sdk_client_real_wire_matrix(
    server_argv,
    tool_name,
    arguments,
    expected_text,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    argv = [(_legacy_python() if part is None else part) for part in server_argv]
    report = _legacy_client_report(tool_name, arguments, argv)
    assert report["protocol_version"] == "2025-11-25"
    assert tool_name in report["tools"]
    assert report["call_error"] is False
    assert expected_text in report["call_text"]
    assert report["missing_error"]
