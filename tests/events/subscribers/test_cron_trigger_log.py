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


def test_age_does_not_trigger_rotation(bus, log_path):
    """Age alone must NOT rotate the live file — it is append-only.

    A weekly age arm existed f6c823e24..2026-07-13 but was dead code from
    birth: handle() appends (refreshing st_mtime) microseconds before every
    hourly-gated check, so age was always ~0 and no cron_triggers-* archive
    was ever produced. It was removed rather than fixed (AuditLogger
    precedent, edfed44c8) — the file grows ~KB/week and operators want the
    full fire history greppable in one place. If a legacy rotation hook is
    ever reintroduced, invoking it on a stale file must leave it in place.
    """
    sub = CronTriggerLog(bus, log_path=log_path)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"event_type":"cron_triggered"}\n', encoding="utf-8")

    thirty_days_ago = time.time() - 30 * 86400
    os.utime(log_path, (thirty_days_ago, thirty_days_ago))

    rotate = getattr(sub, "_rotate_if_needed", None)
    if rotate is not None:  # removed 2026-07-13; guards against reintroduction
        rotate()

    assert log_path.exists(), "age alone must not rotate the live file"
    archive_dir = log_path.parent / "audit"
    archives = list(archive_dir.glob("cron_triggers-*.jsonl")) if archive_dir.exists() else []
    assert archives == []


def test_poll_appends_to_aged_file_without_rotation(bus, log_path):
    """Production path: an event landing on a stale (30-day-old) live file
    appends to it in place — prior lines preserved, no archive created —
    even with the hourly cleanup gate forced open."""
    sub = CronTriggerLog(bus, log_path=log_path)
    _seed_cursor_at_zero(bus, sub.subscriber_id)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"event_type":"cron_triggered","marker":"old-line"}\n', encoding="utf-8"
    )
    thirty_days_ago = time.time() - 30 * 86400
    os.utime(log_path, (thirty_days_ago, thirty_days_ago))

    sub._last_cleanup_check = float("-inf")  # force the hourly gate open
    bus.emit(
        event_type=EventType.CRON_TRIGGERED,
        source="stale-file-test",
        payload={"job_id": "z9", "job_name": "stale-file-test", "caller": "test"},
        job_id="z9",
    )
    sub.poll()

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "append must land in the same file"
    assert json.loads(lines[0])["marker"] == "old-line"
    archive_dir = log_path.parent / "audit"
    archives = list(archive_dir.glob("cron_triggers-*.jsonl")) if archive_dir.exists() else []
    assert archives == []


def test_cleanup_removes_old_archives(bus, log_path):
    """Archives older than RETENTION_DAYS must be deleted.

    Nothing creates cron_triggers-*.jsonl archives anymore (the weekly
    age-rotation arm was removed 2026-07-13 — dead code from birth), but
    the retention sweep stays for anything manually placed in audit/."""
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
