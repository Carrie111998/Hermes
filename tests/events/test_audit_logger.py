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


class TestAuditLogger:
    def test_logs_events_as_jsonl(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)
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

        bus.emit(EventType.CRON_STARTED, "scout", {})
        logger.poll()

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        logger.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_creates_parent_dirs(self, bus, tmp_path):
        deep_path = tmp_path / "a" / "b" / "c" / "audit.jsonl"
        logger = AuditLogger(bus, audit_path=deep_path)

        bus.emit(EventType.CRON_STARTED, "scout", {})
        logger.poll()

        assert deep_path.exists()

    def test_handles_no_events(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)
        count = logger.poll()
        assert count == 0
        assert not audit_path.exists()


class TestRotation:
    """Tests for weekly rotation and 90-day archive cleanup."""

    def test_rotation_moves_old_file_to_archive(self, bus, audit_path):
        import os
        import time as _time

        logger_inst = AuditLogger(bus, audit_path=audit_path)

        # Write some events first
        bus.emit(EventType.CRON_COMPLETED, "scout", {"jobs": 3})
        logger_inst.poll()
        assert audit_path.exists()

        # Fake the file age by setting mtime to 8 days ago
        old_mtime = _time.time() - (8 * 86400)
        os.utime(str(audit_path), (old_mtime, old_mtime))

        # Trigger rotation
        logger_inst._rotate_if_needed()

        # audit.jsonl should be gone (renamed to archive)
        assert not audit_path.exists()

        # Archive dir should contain the rotated file
        archive_dir = audit_path.parent / "audit"
        assert archive_dir.exists()
        archives = list(archive_dir.glob("audit-*.jsonl"))
        assert len(archives) == 1

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
        import os
        import time as _time

        logger_inst = AuditLogger(bus, audit_path=audit_path)

        # Write initial event
        bus.emit(EventType.CRON_COMPLETED, "scout", {"jobs": 1})
        logger_inst.poll()

        # Force rotation by aging the file
        old_mtime = _time.time() - (8 * 86400)
        os.utime(str(audit_path), (old_mtime, old_mtime))
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

    def test_size_cap_triggers_rotation_before_age(self, bus, audit_path):
        """If audit.jsonl exceeds SIZE_CAP_BYTES, rotate even if age < 7 days."""
        from events.subscribers.audit_logger import SIZE_CAP_BYTES

        sub = AuditLogger(bus, audit_path=audit_path)

        # Write enough bytes to exceed the cap.
        audit_path.write_text("x" * (SIZE_CAP_BYTES + 1024), encoding="utf-8")

        # Force a rotation check.
        sub._last_rotation_check = 0
        bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        # The rotated file should be in audit/ subdir; the live audit.jsonl is now
        # the small one written by the most recent handle() call.
        archive_dir = audit_path.parent / "audit"
        assert archive_dir.exists(), "audit/ archive dir should be created"
        rotated = list(archive_dir.glob("audit-*.jsonl"))
        assert len(rotated) == 1, f"expected exactly one rotated file, got {rotated}"
        assert rotated[0].stat().st_size > SIZE_CAP_BYTES, "rotated file should be the oversized one"

        # M4: same-day second oversized rotation must land as audit-{date}-1.jsonl
        # rather than overwriting the first archive file. Verifies the existing
        # collision-counter logic in _rotate_if_needed still works under the
        # new size-cap-triggered rotation cadence.
        audit_path.write_text("y" * (SIZE_CAP_BYTES + 1024), encoding="utf-8")
        sub._last_rotation_check = 0
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
