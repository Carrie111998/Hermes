"""Failure-wins outcome normalization contract."""

import pytest

from events.outcomes import (
    FailureKind,
    OutcomeState,
    evaluate_outcome,
    marker_for_verdict,
)
from events.schema import Event, EventType, Priority


def _event(
    payload: dict,
    event_type: EventType = EventType.AGENT_ITERATION,
    priority: Priority = Priority.LOW,
) -> Event:
    return Event.create(
        event_type=event_type,
        source="test",
        payload=payload,
        priority=priority,
    )


@pytest.mark.parametrize(
    ("payload", "state", "kind", "evidence_path"),
    [
        (
            {"counters": {"exit_code": 1}, "reason": "success"},
            OutcomeState.FAILED,
            FailureKind.OTHER,
            "payload.counters.exit_code",
        ),
        (
            {"exit_code": 2},
            OutcomeState.FAILED,
            FailureKind.OTHER,
            "payload.exit_code",
        ),
        (
            {"message_type": "ERROR", "message": "connection refused"},
            OutcomeState.FAILED,
            FailureKind.CONNECTION,
            "payload.message_type",
        ),
        (
            {"status": "FAILED", "error": "validation rejected"},
            OutcomeState.FAILED,
            FailureKind.VALIDATION,
            "payload.status",
        ),
        (
            {"timeout": True, "phase": "whole_suite"},
            OutcomeState.FAILED,
            FailureKind.TIMEOUT,
            "payload.timeout",
        ),
        (
            {"result": "timed_out", "phase": "browser_navigation"},
            OutcomeState.FAILED,
            FailureKind.TIMEOUT,
            "payload.result",
        ),
        (
            {"reason": "partial"},
            OutcomeState.DEGRADED,
            None,
            "payload.reason",
        ),
        (
            {"status": "degraded"},
            OutcomeState.DEGRADED,
            None,
            "payload.status",
        ),
        (
            {"status": "pending"},
            OutcomeState.PENDING,
            None,
            "payload.status",
        ),
        (
            {"decision_required": True},
            OutcomeState.PENDING,
            None,
            "payload.decision_required",
        ),
        (
            {"status": "healthy", "before": "down"},
            OutcomeState.RECOVERED,
            None,
            "payload.status",
        ),
        (
            {"reason": "success", "counters": {"exit_code": 0}},
            OutcomeState.SUCCEEDED,
            None,
            "payload.reason",
        ),
        (
            {"status": "up"},
            OutcomeState.SUCCEEDED,
            None,
            "payload.status",
        ),
        (
            {"summary": "ordinary telemetry"},
            OutcomeState.UNKNOWN,
            None,
            "event",
        ),
    ],
)
def test_evidence_shapes(payload, state, kind, evidence_path):
    verdict = evaluate_outcome(_event(payload))

    assert verdict.state is state
    assert verdict.failure_kind is kind
    assert any(item.path == evidence_path for item in verdict.evidence)


def test_failure_outranks_success_and_recovery():
    verdict = evaluate_outcome(
        _event(
            {
                "reason": "success",
                "status": "healthy",
                "before": "down",
                "counters": {"exit_code": 1},
            }
        )
    )

    assert verdict.state is OutcomeState.FAILED
    assert any(
        item.path == "payload.counters.exit_code" for item in verdict.evidence
    )


def test_degraded_outranks_pending_and_success():
    verdict = evaluate_outcome(
        _event(
            {
                "result": "partial",
                "status": "pending",
                "reason": "success",
            }
        )
    )

    assert verdict.state is OutcomeState.DEGRADED


@pytest.mark.parametrize(
    ("event_type", "state"),
    [
        (EventType.CRON_FAILED, OutcomeState.FAILED),
        (EventType.APPLICATION_FAILED, OutcomeState.FAILED),
        (EventType.AGENT_FAILURE_CLUSTER, OutcomeState.FAILED),
        (EventType.NOTIFICATION_FAILED, OutcomeState.FAILED),
        (EventType.CRON_STALE, OutcomeState.DEGRADED),
        (EventType.RESOURCE_PRESSURE, OutcomeState.DEGRADED),
        (EventType.WATCHDOG_SELF_DEGRADED, OutcomeState.DEGRADED),
        (EventType.APPROVAL_REQUEST, OutcomeState.PENDING),
        (EventType.APPLICATION_BLOCKED, OutcomeState.PENDING),
        (EventType.CRON_COMPLETED, OutcomeState.SUCCEEDED),
        (EventType.APPLICATION_SUBMITTED, OutcomeState.SUCCEEDED),
        (EventType.DEVFLOW_BUILD_SUCCEEDED, OutcomeState.SUCCEEDED),
        (EventType.NOTIFICATION_DELIVERED, OutcomeState.SUCCEEDED),
        (EventType.GATEWAY_STARTED, OutcomeState.RECOVERED),
        (EventType.WATCHDOG_RECOVERED, OutcomeState.RECOVERED),
    ],
)
def test_explicit_event_types_are_outcome_evidence(event_type, state):
    verdict = evaluate_outcome(_event({}, event_type=event_type))

    assert verdict.state is state
    assert any(item.path == "event.event_type" for item in verdict.evidence)


def test_cron_completed_legacy_failure_marker_is_compatibility_evidence():
    verdict = evaluate_outcome(
        _event(
            {"output_summary": "NIGHTLY GATE: RED - pytest rc=124 after 2700s"},
            event_type=EventType.CRON_COMPLETED,
            priority=Priority.NORMAL,
        )
    )

    assert verdict.state is OutcomeState.FAILED
    assert verdict.failure_kind is FailureKind.TIMEOUT
    assert any(item.path == "payload.output_summary" for item in verdict.evidence)


def test_failure_and_degradation_have_high_priority_floor():
    assert evaluate_outcome(_event({"exit_code": 1})).priority_floor is Priority.HIGH
    assert evaluate_outcome(_event({"status": "partial"})).priority_floor is Priority.HIGH


@pytest.mark.parametrize(
    ("payload", "priority", "expected"),
    [
        ({"exit_code": 1}, Priority.CRITICAL, "🔴"),
        ({"exit_code": 1}, Priority.HIGH, "🟠"),
        ({"status": "partial"}, Priority.HIGH, "🟠"),
        ({"status": "pending"}, Priority.HIGH, "🟡"),
        ({"status": "healthy"}, Priority.HIGH, "🟢"),
        ({"reason": "success"}, Priority.NORMAL, "🟢"),
        ({"summary": "tick"}, Priority.LOW, "🟡"),
    ],
)
def test_marker_table(payload, priority, expected):
    verdict = evaluate_outcome(_event(payload, priority=priority))

    assert marker_for_verdict(verdict, priority) == expected
