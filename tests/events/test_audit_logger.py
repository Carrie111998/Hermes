"""Tests for events.subscribers.audit_logger — JSONL audit trail."""

import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.audit_logger import AuditLogger


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "events" / "audit.jsonl"


def _seed_audit_logger_cursor(bus: EventBus) -> None:
    """Force AuditLogger's cursor to 0 so it sees events emitted BEFORE the
    first poll. The bus's first-registration default (bus.py subscribe(),
    2026-04-28) jumps to head-of-bus to prevent backlog floods on real
    deploys; these tests construct AuditLogger and emit, then poll."""
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES ('audit-logger', 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
    )


class TestAuditLogger:
    def test_logs_events_as_jsonl(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)
        _seed_audit_logger_cursor(bus)
        bus.emit(EventType.CRON_COMPLETED, "scout", {"jobs": 5})
        bus.emit(EventType.JOB_HIGH_SCORE, "matcher", {"score": 9.1})

        logger.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["event_type"] == "cron_completed"
        assert entry1["source"] == "scout"
        assert entry1["payload"] == {"jobs": 5}

        entry2 = json.loads(lines[1])
        assert entry2["event_type"] == "job_high_score"

    def test_appends_to_existing_file(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)
        _seed_audit_logger_cursor(bus)

        bus.emit(EventType.CRON_STARTED, "scout", {})
        logger.poll()

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        logger.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_creates_parent_dirs(self, bus, tmp_path):
        deep_path = tmp_path / "a" / "b" / "c" / "audit.jsonl"
        logger = AuditLogger(bus, audit_path=deep_path)
        _seed_audit_logger_cursor(bus)

        bus.emit(EventType.CRON_STARTED, "scout", {})
        logger.poll()

        assert deep_path.exists()

    def test_handles_no_events(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)
        count = logger.poll()
        assert count == 0
        assert not audit_path.exists()


def _write_oversized(path: Path) -> None:
    """Create a file just over SIZE_CAP_BYTES without building a 256 MiB
    string in memory: seek past the cap and write one byte (the filesystem
    extends with zeros). st_size is all the size-cap arm looks at."""
    from events.subscribers.audit_logger import SIZE_CAP_BYTES

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.seek(SIZE_CAP_BYTES + 1024)
        f.write(b"\n")


