"""End-to-end: hermes cron run --reason flows through to JSONL log.

Spec: docs/superpowers/plans/2026-04-30-cron-trigger-traceability.md
"""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from cron.jobs import create_job
from events.bus import EventBus
from events.schema import EventType
from events.subscribers.cron_trigger_log import CronTriggerLog
from hermes_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _seed_cursor_at_zero(bus: EventBus, subscriber_id: str) -> None:
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES (?, 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
        (subscriber_id,),
    )


def test_cli_run_reaches_jsonl_log(tmp_cron_dir, monkeypatch, capsys):
    bus = EventBus(db_path=tmp_cron_dir / "events.db")
    log_path: Path = tmp_cron_dir / "events" / "cron_triggers.jsonl"
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    sub = CronTriggerLog(bus, log_path=log_path)
    _seed_cursor_at_zero(bus, sub.subscriber_id)

    job = create_job(prompt="x", schedule="every 1h")
    cron_command(
        Namespace(
            cron_command="run",
            job_id=job["id"],
            reason="integration test",
        )
    )

    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1

    sub.poll()
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event_type"] == "cron_triggered"
    assert rec["payload"]["caller"] == "hermes_cli:cron_run"
    assert rec["payload"]["reason"] == "integration test"
    assert rec["payload"]["job_id"] == job["id"]
    assert rec["payload"]["new_next_run_at"]


def test_anonymous_call_still_logs_to_jsonl(tmp_cron_dir, monkeypatch):
    """Even when no caller is supplied (legacy callers), the event is still logged.
    The warning is logged but the event records caller=None, which preserves
    audit-trail completeness even for un-instrumented call sites."""
    from cron.jobs import trigger_job

    bus = EventBus(db_path=tmp_cron_dir / "events.db")
    log_path: Path = tmp_cron_dir / "events" / "cron_triggers.jsonl"
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    sub = CronTriggerLog(bus, log_path=log_path)
    _seed_cursor_at_zero(bus, sub.subscriber_id)

    job = create_job(prompt="x", schedule="every 1h")
    trigger_job(job["id"])  # no caller

    sub.poll()
    rec = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert rec["payload"]["caller"] is None
