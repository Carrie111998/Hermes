"""Regression tests for LSP process ownership and cancellation cleanup."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import psutil
import pytest

from agent.lsp.client import LSPClient


MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


async def _wait_for_file(path: Path, timeout: float = 2.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            value = path.read_text(encoding="ascii").strip()
            if value:
                return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _is_live(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def _wait_until_stopped(pid: int, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _is_live(pid):
            return True
        await asyncio.sleep(0.01)
    return not _is_live(pid)


def _force_stop(pid: int) -> None:
    if not _is_live(pid):
        return
    try:
        process = psutil.Process(pid)
        process.kill()
        process.wait(timeout=2.0)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass


@pytest.mark.asyncio
async def test_start_cancellation_reaps_spawned_process(tmp_path: Path):
    """Cancelling initialize after spawn must not leave the child alive."""
    pid_file = tmp_path / "server.pid"
    code = (
        "import os, sys, time; "
        "open(sys.argv[1], 'w', encoding='ascii').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    client = LSPClient(
        server_id="pyright",
        workspace_root=str(tmp_path),
        command=[sys.executable, "-c", code, str(pid_file)],
    )

    task = asyncio.create_task(client.start())
    pid = int(await _wait_for_file(pid_file))
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_until_stopped(pid) is True
    finally:
        await client._cleanup_process()


@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_shutdown_reaps_process_group_descendants(tmp_path: Path):
    """Graceful shutdown must also terminate descendants in the owned group."""
    child_pid_file = tmp_path / "child.pid"
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "MOCK_LSP_SCRIPT": "clean",
        "MOCK_LSP_CHILD_PID_FILE": str(child_pid_file),
    }
    client = LSPClient(
        server_id="pyright",
        workspace_root=str(repo),
        command=[sys.executable, MOCK_SERVER],
        env=env,
        cwd=str(repo),
    )

    await client.start()
    child_pid = int(await _wait_for_file(child_pid_file))
    try:
        await client.shutdown()
        assert await _wait_until_stopped(child_pid) is True
    finally:
        _force_stop(child_pid)
        await client.shutdown()
