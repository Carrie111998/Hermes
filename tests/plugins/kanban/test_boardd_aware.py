"""Boardd-aware routing tests for the kanban dashboard plugin API layer.

These tests verify that ``plugins.kanban.dashboard.plugin_api._conn``:
  * opens a direct SQLite connection when no boardd broker is active;
  * routes through ``hermes_cli.boardd_shim.BrokerConnection`` when a boardd
    broker is listening on the canonical socket and the requested board is the
    fleet board (custody-active);
  * fails closed (raises) when custody is active but the broker is not
    reachable, rather than silently falling back to SQLite.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

# Keep retry/backoff short so a missing broker fails fast instead of retrying
# for the default 90 seconds.
os.environ.setdefault("KB_CLIENT_RETRY_DEADLINE_S", "1")
os.environ.setdefault("KB_CLIENT_CONNECT_TIMEOUT_S", "1")
os.environ.setdefault("KB_CLIENT_READ_TIMEOUT_S", "2")

from hermes_cli import boardd_shim, kb_client
from hermes_cli import kanban_db as kb

# Import the unit under test.  Importing it sets up the plugin module, so we
# keep this at module level.
from plugins.kanban.dashboard import plugin_api


@pytest.fixture
def isolated_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HERMES_HOME at a temp directory so tests never touch ~/.hermes."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def fleet_board(isolated_hermes_home: Path) -> None:
    """Create a fleet board and make it the current board.

    The plugin routes the fleet board through boardd when custody is active;
    non-fleet boards continue to use direct SQLite.
    """
    kb.create_board("fleet")
    kb.set_current_board("fleet")


@pytest.fixture(autouse=True)
def clear_boardd_sock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure BOARDD_SOCK from the outer shell does not leak into tests.

    Tests that need a temp socket set it explicitly via monkeypatch.
    """
    monkeypatch.delenv("BOARDD_SOCK", raising=False)


class _FakeBoardd(threading.Thread):
    """Minimal in-process boardd that answers ``ping`` and ``query``."""

    def __init__(self, db_path: str, sock_path: str) -> None:
        super().__init__(daemon=True)
        self.db_path = db_path
        self.sock_path = sock_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._txn_token: str | None = None
        self._stop_event = threading.Event()
        self._server: socket.socket | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def run(self) -> None:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(self.sock_path)
            s.listen(4)
            s.settimeout(0.2)
            self._server = s
            self._ready.set()
            while not self._stop_event.is_set():
                try:
                    conn, _ = s.accept()
                except socket.timeout:
                    continue
                self._handle(conn)
        except Exception as exc:  # noqa: BLE001
            self._error = exc
            self._ready.set()

    def _handle(self, conn: socket.socket) -> None:
        rfile = conn.makefile("r", encoding="utf-8")
        wfile = conn.makefile("w", encoding="utf-8")
        try:
            for line in rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    resp = self._dispatch(req)
                except Exception as exc:  # noqa: BLE001
                    resp = {"ok": False, "error": str(exc)}
                wfile.write(json.dumps(resp) + "\n")
                wfile.flush()
        finally:
            try:
                rfile.close()
            except Exception:
                pass
            try:
                wfile.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "result": {"pong": True}}

        if op == "query":
            args = req.get("args", {})
            cur = self._conn.execute(args.get("sql", ""), args.get("params") or [])
            rows = [dict(row) for row in cur.fetchall()]
            return {"ok": True, "result": rows}

        if op == "exec":
            args = req.get("args", {})
            cur = self._conn.execute(args.get("sql", ""), args.get("params") or [])
            return {
                "ok": True,
                "result": {
                    "rowcount": cur.rowcount,
                    "lastrowid": cur.lastrowid,
                },
            }

        if op == "txn_begin":
            self._conn.execute("BEGIN IMMEDIATE")
            self._txn_token = uuid.uuid4().hex
            return {"ok": True, "result": {"txn": self._txn_token}}

        if op == "txn_commit":
            self._conn.execute("COMMIT")
            self._txn_token = None
            return {"ok": True, "result": {}}

        if op == "txn_rollback":
            self._conn.execute("ROLLBACK")
            self._txn_token = None
            return {"ok": True, "result": {}}

        if op == "txn_exec":
            args = req.get("args", {})
            token = args.get("txn")
            if token != self._txn_token:
                return {
                    "ok": False,
                    "error": "stale transaction",
                    "etype": "OperationalError",
                }
            cur = self._conn.execute(args.get("sql", ""), args.get("params") or [])
            rows = [dict(row) for row in cur.fetchall()]
            return {
                "ok": True,
                "result": {
                    "rows": rows,
                    "rowcount": cur.rowcount,
                    "lastrowid": cur.lastrowid,
                },
            }

        if op in ("integrity_check", "quick_check"):
            return {"ok": True, "result": ["ok"]}

        return {"ok": False, "error": f"unknown op {op}", "etype": "ValueError"}

    def start_and_wait(self, timeout: float = 5.0) -> None:
        self.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("fake boardd did not become ready")
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass


@pytest.fixture
def boardd_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, _FakeBoardd]]:
    """Start a temporary in-process boardd broker and yield (sock_path, broker).

    The broker is torn down after the test.  We poll the socket with a raw
    kb_client.Client before yielding so callers get a ready broker.
    """
    db_path = str(tmp_path / "kanban.db")
    sock_path = str(tmp_path / "boardd.sock")
    monkeypatch.setenv("BOARDD_SOCK", sock_path)

    broker = _FakeBoardd(db_path, sock_path)
    broker.start_and_wait()
    try:
        # Final readiness check using the real client.
        deadline = time.monotonic() + 5
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                c = kb_client.Client(sock_path=sock_path)
                c.ping()
                c.close()
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(0.05)
        else:
            raise RuntimeError(f"fake boardd did not become ready: {last_exc}")

        yield sock_path, broker
    finally:
        broker.stop()


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
    fleet_board: None,
    boardd_process: tuple[str, _FakeBoardd],
) -> None:
    """When boardd is listening and the fleet board is current, _conn returns a BrokerConnection."""
    sock_path, _proc = boardd_process
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
    fleet_board: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale broker socket for the fleet board must not silently fall back to SQLite."""
    stale_sock = tmp_path / "stale-boardd.sock"
    # Create a socket file so _boardd_active considers custody active.
    stale_sock.write_text("")
    monkeypatch.setenv("BOARDD_SOCK", str(stale_sock))

    with pytest.raises(Exception, match="boardd custody active but broker unreachable"):
        plugin_api._conn(board=None)
