"""Schema v32: ``messages_fts`` as an FTS5 external-content index over a view.

Inline FTS5 keeps a second, byte-for-byte copy of every indexed string in a
``messages_fts_content`` shadow table — 1273.9 MB of a 5120 MB production
state.db, measured 2026-08-11. External content drops that copy and re-reads
the text from ``messages`` on demand.

The subtlety these tests exist for: v11 moved this DB *away* from external
content, but only because the index had to start covering ``tool_name`` +
``tool_calls`` (#16751) while the schema pointed the FTS column straight at
``messages.content``. The ``messages_fts_source`` view supplies the 3-column
concat, so both requirements hold at once — and each test below pins one half
of that, so a future edit cannot quietly trade one for the other.

External content also changes two things that fail *silently* rather than
loudly, which the rest of the file covers:

* ``DELETE``/``UPDATE`` must hand FTS5 the OLD text through the ``'delete'``
  command so it retracts the right terms. Wrong text = a corrupt index that
  still answers queries.
* ``SELECT COUNT(*) FROM messages_fts`` counts the *view* (every message), not
  index entries — so it can no longer be used to assert that indexing happened.
"""
import sqlite3
import uuid
from pathlib import Path

import pytest

from hermes_state import SCHEMA_VERSION, SessionDB


# The trigram index is unrelated to this conversion and is absent in
# production; disabling it keeps these tests focused and fast.
@pytest.fixture(autouse=True)
def _no_trigram(monkeypatch):
    monkeypatch.setenv("HERMES_DISABLE_MESSAGE_TRIGRAM", "1")


def _seed(db_path: Path, count: int = 10) -> str:
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(count):
        db.append_message(sid, role="user", content=f"hello world {i}")
        db.append_message(
            sid,
            role="tool",
            content=f"pizza {i}",
            tool_name="browser_snapshot",
            tool_calls='{"name":"browser_snapshot"}',
        )
    db.close()
    return sid


def _match_count(conn: sqlite3.Connection, query: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", (query,)
    ).fetchone()[0]


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    """FTS5 integrity-check in its strong (rank=1) form.

    The default rank=0 form only validates the index against its own content
    store; rank=1 re-derives the terms from the content table, which is what
    catches an index row whose text no longer matches (or no longer exists in)
    ``messages``. Under external content that is the failure mode that matters,
    so every trigger test below asserts through this.

    Kept as raw SQL rather than routed through ``SessionDB.check_fts_integrity``
    so the tests fail if the *statement* stops detecting corruption, not merely
    if the wrapper around it changes.
    """
    try:
        conn.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False


def _make_inline_v31(db_path: Path, *, indexed_from_id: int = 0) -> str:
    """Build a DB and force it back to the pre-v32 inline FTS shape.

    ``indexed_from_id`` reproduces production's unindexed prefix: rows at or
    below it exist in ``messages`` but were never added to the index, which is
    the state that makes the new delete trigger dangerous (retracting terms
    that were never inserted is how a stale index becomes a corrupt one).
    """
    sid = _seed(db_path)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%'")
    conn.execute("PRAGMA writable_schema=OFF")
    conn.close()

    concat = (
        "COALESCE(content,'')||' '||COALESCE(tool_name,'')"
        "||' '||COALESCE(tool_calls,'')"
    )
    new_concat = concat.replace("(content", "(new.content").replace(
        "(tool_name", "(new.tool_name"
    ).replace("(tool_calls", "(new.tool_calls")
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(
        f"""
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, {new_concat});
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
  DELETE FROM messages_fts WHERE rowid = old.id;
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
  DELETE FROM messages_fts WHERE rowid = old.id;
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, {new_concat});
END;
"""
    )
    conn.execute(
        f"INSERT INTO messages_fts(rowid, content) "
        f"SELECT id, {concat} FROM messages WHERE id > ?",
        (indexed_from_id,),
    )
    conn.execute("UPDATE schema_version SET version = 31")
    conn.commit()
    conn.close()
    return sid


def test_migration_drops_the_duplicate_content_shadow_table(tmp_path):
    """The whole point: no second copy of the message text on disk."""
    db_path = tmp_path / "state.db"
    _make_inline_v31(db_path)

    raw = sqlite3.connect(str(db_path))
    assert raw.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='messages_fts_content'"
    ).fetchone()[0] == 1, "precondition: the inline DB has the content shadow table"
    raw.close()

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 32
        assert SCHEMA_VERSION == 32
        assert db._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='messages_fts_content'"
        ).fetchone()[0] == 0
        decl = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone()[0]
        assert "content='messages_fts_source'" in decl
        assert "content_rowid='id'" in decl
    finally:
        db.close()


def test_migration_still_indexes_tool_name(tmp_path):
    """#16751 must survive the round trip back to external content.

    This is the requirement that caused v11 to abandon external content in the
    first place, so it is the one most likely to regress.
    """
    db_path = tmp_path / "state.db"
    _make_inline_v31(db_path)
    db = SessionDB(db_path=db_path)
    try:
        assert _match_count(db._conn, "browser_snapshot") == 10
    finally:
        db.close()


def test_migration_rebuild_closes_an_unindexed_prefix(tmp_path):
    """'rebuild' reads the view, so rows the inline index never had get indexed.

    Under inline FTS this is impossible: 'rebuild' regenerates the index from
    ``messages_fts_content``, so rows missing from that shadow copy stay
    missing. The two modes differ here and it is easy to conflate them.
    """
    db_path = tmp_path / "state.db"
    _make_inline_v31(db_path, indexed_from_id=5)

    raw = sqlite3.connect(str(db_path))
    assert raw.execute("SELECT COUNT(*) FROM messages_fts_content").fetchone()[0] == 15
    assert raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 20
    raw.close()

    db = SessionDB(db_path=db_path)
    try:
        assert _match_count(db._conn, "hello") == 10
        assert _match_count(db._conn, "pizza") == 10
        assert _integrity_ok(db._conn)
    finally:
        db.close()


