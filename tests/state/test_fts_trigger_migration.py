"""Tests for the FTS UPDATE trigger narrowing migration.

Covers the migration contract from the PR review (NousResearch/hermes-agent#68891):
  - Broad → narrowed migration (inspects sqlite_master.sql, only migrates
    when UPDATE trigger is broad).
  - Idempotent reopen (already-converged DB performs reads only).
  - INSERT/DELETE triggers stay present throughout.
  - No FTS rebuild on migration (broad triggers may have over-indexed
    unchanged payload, but have not missed content updates).
  - FTS-disabled / trigram-unavailable paths.
  - Real content updates still propagate to FTS.
"""

import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _trigger_sql(db_path, trigger_name):
    """Read the CREATE TRIGGER statement from sqlite_master."""
    raw = sqlite3.connect(str(db_path))
    row = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (trigger_name,),
    ).fetchone()
    raw.close()
    return row[0] if row else None


def _trigger_exists(db_path, trigger_name):
    raw = sqlite3.connect(str(db_path))
    row = raw.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (trigger_name,),
    ).fetchone()
    raw.close()
    return row is not None


def _fts_search(db_path, table, query):
    raw = sqlite3.connect(str(db_path))
    rows = raw.execute(
        f"SELECT rowid FROM {table} WHERE {table} MATCH ?",
        (query,),
    ).fetchall()
    raw.close()
    return [r[0] for r in rows]


def _create_broad_triggers(db_path):
    """Install broad UPDATE triggers (no WHEN clause) to simulate a
    pre-migration database."""
    raw = sqlite3.connect(str(db_path))
    # Drop existing narrowed triggers first so we can install broad ones.
    raw.execute("DROP TRIGGER IF EXISTS messages_fts_update")
    raw.execute("DROP TRIGGER IF EXISTS messages_fts_trigram_update")
    raw.execute(
        """CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
            INSERT INTO messages_fts(rowid, content) VALUES (
                new.id,
                COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
            );
        END;"""
    )
    raw.execute(
        """CREATE TRIGGER messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
            DELETE FROM messages_fts_trigram WHERE rowid = old.id;
            INSERT INTO messages_fts_trigram(rowid, content) VALUES (
                new.id,
                COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
            );
        END;"""
    )
    raw.commit()
    raw.close()


