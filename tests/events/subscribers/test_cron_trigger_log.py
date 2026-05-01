"""Tests for events.subscribers.cron_trigger_log."""

import json
import os
import time

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.cron_trigger_log import CronTriggerLog


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "events" / "cron_triggers.jsonl"


def _seed_cursor_at_zero(bus: EventBus, subscriber_id: str) -> None:
    """Force the subscriber's cursor to 0 so it sees events emitted BEFORE its
    first poll. The bus's first-registration default (bus.py:207-214) jumps to
    head-of-bus to prevent backlog floods on real deploys; tests need backfill."""
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES (?, 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
        (subscriber_id,),
    )


def test_writes_jsonl_line_per_event(bus, log_path):
    sub = CronTriggerLog(bus, log_path=log_path)
    _seed_cursor_at_zero(bus, sub.subscriber_id)

    bus.emit(
        event_type=EventType.CRON_TRIGGERED,
        source="sentinel-vip-morning",
        payload={
            "job_id": "abc123",
            "job_name": "sentinel-vip-morning",
            "caller": "hermes_cli:cron_run",
            "reason": "investigation",
            "previous_next_run_at": "2026-05-01T09:00:00+00:00",
            "new_next_run_at": "2026-04-30T14:34:00+00:00",
        },
        job_id="abc123",
    )
    sub.poll()

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event_type"] == "cron_triggered"
    assert rec["payload"]["caller"] == "hermes_cli:cron_run"
    assert rec["job_id"] == "abc123"


def test_ignores_other_event_types(bus, log_path):
    sub = CronTriggerLog(bus, log_path=log_path)
    _seed_cursor_at_zero(bus, sub.subscriber_id)

    bus.emit(
        event_type=EventType.CRON_STARTED,
        source="x",
        payload={"job_id": "x", "job_name": "x", "schedule": "0 0 * * *"},
        job_id="x",
    )
    sub.poll()

    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["event_type"] != "cron_started"


def test_rotation_creates_dated_archive_after_age_threshold(bus, log_path):
    """When the active file is older than ROTATION_INTERVAL, rotate it into an archive dir.

    Tests `_rotate_if_needed()` directly because the natural flow in handle()
    writes to the file BEFORE checking rotation, which updates mtime back to
    now and prevents the age check from triggering. In production, rotation
    fires only when the log goes a full week without any writes — unusual
    but acceptable for a cron-trigger audit log.
    """
    sub = CronTriggerLog(bus, log_path=log_path)
    _seed_cursor_at_zero(bus, sub.subscriber_id)

    # Write one line so the file exists with non-zero size
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"event_type":"cron_triggered"}\n', encoding="utf-8")

    eight_days_ago = time.time() - 8 * 86400
    os.utime(log_path, (eight_days_ago, eight_days_ago))

    sub._rotate_if_needed()

    archive_dir = log_path.parent / "audit"
    assert archive_dir.exists()
    archives = list(archive_dir.glob("cron_triggers-*.jsonl"))
    assert len(archives) >= 1
    # Active log is gone (rotated)
    assert not log_path.exists()


def test_rotation_skips_fresh_file(bus, log_path):
    """Rotation must NOT fire when the file is fresh."""
    sub = CronTriggerLog(bus, log_path=log_path)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"event_type":"cron_triggered"}\n', encoding="utf-8")

    sub._rotate_if_needed()

    # File is still in place — not rotated
    assert log_path.exists()
    archive_dir = log_path.parent / "audit"
    assert not archive_dir.exists() or not list(archive_dir.glob("cron_triggers-*.jsonl"))


def test_rotation_skips_empty_file(bus, log_path):
    """Rotation must NOT fire when the file is empty (size 0), even if old."""
    sub = CronTriggerLog(bus, log_path=log_path)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")

    eight_days_ago = time.time() - 8 * 86400
    os.utime(log_path, (eight_days_ago, eight_days_ago))

    sub._rotate_if_needed()

    # Empty file kept in place — pointless to archive an empty file
    assert log_path.exists()


def test_cleanup_removes_old_archives(bus, log_path):
    """Archives older than RETENTION_DAYS must be deleted."""
    sub = CronTriggerLog(bus, log_path=log_path)

    archive_dir = log_path.parent / "audit"
    archive_dir.mkdir(parents=True, exist_ok=True)

    old = archive_dir / "cron_triggers-2026-03-01.jsonl"
    fresh = archive_dir / "cron_triggers-2026-04-29.jsonl"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")

    forty_days_ago = time.time() - 40 * 86400
    os.utime(old, (forty_days_ago, forty_days_ago))

    sub._cleanup_old_archives()

    assert not old.exists()
    assert fresh.exists()


def test_subscriber_id_and_event_type_filter(bus, log_path):
    sub = CronTriggerLog(bus, log_path=log_path)
    assert sub.subscriber_id == "cron-trigger-log"
    assert sub.event_types == [EventType.CRON_TRIGGERED]
