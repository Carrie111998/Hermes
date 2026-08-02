"""Sticky sessions.project_id must survive cwd-only updates."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "state.db"
    sdb = SessionDB(db_path=path)
    yield sdb
    sdb.close()


def _insert(db: SessionDB, sid: str) -> None:
    # Minimal create via public API if available; else raw insert after schema.
    conn = db._conn
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, cwd) VALUES (?, ?, ?, ?)",
        (sid, "desktop", time.time(), "/tmp"),
    )
    conn.commit()


def test_update_session_cwd_sets_and_preserves_project_id(db: SessionDB, tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    sid = "sess_sticky_1"
    _insert(db, sid)

    db.update_session_cwd(sid, str(work), project_id="p_health")
    row = db._conn.execute(
        "SELECT cwd, project_id FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert row["cwd"] == str(work)
    assert row["project_id"] == "p_health"

    # Terminal-style cwd update without project_id must not clear sticky id.
    db.update_session_cwd(sid, str(vault), git_repo_root=str(vault))
    row = db._conn.execute(
        "SELECT cwd, git_repo_root, project_id FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert row["cwd"] == str(vault)
    assert row["git_repo_root"] == str(vault)
    assert row["project_id"] == "p_health"

    db.update_session_cwd(sid, str(work), clear_project_id=True)
    row = db._conn.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert row["project_id"] is None
