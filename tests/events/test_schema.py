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


class TestCronTriggeredEventType:
    """The cron_triggered event records off-schedule fires (CLI / LLM / API
    callers of trigger_job). Caller-traceability spec — plan
    docs/superpowers/plans/2026-04-30-cron-trigger-traceability.md."""

    def test_cron_triggered_event_type_exists(self):
        assert EventType.from_string("cron_triggered") is EventType.CRON_TRIGGERED

    def test_cron_triggered_default_priority_is_low(self):
        assert EventType.CRON_TRIGGERED.default_priority is Priority.LOW


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


"""Tests for the EventType catalog -- added 2026-04-26 for the DevFlow bridge."""


def test_devflow_event_types_exist():
    assert EventType.DEVFLOW_RUN_STARTED.type_string == "devflow.run_started"
    assert EventType.DEVFLOW_RUN_COMPLETED.type_string == "devflow.run_completed"
    assert EventType.DEVFLOW_APPROVAL_REQUESTED.type_string == "devflow.approval_requested"
    assert EventType.DEVFLOW_TRACE_SNAPSHOT.type_string == "devflow.trace_snapshot"


def test_devflow_event_types_default_priorities():
    assert EventType.DEVFLOW_RUN_STARTED.default_priority == Priority.NORMAL
    assert EventType.DEVFLOW_RUN_COMPLETED.default_priority == Priority.NORMAL
    assert EventType.DEVFLOW_APPROVAL_REQUESTED.default_priority == Priority.HIGH
    assert EventType.DEVFLOW_TRACE_SNAPSHOT.default_priority == Priority.LOW


def test_devflow_event_types_round_trip_via_from_string():
    for et in (
        EventType.DEVFLOW_RUN_STARTED,
        EventType.DEVFLOW_RUN_COMPLETED,
        EventType.DEVFLOW_APPROVAL_REQUESTED,
        EventType.DEVFLOW_TRACE_SNAPSHOT,
    ):
        assert EventType.from_string(et.type_string) is et


# DevFlow PR + build event coverage — added 2026-04-30 so the
# devflow_firehose Telegram topic surfaces SDLC activity (PRs, builds)
# instead of just bridge ticks. Spec at
# docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.

def test_devflow_pr_event_types_exist():
    assert EventType.DEVFLOW_PR_OPENED.type_string == "devflow.pr_opened"
    assert EventType.DEVFLOW_PR_MERGED.type_string == "devflow.pr_merged"
    assert EventType.DEVFLOW_PR_CLOSED.type_string == "devflow.pr_closed"
    assert EventType.DEVFLOW_PR_REVIEW_REQUESTED.type_string == "devflow.pr_review_requested"


def test_devflow_build_event_types_exist():
    assert EventType.DEVFLOW_BUILD_STARTED.type_string == "devflow.build_started"
    assert EventType.DEVFLOW_BUILD_SUCCEEDED.type_string == "devflow.build_succeeded"
    assert EventType.DEVFLOW_BUILD_FAILED.type_string == "devflow.build_failed"


def test_devflow_pr_build_event_types_default_priorities():
    # Routine activity: NORMAL is default; high-stakes signals (merged,
    # review-requested, build failed) elevate to HIGH so significant_only
    # verbosity surfaces them. Build started is LOW so it batches.
    assert EventType.DEVFLOW_PR_OPENED.default_priority == Priority.NORMAL
    assert EventType.DEVFLOW_PR_MERGED.default_priority == Priority.HIGH
    assert EventType.DEVFLOW_PR_CLOSED.default_priority == Priority.NORMAL
    assert EventType.DEVFLOW_PR_REVIEW_REQUESTED.default_priority == Priority.HIGH
    assert EventType.DEVFLOW_BUILD_STARTED.default_priority == Priority.LOW
    assert EventType.DEVFLOW_BUILD_SUCCEEDED.default_priority == Priority.NORMAL
    assert EventType.DEVFLOW_BUILD_FAILED.default_priority == Priority.HIGH


def test_devflow_pr_build_event_types_round_trip_via_from_string():
    for et in (
        EventType.DEVFLOW_PR_OPENED,
        EventType.DEVFLOW_PR_MERGED,
        EventType.DEVFLOW_PR_CLOSED,
        EventType.DEVFLOW_PR_REVIEW_REQUESTED,
        EventType.DEVFLOW_BUILD_STARTED,
        EventType.DEVFLOW_BUILD_SUCCEEDED,
        EventType.DEVFLOW_BUILD_FAILED,
    ):
        assert EventType.from_string(et.type_string) is et


# Notification reverse-signal coverage — added 2026-04-30. Spec at
# docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
# Telegram + WhatsApp delivery emit these so audit + dashboards + a
# future retry router can see whether a notification reached the user.

def test_notification_delivery_event_types_exist():
    assert EventType.NOTIFICATION_DELIVERED.type_string == "notification_delivered"
    assert EventType.NOTIFICATION_FAILED.type_string == "notification_failed"


def test_notification_delivery_event_types_default_priorities():
    # Success is bus-only telemetry: LOW so the audit log absorbs it
    # without batching pressure on watchdog_alerts. Failure is NORMAL
    # so it surfaces in operator alerts (digest_only verbosity passes
    # NORMAL+ when paired with HIGH gate) without paging-tier escalation.
    assert EventType.NOTIFICATION_DELIVERED.default_priority == Priority.LOW
    assert EventType.NOTIFICATION_FAILED.default_priority == Priority.NORMAL


def test_notification_delivery_event_types_round_trip_via_from_string():
    for et in (EventType.NOTIFICATION_DELIVERED, EventType.NOTIFICATION_FAILED):
        assert EventType.from_string(et.type_string) is et
