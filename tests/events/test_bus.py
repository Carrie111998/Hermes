"""Tests for events.bus -- SQLite-backed EventBus."""

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority


@pytest.fixture
def bus(tmp_path):
    """Create an EventBus backed by a temp SQLite database."""
    db_path = tmp_path / "events" / "event_bus.db"
    return EventBus(db_path=db_path)


def _subscribe(bus, subscriber_id, **kwargs):
    """Seed the cursor read-from-start on first subscribe, then subscribe.

    EventBus defaults a never-seen subscriber's cursor to head (the ADR-0018
    scanner-flood mitigation). These tests emit then subscribe via a raw id and
    expect to consume those events, so seed cursor=0. INSERT OR IGNORE preserves
    a cursor already advanced by a prior ack (so cursor-advance tests still work).
    """
    bus._execute(
        "INSERT OR IGNORE INTO subscriber_cursors "
        "(subscriber_id, last_rowid, updated_at) VALUES (?, 0, datetime('now'))",
        (subscriber_id,),
    )
    return bus.subscribe(subscriber_id, **kwargs)


class TestEmit:
    def test_emit_returns_event_id(self, bus):
        event_id = bus.emit(
            event_type=EventType.CRON_COMPLETED,
            source="scout",
            payload={"duration": 10.5},
        )
        assert event_id
        assert isinstance(event_id, str)

    def test_emit_creates_db_and_dirs(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})
        assert bus.db_path.exists()
        assert bus.db_path.parent.exists()

    def test_emit_with_all_fields(self, bus):
        event_id = bus.emit(
            event_type=EventType.JOB_HIGH_SCORE,
            source="matcher",
            payload={"score": 9.1, "company": "Acme"},
            priority=Priority.CRITICAL,
            correlation_id="corr-1",
            job_id="job-ext-1",
            tags=["vip", "finance"],
        )
        events = bus.query(event_type=EventType.JOB_HIGH_SCORE)
        assert len(events) == 1
        assert events[0].event_id == event_id
        assert events[0].priority == Priority.CRITICAL
        assert events[0].correlation_id == "corr-1"
        assert events[0].job_id == "job-ext-1"
        assert events[0].tags == ["vip", "finance"]


class TestSubscribe:
    def test_subscribe_returns_new_events(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {"a": 1})
        bus.emit(EventType.CRON_COMPLETED, "matcher", {"b": 2})

        events = _subscribe(bus,"test-sub")
        assert len(events) == 2
        assert events[0].payload == {"a": 1}
        assert events[1].payload == {"b": 2}

    def test_subscribe_with_type_filter(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "VP Finance"})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        events = _subscribe(bus,"test-sub", event_types=[EventType.JOB_DISCOVERED])
        assert len(events) == 1
        assert events[0].event_type == EventType.JOB_DISCOVERED

    def test_subscribe_with_priority_filter(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})  # LOW
        bus.emit(EventType.CRON_FAILED, "scout", {})  # HIGH
        bus.emit(EventType.INTERVIEW_SIGNAL, "tracker", {})  # CRITICAL

        events = _subscribe(bus,"test-sub", min_priority=Priority.HIGH)
        assert len(events) == 2

    def test_subscribe_cursor_advances(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {"batch": 1})

        events1 = _subscribe(bus,"test-sub")
        assert len(events1) == 1
        bus.ack("test-sub", [e.event_id for e in events1])

        bus.emit(EventType.CRON_COMPLETED, "matcher", {"batch": 2})

        events2 = _subscribe(bus,"test-sub")
        assert len(events2) == 1
        assert events2[0].payload == {"batch": 2}

    def test_independent_subscribers(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {"x": 1})

        events_a = _subscribe(bus,"sub-a")
        events_b = _subscribe(bus,"sub-b")
        assert len(events_a) == 1
        assert len(events_b) == 1

        bus.ack("sub-a", [e.event_id for e in events_a])

        bus.emit(EventType.CRON_COMPLETED, "matcher", {"x": 2})

        events_a2 = _subscribe(bus,"sub-a")
        events_b2 = _subscribe(bus,"sub-b")
        assert len(events_a2) == 1  # only new event
        assert len(events_b2) == 2  # both events (never acked)


