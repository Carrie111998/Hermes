"""Boardd-aware routing tests for the kanban dashboard plugin API layer.

These tests verify that ``plugins.kanban.dashboard.plugin_api._conn``:
  * opens a direct SQLite connection when no boardd broker is active;
  * routes through ``hermes_cli.boardd_shim.BrokerConnection`` when a boardd
    broker is listening on the canonical socket;
  * fails closed (raises) when the broker socket exists but the broker is not
    reachable, rather than silently falling back to SQLite.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

from hermes_cli import kb_client

# Import the unit under test.  Importing it sets up the plugin module, so we
# keep this at module level.
from plugins.kanban.dashboard import plugin_api


@pytest.fixture
def isolated_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HERMES_HOME at a temp directory so tests never touch ~/.hermes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fast_kb_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed up the fail-closed test by lowering kb_client connect timeouts."""
    monkeypatch.setenv("KB_CLIENT_CONNECT_TIMEOUT_S", "1")


@pytest.fixture(autouse=True)
def clear_boardd_sock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure BOARDD_SOCK from the outer shell does not leak into tests.

    Tests that need a temp socket set it explicitly via monkeypatch.
    """
    monkeypatch.delenv("BOARDD_SOCK", raising=False)


@pytest.fixture
def boardd_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str, subprocess.Popen]]:
    """Start a temporary boardd broker and yield (sock_path, db_path, proc).

    The broker is torn down after the test.  We poll the socket with a raw
    kb_client.Client before yielding so callers get a ready broker.
    """
    repo_root = Path(__file__).resolve().parents[3]
    boardd_py = repo_root / "REFERENCE_boardd.py"
    assert boardd_py.exists(), f"reference boardd not found at {boardd_py}"

    sock_path = str(tmp_path / "boardd.sock")
    db_path = str(tmp_path / "kanban.db")

    monkeypatch.setenv("BOARDD_SOCK", sock_path)

    proc = subprocess.Popen(
        [
            sys.executable,
            str(boardd_py),
            "--db", db_path,
            "--sock", sock_path,
            "--log-level", "WARNING",
        ],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1)
                raise RuntimeError(
                    f"boardd exited early (code {proc.returncode}):\n"
                    f"{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}"
                )
            if os.path.exists(sock_path):
                try:
                    c = kb_client.Client(sock_path=sock_path)
                    c.ping()
                    c.close()
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            time.sleep(0.05)
        else:
            raise RuntimeError(f"boardd did not become ready: {last_exc}")

        yield sock_path, db_path, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        # Ensure the socket file is gone so the fail-closed/standalone tests
        # do not see a stale socket.
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


def _close_plugin_conn(conn) -> None:
    """Close a connection returned by ``plugin_api._conn``.

    For broker connections we also close the thread-local client because
    BrokerConnection.close() intentionally leaves the client socket open.
    """
    try:
        conn.close()
    except Exception:
        pass
    client = getattr(kb_client._tl, "client", None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
        kb_client._tl.client = None


def test_standalone_uses_sqlite(isolated_hermes_home: Path) -> None:
    """When no boardd socket exists, _conn returns a real sqlite3 connection."""
    conn = plugin_api._conn(board=None)
    assert isinstance(conn, sqlite3.Connection), type(conn)
    try:
        row = conn.execute("SELECT 1 AS n").fetchone()
        assert row["n"] == 1
    finally:
        conn.close()


@pytest.mark.live_system_guard_bypass
def test_boardd_active_uses_broker_connection(
    isolated_hermes_home: Path,
    boardd_process: tuple[str, str, subprocess.Popen],
) -> None:
    """When boardd is listening, _conn returns a BrokerConnection that can query."""
    sock_path, _db_path, _proc = boardd_process
    assert os.environ.get("BOARDD_SOCK") == sock_path

    conn = plugin_api._conn(board=None)
    assert type(conn).__name__ == "BrokerConnection", type(conn)
    try:
        row = conn.execute("SELECT 1 AS n").fetchone()
        assert row["n"] == 1
    finally:
        _close_plugin_conn(conn)


def test_fail_closed_when_broker_socket_exists_but_broker_down(
    isolated_hermes_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_kb_timeout: None,
) -> None:
    """A stale broker socket must not silently fall back to SQLite."""
    stale_sock = tmp_path / "stale-boardd.sock"
    # Create a socket file so _boardd_active considers custody active.
    stale_sock.write_text("")
    monkeypatch.setenv("BOARDD_SOCK", str(stale_sock))

    with pytest.raises(RuntimeError, match="boardd custody active but broker unreachable"):
        plugin_api._conn(board=None)
