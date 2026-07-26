"""#68858/#68891: FTS UPDATE triggers must only fire on content column changes.

Status-only updates (active, compacted, observed) must NOT trigger
FTS delete/reinsert, which saturates disk I/O on large state.db during
in-place compaction.

Migration tests (#68891 review): broad→narrow trigger migration must be
conditional, atomic, idempotent, and must NOT rebuild FTS indexes.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


def _create_db(db_path: Path) -> sqlite3.Connection:
    """Create a fresh state.db with the FTS schema from hermes_state."""
    from hermes_state import FTS_SQL, FTS_TRIGRAM_SQL

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY, content TEXT, tool_name TEXT, "
        "tool_calls TEXT, active INTEGER, compacted INTEGER, "
        "observed INTEGER, api_content TEXT, role TEXT DEFAULT 'user'"
        ")"
    )
    conn.execute(
        "CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.executescript(FTS_SQL)
    conn.executescript(FTS_TRIGRAM_SQL)
    conn.commit()
    return conn

def test_cjk_status_update_does_not_read_indexed_columns():
    """CJK UPDATE OF must bypass its payload/role predicate on status writes."""
    from hermes_state import FTS_CJK_TABLE_SQL, FTS_CJK_TRIGGER_SQL

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY, content TEXT, tool_name TEXT, tool_calls TEXT, "
        "role TEXT, active INTEGER, compacted INTEGER)"
    )
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executescript(
        (FTS_CJK_TABLE_SQL + "\n" + FTS_CJK_TRIGGER_SQL).replace(
            "cjk_unicode61", "unicode61"
        )
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 'payload', 'tool', 'calls', 'user', 1, 0)"
    )
    conn.commit()

    reads: list[tuple[str, str]] = []

    def authorize(action, table, column, _database, _source):
        if action == sqlite3.SQLITE_READ and table == "messages":
            reads.append((table, column))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = 1")
    conn.commit()
    conn.set_authorizer(None)
    assert not ({column for _, column in reads} & {"content", "tool_name", "tool_calls", "role"})
    conn.close()


def test_broad_cjk_update_trigger_migrates_to_role_aware_column_list():
    """A capable current DB must atomically narrow its existing CJK trigger."""
    from hermes_state import FTS_CJK_TABLE_SQL, FTS_CJK_TRIGGER_SQL, SessionDB

    desired = (FTS_CJK_TABLE_SQL + "\n" + FTS_CJK_TRIGGER_SQL).replace(
        "cjk_unicode61", "unicode61"
    )
    target = "AFTER UPDATE OF content, tool_name, tool_calls, role ON messages"
    broad = desired.replace(target, "AFTER UPDATE ON messages")
    assert broad != desired

    with sqlite3.connect(":memory:", isolation_level=None) as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, content TEXT, tool_name TEXT, tool_calls TEXT, "
            "role TEXT, active INTEGER, compacted INTEGER)"
        )
        conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executescript(broad)
        changed = SessionDB._migrate_broad_fts_update_triggers(
            conn.cursor(),
            desired,
        )
        sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='messages_fts_cjk_update'"
        ).fetchone()[0]
    assert changed is True
    assert target in sql


def test_fts_update_trigger_fires_on_content_change(db_path):
    """Updating content must reindex FTS."""
    conn = _create_db(db_path)
    conn.execute(
        "INSERT INTO messages (id, content, tool_name, tool_calls, active) "
        "VALUES (1, 'hello world', '', '', 1)"
    )
    conn.commit()

    rows = conn.execute("SELECT content FROM messages_fts WHERE content MATCH 'hello'").fetchall()
    assert len(rows) == 1

    conn.execute("UPDATE messages SET content = 'goodbye world' WHERE id = 1")
    conn.commit()

    rows = conn.execute("SELECT content FROM messages_fts WHERE content MATCH 'goodbye'").fetchall()
    assert len(rows) == 1
    rows = conn.execute("SELECT content FROM messages_fts WHERE content MATCH 'hello'").fetchall()
    assert len(rows) == 0
    conn.close()


def test_fts_update_trigger_does_not_fire_on_status_only_change(db_path):
    """Updating only active/compacted must NOT trigger FTS reindex (#68858)."""
    conn = _create_db(db_path)
    conn.execute(
        "INSERT INTO messages (id, content, tool_name, tool_calls, active) "
        "VALUES (1, 'keep this text', '', '', 1)"
    )
    conn.commit()

    before = conn.total_changes
    reads: list[str] = []

    def authorize(action, table, column, _database, _source):
        if action == sqlite3.SQLITE_READ and table == "messages":
            reads.append(column)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = 1")
    conn.commit()
    conn.set_authorizer(None)
    assert conn.total_changes - before == 1
    assert not (set(reads) & {"content", "tool_name", "tool_calls", "role"})

    rows = conn.execute("SELECT content FROM messages_fts WHERE content MATCH 'keep'").fetchall()
    assert len(rows) == 1
    conn.close()


def test_trigram_update_trigger_fires_on_content_change(db_path):
    """Updating content must reindex trigram FTS."""
    conn = _create_db(db_path)
    conn.execute(
        "INSERT INTO messages (id, content, tool_name, tool_calls, active) "
        "VALUES (1, '中文测试', '', '', 1)"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT content FROM messages_fts_trigram WHERE content MATCH '中文测'"
    ).fetchall()
    assert len(rows) == 1

    conn.execute("UPDATE messages SET content = '日本語テスト' WHERE id = 1")
    conn.commit()

    rows = conn.execute(
        "SELECT content FROM messages_fts_trigram WHERE content MATCH '日本語'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


def test_trigram_update_trigger_does_not_fire_on_status_only_change(db_path):
    """Updating only active/compacted must NOT trigger trigram FTS reindex."""
    conn = _create_db(db_path)
    conn.execute(
        "INSERT INTO messages (id, content, tool_name, tool_calls, active) "
        "VALUES (1, '中文保持测试', '', '', 1)"
    )
    conn.commit()

    before = conn.total_changes
    conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = 1")
    conn.commit()
    assert conn.total_changes - before == 1

    rows = conn.execute(
        "SELECT content FROM messages_fts_trigram WHERE content MATCH '中文保持'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


def test_trigram_update_trigger_tracks_role_membership_changes(db_path):
    """Tool↔non-tool role changes must remove/reinsert trigram membership."""
    conn = _create_db(db_path)
    conn.execute(
        "INSERT INTO messages (id, role, content, tool_name, tool_calls, active) "
        "VALUES (1, 'user', 'membership needle', '', '', 1)"
    )
    conn.commit()

    def matches() -> list[tuple[int]]:
        return conn.execute(
            "SELECT rowid FROM messages_fts_trigram "
            "WHERE messages_fts_trigram MATCH 'membership'"
        ).fetchall()

    assert matches() == [(1,)]
    conn.execute("UPDATE messages SET role = 'tool' WHERE id = 1")
    conn.commit()
    assert matches() == []

    conn.execute("UPDATE messages SET role = 'assistant' WHERE id = 1")
    conn.commit()
    assert matches() == [(1,)]
    conn.close()


# ── #68891 migration tests ────────────────────────────────────────────

_UPDATE_TRIGGERS = (
    "messages_fts_update",
    "messages_fts_trigram_update",
)
_NARROWED_CLAUSES = {
    "messages_fts_update": (
        "AFTER UPDATE OF content, tool_name, tool_calls ON messages"
    ),
    "messages_fts_trigram_update": (
        "AFTER UPDATE OF content, tool_name, tool_calls, role ON messages"
    ),
}


def _create_current_db_with_broad_update_triggers(
    db_path: Path,
    *,
    misleading_comment: bool = False,
) -> int:
    """Create a real current-schema DB, then simulate a pre-narrowing upgrade."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    db.create_session("migration-test", "test")
    message_id = db.append_message(
        "migration-test",
        "user",
        content="preserved text",
    )
    db.close()

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for name in _UPDATE_TRIGGERS:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()
            assert row and row[0], f"missing current trigger {name}"
            narrowed_clause = _NARROWED_CLAUSES[name]
            broad_sql = row[0].replace(
                narrowed_clause,
                "AFTER UPDATE ON messages",
            )
            if misleading_comment:
                broad_sql = broad_sql.replace(
                    "AFTER UPDATE ON messages",
                    "AFTER UPDATE ON messages\n"
                    f"-- {narrowed_clause}\n",
                )
            assert broad_sql != row[0], f"{name} was not in the current narrowed form"
            conn.execute(f'DROP TRIGGER "{name}"')
            conn.execute(broad_sql)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return message_id


def _trigger_sql(db_path: Path, name: str) -> str:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
    return row[0] if row else ""


def test_real_sessiondb_migration_is_narrow_and_never_rebuilds(db_path):
    """Narrowing an intact broad index must preserve data without a rebuild."""
    from hermes_state import SessionDB

    _create_current_db_with_broad_update_triggers(db_path)

    with patch.object(
        SessionDB,
        "_rebuild_fts_indexes",
        side_effect=AssertionError("trigger narrowing must not rebuild FTS"),
    ):
        migrated = SessionDB(db_path=db_path)

    try:
        for name in _UPDATE_TRIGGERS:
            sql = _trigger_sql(db_path, name)
            assert _NARROWED_CLAUSES[name] in sql
            assert "AFTER UPDATE ON messages" not in sql

        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'preserved'"
            ).fetchall()
        assert len(rows) == 1
    finally:
        migrated.close()


def test_broad_trigger_comment_cannot_spoof_narrowed_detection(db_path):
    """A comment mentioning the narrowed clause must not defeat migration."""
    from hermes_state import SessionDB

    _create_current_db_with_broad_update_triggers(
        db_path,
        misleading_comment=True,
    )
    migrated = SessionDB(db_path=db_path)
    try:
        for name in _UPDATE_TRIGGERS:
            sql = _trigger_sql(db_path, name)
            assert _NARROWED_CLAUSES[name] in sql
            assert "AFTER UPDATE ON messages" not in sql
    finally:
        migrated.close()


def test_previous_trigram_narrowing_is_upgraded_for_role_changes(db_path):
    """The prior three-column trigram form must converge to include role."""
    from hermes_state import SessionDB

    seeded = SessionDB(db_path=db_path)
    seeded.close()
    name = "messages_fts_trigram_update"
    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        old_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()[0]
        old_sql = old_sql.replace(", role ON messages", " ON messages")
        conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(old_sql)

    with patch.object(
        SessionDB,
        "_rebuild_fts_indexes",
        side_effect=AssertionError("column-list convergence must not rebuild FTS"),
    ):
        migrated = SessionDB(db_path=db_path)
    migrated.close()
    assert _NARROWED_CLAUSES[name] in _trigger_sql(db_path, name)


def test_reordered_quoted_narrowed_columns_are_semantically_idempotent(db_path):
    """SQLite UPDATE OF column order/quoting must not trigger convergence."""
    from hermes_state import SessionDB

    seeded = SessionDB(db_path=db_path)
    seeded.close()

    reordered = {
        "messages_fts_update": '"tool_calls", [content], `tool_name`',
        "messages_fts_trigram_update": (
            '[role], "tool_calls", `content`, [tool_name]'
        ),
    }
    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        for name, columns in reordered.items():
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()[0]
            canonical = _NARROWED_CLAUSES[name]
            replacement = f"AFTER UPDATE OF {columns} ON messages"
            assert canonical in sql
            conn.execute(f'DROP TRIGGER "{name}"')
            conn.execute(sql.replace(canonical, replacement))

    before = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    with patch.object(
        SessionDB,
        "_rebuild_fts_indexes",
        side_effect=AssertionError("semantic idempotence must not rebuild FTS"),
    ):
        reopened = SessionDB(db_path=db_path)
    reopened.close()
    after = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    assert after == before


def test_quoted_comma_identifier_cannot_spoof_narrowed_detection(db_path):
    """A comma inside one quoted identifier is not a three-column list."""
    from hermes_state import SessionDB

    seeded = SessionDB(db_path=db_path)
    seeded.create_session("quoted-comma", "test")
    message_id = seeded.append_message(
        "quoted-comma", "user", content="oldneedle"
    )
    seeded.close()

    name = "messages_fts_update"
    canonical = _NARROWED_CLAUSES[name]
    malformed = 'AFTER UPDATE OF "content, tool_name", tool_calls ON messages'
    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()[0]
        conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(sql.replace(canonical, malformed))

    migrated = SessionDB(db_path=db_path)
    try:
        assert canonical in _trigger_sql(db_path, name)
        with migrated._lock:
            migrated._conn.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                ("newneedle", message_id),
            )
            migrated._conn.commit()
            old_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts "
                "WHERE messages_fts MATCH 'oldneedle'"
            ).fetchall()
            new_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts "
                "WHERE messages_fts MATCH 'newneedle'"
            ).fetchall()
        assert old_hits == []
        assert [row[0] for row in new_hits] == [message_id]
    finally:
        migrated.close()


def test_trigger_body_string_cannot_spoof_narrowed_header(db_path):
    """Only the CREATE TRIGGER header may establish UPDATE OF columns."""
    from hermes_state import SessionDB

    seeded = SessionDB(db_path=db_path)
    seeded.close()

    name = "messages_fts_update"
    canonical = _NARROWED_CLAUSES[name]
    spoof = "AFTER UPDATE OF content, tool_name, tool_calls ON messages"
    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()[0]
        broad = sql.replace(canonical, "AFTER UPDATE ON messages")
        broad = broad.replace("BEGIN", f"BEGIN\n    SELECT '{spoof}';", 1)
        conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(broad)

    migrated = SessionDB(db_path=db_path)
    migrated.close()
    trigger_sql = _trigger_sql(db_path, name)
    assert canonical in trigger_sql
    assert "AFTER UPDATE ON messages" not in trigger_sql


def test_legacy_inline_migration_is_narrow_idempotent_and_no_rebuild(db_path):
    """Legacy inline FTS uses its own three-column target and stays idempotent."""
    from hermes_state import (
        LEGACY_FTS_SQL,
        LEGACY_FTS_TRIGRAM_SQL,
        SessionDB,
    )

    seeded = SessionDB(db_path=db_path)
    seeded.create_session("legacy-migration", "test")
    seeded.append_message("legacy-migration", "user", content="legacy preserved")
    seeded.close()

    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        for name in (
            "messages_fts_insert",
            "messages_fts_delete",
            "messages_fts_update",
            "messages_fts_trigram_insert",
            "messages_fts_trigram_delete",
            "messages_fts_trigram_update",
        ):
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        conn.execute("DROP TABLE messages_fts_trigram")
        conn.execute("DROP VIEW messages_fts_trigram_src")
        conn.execute("DROP TABLE messages_fts")
        conn.executescript(LEGACY_FTS_SQL + "\n" + LEGACY_FTS_TRIGRAM_SQL)
        for table in ("messages_fts", "messages_fts_trigram"):
            conn.execute(
                f"INSERT INTO {table}(rowid, content) "
                "SELECT id, COALESCE(content, '') || ' ' || "
                "COALESCE(tool_name, '') || ' ' || COALESCE(tool_calls, '') "
                "FROM messages"
            )
        for name in _UPDATE_TRIGGERS:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()[0]
            legacy_clause = "AFTER UPDATE OF content, tool_name, tool_calls ON messages"
            conn.execute(f'DROP TRIGGER "{name}"')
            conn.execute(sql.replace(legacy_clause, "AFTER UPDATE ON messages"))

    with patch.object(
        SessionDB,
        "_rebuild_legacy_fts_indexes",
        side_effect=AssertionError("legacy trigger narrowing must not rebuild FTS"),
    ):
        migrated = SessionDB(db_path=db_path)
    migrated.close()

    legacy_clause = "AFTER UPDATE OF content, tool_name, tool_calls ON messages"
    before = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    assert all(legacy_clause in sql for sql in before.values())
    assert ", role ON messages" not in before["messages_fts_trigram_update"]
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'legacy'"
        ).fetchall()
        before_changes = conn.total_changes
        conn.execute(
            "UPDATE messages SET active = 0 WHERE session_id = ?",
            ("legacy-migration",),
        )
        conn.commit()
        assert conn.total_changes - before_changes == 1

    with patch.object(
        SessionDB,
        "_rebuild_legacy_fts_indexes",
        side_effect=AssertionError("idempotent legacy reopen must not rebuild FTS"),
    ):
        reopened = SessionDB(db_path=db_path)
    reopened.close()
    after = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    assert after == before


@pytest.mark.parametrize("content_source", ["[messages]", "`messages`"])
def test_legacy_external_content_migration_preserves_special_delete_semantics(
    db_path, content_source
):
    """Pre-v11 external FTS must never receive inline DELETE/concat triggers."""
    from hermes_state import SessionDB

    seeded = SessionDB(db_path=db_path)
    seeded.create_session("legacy-external", "test")
    seeded.close()

    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        for name in (
            "messages_fts_insert",
            "messages_fts_delete",
            "messages_fts_update",
            "messages_fts_trigram_insert",
            "messages_fts_trigram_delete",
            "messages_fts_trigram_update",
        ):
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        conn.execute("DROP TABLE messages_fts_trigram")
        conn.execute("DROP VIEW messages_fts_trigram_src")
        conn.execute("DROP TABLE messages_fts")
        conn.executescript(
            f"""
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content, content={content_source}, content_rowid='id'
);
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content, content={content_source}, content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
END;
"""
        )
        conn.execute(
            "INSERT INTO messages "
            "(session_id, timestamp, role, content, tool_name, tool_calls) "
            "VALUES (?, 1.0, 'assistant', ?, ?, '')",
            ("legacy-external", "old needle", "metadata_only"),
        )

    migrated = SessionDB(db_path=db_path)
    try:
        with migrated._lock:
            migrated._conn.execute(
                "UPDATE messages SET content = ?, tool_name = ? "
                "WHERE session_id = ?",
                ("new needle", "new_metadata", "legacy-external"),
            )
            migrated._conn.commit()
            old_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'old'"
            ).fetchall()
            new_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'new'"
            ).fetchall()
            metadata_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts "
                "WHERE messages_fts MATCH 'new_metadata'"
            ).fetchall()
            trigram_old_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'old'"
            ).fetchall()
            trigram_new_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'new'"
            ).fetchall()
            trigram_metadata_hits = migrated._conn.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'new_metadata'"
            ).fetchall()
            # FTS5's integrity-check validates the inverted index against the
            # external content source. The old inline trigger bodies left stale
            # terms and raised "database disk image is malformed" here.
            migrated._conn.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')"
            )
            migrated._conn.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram) "
                "VALUES('integrity-check')"
            )
            table_sql = migrated._conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='messages_fts'"
            ).fetchone()[0]
        assert old_hits == []
        assert len(new_hits) == 1
        assert metadata_hits == []
        assert trigram_old_hits == []
        assert len(trigram_new_hits) == 1
        assert trigram_metadata_hits == []
        assert "content=" in table_sql
    finally:
        migrated.close()


def test_legacy_layout_ignores_option_text_in_comments():
    """An inline FTS declaration cannot be spoofed as external by a comment."""
    from hermes_state import SessionDB

    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5(\n"
            "content -- content='messages'\n"
            ")"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(\n"
            "content, tokenize='trigram' -- content='messages'\n"
            ")"
        )
        assert SessionDB._legacy_fts_layout(conn.cursor()) == "inline"


def test_mixed_legacy_layout_is_ambiguous_and_not_rewritten():
    """Standard/trigram storage disagreement must fail closed."""
    from hermes_state import SessionDB

    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5("
            "content, content=[messages], content_rowid=[id], tokenize='trigram')"
        )
        assert SessionDB._legacy_fts_layout(conn.cursor()) == "ambiguous"


@pytest.mark.parametrize(
    "trigram_options",
    [
        "content='messages_fts_trigram_src', content_rowid='id'",
        "content='messages_fts_trigram_src', content_rowid='id', "
        "tokenize='unicode61'",
        "content='messages_fts_trigram_src', content_rowid='id', "
        "tokenize='trigram', prefix='2'",
    ],
)
def test_current_layout_requires_exact_trigram_options(trigram_options):
    """Current shape without exact trigram semantics must fail closed."""
    from hermes_state import SessionDB

    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE messages("
            "id INTEGER PRIMARY KEY, role TEXT, content TEXT, "
            "tool_name TEXT, tool_calls TEXT)"
        )
        conn.execute(
            "CREATE VIEW messages_fts_trigram_src AS "
            "SELECT id, role, content, tool_name, tool_calls FROM messages "
            "WHERE role <> 'tool'"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "content, tool_name, tool_calls, "
            "content='messages', content_rowid='id')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5("
            f"content, tool_name, tool_calls, {trigram_options})"
        )
        assert SessionDB._legacy_fts_layout(conn.cursor()) == "ambiguous"


def test_current_layout_rejects_unfiltered_trigram_source_view():
    """A current-shaped table cannot make an unfiltered view trustworthy."""
    from hermes_state import SessionDB

    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE messages("
            "id INTEGER PRIMARY KEY, role TEXT, content TEXT, "
            "tool_name TEXT, tool_calls TEXT)"
        )
        conn.execute(
            "CREATE VIEW messages_fts_trigram_src AS "
            "SELECT id, role, content, tool_name, tool_calls FROM messages"
        )
        conn.execute(
            "INSERT INTO messages VALUES "
            "(1, 'tool', 'hidden needle', '', '')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "content, tool_name, tool_calls, "
            "content='messages', content_rowid='id')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5("
            "content, tool_name, tool_calls, "
            "content='messages_fts_trigram_src', content_rowid='id', "
            "tokenize='trigram')"
        )
        conn.execute(
            "INSERT INTO messages_fts_trigram(messages_fts_trigram) "
            "VALUES('rebuild')"
        )
        assert conn.execute(
            "SELECT rowid FROM messages_fts_trigram "
            "WHERE messages_fts_trigram MATCH 'hidden'"
        ).fetchall() == [(1,)]
        assert SessionDB._legacy_fts_layout(conn.cursor()) == "ambiguous"


def test_missing_trigram_table_rejects_malformed_existing_source_view():
    """IF NOT EXISTS must not build a new index over an unsafe old view."""
    from hermes_state import SessionDB

    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE messages("
            "id INTEGER PRIMARY KEY, role TEXT, content TEXT, "
            "tool_name TEXT, tool_calls TEXT)"
        )
        conn.execute(
            "CREATE VIEW messages_fts_trigram_src AS "
            "SELECT id, role, content, tool_name, tool_calls FROM messages"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "content, tool_name, tool_calls, "
            "content='messages', content_rowid='id')"
        )
        assert SessionDB._legacy_fts_layout(conn.cursor()) == "ambiguous"


def test_non_fts_tables_cannot_spoof_current_storage_layout():
    """Ordinary catalog tables cannot impersonate validated FTS5 storage."""
    from hermes_state import SessionDB

    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, "
            "tool_name TEXT, tool_calls TEXT)"
        )
        conn.execute(
            "CREATE VIEW messages_fts_trigram_src AS "
            "SELECT id, role, content, tool_name, tool_calls FROM messages "
            "WHERE role <> 'tool'"
        )
        conn.execute(
            "CREATE TABLE messages_fts("
            "content TEXT, tool_name TEXT, tool_calls TEXT, "
            "CHECK(content='messages' AND \"content_rowid\"='id'))"
        )
        conn.execute(
            "CREATE TABLE messages_fts_trigram("
            "content TEXT, tool_name TEXT, tool_calls TEXT, "
            "CHECK(content='messages_fts_trigram_src' AND "
            "\"content_rowid\"='id' AND \"tokenize\"='trigram'))"
        )
        assert SessionDB._legacy_fts_layout(conn.cursor()) == "ambiguous"


def test_missing_current_trigram_view_is_repaired_without_rebuild(db_path, monkeypatch):
    """A missing canonical view is repairable without discarding the index."""
    from hermes_state import SessionDB

    seeded = SessionDB(db_path=db_path)
    seeded.create_session("missing-view", "test")
    message_id = seeded.append_message(
        "missing-view", "user", content="stable searchable payload"
    )
    seeded.close()

    with sqlite3.connect(str(db_path)) as conn:
        before = conn.execute(
            "SELECT rowid FROM messages_fts_trigram "
            "WHERE messages_fts_trigram MATCH 'searchable'"
        ).fetchall()
        assert before == [(message_id,)]
        conn.execute("DROP VIEW messages_fts_trigram_src")
        assert SessionDB._legacy_fts_layout(conn.cursor()) is None

    monkeypatch.setattr(
        SessionDB,
        "_rebuild_fts_indexes",
        staticmethod(lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("missing-view repair must not rebuild")
        )),
    )
    repaired = SessionDB(db_path=db_path)
    try:
        with repaired._lock:
            view = repaired._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='view' "
                "AND name='messages_fts_trigram_src'"
            ).fetchone()
            assert view is not None
            preserved = repaired._conn.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'searchable'"
            ).fetchall()
            assert [row[0] for row in preserved] == [message_id]
            repaired._conn.execute(
                "UPDATE messages SET role='tool' WHERE id=?", (message_id,)
            )
            repaired._conn.commit()
            excluded = repaired._conn.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'searchable'"
            ).fetchall()
            assert excluded == []
    finally:
        repaired.close()


def test_initializer_layout_and_optional_trigram_decisions_share_write_lock(
    db_path, monkeypatch
):
    """A v23 converter cannot overtake stale legacy repair decisions."""
    from hermes_state import (
        FTS_SQL,
        FTS_TRIGRAM_SQL,
        LEGACY_FTS_SQL,
        LEGACY_FTS_TRIGRAM_SQL,
        SessionDB,
    )

    seeded = SessionDB(db_path=db_path)
    seeded.create_session("optional-race", "test")
    message_id = seeded.append_message(
        "optional-race", "user", content="old payload", tool_name="oldmetadata"
    )
    seeded.close()

    drop_current = """
DROP TRIGGER IF EXISTS messages_fts_insert;
DROP TRIGGER IF EXISTS messages_fts_delete;
DROP TRIGGER IF EXISTS messages_fts_update;
DROP TRIGGER IF EXISTS messages_fts_trigram_insert;
DROP TRIGGER IF EXISTS messages_fts_trigram_delete;
DROP TRIGGER IF EXISTS messages_fts_trigram_update;
DROP TABLE IF EXISTS messages_fts_trigram;
DROP VIEW IF EXISTS messages_fts_trigram_src;
DROP TABLE IF EXISTS messages_fts;
"""
    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        conn.executescript(
            drop_current + LEGACY_FTS_SQL + "\n" + LEGACY_FTS_TRIGRAM_SQL
        )
        for table in ("messages_fts", "messages_fts_trigram"):
            conn.execute(
                f"INSERT INTO {table}(rowid, content) "
                "SELECT id, COALESCE(content, '') || ' ' || "
                "COALESCE(tool_name, '') || ' ' || COALESCE(tool_calls, '') "
                "FROM messages"
            )
        for trigger in (
            "messages_fts_insert", "messages_fts_delete", "messages_fts_update",
            "messages_fts_trigram_insert", "messages_fts_trigram_delete",
            "messages_fts_trigram_update",
        ):
            conn.execute(f'DROP TRIGGER "{trigger}"')

    selected_legacy = threading.Event()
    release_initializer = threading.Event()
    converter_started = threading.Event()
    converter_done = threading.Event()
    result: dict[str, object] = {}
    original_layout = SessionDB._legacy_fts_layout
    original_ensure = SessionDB._ensure_fts_schema

    def paused_layout(cursor):
        layout = original_layout(cursor)
        connection = getattr(cursor, "connection", cursor)
        if (
            threading.current_thread().name == "locked-legacy-initializer"
            and layout == "inline"
            and connection.in_transaction
            and not selected_legacy.is_set()
        ):
            selected_legacy.set()
            assert release_initializer.wait(5), "test did not release initializer"
        return layout

    def optional_trigram_unavailable(self, cursor, table_name, ddl):
        if (
            threading.current_thread().name == "locked-legacy-initializer"
            and table_name == "messages_fts_trigram"
        ):
            return False
        return original_ensure(self, cursor, table_name, ddl)

    monkeypatch.setattr(SessionDB, "_legacy_fts_layout", staticmethod(paused_layout))
    monkeypatch.setattr(SessionDB, "_ensure_fts_schema", optional_trigram_unavailable)

    def initialize() -> None:
        try:
            result["db"] = SessionDB(db_path=db_path)
        except BaseException as exc:
            result["initializer_error"] = exc

    def convert() -> None:
        converter_started.set()
        try:
            with sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None) as conn:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n" + drop_current + FTS_SQL + "\n"
                    + FTS_TRIGRAM_SQL
                    + "\nINSERT INTO messages_fts(messages_fts) VALUES('rebuild');\n"
                    + "INSERT INTO messages_fts_trigram(messages_fts_trigram) "
                    + "VALUES('rebuild');\nCOMMIT;"
                )
        except BaseException as exc:
            result["converter_error"] = exc
        finally:
            converter_done.set()

    initializer = threading.Thread(
        target=initialize, name="locked-legacy-initializer", daemon=True
    )
    initializer.start()
    assert selected_legacy.wait(5), (
        "initializer never classified legacy layout: "
        f"{result.get('initializer_error')!r}"
    )

    converter = threading.Thread(target=convert, daemon=True)
    converter.start()
    assert converter_started.wait(2)
    assert not converter_done.wait(0.25), "converter overtook locked FTS repair"

    release_initializer.set()
    initializer.join(10)
    converter.join(10)
    assert not initializer.is_alive()
    assert not converter.is_alive()
    assert "initializer_error" not in result, result.get("initializer_error")
    assert "converter_error" not in result, result.get("converter_error")

    database = result.get("db")
    if isinstance(database, SessionDB):
        database.close()
    with sqlite3.connect(str(db_path)) as conn:
        assert original_layout(conn.cursor()) is None
        conn.execute(
            "UPDATE messages SET tool_name='newmetadata' WHERE id=?", (message_id,)
        )
        conn.commit()
        for table in ("messages_fts", "messages_fts_trigram"):
            assert conn.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH 'oldmetadata'"
            ).fetchall() == []
            assert conn.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH 'newmetadata'"
            ).fetchall() == [(message_id,)]


def test_capable_cjk_open_marks_missing_trigger_gap_stale(monkeypatch):
    """A missing CJK trigger means an unknown index gap, never safe service."""
    import hermes_state
    from hermes_state import FTS_CJK_STALE_KEY, SessionDB

    table_sql = hermes_state.FTS_CJK_TABLE_SQL.replace(
        "cjk_unicode61", "unicode61"
    )
    trigger_sql = hermes_state.FTS_CJK_TRIGGER_SQL
    with sqlite3.connect(":memory:", isolation_level=None) as conn:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, content TEXT, tool_name TEXT, tool_calls TEXT, "
            "role TEXT, active INTEGER, compacted INTEGER)"
        )
        conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executescript(table_sql + "\n" + trigger_sql)
        conn.execute(
            "INSERT INTO messages VALUES "
            "(1, 'old needle', '', '', 'user', 1, 0)"
        )
        conn.execute("DROP TRIGGER messages_fts_cjk_update")
        conn.execute("UPDATE messages SET content='new needle' WHERE id=1")

        db = SessionDB.__new__(SessionDB)
        db._fts_cjk_loaded = True
        db._fts_cjk_available = False
        monkeypatch.setattr(hermes_state, "FTS_CJK_TABLE_SQL", table_sql)
        db._ensure_fts_cjk_schema(conn.cursor())

        stale = conn.execute(
            "SELECT value FROM state_meta WHERE key=?", (FTS_CJK_STALE_KEY,)
        ).fetchone()
        live = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'messages_fts_cjk_%'"
        ).fetchall()
        assert stale == ("1",)
        assert live == []
        assert db._fts_cjk_available is False


def test_recreation_failure_rolls_back_original_broad_triggers(db_path):
    """A failed replacement must leave both committed broad triggers intact."""
    from hermes_state import SessionDB

    _create_current_db_with_broad_update_triggers(db_path)
    before = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    failing_ddl = """
CREATE TRIGGER IF NOT EXISTS messages_fts_update
AFTER UPDATE OF content, tool_name, tool_calls ON messages
BEGIN
    SELECT 1;
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update
AFTER UPDATE OF content, tool_name, tool_calls, role ON messages
BEGIN
    SELECT FROM;
END;
"""

    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        with pytest.raises(sqlite3.OperationalError):
            SessionDB._migrate_broad_fts_update_triggers(
                conn.cursor(),
                failing_ddl,
            )
        assert not conn.in_transaction

    after = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    assert after == before


def test_two_initializers_reclassify_after_lock(db_path, monkeypatch):
    """Two broad preflights must produce only one locked trigger migration."""
    from hermes_state import SessionDB

    _create_current_db_with_broad_update_triggers(db_path)
    real_connect = sqlite3.connect
    preflight_barrier = threading.Barrier(2)
    traced: list[str] = []
    databases: list[SessionDB] = []
    errors: list[BaseException] = []
    trace_lock = threading.Lock()

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if threading.current_thread().name.startswith("fts-initializer-"):
            saw_preflight = False

            def trace(statement: str) -> None:
                nonlocal saw_preflight
                with trace_lock:
                    traced.append(statement)
                if "WHERE type = 'trigger' AND name IN" in statement:
                    saw_preflight = True
                elif saw_preflight and statement.strip().upper() == "BEGIN IMMEDIATE":
                    preflight_barrier.wait(5)
                    saw_preflight = False

            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    def initialize() -> None:
        try:
            databases.append(SessionDB(db_path=db_path))
        except BaseException as exc:
            errors.append(exc)

    initializers = [
        threading.Thread(
            target=initialize,
            name=f"fts-initializer-{index}",
            daemon=True,
        )
        for index in range(2)
    ]
    for initializer in initializers:
        initializer.start()
    for initializer in initializers:
        initializer.join(10)

    try:
        assert not any(initializer.is_alive() for initializer in initializers)
        assert errors == []
        drops = [
            statement
            for statement in traced
            if statement.lstrip().upper().startswith("DROP TRIGGER")
        ]
        assert len(drops) == 2
        assert {name for name in _UPDATE_TRIGGERS if any(name in sql for sql in drops)} == set(
            _UPDATE_TRIGGERS
        )
    finally:
        for database in databases:
            database.close()


@pytest.mark.parametrize("legacy_layout", ["inline", "external"])
def test_layout_is_reclassified_under_migration_lock(
    db_path, monkeypatch, legacy_layout
):
    """A concurrent v23 conversion cannot receive stale legacy trigger DDL."""
    from hermes_state import (
        FTS_SQL,
        FTS_TRIGRAM_SQL,
        LEGACY_EXTERNAL_FTS_SQL,
        LEGACY_EXTERNAL_FTS_TRIGRAM_SQL,
        LEGACY_FTS_SQL,
        LEGACY_FTS_TRIGRAM_SQL,
        SessionDB,
    )

    seeded = SessionDB(db_path=db_path)
    seeded.create_session("layout-race", "test")
    message_id = seeded.append_message(
        "layout-race", "user", content="stable content", tool_name="oldmetadata"
    )
    seeded.close()

    if legacy_layout == "external":
        legacy_ddl = LEGACY_EXTERNAL_FTS_SQL + "\n" + LEGACY_EXTERNAL_FTS_TRIGRAM_SQL
        legacy_ddl = legacy_ddl.replace(
            "AFTER UPDATE OF content ON messages", "AFTER UPDATE ON messages"
        )
        legacy_backfill = """
INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild');
"""
    else:
        legacy_ddl = LEGACY_FTS_SQL + "\n" + LEGACY_FTS_TRIGRAM_SQL
        legacy_ddl = legacy_ddl.replace(
            "AFTER UPDATE OF content, tool_name, tool_calls ON messages",
            "AFTER UPDATE ON messages",
        )
        legacy_backfill = """
INSERT INTO messages_fts(rowid, content)
SELECT id, COALESCE(content, '') || ' ' || COALESCE(tool_name, '') || ' ' ||
       COALESCE(tool_calls, '') FROM messages;
INSERT INTO messages_fts_trigram(rowid, content)
SELECT id, COALESCE(content, '') || ' ' || COALESCE(tool_name, '') || ' ' ||
       COALESCE(tool_calls, '') FROM messages;
"""

    drop_current = """
DROP TRIGGER IF EXISTS messages_fts_insert;
DROP TRIGGER IF EXISTS messages_fts_delete;
DROP TRIGGER IF EXISTS messages_fts_update;
DROP TRIGGER IF EXISTS messages_fts_trigram_insert;
DROP TRIGGER IF EXISTS messages_fts_trigram_delete;
DROP TRIGGER IF EXISTS messages_fts_trigram_update;
DROP TABLE IF EXISTS messages_fts_trigram;
DROP VIEW IF EXISTS messages_fts_trigram_src;
DROP TABLE IF EXISTS messages_fts;
"""
    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        conn.executescript(drop_current + legacy_ddl + legacy_backfill)

    migration_paused = threading.Event()
    release_initializer = threading.Event()
    result: dict[str, object] = {}
    original_migrate = SessionDB._migrate_broad_fts_update_triggers

    def pause_after_layout_preflight(cursor, ddl, **kwargs):
        if (
            threading.current_thread().name == "stale-layout-initializer"
            and not migration_paused.is_set()
        ):
            migration_paused.set()
            assert release_initializer.wait(5), "test did not complete v23 conversion"
        return original_migrate(cursor, ddl, **kwargs)

    monkeypatch.setattr(
        SessionDB,
        "_migrate_broad_fts_update_triggers",
        staticmethod(pause_after_layout_preflight),
    )

    def initialize() -> None:
        try:
            result["db"] = SessionDB(db_path=db_path)
        except BaseException as exc:
            result["error"] = exc

    initializer = threading.Thread(
        target=initialize, name="stale-layout-initializer", daemon=True
    )
    initializer.start()
    assert migration_paused.wait(5), "initializer never selected legacy DDL"

    current_ddl = FTS_SQL + "\n" + FTS_TRIGRAM_SQL
    converter_started = threading.Event()
    converter_done = threading.Event()

    def convert_to_current() -> None:
        converter_started.set()
        try:
            with sqlite3.connect(
                str(db_path), timeout=5.0, isolation_level=None
            ) as converter:
                converter.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + drop_current
                    + current_ddl
                    + "\nINSERT INTO messages_fts(messages_fts) VALUES('rebuild');\n"
                    + "INSERT INTO messages_fts_trigram(messages_fts_trigram) "
                    + "VALUES('rebuild');\nCOMMIT;"
                )
        except BaseException as exc:
            result["converter_error"] = exc
        finally:
            converter_done.set()

    converter = threading.Thread(target=convert_to_current, daemon=True)
    converter.start()
    assert converter_started.wait(2)
    assert not converter_done.wait(0.25), "converter bypassed initializer lock"

    release_initializer.set()
    initializer.join(10)
    converter.join(10)
    assert not initializer.is_alive()
    assert not converter.is_alive()
    assert "error" not in result, result.get("error")
    assert "converter_error" not in result, result.get("converter_error")

    db = result["db"]
    assert isinstance(db, SessionDB)
    try:
        standard_sql = _trigger_sql(db_path, "messages_fts_update")
        trigram_sql = _trigger_sql(db_path, "messages_fts_trigram_update")
        assert _NARROWED_CLAUSES["messages_fts_update"] in standard_sql
        assert _NARROWED_CLAUSES["messages_fts_trigram_update"] in trigram_sql

        with db._lock:
            db._conn.execute(
                "UPDATE messages SET tool_name=? WHERE id=?",
                ("newmetadata", message_id),
            )
            db._conn.commit()
            for table in ("messages_fts", "messages_fts_trigram"):
                old_hits = db._conn.execute(
                    f"SELECT rowid FROM {table} WHERE {table} MATCH 'oldmetadata'"
                ).fetchall()
                new_hits = db._conn.execute(
                    f"SELECT rowid FROM {table} WHERE {table} MATCH 'newmetadata'"
                ).fetchall()
                assert old_hits == []
                assert [row[0] for row in new_hits] == [message_id]

            db._conn.execute(
                "UPDATE messages SET role='tool' WHERE id=?", (message_id,)
            )
            db._conn.commit()
            excluded = db._conn.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'newmetadata'"
            ).fetchall()
            assert excluded == []

            db._conn.execute(
                "UPDATE messages SET role='user' WHERE id=?", (message_id,)
            )
            db._conn.commit()
            restored = db._conn.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'newmetadata'"
            ).fetchall()
            assert [row[0] for row in restored] == [message_id]
    finally:
        db.close()


def test_real_optimizer_demotion_keeps_writer_out_until_current_schema(
    db_path, monkeypatch
):
    """Real legacy demotion cannot expose a committed triggerless interval."""
    from hermes_state import LEGACY_FTS_SQL, LEGACY_FTS_TRIGRAM_SQL, SessionDB

    seeded = SessionDB(db_path=db_path)
    seeded.create_session("optimizer-race", "test")
    message_id = seeded.append_message(
        "optimizer-race", "user", content="old content", tool_name="oldmetadata"
    )
    seeded.close()

    drop_current = """
DROP TRIGGER IF EXISTS messages_fts_insert;
DROP TRIGGER IF EXISTS messages_fts_delete;
DROP TRIGGER IF EXISTS messages_fts_update;
DROP TRIGGER IF EXISTS messages_fts_trigram_insert;
DROP TRIGGER IF EXISTS messages_fts_trigram_delete;
DROP TRIGGER IF EXISTS messages_fts_trigram_update;
DROP TABLE IF EXISTS messages_fts_trigram;
DROP VIEW IF EXISTS messages_fts_trigram_src;
DROP TABLE IF EXISTS messages_fts;
"""
    with sqlite3.connect(str(db_path), isolation_level=None) as conn:
        conn.executescript(drop_current + LEGACY_FTS_SQL + "\n" + LEGACY_FTS_TRIGRAM_SQL)
        for table in ("messages_fts", "messages_fts_trigram"):
            conn.execute(
                f"INSERT INTO {table}(rowid, content) "
                "SELECT id, COALESCE(content, '') || ' ' || "
                "COALESCE(tool_name, '') || ' ' || COALESCE(tool_calls, '') "
                "FROM messages"
            )

    optimizer = SessionDB(db_path=db_path)
    reached_current_create = threading.Event()
    release_optimizer = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    result: dict[str, object] = {}
    original_execute_ddl = SessionDB._execute_ddl_statements

    def pause_current_creation(cursor, ddl):
        if (
            threading.current_thread().name == "real-fts-optimizer"
            and not reached_current_create.is_set()
        ):
            count = cursor.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'messages_fts%_update'"
            ).fetchone()[0]
            assert count == 0
            assert cursor.in_transaction
            reached_current_create.set()
            assert release_optimizer.wait(5), "test did not release optimizer"
        return original_execute_ddl(cursor, ddl)

    monkeypatch.setattr(
        SessionDB,
        "_execute_ddl_statements",
        staticmethod(pause_current_creation),
    )

    def optimize() -> None:
        try:
            result["high_water"] = optimizer._demote_legacy_fts_to_trash()
        except BaseException as exc:
            result["optimizer_error"] = exc

    def write() -> None:
        try:
            with sqlite3.connect(str(db_path), timeout=5.0) as writer:
                writer_started.set()
                writer.execute(
                    "UPDATE messages SET content=?, tool_name=? WHERE id=?",
                    ("new content", "newmetadata", message_id),
                )
                writer.commit()
        except BaseException as exc:
            result["writer_error"] = exc
        finally:
            writer_done.set()

    optimizer_thread = threading.Thread(
        target=optimize, name="real-fts-optimizer", daemon=True
    )
    optimizer_thread.start()
    assert reached_current_create.wait(5), (
        "optimizer never reached current DDL: "
        f"{result.get('optimizer_error')!r}"
    )

    writer_thread = threading.Thread(target=write, daemon=True)
    writer_thread.start()
    assert writer_started.wait(2)
    assert not writer_done.wait(0.25), "writer committed during triggerless demotion"

    release_optimizer.set()
    optimizer_thread.join(10)
    writer_thread.join(10)
    assert not optimizer_thread.is_alive()
    assert not writer_thread.is_alive()
    assert "optimizer_error" not in result, result.get("optimizer_error")
    assert "writer_error" not in result, result.get("writer_error")

    try:
        while optimizer.fts_rebuild_step():
            pass
        with optimizer._lock:
            layout = optimizer._legacy_fts_layout(optimizer._conn)
            old_hits = optimizer._conn.execute(
                "SELECT rowid FROM messages_fts "
                "WHERE messages_fts MATCH 'oldmetadata'"
            ).fetchall()
            new_hits = optimizer._conn.execute(
                "SELECT rowid FROM messages_fts "
                "WHERE messages_fts MATCH 'newmetadata'"
            ).fetchall()
        assert layout is None
        assert old_hits == []
        assert [row[0] for row in new_hits] == [message_id]
    finally:
        optimizer.close()


def test_writer_blocks_inside_drop_recreate_transaction(db_path, monkeypatch):
    """A writer cannot commit while UPDATE triggers are absent in migration."""
    from hermes_state import SessionDB

    message_id = _create_current_db_with_broad_update_triggers(db_path)
    real_connect = sqlite3.connect
    migration_paused = threading.Event()
    allow_migration = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    result: dict[str, object] = {}

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if threading.current_thread().name == "fts-migrator":

            def trace(statement: str) -> None:
                if (
                    "CREATE TRIGGER IF NOT EXISTS messages_fts_update" in statement
                    and not migration_paused.is_set()
                ):
                    # The broad UPDATE triggers have been dropped, but the first
                    # replacement has not executed. BEGIN IMMEDIATE must keep a
                    # concurrent writer blocked until both replacements commit.
                    migration_paused.set()
                    assert allow_migration.wait(5), "test did not release migration"

            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    def initialize() -> None:
        try:
            result["db"] = SessionDB(db_path=db_path)
        except BaseException as exc:
            result["initializer_error"] = exc

    def write_content() -> None:
        try:
            with real_connect(str(db_path), timeout=5.0) as writer:
                writer_started.set()
                writer.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    ("transactionally indexed", message_id),
                )
                writer.commit()
        except BaseException as exc:
            result["writer_error"] = exc
        finally:
            writer_done.set()

    initializer = threading.Thread(
        target=initialize,
        name="fts-migrator",
        daemon=True,
    )
    initializer.start()
    assert migration_paused.wait(5), "migration never reached post-drop CREATE"

    writer = threading.Thread(target=write_content, daemon=True)
    writer.start()
    assert writer_started.wait(2), "writer never attempted its content update"
    assert not writer_done.wait(0.25), "writer committed inside triggerless transaction"

    allow_migration.set()
    initializer.join(5)
    writer.join(5)
    assert not initializer.is_alive()
    assert not writer.is_alive()
    assert "initializer_error" not in result, result.get("initializer_error")
    assert "writer_error" not in result, result.get("writer_error")

    try:
        with real_connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT rowid FROM messages_fts "
                "WHERE messages_fts MATCH 'transactionally'"
            ).fetchall()
        assert [row[0] for row in rows] == [message_id]
    finally:
        db = result.get("db")
        if isinstance(db, SessionDB):
            db.close()


def test_writer_cannot_bypass_fts_during_real_migration(db_path, monkeypatch):
    """A concurrent content write must be indexed across broad→narrow migration."""
    from hermes_state import SessionDB

    message_id = _create_current_db_with_broad_update_triggers(db_path)
    original_ensure = SessionDB._ensure_fts_schema
    migration_paused = threading.Event()
    allow_initializer = threading.Event()
    result: dict[str, object] = {}

    def pause_before_ensure(self, cursor, table_name, ddl):
        if table_name == "messages_fts" and not migration_paused.is_set():
            migration_paused.set()
            assert allow_initializer.wait(5), "writer did not exercise migration window"
        return original_ensure(self, cursor, table_name, ddl)

    monkeypatch.setattr(SessionDB, "_ensure_fts_schema", pause_before_ensure)
    # The production contract is that trigger narrowing does not rebuild an
    # already-valid index. Disable the legacy repair path so this test exposes
    # any write that commits in a triggerless window instead of being masked
    # by a full post-hoc rebuild.
    monkeypatch.setattr(SessionDB, "_rebuild_fts_indexes", lambda *args, **kwargs: None)

    def initialize() -> None:
        try:
            result["db"] = SessionDB(db_path=db_path)
        except BaseException as exc:  # surfaced in the main test thread
            result["error"] = exc

    initializer = threading.Thread(target=initialize, daemon=True)
    initializer.start()
    assert migration_paused.wait(5), "initializer never reached FTS schema ensure"

    writer_started = threading.Event()
    writer_done = threading.Event()

    def write() -> None:
        writer_started.set()
        try:
            with sqlite3.connect(str(db_path), timeout=5.0) as writer:
                writer.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    ("concurrent replacement", message_id),
                )
                writer.commit()
        except BaseException as exc:
            result["writer_error"] = exc
        finally:
            writer_done.set()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    assert writer_started.wait(2)
    assert not writer_done.wait(0.25), "writer bypassed schema-repair lock"

    allow_initializer.set()
    initializer.join(5)
    writer.join(5)
    assert not initializer.is_alive()
    assert not writer.is_alive()
    assert "error" not in result, result.get("error")
    assert "writer_error" not in result, result.get("writer_error")

    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'replacement'"
            ).fetchall()
        assert [row[0] for row in rows] == [message_id]
    finally:
        db = result.get("db")
        if isinstance(db, SessionDB):
            db.close()


def test_second_open_is_idempotent_and_does_not_rebuild(db_path):
    """Once narrowed, a second real SessionDB open performs no FTS rebuild."""
    from hermes_state import SessionDB

    _create_current_db_with_broad_update_triggers(db_path)
    first = SessionDB(db_path=db_path)
    first.close()

    before = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    with patch.object(
        SessionDB,
        "_rebuild_fts_indexes",
        side_effect=AssertionError("idempotent reopen must not rebuild FTS"),
    ):
        second = SessionDB(db_path=db_path)
    second.close()

    after = {name: _trigger_sql(db_path, name) for name in _UPDATE_TRIGGERS}
    assert after == before