class TestSubscribeBootstrapsCursorForDirectConsumers:
    """A consumer that calls bus.subscribe() DIRECTLY — not via BaseSubscriber,
    which seeds its cursor at construction (events/subscribers/base.py) — must
    still bootstrap a persisted cursor on its first poll.

    Without persistence, subscribe() recomputes the head-default MAX(rowid) on
    EVERY poll for a subscriber that has no cursor row, so it re-jumps to head
    each time and silently drops any event emitted between polls — the caller
    can never receive (and therefore never ack) it, and the cursor is never
    persisted. This is the devflow-bridge fresh-bus deadlock (2026-07-18): the
    standalone bridge cron drives bus.subscribe() directly.

    The fix mirrors BaseSubscriber's seed-at-construction: on first poll,
    persist the head-default cursor row (INSERT OR IGNORE at head). ADR-0018
    flood prevention is preserved because we seed at HEAD, not zero, so
    pre-first-poll history is still skipped.
    """

    def test_direct_consumer_receives_event_emitted_between_polls(self, bus):
        # Pre-existing history that must NOT be replayed (flood prevention).
        bus.emit(EventType.CRON_STARTED, "scout", {"old": 1})

        # First poll for a never-seen subscriber: head-default => no backlog.
        first = bus.subscribe("devflow-bridge")
        assert first == []

        # An event lands AFTER the first poll but BEFORE the second.
        between = bus.emit(EventType.CRON_COMPLETED, "scout", {"new": 1})

        # Second poll must deliver the between-poll event — not re-jump to head
        # and drop it.
        second = bus.subscribe("devflow-bridge")
        assert [e.event_id for e in second] == [between]

    def test_first_direct_poll_persists_cursor_at_head(self, bus):
        # Two events of pre-poll history establish a non-zero head.
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        head = bus._get_conn().execute("SELECT MAX(rowid) FROM events").fetchone()[0]

        bus.subscribe("devflow-bridge")

        row = bus._get_conn().execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            ("devflow-bridge",),
        ).fetchone()
        assert row is not None, "first poll must bootstrap a persisted cursor row"
        # Seeded at head (flood prevention preserved), not zero (no replay).
        assert row["last_rowid"] == head

    def test_first_poll_does_not_clobber_a_preexisting_cursor(self, bus):
        # A cursor already advanced by a prior ack / explicit backfill seed must
        # win over the head-default — INSERT OR IGNORE, never overwrite.
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus._execute(
            "INSERT INTO subscriber_cursors (subscriber_id, last_rowid) VALUES (?, 0)",
            ("devflow-bridge",),
        )

        events = bus.subscribe("devflow-bridge")
        # Cursor was 0, so BOTH events backfill (opt-in behaviour preserved).
        assert len(events) == 2
        row = bus._get_conn().execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            ("devflow-bridge",),
        ).fetchone()
        assert row["last_rowid"] == 0


