"""POSIX privacy invariants for the canonical SQLite session store."""

import os
import sqlite3
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX mode bits not enforced on Windows",
)


@contextmanager
def _permissive_umask():
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _force_wal(monkeypatch):
    monkeypatch.setattr(
        hermes_state,
        "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )
    monkeypatch.setattr(hermes_state, "resolve_journal_mode", lambda: "wal")


def test_session_db_tightens_database_and_created_wal_sidecars(tmp_path):
    db_path = tmp_path / "state.db"
    db_path.touch()
    db_path.chmod(0o644)

    with _permissive_umask():
        db = SessionDB(db_path=db_path)
        try:
            assert db._wal_active is True
            paths = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
            assert all(path.exists() for path in paths)
            assert {path.name: _mode(path) for path in paths} == {
                "state.db": 0o600,
                "state.db-wal": 0o600,
                "state.db-shm": 0o600,
            }
        finally:
            db.close()


def test_session_db_tightens_existing_wal_sidecars(tmp_path):
    db_path = tmp_path / "state.db"

    with _permissive_umask():
        seed = sqlite3.connect(db_path, isolation_level=None)
        try:
            assert seed.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            seed.execute("CREATE TABLE seed (value INTEGER)")
            paths = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
            assert all(path.exists() for path in paths)
            for path in paths:
                path.chmod(0o644)

            db = SessionDB(db_path=db_path)
            try:
                assert {path.name: _mode(path) for path in paths} == {
                    "state.db": 0o600,
                    "state.db-wal": 0o600,
                    "state.db-shm": 0o600,
                }
            finally:
                db.close()
        finally:
            seed.close()
