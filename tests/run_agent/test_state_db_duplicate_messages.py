"""Tests for the state.db byte-identical duplicate-message hole (mechanism 1).

A profile state.db was observed carrying two message rows with the same
(session_id, role, content, timestamp-to-the-millisecond) — e.g. pair
id 638962 == id 640246, ts REAL 1785411770.4681301 — so the same report
rendered twice in the Desktop chat view. Any residual writer path (a poll
loop, a race in a flush, a missed compaction rebaseline) could persist a
byte-identical row because the ``messages`` table had no uniqueness guard.

The fix boundary: dedupe applies ONLY within the ACTIVE (active=1) row
class. The in-place compaction flow INTENTIONALLY keeps the same
content+timestamp once as (active=0, compacted=1) — the archived original —
and once as (active=1, compacted=0) — the re-inserted live copy. A
constraint spanning (active, compacted) would forbid that pair and break
compaction durability.
"""

import sqlite3
import tempfile
from pathlib import Path

CONTENT = "quarterly report: revenue up 12%"
TS = 1785411770.4681301  # fractional-millisecond REAL, as in the live DB


def _raw_counts(db_path):
    """Direct sqlite read — independent of the SessionDB Python API."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(active = 1 AND compacted = 0), 0), "
            "COALESCE(SUM(active = 0 AND compacted = 1), 0) "
            "FROM messages WHERE content = ?",
            (CONTENT,),
        ).fetchone()
    finally:
        conn.close()


class TestAppendMessageIdempotence:
    def test_append_message_idempotent_duplicate_insert(self):
        """Re-appending a byte-identical (session, role, content, timestamp)
        message must not create a second row, double-count the session, or
        double-index FTS."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db = SessionDB(db_path=db_path)
            try:
                sid = "20260730_170159_f8432a"
                db.create_session(sid, "cli", model="test/model")

                first_id = db.append_message(
                    session_id=sid, role="assistant",
                    content=CONTENT, timestamp=TS,
                )
                # The exact same write again — the idempotence-hole path.
                second_id = db.append_message(
                    session_id=sid, role="assistant",
                    content=CONTENT, timestamp=TS,
                )

                assert second_id == first_id, (
                    "idempotent re-append must resolve to the existing row"
                )
                # Direct DB read: exactly ONE message row for this content.
                total, active, archived = _raw_counts(db_path)
                assert total == 1, (
                    f"duplicate insert persisted: {total} rows share the same "
                    "content"
                )
                assert (active, archived) == (1, 0)
                # Session counters must track the single persisted row.
                assert db.get_session(sid)["message_count"] == 1
                # FTS got exactly one entry — the duplicate insert must not
                # fire the messages_fts_insert trigger for a skipped row.
                fts_count = db._conn.execute(
                    "SELECT COUNT(*) FROM messages_fts"
                ).fetchone()[0]
                assert fts_count == 1, (
                    "skipped duplicate insert still fired the FTS insert "
                    "trigger"
                )
            finally:
                db.close()


class TestInPlaceCompactionDuplicateBoundary:
    def test_in_place_compaction_keeps_single_live_copy_and_archive(self):
        """The mechanism-2 pair must survive the dedupe guard while a
        duplicate replay is still collapsed.

        Flow: an active message is soft-archived by in-place compaction
        (active=0, compacted=1) and the same content+timestamp is
        re-inserted as the new active row — the legitimate archive+active
        pair. A residual replay then re-appends the identical row a second
        time. The store must end with exactly ONE live copy AND the archive
        copy intact — not two live rows, and the archive copy NOT swallowed.
        """
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db = SessionDB(db_path=db_path)
            try:
                sid = "20260730_170159_f8432a"
                db.create_session(sid, "cli", model="test/model")
                db.append_message(
                    session_id=sid, role="assistant",
                    content=CONTENT, timestamp=TS,
                )

                # In-place compaction: soft-archive the live row, re-insert
                # the same content+timestamp as the fresh active copy.
                db.archive_and_compact(
                    sid,
                    [{"role": "assistant", "content": CONTENT,
                      "timestamp": TS}],
                )

                # Residual duplicate writer (race / poll-loop / missed
                # rebaseline) re-appends the identical row.
                db.append_message(
                    session_id=sid, role="assistant",
                    content=CONTENT, timestamp=TS,
                )

                total, active, archived = _raw_counts(db_path)
                assert total == 2, (
                    f"the archive+live pair must total 2 rows, found {total}"
                )
                assert active == 1, (
                    f"duplicate replay persisted: {active} live active=1 "
                    "copies of the same content+timestamp"
                )
                assert archived == 1, (
                    "the compaction archive copy (active=0, compacted=1) "
                    "must survive the dedupe constraint — it is intentional"
                )
                # Live count tracks just the active row.
                assert db.get_session(sid)["message_count"] == 1
            finally:
                db.close()


