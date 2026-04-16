"""Tests for events.schema — Event dataclass, EventType enum, Priority enum."""

import json
from datetime import datetime, timezone

from events.schema import Event, EventType, Priority


class TestPriority:
    def test_ordering(self):
        assert Priority.CRITICAL.level > Priority.HIGH.level
        assert Priority.HIGH.level > Priority.NORMAL.level
        assert Priority.NORMAL.level > Priority.LOW.level

    def test_from_string(self):
        assert Priority.from_string("critical") == Priority.CRITICAL
        assert Priority.from_string("HIGH") == Priority.HIGH
        assert Priority.from_string("Normal") == Priority.NORMAL
        assert Priority.from_string("low") == Priority.LOW
        assert Priority.from_string("unknown") == Priority.NORMAL  # fallback


class TestEventType:
    def test_all_catalog_types_exist(self):
        expected = [
            "cron_started", "cron_completed", "cron_failed", "cron_failed_consecutive",
            "job_discovered", "job_scored", "job_high_score", "job_vip_discovered",
            "tailor_completed", "application_ready", "application_submitted",
            "application_failed", "application_blocked",
            "stage_transition", "interview_signal", "offer_signal", "followup_due",
            "digest_generated", "gateway_health", "agent_error",
            "memory_consolidated", "skill_evolved", "mailbox_message",
        ]
        for name in expected:
            assert hasattr(EventType, name.upper()), f"Missing EventType.{name.upper()}"

    def test_default_priority(self):
        assert EventType.CRON_STARTED.default_priority == Priority.LOW
        assert EventType.CRON_FAILED.default_priority == Priority.HIGH
        assert EventType.INTERVIEW_SIGNAL.default_priority == Priority.CRITICAL
        assert EventType.JOB_SCORED.default_priority == Priority.NORMAL


class TestEvent:
    def test_create_minimal(self):
        event = Event.create(
            event_type=EventType.CRON_COMPLETED,
            source="scout",
            payload={"duration": 42.5},
        )
        assert event.event_type == EventType.CRON_COMPLETED
        assert event.source == "scout"
        assert event.priority == Priority.NORMAL  # default for cron_completed
        assert event.payload == {"duration": 42.5}
        assert event.event_id  # UUID generated
        assert event.timestamp  # Timestamp generated

    def test_create_with_overrides(self):
        event = Event.create(
            event_type=EventType.JOB_SCORED,
            source="matcher",
            payload={"score": 9.1},
            priority=Priority.HIGH,
            correlation_id="abc-123",
            job_id="ext-456",
            tags=["vip"],
        )
        assert event.priority == Priority.HIGH
        assert event.correlation_id == "abc-123"
        assert event.job_id == "ext-456"
        assert event.tags == ["vip"]

    def test_to_dict_roundtrip(self):
        event = Event.create(
            event_type=EventType.APPLICATION_SUBMITTED,
            source="applier",
            payload={"company": "Acme"},
            job_id="job-1",
            tags=["jobflow"],
        )
        d = event.to_dict()
        restored = Event.from_dict(d)
        assert restored.event_id == event.event_id
        assert restored.event_type == event.event_type
        assert restored.source == event.source
        assert restored.payload == event.payload
        assert restored.job_id == event.job_id
        assert restored.tags == event.tags

    def test_to_dict_is_json_serializable(self):
        event = Event.create(
            event_type=EventType.CRON_STARTED,
            source="scout",
            payload={"key": "value"},
        )
        json_str = json.dumps(event.to_dict())
        assert json_str  # No serialization error
