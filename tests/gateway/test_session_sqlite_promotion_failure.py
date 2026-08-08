"""Real-SQLite fail-closed coverage for terminal session promotion failures."""
from __future__ import annotations

import json
import sqlite3

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="promotion-failure-chat",
        user_id="promotion-failure-user",
        chat_type="dm",
    )


def _store(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    database = SessionDB(db_path=tmp_path / "state.db")
    store._db = database
    source = _source()
    original = store.get_or_create_session(source)
    return store, database, source, original


def _install_promotion_failure(database: SessionDB, session_id: str) -> None:
    assert session_id.replace("-", "").replace("_", "").isalnum()

    def install(connection):
        connection.execute(
            f"""
            CREATE TRIGGER fail_terminal_promotion
            BEFORE UPDATE OF ended_at, end_reason ON sessions
            WHEN OLD.id = '{session_id}'
            BEGIN
                SELECT RAISE(ABORT, 'forced promotion failure');
            END
            """
        )

    database._execute_write(install)


def _drop_promotion_failure(database: SessionDB) -> None:
    database._execute_write(
        lambda connection: connection.execute(
            "DROP TRIGGER IF EXISTS fail_terminal_promotion"
        )
    )


def _assert_failed_boundary(database: SessionDB, session_key: str, session_id: str) -> None:
    connection = sqlite3.connect(database.db_path)
    try:
        old = connection.execute(
            "SELECT ended_at, end_reason FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert old == (None, None)
        active = connection.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id"
        ).fetchall()
        assert active == [(session_id,)]
        route_row = connection.execute(
            "SELECT entry_json FROM gateway_routing WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        assert route_row is not None
        route = json.loads(route_row[0])
        assert route["session_id"] == session_id
        assert route["metadata"]["terminal_transition"]["session_id"] == session_id
    finally:
        connection.close()


def test_explicit_reset_sqlite_promotion_failure_retains_durable_boundary(tmp_path):
    store, database, _source_value, original = _store(tmp_path)
    _install_promotion_failure(database, original.session_id)

    with pytest.raises(RuntimeError, match="could not close prior SQLite session"):
        store.reset_session(original.session_key)

    _assert_failed_boundary(database, original.session_key, original.session_id)
    _drop_promotion_failure(database)
    replacement = store.reset_session(original.session_key)
    assert replacement.session_id != original.session_id
    assert database.get_session(original.session_id)["end_reason"] == "session_reset"


def test_auto_reset_sqlite_promotion_failure_retains_durable_boundary(tmp_path):
    store, database, source, original = _store(tmp_path)
    original.suspended = True
    store._save_entries()
    _install_promotion_failure(database, original.session_id)

    with pytest.raises(RuntimeError, match="could not close prior SQLite session"):
        store.get_or_create_session(source)

    _assert_failed_boundary(database, original.session_key, original.session_id)
    _drop_promotion_failure(database)
    replacement = store.get_or_create_session(source)
    assert replacement.session_id != original.session_id
    assert database.get_session(original.session_id)["end_reason"] == "suspended"


def test_switch_sqlite_promotion_failure_retains_durable_boundary(tmp_path):
    store, database, _source_value, original = _store(tmp_path)
    target_session_id = "promotion-retry-target"
    database.create_session(target_session_id, "telegram")
    database.end_session(target_session_id, "session_reset")
    _install_promotion_failure(database, original.session_id)

    with pytest.raises(RuntimeError, match="SQLite publication failed"):
        store.switch_session(original.session_key, target_session_id)

    _assert_failed_boundary(database, original.session_key, original.session_id)
    assert database.get_session(target_session_id)["ended_at"] is not None
    _drop_promotion_failure(database)
    replacement = store.switch_session(original.session_key, target_session_id)
    assert replacement.session_id == target_session_id
    assert database.get_session(original.session_id)["end_reason"] == "session_switch"
    assert database.get_session(target_session_id)["ended_at"] is None