class TestRotation:
    """Tests for the emergency size-cap backstop and 90-day archive cleanup.

    Live-file rotation is OWNED by the external audit-rotate cron
    (~/.hermes/scripts/audit_rotate.py, daily tail-preserving trim into
    per-day archives); in-gateway only the 256 MiB size cap remains, as a
    backstop. The weekly age arm was removed 2026-07-13 — dead code from
    birth (handle() refreshed st_mtime microseconds before every check).
    """

    def test_age_does_not_trigger_rotation(self, bus, audit_path):
        """A stale-mtime file under the size cap must NOT rotate: in-gateway
        rotation bare-renames the file, emptying the live tail that
        curator/critic_retro/scribe consumers read (they need 7-14 days)."""
        import os
        import time as _time

        logger_inst = AuditLogger(bus, audit_path=audit_path)
        _seed_audit_logger_cursor(bus)

        bus.emit(EventType.CRON_COMPLETED, "scout", {"jobs": 3})
        logger_inst.poll()
        assert audit_path.exists()

        # 30 days old — far beyond the removed ROTATION_INTERVAL (7 days).
        old_mtime = _time.time() - (30 * 86400)
        os.utime(str(audit_path), (old_mtime, old_mtime))

        logger_inst._rotate_if_needed()

        assert audit_path.exists(), "age alone must not rotate the live file"
        archive_dir = audit_path.parent / "audit"
        archives = list(archive_dir.glob("audit-*.jsonl")) if archive_dir.exists() else []
        assert archives == []

    def test_cleanup_removes_old_archives(self, bus, audit_path):
        import os
        import time as _time

        logger_inst = AuditLogger(bus, audit_path=audit_path)

        # Create archive directory with an old file
        archive_dir = audit_path.parent / "audit"
        archive_dir.mkdir(parents=True, exist_ok=True)

        old_file = archive_dir / "audit-2025-01-01.jsonl"
        old_file.write_text('{"event_type":"cron_completed"}\n')
        # Set mtime to 100 days ago (beyond 90-day retention)
        old_mtime = _time.time() - (100 * 86400)
        os.utime(str(old_file), (old_mtime, old_mtime))

        recent_file = archive_dir / "audit-2026-04-10.jsonl"
        recent_file.write_text('{"event_type":"cron_completed"}\n')

        # Run cleanup
        logger_inst._cleanup_old_archives()

        # Old file removed, recent file kept
        assert not old_file.exists()
        assert recent_file.exists()

    def test_new_events_written_after_rotation(self, bus, audit_path):
        logger_inst = AuditLogger(bus, audit_path=audit_path)
        _seed_audit_logger_cursor(bus)

        # Force the size-cap backstop — the only in-gateway rotation trigger.
        _write_oversized(audit_path)
        logger_inst._rotate_if_needed()

        assert not audit_path.exists()

        # Write new event after rotation
        bus.emit(EventType.JOB_HIGH_SCORE, "matcher", {"score": 9.5})
        logger_inst.poll()

        # Fresh audit.jsonl should exist with the new event
        assert audit_path.exists()
        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event_type"] == "job_high_score"

    def test_size_cap_triggers_rotation(self, bus, audit_path):
        """The emergency backstop: audit.jsonl over SIZE_CAP_BYTES rotates
        wholesale into audit/. Only reachable if the audit-rotate cron has
        been broken for weeks, but it must still work when that day comes."""
        from events.subscribers.audit_logger import SIZE_CAP_BYTES

        sub = AuditLogger(bus, audit_path=audit_path)
        _seed_audit_logger_cursor(bus)

        # Build a file exceeding the cap.
        _write_oversized(audit_path)

        # Force a rotation check. Use -inf so the gate (`now - last > 3600`)
        # always fires regardless of how recently `time.monotonic()`'s clock
        # was reset (fresh CI VM, fresh boot — see 2026-05-02 follow-up).
        sub._last_rotation_check = float("-inf")
        bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        # handle() appends the event line first, then the size check fires:
        # the oversized file (plus that line) lands in the audit/ subdir.
        archive_dir = audit_path.parent / "audit"
        assert archive_dir.exists(), "audit/ archive dir should be created"
        rotated = list(archive_dir.glob("audit-*.jsonl"))
        assert len(rotated) == 1, f"expected exactly one rotated file, got {rotated}"
        assert rotated[0].stat().st_size > SIZE_CAP_BYTES, "rotated file should be the oversized one"

        # M4: same-day second oversized rotation must land as audit-{date}-1.jsonl
        # rather than overwriting the first archive file. Verifies the
        # collision-counter logic in _rotate_if_needed still works when the
        # size-cap backstop fires twice in one day.
        _write_oversized(audit_path)
        sub._last_rotation_check = float("-inf")
        bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        rotated = sorted(archive_dir.glob("audit-*.jsonl"))
        assert len(rotated) == 2, f"expected 2 rotated files after second oversized rotation, got {rotated}"
        # Both files should be sized > SIZE_CAP_BYTES (the live audit.jsonl
        # was huge in both rotations).
        for f in rotated:
            assert f.stat().st_size > SIZE_CAP_BYTES, \
                f"{f.name} should be above size cap"
        # The second file's name should have the collision suffix.
        assert "-1.jsonl" in rotated[1].name or "-1.jsonl" in rotated[0].name, \
            f"one file should have collision suffix, got {[r.name for r in rotated]}"



class TestStartupCursorSeed:
    """AuditLogger.startup() seeds the cursor row at last_rowid=0 so the
    operator-facing audit trail captures every event — including those
    emitted between gateway startup and AuditLogger's first non-empty poll.

    Without the seed, bus.subscribe()'s first-call default (per
    2026-04-28 scribe backlog-flood fix) jumps to MAX(rowid) = current
    bus head and silently skips those early events.
    """

    def test_startup_seeds_cursor_at_zero_when_missing(self, bus, audit_path):
        logger_inst = AuditLogger(bus, audit_path=audit_path)

        logger_inst.startup()

        conn = bus._get_conn()
        row = conn.execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            ("audit-logger",),
        ).fetchone()
        assert row is not None
        assert row["last_rowid"] == 0

    def test_startup_does_not_overwrite_existing_cursor(self, bus, audit_path):
        # Simulate a gateway restart with persistent state: cursor row
        # already exists at last_rowid=42 from a prior session.
        conn = bus._get_conn()
        conn.execute(
            "INSERT INTO subscriber_cursors (subscriber_id, last_rowid) VALUES (?, 42)",
            ("audit-logger",),
        )
        conn.commit()

        logger_inst = AuditLogger(bus, audit_path=audit_path)
        logger_inst.startup()

        row = conn.execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            ("audit-logger",),
        ).fetchone()
        assert row["last_rowid"] == 42

    def test_audit_picks_up_events_emitted_before_first_poll(self, bus, audit_path):
        # Events emitted BEFORE AuditLogger has ever polled — exactly the
        # gateway-startup-to-first-poll window the seed protects.
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        logger_inst = AuditLogger(bus, audit_path=audit_path)
        logger_inst.startup()
        logger_inst.poll()

        assert audit_path.exists()
        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "cron_started"
        assert json.loads(lines[1])["event_type"] == "cron_completed"
