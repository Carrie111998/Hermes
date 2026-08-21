"""Current-main contracts for profile-owned trigram FTS configuration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_state import SessionDB


def _write_config(home: Path, enabled: bool) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"sessions:\n  trigram_fts: {'true' if enabled else 'false'}\n",
        encoding="utf-8",
    )
    return home / "state.db"


def _object_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone() is not None


def test_explicit_db_path_uses_adjacent_profile_config(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    wrong_home = tmp_path / "wrong-home"
    db_path = _write_config(profile, False)
    _write_config(wrong_home, True)

    monkeypatch.setenv("HERMES_HOME", str(wrong_home))
    monkeypatch.setenv("HERMES_TRIGRAM_FTS", "1")  # stale legacy carrier

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn is not None
        assert db._trigram_available is False
        assert not _object_exists(db._conn, "messages_fts_trigram")
        assert _object_exists(db._conn, "messages_fts")
    finally:
        db.close()


def test_cli_sessiondb_honors_yaml_without_gateway_bridge(tmp_path, monkeypatch):
    home = tmp_path / "cli-profile"
    db_path = _write_config(home, False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TRIGRAM_FTS", raising=False)

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn is not None
        assert db._trigram_available is False
        assert not _object_exists(db._conn, "messages_fts_trigram")
    finally:
        db.close()


def test_profile_config_honors_env_expansion_and_managed_overlay(
    tmp_path, monkeypatch
):
    profile = tmp_path / "expanded-profile"
    profile.mkdir()
    db_path = profile / "state.db"
    (profile / "config.yaml").write_text(
        "sessions:\n  trigram_fts: ${TRIGRAM_SWITCH}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRIGRAM_SWITCH", "false")

    expanded = SessionDB(db_path=db_path)
    try:
        assert expanded._trigram_available is False
    finally:
        expanded.close()

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        "sessions:\n  trigram_fts: false\n",
        encoding="utf-8",
    )
    (profile / "config.yaml").write_text(
        "sessions:\n  trigram_fts: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    managed_db = SessionDB(db_path=db_path)
    try:
        assert managed_db._trigram_available is False
    finally:
        managed_db.close()
        managed_scope.invalidate_managed_cache()


def test_read_only_profile_does_not_serve_disabled_stale_trigram(
    tmp_path, monkeypatch
):
    home = tmp_path / "readonly-profile"
    db_path = _write_config(home, True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))

    writer = SessionDB(db_path=db_path)
    writer.create_session("s1", source="test")
    writer.append_message("s1", role="user", content="프로젝트 관리")
    writer.close()

    _write_config(home, False)
    reader = SessionDB(db_path=db_path, read_only=True)
    try:
        assert reader._fts_enabled is True
        assert reader._trigram_available is False
        assert reader.search_messages("관리")
    finally:
        reader.close()


def test_read_only_reenable_waits_for_writable_stale_rebuild(tmp_path, monkeypatch):
    home = tmp_path / "readonly-reenable"
    db_path = _write_config(home, True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))

    enabled = SessionDB(db_path=db_path)
    enabled.create_session("s1", source="test")
    enabled.append_message("s1", role="user", content="before quarantine")
    enabled.close()

    _write_config(home, False)
    disabled = SessionDB(db_path=db_path)
    disabled.append_message(
        "s1", role="user", content="DURING_DISABLED_UNIQUE 관리"
    )
    disabled.close()

    _write_config(home, True)
    reader = SessionDB(db_path=db_path, read_only=True)
    try:
        assert reader._trigram_available is False
        assert len(reader.search_messages("DURING_DISABLED_UNIQUE")) == 1
    finally:
        reader.close()

    rebuilt = SessionDB(db_path=db_path)
    try:
        assert rebuilt._trigram_available is True
        assert len(rebuilt.search_messages("DURING_DISABLED_UNIQUE")) == 1
    finally:
        rebuilt.close()


def test_read_only_during_reenable_rebuild_stays_on_fallback(
    tmp_path, monkeypatch
):
    home = tmp_path / "reenable-barrier"
    db_path = _write_config(home, True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))

    enabled = SessionDB(db_path=db_path)
    enabled.create_session("s1", source="test")
    enabled.append_message("s1", role="user", content="before barrier")
    enabled.close()

    _write_config(home, False)
    disabled = SessionDB(db_path=db_path)
    disabled.append_message("s1", role="user", content="BARRIER_UNIQUE 관리")
    disabled.close()
    _write_config(home, True)

    from hermes_state_schema import SessionSchemaMixin

    original = SessionSchemaMixin._rebuild_fts_indexes
    observed = {}

    def _probe_then_rebuild(cursor, *, include_trigram=True):
        reader = SessionDB(db_path=db_path, read_only=True)
        try:
            observed["available"] = reader._trigram_available
            observed["hits"] = len(reader.search_messages("BARRIER_UNIQUE"))
        finally:
            reader.close()
        return original(cursor, include_trigram=include_trigram)

    monkeypatch.setattr(
        SessionSchemaMixin,
        "_rebuild_fts_indexes",
        staticmethod(_probe_then_rebuild),
    )
    rebuilt = SessionDB(db_path=db_path)
    try:
        assert observed == {"available": False, "hits": 1}
        assert rebuilt._trigram_available is True
        assert len(rebuilt.search_messages("BARRIER_UNIQUE")) == 1
    finally:
        rebuilt.close()


def test_fts_stale_recovery_keeps_disabled_trigram_quarantined(
    tmp_path, monkeypatch
):
    home = tmp_path / "stale-recovery"
    db_path = _write_config(home, True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))

    enabled = SessionDB(db_path=db_path)
    enabled.create_session("s1", source="test")
    enabled.append_message("s1", role="user", content="stale recovery needle")
    assert enabled._conn is not None
    enabled._conn.execute(
        "INSERT INTO state_meta (key, value) VALUES ('fts_stale', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = '1'"
    )
    enabled._conn.commit()
    enabled.close()

    _write_config(home, False)
    recovered = SessionDB(db_path=db_path)
    try:
        assert recovered._conn is not None
        assert recovered._trigram_available is False
        trigger_count = recovered._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'messages_fts_trigram%'"
        ).fetchone()[0]
        assert trigger_count == 0
        assert recovered._conn.execute(
            "SELECT value FROM state_meta WHERE key = 'fts_trigram_stale'"
        ).fetchone() is not None
        assert len(recovered.search_messages("needle")) == 1
    finally:
        recovered.close()


def test_optimize_storage_retires_disabled_trigram_without_deleting_messages(
    tmp_path, monkeypatch
):
    home = tmp_path / "optimize-profile"
    db_path = _write_config(home, True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))

    enabled = SessionDB(db_path=db_path)
    enabled.create_session("s1", source="test")
    enabled.append_message("s1", role="user", content="canonical message")
    enabled.close()

    _write_config(home, False)
    disabled = SessionDB(db_path=db_path)
    try:
        assert disabled._conn is not None
        assert _object_exists(disabled._conn, "messages_fts_trigram")
        assert disabled.fts_optimize_available() is True
        result = disabled.optimize_fts_storage(vacuum=False)
        assert result["ok"] is True
        assert not _object_exists(disabled._conn, "messages_fts_trigram")
        assert not _object_exists(disabled._conn, "messages_fts_trigram_src")
        assert disabled._conn.execute(
            "SELECT content FROM messages WHERE session_id = 's1'"
        ).fetchone()[0] == "canonical message"
        assert _object_exists(disabled._conn, "messages_fts")
    finally:
        disabled.close()

    _write_config(home, True)
    reenabled = SessionDB(db_path=db_path)
    try:
        assert reenabled._conn is not None
        assert reenabled._trigram_available is True
        assert _object_exists(reenabled._conn, "messages_fts_trigram")
    finally:
        reenabled.close()