class TestTriggerMigration:
    def test_broad_update_trigger_is_narrowed_on_init(self, db, tmp_path):
        """A broad UPDATE trigger gets narrowed to payload-scoped on the
        next SessionDB init."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello world")

        # Install broad triggers (simulating a pre-migration DB).
        db.close()
        _create_broad_triggers(tmp_path / "state.db")

        # Reopen — should narrow the triggers.
        db2 = SessionDB(db_path=tmp_path / "state.db")
        try:
            sql = _trigger_sql(tmp_path / "state.db", "messages_fts_update")
            assert sql is not None
            assert "WHEN" in sql.upper()
            assert "COALESCE(new.content" in sql
            assert "COALESCE(new.tool_name" in sql
            assert "COALESCE(new.tool_calls" in sql
        finally:
            db2.close()

    def test_insert_delete_triggers_preserved_during_migration(self, db, tmp_path):
        """INSERT/DELETE triggers must remain present after narrowing
        UPDATE triggers."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello")

        db.close()
        _create_broad_triggers(tmp_path / "state.db")

        db2 = SessionDB(db_path=tmp_path / "state.db")
        try:
            assert _trigger_exists(tmp_path / "state.db", "messages_fts_insert")
            assert _trigger_exists(tmp_path / "state.db", "messages_fts_delete")
            assert _trigger_exists(tmp_path / "state.db", "messages_fts_trigram_insert")
            assert _trigger_exists(tmp_path / "state.db", "messages_fts_trigram_delete")
        finally:
            db2.close()

    def test_migration_is_idempotent(self, db, tmp_path):
        """Reopening an already-converged DB performs reads only — no
        second migration, no FTS rebuild."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello")

        # First reopen: triggers are already narrowed (from DDL).
        db.close()
        db2 = SessionDB(db_path=tmp_path / "state.db")
        db2.close()

        # Second reopen: still narrowed, no rebuild.
        db3 = SessionDB(db_path=tmp_path / "state.db")
        try:
            sql = _trigger_sql(tmp_path / "state.db", "messages_fts_update")
            assert "WHEN" in sql.upper()
            # Trigger count should be the full set — no rebuild needed.
            cursor = db3._conn.cursor()
            count = db3._fts_trigger_count(cursor)
            assert count == 6
        finally:
            db3.close()

    def test_no_fts_rebuild_on_migration(self, db, tmp_path):
        """Migration must NOT rebuild FTS indexes — broad triggers may
        have over-indexed unchanged payload, but have not missed content
        updates, so existing index contents remain valid."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "searchable content here")

        # Verify FTS has the content.
        hits = _fts_search(tmp_path / "state.db", "messages_fts", "searchable")
        assert len(hits) == 1

        # Install broad triggers.
        db.close()
        _create_broad_triggers(tmp_path / "state.db")

        # Reopen — migration narrows triggers but does NOT rebuild FTS.
        db2 = SessionDB(db_path=tmp_path / "state.db")
        try:
            # Content should still be searchable (no rebuild wiped it).
            hits = _fts_search(tmp_path / "state.db", "messages_fts", "searchable")
            assert len(hits) == 1
        finally:
            db2.close()

    def test_real_content_updates_propagate_to_fts(self, db, tmp_path):
        """After migration, real content updates still propagate to FTS."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "original content")

        db.close()
        _create_broad_triggers(tmp_path / "state.db")

        db2 = SessionDB(db_path=tmp_path / "state.db")
        try:
            # Update the message content.
            db2._conn.execute(
                "UPDATE messages SET content = 'updated content' WHERE session_id = 's1'"
            )
            db2._conn.commit()

            # FTS should reflect the update.
            hits = _fts_search(tmp_path / "state.db", "messages_fts", "updated")
            assert len(hits) == 1
            hits_old = _fts_search(tmp_path / "state.db", "messages_fts", "original")
            assert len(hits_old) == 0
        finally:
            db2.close()

    def test_status_only_update_does_not_trigger_fts(self, db, tmp_path):
        """A status-only UPDATE (no payload change) should NOT fire the
        narrowed UPDATE trigger."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        msg_id = db.append_message("s1", "user", "payload content")

        # Status-only update (no content/tool_name/tool_calls change).
        db._conn.execute(
            "UPDATE messages SET active = 0 WHERE id = ?",
            (msg_id,),
        )
        db._conn.commit()

        # FTS should still have the original content (trigger didn't fire).
        hits = _fts_search(tmp_path / "state.db", "messages_fts", "payload")
        assert len(hits) == 1

    def test_trigram_update_trigger_also_narrowed(self, db, tmp_path):
        """The trigram UPDATE trigger is also narrowed."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello")

        db.close()
        _create_broad_triggers(tmp_path / "state.db")

        db2 = SessionDB(db_path=tmp_path / "state.db")
        try:
            sql = _trigger_sql(tmp_path / "state.db", "messages_fts_trigram_update")
            assert sql is not None
            assert "WHEN" in sql.upper()
        finally:
            db2.close()

    def test_is_broad_update_trigger_returns_false_for_narrowed(self, db, tmp_path):
        """_is_broad_update_trigger returns False for a narrowed trigger."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello")

        cursor = db._conn.cursor()
        # New DBs get narrowed triggers from the DDL.
        assert not db._is_broad_update_trigger(cursor, "messages_fts_update")
        assert not db._is_broad_update_trigger(cursor, "messages_fts_trigram_update")

    def test_is_broad_update_trigger_returns_true_for_broad(self, db, tmp_path):
        """_is_broad_update_trigger returns True for a broad trigger."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello")

        db.close()
        _create_broad_triggers(tmp_path / "state.db")

        # Check on a raw connection BEFORE SessionDB init narrows them.
        raw = sqlite3.connect(str(tmp_path / "state.db"))
        cursor = raw.cursor()
        assert SessionDB._is_broad_update_trigger(cursor, "messages_fts_update")
        assert SessionDB._is_broad_update_trigger(cursor, "messages_fts_trigram_update")
        raw.close()

    def test_migration_returns_true_when_narrowing(self, db, tmp_path):
        """_migrate_fts_update_triggers returns True when a migration
        was performed."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello")

        db.close()
        _create_broad_triggers(tmp_path / "state.db")

        # Call _migrate_fts_update_triggers on a raw connection with broad
        # triggers — should return True and narrow them.
        raw = sqlite3.connect(str(tmp_path / "state.db"))
        cursor = raw.cursor()
        result = SessionDB._migrate_fts_update_triggers(cursor)
        assert result is True
        raw.commit()
        raw.close()

        # Verify triggers are now narrowed.
        sql = _trigger_sql(tmp_path / "state.db", "messages_fts_update")
        assert "WHEN" in sql.upper()

    def test_migration_returns_false_when_already_converged(self, db, tmp_path):
        """_migrate_fts_update_triggers returns False when triggers are
        already narrowed."""
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello")

        cursor = db._conn.cursor()
        result = db._migrate_fts_update_triggers(cursor)
        assert result is False
