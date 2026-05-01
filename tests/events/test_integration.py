"""Integration test — end-to-end event flow from emit to subscriber consumption."""

import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.cron_emitter import CronEventEmitter
from events.producers.health_monitor import GatewayHealthMonitor
from events.producers.mailbox_watcher import MailboxWatcher
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger


@pytest.fixture
def setup(tmp_path):
    """Create a complete event bus setup with producers and subscribers."""
    db_path = tmp_path / "events" / "event_bus.db"
    bus = EventBus(db_path=db_path)
    return {
        "bus": bus,
        "tmp_path": tmp_path,
        "emitter": CronEventEmitter(bus),
        "health": GatewayHealthMonitor(bus),
    }


def _seed_audit_logger_cursor(bus: EventBus) -> None:
    """Force AuditLogger's cursor to 0 so it sees events emitted BEFORE the
    first poll. The bus's first-registration default (bus.py subscribe(),
    2026-04-28) jumps to head-of-bus to prevent backlog floods on real
    deploys; these end-to-end tests construct AuditLogger and emit, then
    poll."""
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES ('audit-logger', 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
    )


class TestEndToEnd:
    def test_cron_emit_to_audit_log(self, setup):
        bus = setup["bus"]
        emitter = setup["emitter"]
        tmp_path = setup["tmp_path"]

        audit_path = tmp_path / "events" / "audit.jsonl"
        audit = AuditLogger(bus, audit_path=audit_path)
        _seed_audit_logger_cursor(bus)

        # Producer emits
        emitter.on_job_started("j1", "jobflow-scout", "0 8 * * *")
        emitter.on_job_completed("j1", "jobflow-scout", True, 120.0, "Found 8 jobs")

        # Subscriber consumes
        audit.poll()

        lines = audit_path.read_text().strip().split("\n")
        # 2 lifecycle events: cron_started, cron_completed.
        # Domain events (job_discovered, etc.) now come from MailboxTranslator
        # consuming mailbox_message events, not from regex-parsing agent output.
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "cron_started"
        assert json.loads(lines[1])["event_type"] == "cron_completed"

    def test_health_state_change_flow(self, setup):
        bus = setup["bus"]
        health = setup["health"]
        tmp_path = setup["tmp_path"]

        audit_path = tmp_path / "events" / "audit.jsonl"
        audit = AuditLogger(bus, audit_path=audit_path)
        _seed_audit_logger_cursor(bus)

        health.report_health("telegram", healthy=True)
        health.report_health("telegram", healthy=False)
        health.report_health("telegram", healthy=False)  # no event
        health.report_health("telegram", healthy=True)

        audit.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 3  # up, down, up (no duplicate down)

    def test_mailbox_to_event_flow(self, setup):
        bus = setup["bus"]
        tmp_path = setup["tmp_path"]

        mailbox = tmp_path / "mailbox" / "main" / "inbox"
        mailbox.mkdir(parents=True)

        msg = {"type": "SCORE_RESULT", "from": "matcher", "to": "main",
               "payload": {"score": 8.5}}
        (mailbox / "20260415T120000Z_SCORE_RESULT_matcher.json").write_text(
            json.dumps(msg), encoding="utf-8",
        )

        watcher = MailboxWatcher(bus, mailbox_root=tmp_path / "mailbox")
        watcher.scan()

        events = bus.query(event_type=EventType.MAILBOX_MESSAGE)
        assert len(events) == 1
        assert events[0].payload["message_type"] == "SCORE_RESULT"

    def test_full_registry_poll(self, setup):
        bus = setup["bus"]
        emitter = setup["emitter"]
        tmp_path = setup["tmp_path"]

        registry = SubscriberRegistry()
        audit = AuditLogger(bus, audit_path=tmp_path / "events" / "audit.jsonl")
        _seed_audit_logger_cursor(bus)
        registry.register(audit)

        emitter.on_job_started("j1", "scout", "0 8 * * *")
        emitter.on_job_completed("j1", "scout", True, 60.0, "5 jobs")
        emitter.on_job_completed("j2", "matcher", False, 10.0, error="timeout", consecutive_errors=3)

        results = registry.poll_all()
        assert results["audit-logger"] == 4  # started + completed + failed + consecutive
