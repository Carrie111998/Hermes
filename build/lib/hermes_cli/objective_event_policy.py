"""Deterministic admission policy for governed objective wake events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class EventAdmission:
    priority_class: str
    priority: int
    deadline_at: Optional[int] = None


def classify(event_type: str, payload: Any) -> EventAdmission:
    """Classify urgency from trusted event semantics, never planner preference."""
    event = event_type.strip().lower()
    data = payload if isinstance(payload, Mapping) else {}
    deadline = data.get("due_at")
    deadline_at = (
        int(deadline)
        if isinstance(deadline, int) and not isinstance(deadline, bool) and deadline > 0
        else None
    )
    if event == "compensation.required":
        return EventAdmission("critical", 100, deadline_at)
    if event in {
        "execution.failed",
        "objective.runtime.unhealthy",
        "payment.failed",
        "payment.disputed",
        "security.incident",
    }:
        return EventAdmission("critical", 95, deadline_at)
    if event == "compliance.deadline.approaching":
        return EventAdmission(
            "critical" if bool(data.get("overdue")) else "high",
            100 if bool(data.get("overdue")) else 90,
            deadline_at,
        )
    if event == "commitment.breached":
        return EventAdmission("critical", 98, deadline_at)
    if event == "commitment.deadline.approaching":
        return EventAdmission(
            "critical" if bool(data.get("overdue")) else "high",
            94 if bool(data.get("overdue")) else 82,
            deadline_at,
        )
    if event == "strategy.experiment.reviewed":
        verdict = str(data.get("verdict") or "")
        return EventAdmission(
            "high" if verdict in {"not_supported", "no_evidence"} else "normal",
            80 if verdict in {"not_supported", "no_evidence"} else 50,
        )
    if event == "strategy.metric_target.reviewed":
        verdict = str(data.get("verdict") or "")
        return EventAdmission(
            "high" if verdict in {"off_track", "no_evidence"} else "normal",
            80 if verdict in {"off_track", "no_evidence"} else 45,
        )
    if event in {
        "approval.granted",
        "approval.expired",
        "intervention.resolved",
        "external.content.reviewed",
        "objective.successor.required",
    }:
        return EventAdmission("high", 75, deadline_at)
    if event.startswith("kanban.task.") and event.endswith(("failed", "blocked")):
        return EventAdmission("high", 75, deadline_at)
    if event in {"objective.accepted", "execution.retry_due"}:
        return EventAdmission("normal", 60, deadline_at)
    if event.startswith(("agentmail.", "crm.", "payments.")):
        return EventAdmission("normal", 55, deadline_at)
    if event.startswith("kanban.task."):
        return EventAdmission("normal", 50, deadline_at)
    if event == "cycle.actions_verified":
        return EventAdmission("normal", 45, deadline_at)
    if event.startswith("schedule.") or event == "ceo.operating_review":
        return EventAdmission("routine", 25, deadline_at)
    return EventAdmission("normal", 50, deadline_at)
