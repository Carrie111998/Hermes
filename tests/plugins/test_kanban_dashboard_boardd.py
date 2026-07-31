"""Boardd-aware routing tests for the kanban dashboard plugin.

These tests verify that ``plugins/kanban/dashboard/plugin_api.py`` routes fleet
board traffic through the boardd broker when it is alive, falls back to a
writable local SQLite board in standalone mode, and fails closed (no silent
SQLite fallback) when boardd authority is configured but unreachable.

All tests run against a temporary boardd authority (temp DB + temp Unix socket)
so the production fleet board is never touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Use a fixed, test-file-local socket so the plugin's copy of kb_client resolves
# the broker consistently across all tests in this file.  Each test run is in a
# fresh subprocess, so this path is isolated from other test files.
_TEST_SOCKET = os.environ.get(
    "BOARDD_SOCK",
    "/tmp/test-kanban-dashboard-boardd.sock",
)
os.environ["BOARDD_SOCK"] = _TEST_SOCKET
# Keep retry/backoff short so a missing broker fails fast instead of retrying
# for the default 90 seconds.
os.environ.setdefault("KB_CLIENT_RETRY_DEADLINE_S", "0.5")
os.environ.setdefault("KB_CLIENT_CONNECT_TIMEOUT_S", "0.5")
os.environ.setdefault("KB_CLIENT_READ_TIMEOUT_S", "2.0")

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its module."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_boardd_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load the plugin once after the environment above is pinned.  This imports
# boardd_shim and installs the boardd-aware wrappers on ``kanban_db.connect``.
PLUGIN_MOD = _load_plugin_router()
ROUTER = PLUGIN_MOD.router


# ---------------------------------------------------------------------------
# Minimal boardd broker for testing
# ---------------------------------------------------------------------------
class _FakeBoardd:
    """A tiny, single-threaded boardd broker over a Unix domain socket.

    Supports the protocol surface used by ``boardd_shim.BrokerConnection``:
    ``ping``, ``query``, ``exec`` (autocommit writes), and interactive
    transactions via ``txn_begin`` / ``txn_exec`` / ``txn_commit`` /
    ``txn_rollback``.  All SQL executes against a real SQLite DB so the plugin
    sees genuine kanban_db behavior.
    """

    def __init__(self, db_path: str, sock_path: str):
        self.db_path = db_path
        self.sock_path = sock_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._txn_token: str | None = None
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(sock_path)
        self._sock.listen(1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass
        self._thread.join(timeout=3)
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

    def _serve(self):
        while not self._stop.is_set():
            try:
                self._sock.settimeout(0.2)
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                self._handle_client(client)
            except Exception:
                pass
            finally:
                try:
                    client.close()
                except Exception:
                    pass

    def _handle_client(self, client: socket.socket):
        rfile = client.makefile("r", encoding="utf-8")
        wfile = client.makefile("w", encoding="utf-8")
        try:
            for line in rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    resp = self._dispatch(req)
                except Exception as exc:
                    resp = {"ok": False, "error": str(exc)}
                wfile.write(json.dumps(resp) + "\n")
                wfile.flush()
        finally:
            rfile.close()
            wfile.close()
            client.close()

    def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "result": {"pong": True}}

        if op == "query":
            args = req.get("args", {})
            return self._do_query(args.get("sql", ""), args.get("params") or [])

        if op == "exec":
            args = req.get("args", {})
            return self._do_exec(args.get("sql", ""), args.get("params") or [])

        if op == "txn_begin":
            self._conn.execute("BEGIN IMMEDIATE")
            self._txn_token = uuid.uuid4().hex
            return {"ok": True, "result": {"txn": self._txn_token}}

        if op == "txn_exec":
            args = req.get("args", {})
            token = args.get("txn")
            if token != self._txn_token:
                return {
                    "ok": False,
                    "error": "stale transaction",
                    "etype": "OperationalError",
                }
            return self._do_txn_exec(
                args.get("sql", ""), args.get("params") or []
            )

        if op == "txn_commit":
            token = req.get("args", {}).get("txn")
            if token != self._txn_token:
                return {
                    "ok": False,
                    "error": "stale transaction",
                    "etype": "OperationalError",
                }
            self._conn.execute("COMMIT")
            self._txn_token = None
            return {"ok": True, "result": {}}

        if op == "txn_rollback":
            token = req.get("args", {}).get("txn")
            if token != self._txn_token:
                return {
                    "ok": False,
                    "error": "stale transaction",
                    "etype": "OperationalError",
                }
            self._conn.execute("ROLLBACK")
            self._txn_token = None
            return {"ok": True, "result": {}}

        if op in ("integrity_check", "quick_check"):
            return {"ok": True, "result": ["ok"]}

        return {"ok": False, "error": f"unknown op {op}"}

    def _do_query(self, sql: str, params: list) -> dict:
        cur = self._conn.execute(sql, params)
        rows = [dict(row) for row in cur.fetchall()]
        return {"ok": True, "result": rows}

    def _do_exec(self, sql: str, params: list) -> dict:
        cur = self._conn.execute(sql, params)
        return {
            "ok": True,
            "result": {
                "rowcount": cur.rowcount,
                "lastrowid": cur.lastrowid,
            },
        }

    def _do_txn_exec(self, sql: str, params: list) -> dict:
        s = (sql or "").strip().lower().split(None, 1)[0]
        cur = self._conn.execute(sql, params)
        rows = None
        if s in ("select", "with", "values", "pragma", "explain"):
            rows = [dict(row) for row in cur.fetchall()]
        return {
            "ok": True,
            "result": {
                "rows": rows,
                "rowcount": cur.rowcount,
                "lastrowid": cur.lastrowid,
            },
        }


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty default kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(ROUTER, prefix="/api/plugins/kanban")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Standalone (no broker) mode
# ---------------------------------------------------------------------------
def test_standalone_routes_through_local_sqlite(client):
    """Without a broker, the plugin uses the writable local kanban DB."""
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text
    data = r.json()
    assert {c["name"] for c in data["columns"]} == kb.VALID_STATUSES - {"archived"}

    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "standalone task", "assignee": "worker"},
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "standalone task"

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    ready = next(c for c in r.json()["columns"] if c["name"] == "ready")
    assert any(t["id"] == task["id"] for t in ready["tasks"])


# ---------------------------------------------------------------------------
# Boardd-broker mode
# ---------------------------------------------------------------------------
def test_broker_routes_fleet_reads_and_writes_through_broker(tmp_path, monkeypatch):
    """With a live boardd broker and the fleet board active, all traffic goes
    through the broker; the local mirror is not written."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Create the local fleet board (metadata + empty mirror DB).  The broker
    # owns a separate DB file; we initialise it with the same schema.
    kb.create_board("fleet")
    kb.set_current_board("fleet")
    broker_db = tmp_path / "broker.db"
    kb.init_db(db_path=broker_db)

    # Clean up any stale socket from a previous aborted run.
    try:
        os.unlink(_TEST_SOCKET)
    except FileNotFoundError:
        pass

    broker = _FakeBoardd(str(broker_db), _TEST_SOCKET)
    broker.start()
    try:
        app = FastAPI()
        app.include_router(ROUTER, prefix="/api/plugins/kanban")
        c = TestClient(app)

        # Read through the broker.
        r = c.get("/api/plugins/kanban/board")
        assert r.status_code == 200, r.text
        data = r.json()
        assert {col["name"] for col in data["columns"]} == kb.VALID_STATUSES - {
            "archived"
        }

        # Write through the broker (a reversible, synthetic task).
        r = c.post(
            "/api/plugins/kanban/tasks",
            json={"title": "brokered task", "assignee": "worker"},
        )
        assert r.status_code == 200, r.text
        task = r.json()["task"]
        assert task["title"] == "brokered task"

        # The task is visible through the broker.
        r = c.get("/api/plugins/kanban/board")
        assert r.status_code == 200
        ready = next(col for col in r.json()["columns"] if col["name"] == "ready")
        assert any(t["id"] == task["id"] for t in ready["tasks"])

        # But the SAME task is NOT in the local mirror DB.
        local_db = kb.kanban_db_path(board="fleet")
        local_conn = sqlite3.connect(str(local_db))
        try:
            cur = local_conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task["id"],)
            )
            assert cur.fetchone() is None
        finally:
            local_conn.close()
    finally:
        broker.stop()


