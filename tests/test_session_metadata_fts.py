"""C1: session-metadata Unicode FTS substrate (#128 / fork #25).

Covers the named ``sessions.row_id`` migration (preserving exact legacy
hidden rowids including deleted-row holes), the raw ``(title, id,
display_name)`` Unicode external-content ``sessions_fts``, and the
resumable H/P rebuild lifecycle. CJK (#128 C2), trigram (C3) and candidate
routing (C4) are out of scope here.
"""

import sqlite3
import time

import pytest

from hermes_state import SCHEMA_SQL, SessionDB


@pytest.fixture()
def db(tmp_path):
    """Fresh SessionDB (#128 layout: named sessions.row_id + raw Unicode
    external-content session metadata FTS) over a temp database file."""
    db_path = tmp_path / "state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    session_db.close()


def _col_names(conn, table):
    return {
        r["name"] if isinstance(r, sqlite3.Row) else r[1]
        for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _row_id_map(conn):
    return {
        r["id"] if isinstance(r, sqlite3.Row) else r[0]:
        r["row_id"] if isinstance(r, sqlite3.Row) else r[1]
        for r in conn.execute("SELECT id, row_id FROM sessions").fetchall()
    }


# The pre-#128 sessions shape: ``id TEXT PRIMARY KEY`` with NO named row_id.
# SCHEMA_SQL now declares the new shape, so a real legacy table must be
# demoted explicitly.
_LEGACY_SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    system_prompt_hash TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    last_activity_at REAL,
    last_activity_description TEXT,
    last_activity_provenance TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id),
    FOREIGN KEY (system_prompt_hash) REFERENCES system_prompts(hash)
)
"""


def _build_legacy_sessions_db(db_path):
    """Pre-#128 DB by hand: sessions WITHOUT row_id, explicit hidden rowids
    with deleted-row holes (1=A, 3=B, 7=C), plus a message row keyed by text
    id. Opening the new SessionDB must migrate to named row_id preserving
    every surviving numeric rowid and its relationships."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA_SQL)
    conn.execute("DROP TABLE sessions")
    conn.executescript(_LEGACY_SESSIONS_DDL)
    t0 = time.time()
    for rowid, sid, title in (
        (1, "A", "Alpha Project"),
        (3, "B", "Beta Project"),
        (7, "C", "Gamma Project"),
    ):
        conn.execute(
            "INSERT INTO sessions (rowid, id, source, started_at, title) "
            "VALUES (?, ?, 'cli', ?, ?)",
            (rowid, sid, t0 + rowid, title),
        )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, 'user', 'message-for-A', ?)",
        ("A", t0),
    )
    conn.commit()
    conn.close()


class TestNamedRowIdMigration:
    def test_fresh_install_sessions_has_named_row_id(self, db):
        assert "row_id" in _col_names(db._conn, "sessions")

    def test_legacy_rowid_holes_preserved_exactly(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_legacy_sessions_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            assert _row_id_map(db._conn) == {"A": 1, "B": 3, "C": 7}
        finally:
            db.close()

    def test_relationships_survive_migration(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_legacy_sessions_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            n = db._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 'A'"
            ).fetchone()[0]
            assert n == 1
        finally:
            db.close()

    def test_migration_is_idempotent_on_reopen(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_legacy_sessions_db(db_path)
        db1 = SessionDB(db_path=db_path)
        db1.close()
        db2 = SessionDB(db_path=db_path)
        try:
            assert _row_id_map(db2._conn) == {"A": 1, "B": 3, "C": 7}
        finally:
            db2.close()


class TestUnicodeExternalContent:
    def test_ddl_is_external_content_raw_metadata(self, db):
        sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'sessions_fts'"
        ).fetchone()[0]
        assert "content='sessions'" in sql
        assert "content_rowid='row_id'" in sql
        assert "tokenize='unicode61'" in sql

    def test_raw_unicode_search_covers_title_id_display_name(self, db):
        def seed(conn):
            for sid, title, dn in [
                ("s1", "Arby's Faribault, MN", None),
                ("s2", "Quarterly Budget Review", "Acme Guild / #finance"),
            ]:
                conn.execute(
                    "INSERT INTO sessions (id, source, started_at, title, display_name) "
                    "VALUES (?,?,?,?,?)",
                    (sid, "cli", 1.0, title, dn),
                )

        db._execute_write(seed)

        def hits(q):
            return [
                r[0]
                for r in db._conn.execute(
                    "SELECT rowid FROM sessions_fts WHERE sessions_fts MATCH ?",
                    (q,),
                ).fetchall()
            ]

        assert hits("Faribault") == [1]  # title
        assert hits("finance") == [2]  # display_name
        assert hits("s2") == [2]  # id

    def test_unrelated_metadata_update_does_not_rewrite_fts(self, db):
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO sessions (id, source, started_at, title) "
                "VALUES ('s1','cli',1.0,'Original Title')"
            )
        )
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET message_count = 5 WHERE id = 's1'"
            )
        )
        n = db._conn.execute("SELECT COUNT(*) FROM sessions_fts").fetchone()[0]
        assert n == 1


class TestRebuildMarkers:
    def _open_legacy(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_legacy_sessions_db(db_path)
        return SessionDB(db_path=db_path)

    def _session_markers(self, db):
        return {
            r["key"]: r["value"]
            for r in db._conn.execute(
                "SELECT key, value FROM state_meta WHERE key LIKE 'fts_session_%'"
            ).fetchall()
        }

    def test_populated_legacy_db_stages_rebuild_markers(self, tmp_path):
        db = self._open_legacy(tmp_path)
        try:
            m = self._session_markers(db)
            assert m.get("fts_session_rebuild_high_water") == "7"
            assert m.get("fts_session_rebuild_progress") == "0"
        finally:
            db.close()

    def test_empty_db_has_no_markers(self, db):
        assert self._session_markers(db) == {}

    def test_rebuild_step_backfills_then_clears_markers(self, tmp_path):
        db = self._open_legacy(tmp_path)
        try:
            assert db.fts_session_rebuild_status() is not None
            while db.fts_session_rebuild_step():
                pass
            assert self._session_markers(db) == {}
            n = db._conn.execute("SELECT COUNT(*) FROM sessions_fts").fetchone()[0]
            assert n == 3
        finally:
            db.close()