def test_delete_trigger_retracts_the_right_terms(tmp_path):
    """Deleting a formerly-unindexed row must not corrupt the index.

    Pre-migration id<=5 was never indexed. If v32 skipped its rebuild, the
    delete trigger would retract terms that were never inserted — integrity
    would fail here while ordinary searches kept looking fine.
    """
    db_path = tmp_path / "state.db"
    _make_inline_v31(db_path, indexed_from_id=5)
    db = SessionDB(db_path=db_path)
    try:
        db._conn.execute("DELETE FROM messages WHERE id IN (1, 9)")  # both 'hello' rows
        db._conn.commit()
        assert _match_count(db._conn, "hello") == 8
        assert _integrity_ok(db._conn)
    finally:
        db.close()


def test_update_trigger_retracts_the_old_text(tmp_path):
    db_path = tmp_path / "state.db"
    _make_inline_v31(db_path)
    db = SessionDB(db_path=db_path)
    try:
        db._conn.execute("UPDATE messages SET content='zebra' WHERE id=11")
        db._conn.commit()
        assert _match_count(db._conn, "zebra") == 1
        assert _match_count(db._conn, "hello") == 9, "old term must be retracted"
        assert _integrity_ok(db._conn)
    finally:
        db.close()


def test_bare_count_star_counts_the_view_not_the_index(tmp_path):
    """Pin the silent trap so nobody reintroduces COUNT(*) as an index assertion.

    If this ever starts equalling the indexed-row count again, the index is no
    longer external-content and the space win is gone.
    """
    db_path = tmp_path / "state.db"
    _make_inline_v31(db_path, indexed_from_id=5)
    db = SessionDB(db_path=db_path)
    try:
        bare = db._conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        messages = db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert bare == messages == 20
    finally:
        db.close()


def test_snippet_still_works_without_a_stored_copy(tmp_path):
    """snippet() re-reads through the view; search results must be unchanged."""
    db_path = tmp_path / "state.db"
    _make_inline_v31(db_path)
    db = SessionDB(db_path=db_path)
    try:
        rows = db.search_messages("pizza", limit=5)
        assert rows
        assert all(">>>pizza<<<" in r["snippet"] for r in rows)
    finally:
        db.close()


def test_search_survives_a_corrupt_index_block(tmp_path):
    """The read-path guard: corrupt index b-tree → rebuild → retry → results.

    Before v32 only the write path auto-rebuilt, so a corrupt index surfaced
    "database disk image is malformed" raw to every search caller.
    """
    db_path = tmp_path / "state.db"
    _seed(db_path)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute(
        "UPDATE messages_fts_data SET block = randomblob(64) "
        "WHERE id = (SELECT MAX(id) FROM messages_fts_data)"
    )
    conn.close()

    db = SessionDB(db_path=db_path)
    try:
        assert db._fts_runtime_rebuild_attempted is False
        rows = db.search_messages("pizza", limit=25)
        assert len(rows) == 10
        assert db._fts_runtime_rebuild_attempted is True, "the guard must have fired"
        assert _integrity_ok(db._conn)
    finally:
        db.close()


def test_search_ignores_orphaned_index_rowids(tmp_path):
    """An orphan degrades to a missing result, not an exception.

    ``search_messages`` INNER JOINs ``messages`` on the index rowid, so an
    index entry with no surviving message row is filtered out before
    ``snippet()`` can be evaluated on it. That bounds the blast radius of the
    one corruption class external content newly makes possible — but it also
    means searches cannot be used to *detect* it, which is why
    ``_integrity_ok`` (rank=1) is the health probe instead.
    """
    db_path = tmp_path / "state.db"
    _seed(db_path)
    db = SessionDB(db_path=db_path)
    try:
        # Manufacture an orphan, restoring the trigger verbatim so the trigger
        # count never drops and _init_schema's repair path stays out of it.
        ddl = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages_fts_delete'"
        ).fetchone()[0]
        db._conn.execute("DROP TRIGGER messages_fts_delete")
        db._conn.execute("DELETE FROM messages WHERE id = 3")
        db._conn.execute(ddl)
        db._conn.commit()

        assert not _integrity_ok(db._conn), "rank=1 must detect the orphan"
        rows = db.search_messages("hello", limit=25)
        assert len(rows) == 9, "orphan is skipped, not raised"

        assert db.rebuild_fts() == 1
        assert _integrity_ok(db._conn), "'rebuild' repairs it"
    finally:
        db.close()


def test_check_fts_integrity_reports_the_orphan(tmp_path):
    """The health probe must report what searches cannot surface."""
    db_path = tmp_path / "state.db"
    _seed(db_path)
    db = SessionDB(db_path=db_path)
    try:
        assert db.check_fts_integrity() == {"messages_fts": None}

        ddl = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages_fts_delete'"
        ).fetchone()[0]
        db._conn.execute("DROP TRIGGER messages_fts_delete")
        db._conn.execute("DELETE FROM messages WHERE id = 3")
        db._conn.execute(ddl)
        db._conn.commit()

        report = db.check_fts_integrity()
        assert set(report) == {"messages_fts"}
        assert report["messages_fts"] is not None
        assert "malformed" in report["messages_fts"]

        db.rebuild_fts()
        assert db.check_fts_integrity() == {"messages_fts": None}
    finally:
        db.close()