# ---------------------------------------------------------------------------
# Fail-closed mode
# ---------------------------------------------------------------------------
def test_fail_closed_no_silent_sqlite_fallback(tmp_path, monkeypatch):
    """When boardd authority is configured for fleet but the broker is down,
    requests fail instead of silently writing to the local mirror."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Force fleet authority without a running broker.  The socket must be absent
    # so the ping test fails and the env var makes it authoritative anyway.
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "1")
    try:
        os.unlink(_TEST_SOCKET)
    except FileNotFoundError:
        pass

    kb.create_board("fleet")
    kb.set_current_board("fleet")

    app = FastAPI()
    app.include_router(ROUTER, prefix="/api/plugins/kanban")
    PLUGIN_MOD.register_boardd_exception_handlers(app)
    c = TestClient(app)

    # Both reads and writes must surface broker unavailability as HTTP 503.
    r = c.get("/api/plugins/kanban/board")
    assert r.status_code == 503, r.text

    r = c.post(
        "/api/plugins/kanban/tasks",
        json={"title": "should not land locally", "assignee": "worker"},
    )
    assert r.status_code == 503, r.text

    # The local mirror must remain untouched (no silent fallback).
    local_db = kb.kanban_db_path(board="fleet")
    local_conn = sqlite3.connect(str(local_db))
    try:
        cur = local_conn.execute("SELECT COUNT(*) FROM tasks")
        assert cur.fetchone()[0] == 0

        # Also prove the failed POST did not create any task row.
        cur = local_conn.execute(
            "SELECT id FROM tasks WHERE title = ?", ("should not land locally",)
        )
        assert cur.fetchone() is None
    finally:
        local_conn.close()