class TestMigrationCollapsesExistingDuplicates:
    def test_upgrade_open_collapses_dirty_active_rows_and_guards_future(self):
        """An existing DB that already carries byte-identical ACTIVE duplicate
        rows (written before the guard existed) must open cleanly: the
        migration keeps the first copy, preserves the intentional
        archive+live pair, installs the guard index, and blocks new dupes.
        """
        from hermes_state import SessionDB
        from hermes_state_common import SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db = SessionDB(db_path=db_path)
            sid = "20260730_170159_f8432a"
            db.create_session(sid, "cli", model="test/model")
            db.close()

            # Recreate a pre-upgrade dirty state: guard index absent, two
            # live byte-identical rows, plus the intentional compaction pair
            # (same content+timestamp archived AND active). Plain INSERTs
            # only succeed while the index is dropped — as before the fix.
            raw = sqlite3.connect(db_path)
            try:
                raw.execute("DROP INDEX idx_messages_active_dedupe")
                ts_pair = TS + 1000.0
                for i in range(2):  # byte-identical live duplicates
                    raw.execute(
                        "INSERT INTO messages (session_id, role, content, "
                        "timestamp, active, compacted) VALUES (?, 'assistant', "
                        "?, ?, 1, 0)",
                        (sid, CONTENT, TS),
                    )
                raw.execute(  # archived original of the compaction pair
                    "INSERT INTO messages (session_id, role, content, "
                    "timestamp, active, compacted) VALUES (?, 'assistant', "
                    "?, ?, 0, 1)",
                    (sid, CONTENT, ts_pair),
                )
                raw.execute(  # its live re-insert — the legitimate pair
                    "INSERT INTO messages (session_id, role, content, "
                    "timestamp, active, compacted) VALUES (?, 'assistant', "
                    "?, ?, 1, 0)",
                    (sid, CONTENT, ts_pair),
                )
                raw.commit()
            finally:
                raw.close()

            db = SessionDB(db_path=db_path)  # open runs the migration
            try:
                rows = db._conn.execute(
                    "SELECT id, active, compacted FROM messages "
                    "WHERE content = ? ORDER BY id",
                    (CONTENT,),
                ).fetchall()
                live = [r for r in rows if r["active"] == 1]
                archived = [r for r in rows if r["active"] == 0]
                assert len(rows) == 3 and len(live) == 2, (
                    f"expected merged duplicate + intact pair, found {rows}"
                )
                assert len(archived) == 1 and archived[0]["compacted"] == 1
                # The surviving duplicate copy is the FIRST-inserted row.
                assert live[0]["id"] == 1
                # The guard index landed and schema bookkeeping advanced.
                assert db._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'idx_messages_active_dedupe'"
                ).fetchone() is not None
                assert db._conn.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()[0] == SCHEMA_VERSION
                # New duplicate writes are blocked from here on.
                db.append_message(
                    session_id=sid, role="assistant",
                    content=CONTENT, timestamp=TS,
                )
                live_after = db._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE content = ? "
                    "AND active = 1 AND timestamp = ?",
                    (CONTENT, TS),
                ).fetchone()[0]
                assert live_after == 1
            finally:
                db.close()