class TestQuery:
    def test_query_skips_unknown_event_type(self, bus):
        """A row whose event_type isn't in the EventType enum (cross-version
        skew / legacy producer) must be SKIPPED by query(), not crash the whole
        call. Regression for 2026-07-10: 9 'weekly_analytics_summary' rows made
        query() raise, crashing DigestComposer.compose() every poll tick."""
        bus.emit(EventType.CRON_STARTED, "scout", {})
        conn = bus._get_conn()
        conn.execute(
            "INSERT INTO events (event_id, event_type, source, timestamp, priority) "
            "VALUES ('bad-1', 'weekly_analytics_summary', 'analytics', "
            "'2026-07-01T00:00:00Z', 'low')"
        )
        conn.commit()

        results = bus.query()  # must NOT raise
        assert len(results) == 1
        assert results[0].event_type == EventType.CRON_STARTED

    def test_query_by_type(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        results = bus.query(event_type=EventType.JOB_DISCOVERED)
        assert len(results) == 1

    def test_query_by_source(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "matcher", {})

        results = bus.query(source="matcher")
        assert len(results) == 1
        assert results[0].source == "matcher"

    def test_query_by_correlation_id(self, bus):
        bus.emit(EventType.JOB_DISCOVERED, "scout", {}, correlation_id="flow-1")
        bus.emit(EventType.JOB_SCORED, "matcher", {}, correlation_id="flow-1")
        bus.emit(EventType.JOB_DISCOVERED, "scout", {}, correlation_id="flow-2")

        results = bus.query(correlation_id="flow-1")
        assert len(results) == 2


class TestCleanup:
    def test_cleanup_removes_old_events(self, bus):
        # Emit an event, then manually backdate it
        event_id = bus.emit(EventType.CRON_STARTED, "scout", {})
        bus._execute(
            "UPDATE events SET created_at = datetime('now', '-31 days') WHERE event_id = ?",
            (event_id,),
        )
        bus.emit(EventType.CRON_STARTED, "scout", {})  # recent event

        removed = bus.cleanup(retention_days=30)
        assert removed == 1

        remaining = bus.query()
        assert len(remaining) == 1


class TestSubscriberLag:
    """Lag = events emitted after a subscriber's current cursor position."""

    def test_lag_is_zero_for_unknown_subscriber(self, bus):
        # An unknown subscriber starts at head (ADR-0018 scanner-flood
        # mitigation — it never replays history), so it has no backlog.
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        # With no cursor, lag is 0 (head-default), not the total event count.
        assert bus.subscriber_lag("never-seen") == 0

    def test_lag_after_partial_processing(self, bus):
        # Emit 5 events
        ids = [bus.emit(EventType.CRON_STARTED, "scout", {"i": i}) for i in range(5)]
        # Ack only the first 2
        bus.ack("sub-1", ids[:2])
        # 3 events should still be unread
        assert bus.subscriber_lag("sub-1") == 3

    def test_lag_after_full_processing(self, bus):
        ids = [bus.emit(EventType.CRON_STARTED, "scout", {"i": i}) for i in range(3)]
        bus.ack("sub-1", ids)
        assert bus.subscriber_lag("sub-1") == 0

    def test_lag_increases_as_new_events_emit(self, bus):
        ids = [bus.emit(EventType.CRON_STARTED, "scout", {}) for _ in range(2)]
        bus.ack("sub-1", ids)
        assert bus.subscriber_lag("sub-1") == 0
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        assert bus.subscriber_lag("sub-1") == 2


class TestSchemaMigration:
    def test_migrates_legacy_db_without_status_column(self, tmp_path):
        """EventBus must upgrade pre-0.9.1 databases that lack the status column."""
        import sqlite3

        db_path = tmp_path / "legacy" / "event_bus.db"
        db_path.parent.mkdir(parents=True)

        # Create a legacy DB with the old schema (no status column, old index)
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE events (
                event_id     TEXT PRIMARY KEY,
                event_type   TEXT NOT NULL,
                source       TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                priority     TEXT NOT NULL,
                payload      TEXT NOT NULL DEFAULT '{}',
                correlation_id TEXT,
                job_id       TEXT,
                tags         TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX idx_events_type_ts ON events (event_type, created_at);
        """)
        # Insert some legacy data
        conn.execute(
            "INSERT INTO events (event_id, event_type, source, timestamp, priority) "
            "VALUES ('legacy-1', 'cron_started', 'old', '2026-01-01T00:00:00Z', 'low')"
        )
        conn.commit()
        conn.close()

        # Opening with the new EventBus should migrate the schema in place
        bus = EventBus(db_path=db_path)

        # Verify the status column now exists
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "status" in cols
        # Verify the new index was created
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
        assert "idx_events_type_status_ts" in indexes
        # Legacy row is preserved
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1
        conn.close()

        # New events can be emitted against the migrated DB
        event_id = bus.emit(EventType.CRON_COMPLETED, "new", {"ok": True})
        assert event_id
        assert len(bus.query()) == 2  # legacy + new
        bus.close()


class TestThreadSafety:
    def test_concurrent_emits(self, bus):
        errors = []

        def emit_events(source: str):
            try:
                for i in range(20):
                    bus.emit(EventType.CRON_COMPLETED, source, {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=emit_events, args=(f"agent-{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        all_events = bus.query()
        assert len(all_events) == 80  # 4 threads * 20 events


def test_checkpoint_exposes_wal_data_to_other_connections(tmp_path):
    import sqlite3
    from events.bus import EventBus
    from events.schema import EventType

    db = tmp_path / "bus.db"
    bus = EventBus(db_path=db)
    try:
        bus.emit(EventType.CRON_STARTED, "test", {})
        bus.checkpoint()

        other = sqlite3.connect(str(db))
        count = other.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        other.close()
        assert count == 1
    finally:
        bus.close()


class TestWalPragmas:
    """SR-446 / ADR-0018 — explicit WAL-tuning PRAGMAs on every connection.

    Rationale: SR-409 flood on 2026-04-19 left event_bus.db-wal at 69 MB.
    Root cause per research 12 + ADR-0018: PASSIVE checkpoints silently skip
    under reader contention × 8 subscribers polling at 1 Hz × stdlib sqlite3
    implicit-transaction semantics. Fix is three explicit PRAGMAs on every
    connection:

      synchronous=NORMAL          (safe with WAL; FULL is overkill)
      journal_size_limit=32MB     (previously -1 / unbounded)
      wal_autocheckpoint=1000     (was the implicit default; now explicit)

    These tests pin the PRAGMAs at connection time. A regression would mean
    the live DB returns to unbounded WAL growth. Trade-off: lowering below
    1000 is an anti-pattern (more skipped checkpoints, not fewer) — if a
    future contributor tries `wal_autocheckpoint=100`, they'll notice this
    test and hopefully read ADR-0018 before flipping it.
    """

    def test_synchronous_is_normal(self, bus):
        """PRAGMA synchronous returns 1 (NORMAL)."""
        result = bus._get_conn().execute("PRAGMA synchronous").fetchone()
        assert result[0] == 1, (
            f"Expected synchronous=NORMAL (1); got {result[0]}. "
            f"FULL (2) is overkill with WAL + our durability model."
        )

    def test_journal_size_limit_is_32mb(self, bus):
        """PRAGMA journal_size_limit returns 32 MiB in bytes (33554432)."""
        result = bus._get_conn().execute("PRAGMA journal_size_limit").fetchone()
        assert result[0] == 33554432, (
            f"Expected journal_size_limit=33554432 (32 MiB); got {result[0]}. "
            f"Unbounded (-1) allowed the SR-409 69 MB WAL growth."
        )

    def test_wal_autocheckpoint_is_1000(self, bus):
        """PRAGMA wal_autocheckpoint returns 1000 pages (explicit)."""
        result = bus._get_conn().execute("PRAGMA wal_autocheckpoint").fetchone()
        assert result[0] == 1000, (
            f"Expected wal_autocheckpoint=1000 pages; got {result[0]}. "
            f"Lowering below 1000 is an anti-pattern per ADR-0018 — it "
            f"increases skipped checkpoints under reader contention."
        )

    def test_journal_mode_still_wal(self, bus):
        """Guard against a regression that drops WAL mode while tuning PRAGMAs."""
        result = bus._get_conn().execute("PRAGMA journal_mode").fetchone()
        assert result[0].lower() == "wal", (
            f"journal_mode must remain WAL; got {result[0]}. "
            f"Dropping WAL would lose at-least-once delivery semantics."
        )


class TestExecuteRollsBackOnError:
    """A failed write must roll back so the thread-local connection is not left
    mid-transaction — otherwise a later SELECT pins a stale read snapshot and
    every subsequent write fails SQLITE_BUSY_SNAPSHOT until the connection is
    reset (the 2026-07-14 subscriber-ack wedge)."""

    def test_execute_rolls_back_on_error(self, bus):
        bus.emit(EventType.CRON_STARTED, "test", {})
        conn = bus._get_conn()
        conn.execute("PRAGMA busy_timeout=100")  # fail fast; don't wait the 5s default
        holder = sqlite3.connect(str(bus.db_path))
        try:
            holder.execute("BEGIN IMMEDIATE")  # hold the write lock, uncommitted
            holder.execute(
                "INSERT INTO handled_events (subscriber_id, event_id) VALUES ('h', 'h1')"
            )
            with pytest.raises(sqlite3.OperationalError):
                bus._execute(
                    "INSERT OR IGNORE INTO handled_events (subscriber_id, event_id) "
                    "VALUES (?, ?)",
                    ("s", "e1"),
                )
            assert bus._get_conn().in_transaction is False, (
                "a failed write left the connection mid-transaction; a later SELECT "
                "would pin a stale snapshot and wedge every subsequent write"
            )
        finally:
            holder.rollback()
            holder.close()


class TestDeadLetters:
    """SR-109 dead-letter table — records per-(event, subscriber) handler failures."""

    def test_record_dead_letter_stores_row(self, bus):
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.record_dead_letter("digest-composer", eid, "KeyError: 'score'")

        rows = bus.get_dead_letters()
        assert len(rows) == 1
        assert rows[0]["event_id"] == eid
        assert rows[0]["subscriber_id"] == "digest-composer"
        assert "KeyError" in rows[0]["error"]
        assert rows[0]["failed_at"]

    def test_record_dead_letter_upserts_on_repeat(self, bus):
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.record_dead_letter("digest-composer", eid, "first error")
        bus.record_dead_letter("digest-composer", eid, "second error")

        rows = bus.get_dead_letters()
        assert len(rows) == 1
        assert rows[0]["error"] == "second error"
        assert rows[0]["attempts"] == 2

    def test_record_dead_letter_separate_subscribers(self, bus):
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.record_dead_letter("digest-composer", eid, "boom")
        bus.record_dead_letter("whatsapp-escalator", eid, "ouch")

        rows = bus.get_dead_letters()
        assert len(rows) == 2

    def test_get_dead_letters_filters_by_subscriber(self, bus):
        e1 = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        e2 = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.record_dead_letter("digest-composer", e1, "a")
        bus.record_dead_letter("telegram-notifier", e2, "b")

        digest = bus.get_dead_letters(subscriber_id="digest-composer")
        assert len(digest) == 1
        assert digest[0]["event_id"] == e1

    def test_get_dead_letters_orders_newest_first(self, bus):
        e1 = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        e2 = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.record_dead_letter("sub", e1, "first")
        bus.record_dead_letter("sub", e2, "second")
        # SQLite datetime('now') has second resolution and test timing can
        # straddle second boundaries. Use absolute timestamps to guarantee
        # e1 is strictly older than e2.
        bus._execute(
            "UPDATE dead_letters SET failed_at = '2020-01-01 00:00:00' "
            "WHERE event_id = ?", (e1,),
        )
        bus._execute(
            "UPDATE dead_letters SET failed_at = '2030-01-01 00:00:00' "
            "WHERE event_id = ?", (e2,),
        )

        rows = bus.get_dead_letters()
        assert [r["event_id"] for r in rows] == [e2, e1]

    def test_get_dead_letters_limit(self, bus):
        for i in range(5):
            eid = bus.emit(EventType.CRON_COMPLETED, "scout", {"i": i})
            bus.record_dead_letter("sub", eid, f"err-{i}")

        rows = bus.get_dead_letters(limit=3)
        assert len(rows) == 3

    def test_get_dead_letters_joins_event_type(self, bus):
        """Returned rows include event_type for display in doctor."""
        eid = bus.emit(EventType.JOB_HIGH_SCORE, "matcher", {})
        bus.record_dead_letter("digest-composer", eid, "boom")

        rows = bus.get_dead_letters()
        assert rows[0]["event_type"] == EventType.JOB_HIGH_SCORE.type_string


class TestHandledEvents:
    """SR-101 handled_events table — per-(subscriber, event) dedup for at-least-once delivery."""

    def test_is_handled_false_for_unknown(self, bus):
        assert bus.is_handled("sub-a", "nonexistent-event-id") is False

    def test_mark_and_check_handled(self, bus):
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.mark_handled("sub-a", eid)
        assert bus.is_handled("sub-a", eid) is True

    def test_handled_is_per_subscriber(self, bus):
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.mark_handled("sub-a", eid)
        assert bus.is_handled("sub-a", eid) is True
        assert bus.is_handled("sub-b", eid) is False

    def test_mark_handled_is_idempotent(self, bus):
        eid = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.mark_handled("sub-a", eid)
        # A second call must not raise (UNIQUE constraint is handled via OR IGNORE).
        bus.mark_handled("sub-a", eid)
        assert bus.is_handled("sub-a", eid) is True


class TestSubscribeUnknownEventType:
    """Cross-version code skew: a producer using newer code can write event_types
    that the consumer doesn't know yet. Subscribe must skip them, not crash.
    Production incident 2026-04-26: a `curator_daily` row inserted by the curator
    (post-Phase-3.1 code) crashed every gateway subscriber poll because the
    running gateway was started before that EventType was added to schema.py.
    """

    def _insert_unknown_row(self, bus, event_id: str = "poison-1",
                            event_type: str = "unknown_future_type"):
        conn = bus._get_conn()
        conn.execute(
            "INSERT INTO events (event_id, event_type, source, timestamp, "
            "priority, payload, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, event_type, "curator", "2026-04-26T22:31:47Z",
             "NORMAL", "{}", "[]"),
        )
        conn.commit()

    def test_subscribe_does_not_raise_on_unknown_event_type(self, bus):
        self._insert_unknown_row(bus)
        # Pre-fix: ValueError("Unknown event type in DB: unknown_future_type")
        events = _subscribe(bus,"test-sub")
        assert events == []

    def test_subscribe_returns_valid_events_alongside_unknowns(self, bus):
        valid_id_1 = bus.emit(EventType.CRON_STARTED, "scout", {})
        self._insert_unknown_row(bus, event_id="poison-mid")
        valid_id_2 = bus.emit(EventType.CRON_COMPLETED, "matcher", {})

        events = _subscribe(bus,"test-sub")
        ids = [e.event_id for e in events]
        assert valid_id_1 in ids
        assert valid_id_2 in ids
        assert "poison-mid" not in ids
        assert len(events) == 2

    def test_subscribe_does_not_redeliver_unknown_when_valid_events_advance_cursor(
        self, bus
    ):
        """When the unknown row precedes a valid event in the same batch, the
        subscriber's ack of the valid event will advance the cursor past the
        unknown row, so it never re-appears."""
        self._insert_unknown_row(bus, event_id="poison-before-valid")
        valid_id = bus.emit(EventType.CRON_COMPLETED, "scout", {})

        events1 = _subscribe(bus,"test-sub")
        assert len(events1) == 1
        assert events1[0].event_id == valid_id

        # Subscriber acks the valid event — cursor advances past poison rowid
        bus.ack("test-sub", [valid_id])

        # Next subscribe must not re-read the poison row
        events2 = _subscribe(bus,"test-sub")
        assert events2 == []

    def test_all_poison_batch_advances_cursor_and_dead_letters(self, bus):
        """POISON-BATCH GUARD (2026-06-10): when EVERY row in a poll is
        unparseable the caller has nothing to ack, so subscribe() itself
        must advance the cursor past the batch (dead-lettering the rows) —
        otherwise the subscriber re-reads the same capped batch forever."""
        for i in range(3):
            self._insert_unknown_row(bus, event_id=f"poison-{i}")

        assert _subscribe(bus, "test-sub") == []

        # Every poison row is dead-lettered for triage.
        letters = bus._get_conn().execute(
            "SELECT event_id, error FROM dead_letters WHERE subscriber_id = ?",
            ("test-sub",),
        ).fetchall()
        assert {r["event_id"] for r in letters} == {"poison-0", "poison-1", "poison-2"}
        assert all("unparseable_event" in r["error"] for r in letters)

        # Cursor advanced past the batch: a later valid event is delivered
        # and the poison rows never re-appear.
        valid_id = bus.emit(EventType.CRON_COMPLETED, "scout", {})
        events2 = _subscribe(bus, "test-sub")
        assert [e.event_id for e in events2] == [valid_id]

    def test_all_poison_batch_sets_cursor_to_batch_max_rowid(self, bus):
        self._insert_unknown_row(bus, event_id="poison-solo")
        assert _subscribe(bus, "test-sub") == []

        conn = bus._get_conn()
        head = conn.execute("SELECT MAX(rowid) FROM events").fetchone()[0]
        cursor = conn.execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            ("test-sub",),
        ).fetchone()["last_rowid"]
        assert cursor == head

    def test_mixed_batch_does_not_trigger_poison_guard(self, bus):
        """One valid event in the batch means the normal ack path owns the
        cursor — the guard must NOT advance it (the subscriber may still be
        processing and crash before ack)."""
        self._insert_unknown_row(bus, event_id="poison-mixed")
        bus.emit(EventType.CRON_STARTED, "scout", {})

        events = _subscribe(bus, "test-sub")
        assert len(events) == 1

        cursor = bus._get_conn().execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            ("test-sub",),
        ).fetchone()["last_rowid"]
        assert cursor == 0  # untouched until the caller acks

    def test_subscribe_logs_warning_for_unknown_event_type(self, bus, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="events.bus")
        self._insert_unknown_row(bus, event_type="some_future_type")
        _subscribe(bus,"test-sub")
        # The unknown event_type string and event_id should appear in logs so
        # operators can correlate to the producer.
        warning_msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("some_future_type" in m for m in warning_msgs), (
            f"Expected warning mentioning unknown event_type; got: {warning_msgs}"
        )


class TestCheckpointModes:
    def test_truncate_resets_wal_and_reports(self, tmp_path):
        bus = EventBus(tmp_path / "bus.db")
        for i in range(50):
            bus.emit(EventType.GATEWAY_STARTED, "test", {"i": i})
        wal = tmp_path / "bus.db-wal"
        assert wal.stat().st_size > 0
        result = bus.checkpoint("TRUNCATE")
        assert result is not None
        assert result[0] == 0            # busy flag clear - reset happened
        assert wal.stat().st_size == 0   # WAL truncated to zero bytes

    def test_invalid_mode_rejected(self, tmp_path):
        bus = EventBus(tmp_path / "bus.db")
        with pytest.raises(ValueError):
            bus.checkpoint("EVIL")

    def test_passive_default_returns_tuple(self, tmp_path):
        bus = EventBus(tmp_path / "bus.db")
        bus.emit(EventType.GATEWAY_STARTED, "test", {})
        result = bus.checkpoint()
        assert result is not None and len(result) == 3
