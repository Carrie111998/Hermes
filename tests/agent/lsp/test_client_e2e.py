"""End-to-end client tests against the in-process mock LSP server.

Spins up :file:`_mock_lsp_server.py` as an actual subprocess, drives
it through real LSP traffic, and asserts diagnostic flow.  This is
the closest thing we have to integration coverage without requiring
pyright/gopls/etc. to be installed in CI.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent.lsp import client as client_module
from agent.lsp.client import LSPClient


MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


def _client(workspace: Path, script: str = "clean") -> LSPClient:
    env = {"MOCK_LSP_SCRIPT": script, "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
    return LSPClient(
        server_id=f"mock-{script}",
        workspace_root=str(workspace),
        command=[sys.executable, MOCK_SERVER],
        env=env,
        cwd=str(workspace),
    )


async def _wait_for_path(path: Path, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cleanup_waits_for_protocol_exit_before_signalling(tmp_path: Path):
    """Do not signal a PID that may have been reused after protocol exit."""
    events: list[str] = []

    class GracefulProcess:
        returncode = None

        async def wait(self):
            events.append("wait")
            self.returncode = 0
            return 0

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    client = _client(tmp_path)
    client._proc = GracefulProcess()  # type: ignore[assignment]

    await client._cleanup_process()

    assert events == ["wait"]
    assert client._proc is None


@pytest.mark.asyncio
async def test_cleanup_terminates_only_after_graceful_exit_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    events: list[str] = []

    class StubbornProcess:
        returncode = None
        terminated = False

        async def wait(self):
            events.append("wait")
            if not self.terminated:
                await asyncio.Future()
            self.returncode = -15
            return -15

        def terminate(self):
            events.append("terminate")
            self.terminated = True

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(client_module, "SHUTDOWN_GRACE", 0.01)
    client = _client(tmp_path)
    client._proc = StubbornProcess()  # type: ignore[assignment]

    await client._cleanup_process()

    assert events == ["wait", "terminate", "wait"]
    assert client._proc is None


@pytest.mark.asyncio
async def test_start_cancellation_owns_cleanup_until_spawned_child_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pid_file = tmp_path / "lsp.pid"
    monkeypatch.setenv("MOCK_LSP_PID_FILE", str(pid_file))
    monkeypatch.setattr(client_module, "SHUTDOWN_GRACE", 0.03)
    client = _client(tmp_path, "hang_initialize")
    start_task = asyncio.create_task(client.start())

    try:
        await _wait_for_path(pid_file)
        assert client._proc is not None and client._proc.returncode is None

        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        assert client.state == "error"
        assert client._proc is None
    finally:
        if not start_task.done():
            start_task.cancel()
        await client.shutdown()


@pytest.mark.asyncio
async def test_shutdown_outer_cancellation_does_not_orphan_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A caller timeout must not cancel teardown after the handle is detached."""
    events: list[str] = []
    exited = asyncio.Event()

    class StubbornProcess:
        returncode = None
        terminated = False

        async def wait(self):
            events.append("wait")
            if not self.terminated:
                await asyncio.Future()
            self.returncode = -15
            exited.set()
            return -15

        def terminate(self):
            events.append("terminate")
            self.terminated = True

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(client_module, "SHUTDOWN_GRACE", 0.03)
    client = _client(tmp_path)
    client._proc = StubbornProcess()  # type: ignore[assignment]

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.shutdown(), timeout=0.005)

    await asyncio.wait_for(exited.wait(), timeout=0.2)
    assert events == ["wait", "terminate", "wait"]
    assert client._proc is None


@pytest.mark.asyncio
async def test_client_lifecycle_clean(tmp_path: Path):
    """Full lifecycle: spawn, initialize, open, get clean diagnostics, shutdown."""
    f = tmp_path / "x.py"
    f.write_text("print('hi')\n")

    client = _client(tmp_path, "clean")
    await client.start()
    try:
        assert client.is_running
        version = await client.open_file(str(f), language_id="python")
        assert version == 0
        await client.wait_for_diagnostics(str(f), version, mode="document")
        diags = client.diagnostics_for(str(f))
        assert diags == []
    finally:
        await client.shutdown()
    assert not client.is_running


@pytest.mark.asyncio
async def test_client_receives_published_errors(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text("print('hi')\n")

    client = _client(tmp_path, "errors")
    await client.start()
    try:
        version = await client.open_file(str(f), language_id="python")
        await client.wait_for_diagnostics(str(f), version, mode="document")
        diags = client.diagnostics_for(str(f))
        assert len(diags) == 1
        d = diags[0]
        assert d["severity"] == 1
        assert d["code"] == "MOCK001"
        assert d["source"] == "mock-lsp"
        assert "synthetic error" in d["message"]
    finally:
        await client.shutdown()








