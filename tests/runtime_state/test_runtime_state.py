"""Behavioral tests for the standalone ARCH-001 runtime-state package."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import shutil
import threading

import pytest

from runtime_state import (
    DEFAULT_RETRY_CONFIG,
    INVALID_PROFILE_REFERENCE,
    MIGRATION_1_CHECKSUM,
    NOT_FOUND,
    OWNER_MISMATCH,
    RuntimeStateDB,
    RuntimeStateMigrationError,
    RuntimeStateSchemaError,
    SCHEMA_VERSION,
    STALE_VERSION,
    SUCCESS,
    RetryConfig,
    cas_claim_owner,
    cas_release_owner,
    cas_update_columns,
    create_approval_state,
    create_compression_state,
    create_session_state,
    create_task_state,
)
import runtime_state.migrations as migrations


def _insert_session(db: RuntimeStateDB, profile: str, session_id: str) -> None:
    db.connection.execute(
        "INSERT INTO session_state "
        "(profile_name, session_id, user_id, workspace, status, schema_version, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
        (profile, session_id, "user", "/workspace", SCHEMA_VERSION, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
    )
    db.connection.commit()


def _insert_task(
    db: RuntimeStateDB, profile: str, task_id: str, session_id: str
) -> None:
    db.connection.execute(
        "INSERT INTO task_state "
        "(profile_name, task_id, session_id, status, schema_version, created_at, updated_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
        (profile, task_id, session_id, SCHEMA_VERSION, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
    )
    db.connection.commit()


def test_fresh_install_is_wal_fk_enabled_and_seeded(tmp_path):
    path = tmp_path / "runtime-state.db"
    with RuntimeStateDB(path) as db:
        assert db.schema_version == SCHEMA_VERSION
        assert db.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.connection.execute(
            "SELECT checksum_sha256 FROM runtime_state_migrations WHERE version = 1"
        ).fetchone()[0] == MIGRATION_1_CHECKSUM
        assert db.connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_reopen_is_idempotent(tmp_path):
    path = tmp_path / "runtime-state.db"
    RuntimeStateDB(path).close()
    with RuntimeStateDB(path) as db:
        assert db.schema_version == SCHEMA_VERSION
        assert db.connection.execute(
            "SELECT COUNT(*) FROM runtime_state_migrations"
        ).fetchone()[0] == 1


def test_zero_byte_existing_file_is_treated_as_fresh(tmp_path):
    path = tmp_path / "runtime-state.db"
    path.touch()
    with RuntimeStateDB(path) as db:
        assert db.schema_version == SCHEMA_VERSION


def test_incompatible_schema_fails_before_write(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_version ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "schema_version INTEGER NOT NULL, "
        "min_compatible_schema_version INTEGER NOT NULL, "
        "updated_at TEXT NOT NULL);"
        "INSERT INTO schema_version VALUES (1, 99, 99, '2026-01-01T00:00:00.000Z');"
    )
    conn.close()
    before = path.read_bytes()
    with pytest.raises(RuntimeStateSchemaError):
        RuntimeStateDB(path)
    assert path.read_bytes() == before


def test_profile_scoped_cas_does_not_cross_write(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        _insert_session(db, "p1", "same-session")
        _insert_session(db, "p2", "same-session")
        first = cas_claim_owner(db.connection, "session_state", "p1", "same-session", "one", 0)
        second = cas_claim_owner(db.connection, "session_state", "p2", "same-session", "two", 0)
        assert first.success and second.success
        rows = db.connection.execute(
            "SELECT profile_name, owner, owner_version FROM session_state "
            "WHERE session_id = 'same-session' ORDER BY profile_name"
        ).fetchall()
        assert rows == [("p1", "one", 1), ("p2", "two", 1)]


def test_concurrent_claims_allow_one_owner_without_lost_update(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStateDB(path) as db:
        _insert_session(db, "p", "session")

    barrier = threading.Barrier(2)

    def claim(owner: str):
        with RuntimeStateDB(path) as db:
            barrier.wait(timeout=5)
            return cas_claim_owner(
                db.connection, "session_state", "p", "session", owner, 0
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("owner-a", "owner-b")))

    assert sum(result.success for result in results) == 1
    assert sum(result.error == OWNER_MISMATCH for result in results) == 1
    with RuntimeStateDB(path) as db:
        owner, version = db.connection.execute(
            "SELECT owner, owner_version FROM session_state "
            "WHERE profile_name = 'p' AND session_id = 'session'"
        ).fetchone()
    assert owner in {"owner-a", "owner-b"}
    assert version == 1


def test_cas_reports_owner_mismatch_and_stale_version(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        _insert_session(db, "p", "session")
        claimed = cas_claim_owner(db.connection, "session_state", "p", "session", "owner-a", 0)
        assert claimed.error == SUCCESS
        mismatch = cas_claim_owner(db.connection, "session_state", "p", "session", "owner-b", 1)
        assert not mismatch.success and mismatch.error == OWNER_MISMATCH
        stale = cas_update_columns(
            db.connection, "session_state", "p", "session", "owner-a", 0, {"status": "degraded"}
        )
        assert not stale.success and stale.error == STALE_VERSION


def test_update_and_release_stamp_version_and_clear_owner(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        _insert_session(db, "p", "session")
        assert cas_claim_owner(db.connection, "session_state", "p", "session", "owner", 0).success
        updated = cas_update_columns(
            db.connection, "session_state", "p", "session", "owner", 1, {"status": "completed"}
        )
        assert updated.success and updated.owner_version == 2
        released = cas_release_owner(db.connection, "session_state", "p", "session", "owner", 2)
        assert released.success and released.owner is None and released.owner_version == 3
        row = db.connection.execute(
            "SELECT owner, owner_version, status, schema_version FROM session_state "
            "WHERE profile_name = 'p' AND session_id = 'session'"
        ).fetchone()
        assert row == (None, 3, "completed", SCHEMA_VERSION)


def test_missing_row_is_not_found(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        result = cas_claim_owner(db.connection, "session_state", "p", "missing", "owner", 0)
        assert not result.success and result.error == NOT_FOUND


def test_cross_profile_task_reference_is_rejected(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        _insert_session(db, "p1", "s1")
        _insert_session(db, "p2", "s2")
        _insert_task(db, "p1", "task", "s1")
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "INSERT INTO approval_state "
                "(profile_name, approval_id, session_id, task_id, approval_status, "
                "breaker_status, schema_version, created_at, updated_at) "
                "VALUES ('p2', 'approval', 's2', 'task', 'pending', 'closed', ?, ?, ?)",
                (SCHEMA_VERSION, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
            )
        db.connection.rollback()


def test_task_session_composite_reference_is_rejected(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        _insert_session(db, "p", "s1")
        _insert_session(db, "p", "s2")
        _insert_task(db, "p", "task", "s1")
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "INSERT INTO compression_state "
                "(profile_name, session_id, task_id, compression_status, schema_version, created_at, updated_at) "
                "VALUES ('p', 's2', 'task', 'idle', ?, ?, ?)",
                (SCHEMA_VERSION, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
            )
        db.connection.rollback()


def test_invalid_status_is_rejected_by_ddl(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        _insert_session(db, "p", "s")
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "UPDATE session_state SET status = 'typo' "
                "WHERE profile_name = 'p' AND session_id = 's'"
            )
        db.connection.rollback()


def test_migration_checksum_tamper_fails_closed(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStateDB(path) as db:
        db.connection.execute(
            "UPDATE runtime_state_migrations SET description = 'tampered' WHERE version = 1"
        )
        db.connection.commit()
    with pytest.raises(RuntimeStateSchemaError, match="migration history"):
        RuntimeStateDB(path)


def test_busy_timeout_bounds_are_enforced():
    assert DEFAULT_RETRY_CONFIG.busy_timeout_ms == 5000
    RetryConfig(busy_timeout_ms=1)
    RetryConfig(busy_timeout_ms=10000)
    with pytest.raises(ValueError):
        RetryConfig(busy_timeout_ms=0)
    with pytest.raises(ValueError):
        RetryConfig(busy_timeout_ms=10001)


def test_central_create_api_validates_profile_references(tmp_path):
    with RuntimeStateDB(tmp_path / "state.db") as db:
        session = create_session_state(
            db.connection,
            "p",
            "session",
            user_id="user",
            workspace="/workspace",
            now="2026-01-01T00:00:00.000Z",
        )
        assert session.success and session.owner_version == 0
        task = create_task_state(
            db.connection,
            "p",
            "task",
            "session",
            now="2026-01-01T00:00:00.000Z",
        )
        assert task.success
        approval = create_approval_state(
            db.connection,
            "p",
            "approval",
            session_id="session",
            task_id="task",
            now="2026-01-01T00:00:00.000Z",
        )
        compression = create_compression_state(
            db.connection,
            "p",
            "session",
            task_id="task",
            now="2026-01-01T00:00:00.000Z",
        )
        assert approval.success and compression.success
        duplicate = create_session_state(
            db.connection,
            "p",
            "session",
            user_id="other-user",
            workspace="/other",
            now="2026-01-01T00:00:00.000Z",
        )
        assert duplicate.success
        assert db.connection.execute(
            "SELECT user_id, workspace FROM session_state "
            "WHERE profile_name = 'p' AND session_id = 'session'"
        ).fetchone() == ("user", "/workspace")
        invalid = create_task_state(
            db.connection,
            "other-profile",
            "task-2",
            "session",
        )
        assert not invalid.success and invalid.error == INVALID_PROFILE_REFERENCE


def test_initial_migration_failure_rolls_back_partial_schema(tmp_path, monkeypatch):
    path = tmp_path / "migration-failure.db"

    monkeypatch.setattr(
        migrations,
        "_split_sql",
        lambda _sql: [
            "CREATE TABLE partial_migration (id INTEGER)",
            "SELECT missing_column FROM partial_migration",
        ],
    )

    with pytest.raises(RuntimeStateMigrationError):
        RuntimeStateDB(path)

    with sqlite3.connect(path) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    assert tables == []


def test_online_backup_restore_preserves_runtime_state(tmp_path):
    path = tmp_path / "runtime-state.db"
    backup = tmp_path / "runtime-state.backup.db"

    with RuntimeStateDB(path) as db:
        created = create_session_state(
            db.connection,
            "profile",
            "session",
            user_id="user",
            workspace="/workspace",
        )
        assert created.success
        with sqlite3.connect(backup) as backup_conn:
            db.connection.backup(backup_conn)

    for suffix in ("-wal", "-shm"):
        (path.parent / f"{path.name}{suffix}").unlink(missing_ok=True)
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(RuntimeStateSchemaError):
        RuntimeStateDB(path)

    shutil.copy2(backup, path)
    with RuntimeStateDB(path) as restored:
        row = restored.connection.execute(
            "SELECT profile_name, session_id, user_id FROM session_state"
        ).fetchone()
    assert row == ("profile", "session", "user")
