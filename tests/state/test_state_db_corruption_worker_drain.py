"""Session-DB corruption → worker drain: regression tests for incident 2026-08-03.

Incident trace
--------------
The root/default ``state.db`` suffered SQLite B-tree corruption in the
``messages`` table and the FTS trees; the Quill and Orion profiles had
malformed FTS indexes as well. Dispatched kanban workers opened the store
successfully (the corruption is invisible to a normal open), failed the
FIRST canonical transcript write, exited, and the two-failure circuit
breaker drained the fleet:

    worker spawn
      → SessionDB() open OK            (corruption is not schema-level)
      → first append_message fails      (INSERT INTO messages → FTS triggers
                                         / b-tree read raises DatabaseError)
      → conversation_loop sets ``session_persistence_failed``
        (agent/conversation_loop.py:6089-6097)
      → user-facing message: "session storage could not be written"
        (run_agent.AIAgent._format_turn_completion_explanation)
      → run fails, dispatcher failure counter ticks
      → after ``kanban.failure_limit`` (default 2) the task auto-blocks

Two corruption classes matter here:

1. **FTS index corruption** (shadow-table b-tree damage in
   ``messages_fts*``). CURRENT code self-heals: ``_execute_write`` detects
   the malformed-image class, runs a one-shot in-place FTS ``'rebuild'``,
   and retries the write (see ``tests/state/test_fts_runtime_rebuild.py``).

2. **``messages`` table b-tree corruption**. NOT self-healing:
   ``repair_state_db_schema`` escalates through FTS rebuild / REINDEX /
   schema dedup / drop-FTS and every pass fails on the malformed image;
   manual restore from a backup is required. A worker hitting this class
   still drains exactly as in the incident.

These tests (a) reproduce the incident's write-failure against a real
corrupted ``messages`` b-tree, and (b) pin the contract for the
pre-dispatch health probe that must be wired into the kanban dispatcher
so workers are never spawned against an unhealthy state.db. The probe
wiring does not exist yet — ``test_pre_dispatch_state_db_probe_*`` is
expected to FAIL against current code (that is the point: it is the
contract to implement later).
"""
import sqlite3
import uuid
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB, _db_opens_cleanly, repair_state_db_schema


# ── Fixture builders ──────────────────────────────────────────────────────────


def _build_healthy_db(db_path: Path) -> str:
    """Create a small healthy state.db with one session and 10 messages."""
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(5):
        db.append_message(sid, role="user", content=f"hello world {i}")
        db.append_message(sid, role="assistant", content=f"reply about pizza {i}")
    db.close()
    return sid


