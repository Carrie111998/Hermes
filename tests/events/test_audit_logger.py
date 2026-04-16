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
