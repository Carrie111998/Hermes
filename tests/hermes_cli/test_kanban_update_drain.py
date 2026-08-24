"""Transactional updates see detached workers across all Kanban boards."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db


def _make_probe_db(path: Path, rows: list[tuple[str, object]]) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE tasks (status TEXT, worker_pid INTEGER)")
        conn.executemany(
            "INSERT INTO tasks (status, worker_pid) VALUES (?, ?)",
            rows,
        )
    finally:
        conn.close()


def _route_boards(monkeypatch, paths: dict[str, Path]) -> None:
    monkeypatch.setattr(
        kanban_db,
        "list_boards",
        lambda include_archived=False: [{"slug": slug} for slug in paths],
    )
    monkeypatch.setattr(
        kanban_db,
        "kanban_db_path",
        lambda slug=None: paths[str(slug or kanban_db.DEFAULT_BOARD)],
    )


def test_active_worker_probe_missing_db_creates_nothing(tmp_path, monkeypatch):
    from hermes_cli import sqlite_safe_read

    missing = tmp_path / "not-created" / "kanban.db"
    _route_boards(monkeypatch, {"default": missing})

    def forbidden_open(*args, **kwargs):
        raise AssertionError("missing board must not open SQLite")

    monkeypatch.setattr(sqlite_safe_read, "connect_tracked", forbidden_open)

    assert kanban_db.active_worker_pids_all_boards() == []
    assert not missing.exists()
    assert not missing.parent.exists()


def test_active_worker_probe_is_read_only_and_deduplicates_paths_and_pids(
    tmp_path,
    monkeypatch,
):
    default_db = tmp_path / "default.db"
    shared_db = tmp_path / "shared.db"
    _make_probe_db(
        default_db,
        [
            ("running", 11),
            ("running", 12),
            ("done", 99),
            ("running", "not-a-pid"),
        ],
    )
    _make_probe_db(
        shared_db,
        [("running", 12), ("running", 13), ("running", 13)],
    )
    _route_boards(
        monkeypatch,
        {"default": default_db, "work": shared_db, "alias": shared_db},
    )

    def forbidden_writable_connect(*args, **kwargs):
        raise AssertionError("worker drain probe must not initialize a board")

    monkeypatch.setattr(kanban_db, "connect", forbidden_writable_connect)

    real_connect = sqlite3.connect
    opens: list[tuple[object, dict]] = []

    def recording_connect(database, *args, **kwargs):
        opens.append((database, dict(kwargs)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(kanban_db.sqlite3, "connect", recording_connect)
    liveness_checks: list[int] = []

    def pid_alive(pid: int) -> bool:
        liveness_checks.append(pid)
        return pid != 12

    monkeypatch.setattr(kanban_db, "_pid_alive", pid_alive)

    assert kanban_db.active_worker_pids_all_boards() == [11, 13]
    assert sorted(liveness_checks) == [11, 12, 13]
    assert len(opens) == 2, "aliases resolving to one DB must be opened once"
    for database, kwargs in opens:
        assert str(database).endswith("?mode=ro")
        assert kwargs["uri"] is True
        assert kwargs["timeout"] == pytest.approx(
            kanban_db._ACTIVE_WORKER_PROBE_BUSY_TIMEOUT_MS / 1000.0
        )

    # A writable ``connect()`` would run the full Kanban schema initializer.
    # The real read-only path leaves each minimal probe fixture untouched.
    for path in (default_db, shared_db):
        conn = real_connect(path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        assert tables == {"tasks"}


def test_active_worker_probe_locked_db_fails_closed_with_bounded_wait(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "locked.db"
    _make_probe_db(db_path, [("running", 42)])
    _route_boards(monkeypatch, {"default": db_path})
    monkeypatch.setattr(kanban_db, "_ACTIVE_WORKER_PROBE_BUSY_TIMEOUT_MS", 25)

    locker = sqlite3.connect(db_path, isolation_level=None, timeout=0.0)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            kanban_db.active_worker_pids_all_boards()
        elapsed = time.monotonic() - started
    finally:
        locker.execute("ROLLBACK")
        locker.close()

    assert elapsed < 1.0, "rollout probe must not inherit the 120s writer wait"


def test_active_worker_probe_propagates_database_errors(tmp_path, monkeypatch):
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    try:
        conn.execute("CREATE TABLE something_else (value TEXT)")
        conn.commit()
    finally:
        conn.close()
    _route_boards(monkeypatch, {"default": legacy_db})

    with pytest.raises(sqlite3.OperationalError, match="no such table: tasks"):
        kanban_db.active_worker_pids_all_boards()


def test_active_worker_probe_propagates_liveness_errors(tmp_path, monkeypatch):
    db_path = tmp_path / "workers.db"
    _make_probe_db(db_path, [("running", 42)])
    _route_boards(monkeypatch, {"default": db_path})

    def broken_liveness_probe(pid: int) -> bool:
        raise RuntimeError(f"cannot inspect pid {pid}")

    monkeypatch.setattr(kanban_db, "_pid_alive", broken_liveness_probe)

    with pytest.raises(RuntimeError, match="cannot inspect pid 42"):
        kanban_db.active_worker_pids_all_boards()