def _force_delete_journal(db_path: Path) -> int:
    """Checkpoint any WAL, switch to DELETE journal mode, VACUUM, and return
    the ``messages`` table's rootpage.

    VACUUM rewrites the file so the b-tree layout is deterministic and the
    rootpage we corrupt is the one every subsequent statement reads.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        row = conn.execute(
            "SELECT rootpage FROM sqlite_master "
            "WHERE type = 'table' AND name = 'messages'"
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def _corrupt_messages_btree(db_path: Path) -> None:
    """Physically corrupt the ``messages`` table b-tree root page.

    Sets the page's cell-count field to 0xFFFF (65535) so SQLite can never
    resolve rowid bounds — the exact "database disk image is malformed"
    class from the incident. This is a real on-disk page failure, not a
    mocked cursor exception. The technique mirrors
    ``tests/hermes_cli/test_session_recovery.py``.
    """
    root_page = _force_delete_journal(db_path)
    data = bytearray(db_path.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    if page_size == 1:
        page_size = 65_536
    page_start = (root_page - 1) * page_size
    header_offset = page_start + (100 if root_page == 1 else 0)
    assert data[header_offset] in {0x02, 0x05, 0x0A, 0x0D}, (
        f"unexpected messages b-tree page type {data[header_offset]:#x}"
    )
    # Impossible cell count → btreeInitPage() fails on every read/write.
    data[header_offset + 3 : header_offset + 5] = b"\xff\xff"
    db_path.write_bytes(data)


def _corrupt_fts_index_data(db_path: Path) -> None:
    """Overwrite the FTS5 shadow b-tree blocks (the #50502 write class)."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("UPDATE messages_fts_data SET block = X'DEADBEEFDEADBEEF'")
    conn.close()


# ── Incident reproduction: the canonical write fails ─────────────────────────


class TestCanonicalWriteAgainstCorruptStore:
    def test_append_message_fails_on_messages_btree_corruption(self, tmp_path):
        """A worker's first canonical transcript write against the corrupted
        store raises — the exact failure that became
        ``session_persistence_failed`` in the incident."""
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        _corrupt_messages_btree(db_path)

        # The corruption is invisible to a normal open (schema is fine, the
        # sessions b-tree reads fine) — this is why workers start at all.
        db = SessionDB(db_path=db_path)
        try:
            assert db._conn is not None
            sid = db._conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
            assert sid, "session survived the corruption (fixture sanity)"

            with pytest.raises(sqlite3.DatabaseError) as exc_info:
                db.append_message(sid, role="user", content="post-corruption probe")
            assert "database disk image is malformed" in str(exc_info.value)
        finally:
            db.close()

    def test_corruption_is_silent_to_plain_reads(self, tmp_path):
        """Base-table reads of OTHER tables still succeed — the silent class
        that let the fleet spin up workers against a doomed store."""
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        _corrupt_messages_btree(db_path)

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1
        finally:
            conn.close()

    def test_write_failure_maps_to_session_storage_message(self):
        """The incident's user-facing string is still the contract: the
        canonical-write failure becomes ``session_persistence_failed``, which
        renders as 'session storage could not be written'."""
        from run_agent import AIAgent

        text = AIAgent._format_turn_completion_explanation(
            "session_persistence_failed"
        )
        assert "session storage could not be written" in text

    def test_messages_btree_corruption_is_not_self_healing(self, tmp_path):
        """Unlike the FTS class, the messages b-tree class cannot be repaired
        by the automatic ladder — this is the class that drains workers."""
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        _corrupt_messages_btree(db_path)

        report = repair_state_db_schema(db_path, backup=False)
        assert report["repaired"] is False
        assert report["error"], "repair must surface why it failed"

        # The detector still flags it after the repair attempt.
        assert _db_opens_cleanly(db_path) is not None


# ── Contract for the pre-dispatch health probe (TO BE IMPLEMENTED) ──────────
# The kanban dispatcher currently spawns ``hermes -p <assignee>`` workers
# without checking the assignee's state.db health
# (``hermes_cli.kanban_db.dispatch_once`` / ``_default_spawn``). A worker
# pointed at a corrupt store fails its first write and drains the fleet via
# the failure circuit breaker. The probe below is the contract to implement:
# a module-level function in ``hermes_cli/kanban_db.py``
# (``pre_dispatch_state_db_probe(profile_name) -> Optional[str]``) that
# resolves the profile's state.db and delegates to
# ``hermes_state._db_opens_cleanly``; ``dispatch_once`` must consult it per
# assignee before spawning and block/skip instead of spawning a doomed
# worker. These tests FAIL on current code on purpose.


def test_pre_dispatch_state_db_probe_exists_in_kanban_dispatch():
    """The dispatcher must expose the probe — FAILS today (not implemented).

    Expected API once implemented::

        def pre_dispatch_state_db_probe(profile_name: str) -> Optional[str]:
            \"\"\"None if the profile's state.db is healthy enough to spawn a
            worker; else a human-readable reason naming the corruption.
            Resolves the profile's HERMES_HOME state.db and delegates to
            hermes_state._db_opens_cleanly.\"\"\"

    ``dispatch_once`` should call this for every assignee before
    ``_default_spawn`` and refuse to spawn (blocking the task with the
    reason) when it returns non-None.
    """
    from hermes_cli import kanban_db

    probe = getattr(kanban_db, "pre_dispatch_state_db_probe", None)
    assert probe is not None, (
        "pre-dispatch state.db health probe is not wired into the kanban "
        "dispatcher yet. Implement `pre_dispatch_state_db_probe(profile_name) "
        "-> Optional[str]` in hermes_cli/kanban_db.py (delegating to "
        "hermes_state._db_opens_cleanly on the profile's state.db) and call "
        "it from dispatch_once before spawning workers. See "
        "docs/design/state-db-corruption-worker-drain.md for the incident "
        "trace."
    )


class TestProbeDetectorContract:
    """The detector the probe must delegate to already flags both incident
    corruption classes on real fixtures — so a probe that delegates is
    sufficient. These assertions PASS today (they pin the detector
    behavior); the *wiring* is what is missing (test above fails)."""

    def test_detector_flags_messages_btree_corruption(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        assert _db_opens_cleanly(db_path) is None  # healthy before

        _corrupt_messages_btree(db_path)
        reason = _db_opens_cleanly(db_path)
        assert reason is not None
        assert "malformed" in reason.lower()

    def test_detector_flags_fts_index_corruption(self, tmp_path):
        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        if not db._fts_enabled:
            db.close()
            pytest.skip("FTS5 unavailable in this build")
        sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
        db.append_message(sid, role="user", content="hello world")
        db.close()

        _corrupt_fts_index_data(db_path)
        reason = _db_opens_cleanly(db_path)
        assert reason is not None

    def test_detector_returns_none_when_healthy(self, tmp_path):
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        assert _db_opens_cleanly(db_path) is None


# ── Why the fleet drained: FTS self-heals, messages b-tree does not ──────────


class TestCorruptionClassAsymmetry:
    def test_fts_corruption_self_heals_on_write(self, tmp_path):
        """The FTS class (Quill/Orion's malformed indexes) self-heals: the
        one-shot in-place rebuild lets a worker's write succeed."""
        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        if not db._fts_enabled:
            db.close()
            pytest.skip("FTS5 unavailable in this build")
        try:
            sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
            db.append_message(sid, role="user", content="before corruption")
            db.close()

            _corrupt_fts_index_data(db_path)
            db = SessionDB(db_path=db_path)
            msg_id = db.append_message(
                sid, role="user", content="write survives via in-place rebuild"
            )
            assert msg_id is not None
            assert db._fts_runtime_rebuild_attempted is True
        finally:
            db.close()

    def test_messages_btree_corruption_does_not_self_heal_on_write(self, tmp_path):
        """The messages b-tree class (the root/default incident) does NOT:
        the write raises and no in-place path can repair it — the worker's
        only outcome is ``session_persistence_failed``."""
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        _corrupt_messages_btree(db_path)

        db = SessionDB(db_path=db_path)
        try:
            assert db._conn is not None
            sid = db._conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
            with pytest.raises(sqlite3.DatabaseError):
                db.append_message(sid, role="user", content="doomed write")
            # The write never landed.
            conn = sqlite3.connect(str(db_path), isolation_level=None)
            try:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]
            finally:
                conn.close()
            assert rows == 10
        finally:
            db.close()
