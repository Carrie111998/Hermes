"""C3: optional trigram session-metadata FTS capability (#128 / fork #30).

Same external-content document as the Unicode lane, but the indexed text is
a DERIVED compact projection (``compact(title)``, RAW ``id``,
``compact(display_name)``) read through ``sessions_fts_trigram_src`` and
tokenized with ``trigram`` — so punctuation-compacted infix queries (e.g.
``an94`` finds ``AN-94``) work at index speed. The lane owns an independent
``fts_session_trigram_*`` marker pair; canonical ``sessions`` stays raw.
"""

import sqlite3
import time

import pytest

from hermes_state import SCHEMA_SQL, SessionDB
from hermes_state_common import compact_session_metadata_text

TRIG_HW = "fts_session_trigram_rebuild_high_water"
TRIG_PROG = "fts_session_trigram_rebuild_progress"
UNI_HW = "fts_session_rebuild_high_water"


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def _build_populated_sessions_db(db_path, n=12):
    """Modern sessions (named row_id) with ``n`` rows and no FTS surfaces."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA_SQL)
    t0 = time.time()
    conn.executemany(
        "INSERT INTO sessions (row_id, id, source, started_at, title, display_name) "
        "VALUES (?, ?, 'cli', ?, ?, ?)",
        [
            (i, f"s{i}", t0 + i, f"Title {i}", f"channel #{i}")
            for i in range(1, n + 1)
        ],
    )
    conn.commit()
    conn.close()


def _trig_markers(db):
    return {
        r["key"]: r["value"]
        for r in db._conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE 'fts_session_trigram_%'"
        ).fetchall()
    }


class TestTrigramExternalContentShape:
    def test_ddl_is_external_content_compact_raw(self, db):
        view_sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'sessions_fts_trigram_src'"
        ).fetchone()[0]
        # compact(title) / RAW id / compact(display_name) projection
        assert "REPLACE(COALESCE(title, ''), '-'" in view_sql
        assert "id AS id" in view_sql
        table_sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'sessions_fts_trigram'"
        ).fetchone()[0]
        assert "content='sessions_fts_trigram_src'" in table_sql
        assert "content_rowid='row_id'" in table_sql
        assert "tokenize='trigram'" in table_sql

    def test_compact_policy_deletes_exact_separator_set(self):
        # The canonical separator set is '-', '_', '.', ' ' — never arbitrary
        # punctuation (so '#' survives, matching the stored VIEW).
        assert compact_session_metadata_text("AN-94 #ops") == "AN94#ops"
        assert compact_session_metadata_text("foo_bar.baz qux") == "foobarbazqux"
        assert compact_session_metadata_text(None) == ""

    def test_trigram_search_covers_compact_title_and_display_name(self, db):
        def seed(conn):
            for sid, title, dn in [
                ("s1", "AN-94 Project", None),
                ("s2", "Budget", "#an-94-ops"),
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
                    "SELECT rowid FROM sessions_fts_trigram "
                    "WHERE sessions_fts_trigram MATCH ?",
                    (q,),
                ).fetchall()
            ]

        # compact(title) "AN94" and compact(display_name) "an94ops" both
        # contain the trigram "an9"/"94"; raw id "s2" not matched by "an94".
        assert hits("an94") == [1, 2]

    def test_empty_db_complete_no_markers(self, db):
        assert _trig_markers(db) == {}
        assert db._sessions_trigram_available is True


class TestTrigramRebuildMarkers:
    def test_populated_db_stages_trigram_markers(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_populated_sessions_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            m = _trig_markers(db)
            assert m.get(TRIG_HW) == "12"
            assert m.get(TRIG_PROG) == "0"
            assert db._sessions_trigram_available is False
        finally:
            db.close()

    def test_trigram_markers_independent_of_unicode_lane(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_populated_sessions_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            m = _trig_markers(db)
            uni = {
                r["key"]: r["value"]
                for r in db._conn.execute(
                    "SELECT key, value FROM state_meta WHERE key LIKE 'fts_session_rebuild_%'"
                ).fetchall()
            }
            assert UNI_HW in uni
            assert UNI_HW not in m
        finally:
            db.close()

    def test_rebuild_backfills_then_serves(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_populated_sessions_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            while db.fts_session_trigram_rebuild_step():
                pass
            assert _trig_markers(db) == {}
            assert db._sessions_trigram_available is True
            n = db._conn.execute(
                "SELECT COUNT(*) FROM sessions_fts_trigram"
            ).fetchone()[0]
            assert n == 12
        finally:
            db.close()
