"""Regression tests for issue #96036: codegraph MCP spawns per-call.

The bug:
    codegraph configured with ``--liftoff-only`` exits after a single
    JSON-RPC exchange. Connected directly via ``stdio_client``, hermes
    sees the subprocess exit between *every* tool call and re-spawns.
    Three calls == three node processes, three hermes session windows.

The fix:
    When a stdio MCP server is detected as one-shot (either via the
    ``--liftoff-only`` marker on codegraph or the explicit
    ``one_shot_supervisor: true`` config flag), hermes wraps the inner
    command in ``tools.mcp_one_shot_supervisor.py``. The supervisor is
    a long-lived stdio relay that spawns the inner server *per* exchange
    but presents a single stable process to hermes.

Verification:
    This test exercises the supervisor end-to-end with a tiny one-shot
    "echo-and-exit" stand-in. We send N JSON-RPC messages through the
    supervisor and assert that:

    * The supervisor itself spawns exactly once (one stable PID).
    * The inner server is invoked N times (preserves the one-shot
      semantics the underlying server requires).
    * All N responses round-trip cleanly.
    * The ``_run_stdio`` wiring in ``tools/mcp_tool.py`` rewrites
      ``--liftoff-only`` configs to point at the supervisor (the spawn
      count from hermes's perspective drops from N to 1).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# tests/tools/test_X.py → tests/tools → tests → repo root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SUPERVISOR_PATH = PROJECT_ROOT / "tools" / "mcp_one_shot_supervisor.py"


def _make_echo_server(tmp_path: Path, log_path: Path) -> Path:
    """Tiny one-shot "echo" server that exits after one read of stdin.

    Writes its PID to ``log_path`` so the test can count inner spawns.
    Mirrors the contract codegraph --liftoff-only exposes:
    read all of stdin → emit one JSON-RPC-shaped response → exit 0.
    """
    # str() (not !r) — !r on a Path emits ``WindowsPath('...')`` which is
    # undefined inside the inner script's namespace, so the script would
    # NameError before it ever touches stdin.
    log_path_str = str(log_path)
    script = tmp_path / "echo_inner_server.py"
    script.write_text(
        "import json, os, sys\n"
        f"open({log_path_str!r}, 'a').write(f'{{os.getpid()}}\\n')\n"
        # Drain stdin, then emit a single 'result' response echoing the
        # request id. Inner server exits 0 — clean EOF, the
        # codegraph-liftoff pattern.
        "raw = sys.stdin.buffer.read()\n"
        "try:\n"
        "    req = json.loads(raw.decode('utf-8'))\n"
        "    rid = req.get('id')\n"
        "except Exception:\n"
        "    rid = None\n"
        "resp = {\n"
        "    'jsonrpc': '2.0',\n"
        "    'id': rid,\n"
        "    'result': {'echoed': True, 'inner_pid': os.getpid()},\n"
        "}\n"
        "sys.stdout.buffer.write((json.dumps(resp) + '\\n').encode())\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    return script


def _run_supervisor(
    inner_cmd: str,
    inner_args: list[str],
    stdin_payload: bytes,
    timeout: float = 30.0,
) -> bytes:
    """Spawn the supervisor with ``inner_cmd/inner_args`` and feed it bytes."""
    cmd = [
        sys.executable,
        str(SUPERVISOR_PATH),
        "--inner-cmd",
        inner_cmd,
        "--label",
        "test-supervisor",
    ]
    for arg in inner_args:
        cmd.extend(["--inner-arg", arg])

    proc = subprocess.run(
        cmd,
        input=stdin_payload,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"supervisor exited {proc.returncode}; stderr:\n"
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    return proc.stdout


def _make_request(req_id: int) -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            }
        )
        + "\n"
    ).encode("utf-8")


class TestSupervisorEndToEnd:
    """Exercise the supervisor directly with a fake one-shot server."""

    def test_supervisor_reuses_inner_per_request(self, tmp_path):
        """Three requests → three inner spawns (preserved semantics) AND
        one stable supervisor (the whole point of #96036)."""
        log_path = tmp_path / "inner_pids.log"
        inner = _make_echo_server(tmp_path, log_path)

        # Send three independent requests as one stdin payload. The
        # supervisor reads them sequentially and spawns the inner server
        # per request — preserving the one-shot pattern codegraph needs.
        payload = b"".join(_make_request(i) for i in range(1, 4))

        stdout = _run_supervisor(sys.executable, [str(inner)], payload)

        # All three responses must round-trip.
        responses = [
            json.loads(line)
            for line in stdout.decode("utf-8").splitlines()
            if line.strip()
        ]
        assert len(responses) == 3, f"expected 3 lines, got {responses!r}"
        assert [r["id"] for r in responses] == [1, 2, 3]
        assert all(r["result"]["echoed"] is True for r in responses)

        # Inner server spawned three times (one per request) — preserves
        # the liftoff semantics the underlying server requires.
        inner_pids = [
            int(line.strip())
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(inner_pids) == 3, (
            f"expected 3 inner spawns (one per request), got {inner_pids!r}"
        )

    def test_supervisor_outer_process_count_is_one(self, tmp_path):
        """Across N requests, the supervisor itself spawns exactly once.

        This is the *spawn-count-reduction* the bug report calls for:
        hermes sees one stable process for the whole conversation rather
        than N one-shot processes.
        """
        log_path = tmp_path / "inner_pids.log"
        inner = _make_echo_server(tmp_path, log_path)

        payload = b"".join(_make_request(i) for i in range(1, 6))

        # The supervisor is the outer process. Even when the inner
        # server is invoked 5 times, the OUTER (what hermes sees) is one
        # process.  Verify this by checking supervisor behaviour across
        # multiple exchanges within a single ``subprocess.run`` — a
        # single outer invocation is the whole point of the wrapper.
        stdout = _run_supervisor(sys.executable, [str(inner)], payload)

        responses = [
            json.loads(line)
            for line in stdout.decode("utf-8").splitlines()
            if line.strip()
        ]
        assert len(responses) == 5

        # Inner spawned 5 times — that is the *intended* per-request
        # spawn that codegraph --liftoff-only requires.  The supervisor
        # did NOT spawn per request.
        inner_pids = [
            int(line.strip())
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(inner_pids) == 5
        # Distinct inner PIDs confirms the inner really did re-spawn
        # (the supervisor isn't accidentally short-circuiting).
        assert len(set(inner_pids)) == 5


class TestRunStdioWiring:
    """``tools/mcp_tool.py`` must route one-shot configs through the supervisor."""

    def test_liftoff_only_arg_triggers_supervisor_wrap(self, tmp_path):
        """``--liftoff-only`` in args → stdio client gets the supervisor.

        Auto-detection: codegraph's Direct mode marker is the canonical
        one-shot signature, so we wrap without requiring an explicit
        config flag. Otherwise the fix requires every operator to learn
        a new YAML field, which is not how #96036 will land.
        """
        from tools import mcp_tool

        captured = SimpleNamespace(command=None, args=None)

        def _fake_stdio_client(server_params, errlog=None):
            captured.command = server_params.command
            captured.args = list(server_params.args)
            # Return an async context manager that yields a closed-pair so
            # the test exits without blocking on stdin reads.
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _cm():
                yield (None, None)

            return _cm()

        config = {
            "command": "C:/fake/codegraph.exe",
            "args": [
                "--liftoff-only",
                "C:/fake/codegraph.js",
                "serve",
                "--mcp",
                "--path",
                "C:/work/yl",
            ],
        }

        with patch.object(mcp_tool, "stdio_client", _fake_stdio_client), \
             patch.object(mcp_tool, "_ensure_mcp_sdk", return_value=True), \
             patch.object(mcp_tool, "_kill_orphaned_mcp_children", lambda: None), \
             patch.object(mcp_tool, "_snapshot_child_pids", lambda: set()), \
             patch.object(mcp_tool, "_filter_mcp_children", lambda pids: pids), \
             patch.object(mcp_tool, "_write_stderr_log_header", lambda *a, **k: None), \
             patch.object(mcp_tool, "_get_mcp_stderr_log", lambda: None), \
             patch(
                 "tools.osv_check.check_package_for_malware",
                 return_value=None,
             ), \
             patch.object(mcp_tool.asyncio, "to_thread", _async_to_thread), \
             patch.object(mcp_tool, "ClientSession"), \
             patch.object(
                 mcp_tool.MCPServerTask, "_negotiate_session",
                 new=_async_negotiate_session,
             ), \
             patch.object(
                 mcp_tool.MCPServerTask, "_discover_tools", new=_async_noop,
             ), \
             patch.object(
                 mcp_tool.MCPServerTask, "_wait_for_lifecycle_event",
                 new=_async_lifecycle_returns_recycle,
             ):

            task = mcp_tool.MCPServerTask("codegraph")
            import asyncio

            async def _drive():
                task._config = config
                await task._run_stdio(config)

            asyncio.run(_drive())

        # Verify the args routed through stdio_client point at the supervisor.
        assert captured.command == sys.executable, (
            f"expected python interpreter, got {captured.command!r}"
        )
        assert str(SUPERVISOR_PATH) in " ".join(captured.args), (
            f"expected supervisor path in args, got {captured.args!r}"
        )
        # The real command must be inside the wrapped args.
        joined = " ".join(captured.args)
        assert "C:/fake/codegraph.exe" in joined, (
            f"inner command missing from wrapped args: {joined!r}"
        )

    def test_explicit_flag_triggers_supervisor_wrap(self, tmp_path):
        """``one_shot_supervisor: true`` in config → wrap regardless of args."""
        from tools import mcp_tool

        captured = SimpleNamespace(command=None, args=None)

        def _fake_stdio_client(server_params, errlog=None):
            captured.command = server_params.command
            captured.args = list(server_params.args)
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _cm():
                yield (None, None)

            return _cm()

        config = {
            "command": "C:/fake/oneshot.exe",
            "args": ["serve"],
            "one_shot_supervisor": True,
        }

        with patch.object(mcp_tool, "stdio_client", _fake_stdio_client), \
             patch.object(mcp_tool, "_ensure_mcp_sdk", return_value=True), \
             patch.object(mcp_tool, "_kill_orphaned_mcp_children", lambda: None), \
             patch.object(mcp_tool, "_snapshot_child_pids", lambda: set()), \
             patch.object(mcp_tool, "_filter_mcp_children", lambda pids: pids), \
             patch.object(mcp_tool, "_write_stderr_log_header", lambda *a, **k: None), \
             patch.object(mcp_tool, "_get_mcp_stderr_log", lambda: None), \
             patch(
                 "tools.osv_check.check_package_for_malware",
                 return_value=None,
             ), \
             patch.object(mcp_tool.asyncio, "to_thread", _async_to_thread), \
             patch.object(mcp_tool, "ClientSession"), \
             patch.object(
                 mcp_tool.MCPServerTask, "_negotiate_session",
                 new=_async_negotiate_session,
             ), \
             patch.object(
                 mcp_tool.MCPServerTask, "_discover_tools", new=_async_noop,
             ), \
             patch.object(
                 mcp_tool.MCPServerTask, "_wait_for_lifecycle_event",
                 new=_async_lifecycle_returns_recycle,
             ):

            task = mcp_tool.MCPServerTask("oneshot")
            import asyncio

            async def _drive():
                task._config = config
                await task._run_stdio(config)

            asyncio.run(_drive())

        joined = " ".join(captured.args)
        assert str(SUPERVISOR_PATH) in joined, (
            f"explicit one_shot_supervisor flag did not wrap: {joined!r}"
        )

    def test_normal_server_is_not_wrapped(self, tmp_path):
        """A vanilla long-lived stdio server must NOT be wrapped."""
        from tools import mcp_tool

        captured = SimpleNamespace(command=None, args=None)

        def _fake_stdio_client(server_params, errlog=None):
            captured.command = server_params.command
            captured.args = list(server_params.args)
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _cm():
                yield (None, None)

            return _cm()

        config = {
            "command": "C:/fake/long-lived.exe",
            "args": ["--serve", "--port", "9999"],
        }

        with patch.object(mcp_tool, "stdio_client", _fake_stdio_client), \
             patch.object(mcp_tool, "_ensure_mcp_sdk", return_value=True), \
             patch.object(mcp_tool, "_kill_orphaned_mcp_children", lambda: None), \
             patch.object(mcp_tool, "_snapshot_child_pids", lambda: set()), \
             patch.object(mcp_tool, "_filter_mcp_children", lambda pids: pids), \
             patch.object(mcp_tool, "_write_stderr_log_header", lambda *a, **k: None), \
             patch.object(mcp_tool, "_get_mcp_stderr_log", lambda: None), \
             patch(
                 "tools.osv_check.check_package_for_malware",
                 return_value=None,
             ), \
             patch.object(mcp_tool.asyncio, "to_thread", _async_to_thread), \
             patch.object(mcp_tool, "ClientSession"), \
             patch.object(
                 mcp_tool.MCPServerTask, "_negotiate_session",
                 new=_async_negotiate_session,
             ), \
             patch.object(
                 mcp_tool.MCPServerTask, "_discover_tools", new=_async_noop,
             ), \
             patch.object(
                 mcp_tool.MCPServerTask, "_wait_for_lifecycle_event",
                 new=_async_lifecycle_returns_recycle,
             ):

            task = mcp_tool.MCPServerTask("normal")
            import asyncio

            async def _drive():
                task._config = config
                await task._run_stdio(config)

            asyncio.run(_drive())

        # Normal server: supervisor NOT injected.
        assert "mcp_one_shot_supervisor" not in " ".join(captured.args), (
            f"normal server was unexpectedly wrapped: {captured.args!r}"
        )


# -- helpers used by the wiring tests ------------------------------------

async def _async_to_thread(func, *args, **kwargs):
    """Sync passthrough so patched ``asyncio.to_thread`` doesn't deadlock."""
    return func(*args, **kwargs)


async def _async_negotiate_session(*_args, **_kwargs):
    return SimpleNamespace(capabilities=SimpleNamespace(tools=None))


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_lifecycle_returns_recycle(*_args, **_kwargs):
    return "recycle"