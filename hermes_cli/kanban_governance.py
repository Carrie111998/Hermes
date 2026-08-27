"""Kanban governance contracts for blocked-card enforcement.

Validates typed blocker classes, decision classes, and operational rules
to ensure blocked cards follow governance invariants.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

# Required blocker classes for typed blocks
BLOCKER_CLASSES = {
    "infra_default",
    "lane_work",
    "review_fix",
    "dependency_wait",
    "human_decision",
}

# Thresholds for anomaly detection
REVIEW_LOOP_THRESHOLD = 4  # Defect after 4 review requests on the same task

# Valid decision classes for human_decision blocks
HUMAN_DECISION_CLASSES = {
    "scope_authorization",
    "irreversible_risk_acceptance",
    "policy_override",
    "principal_intent_ambiguity",
}

# Patterns that indicate an operational hold on Christopher
CHRISTOPHER_HOLD_PATTERNS = {
    "awaiting christopher",
    "held by christopher",
    "christopher",  # catch generic references
}


@dataclass(frozen=True)
class GovernanceBlockValidation:
    """Result of validating a block transition against governance rules."""
    ok: bool
    blocker_class: Optional[str] = None
    decision_class: Optional[str] = None
    error: Optional[str] = None


def validate_block_transition(
    *,
    reason: Optional[str] = None,
    blocker_class: Optional[str] = None,
    decision_class: Optional[str] = None,
) -> GovernanceBlockValidation:
    """Validate a block transition against governance invariants.

    Args:
        reason: The human-readable reason for blocking.
        blocker_class: One of BLOCKER_CLASSES or None (None is legacy-compatible).
        decision_class: One of HUMAN_DECISION_CLASSES (only valid when
            blocker_class is "human_decision").

    Returns:
        GovernanceBlockValidation with ok=True if validation passes,
        ok=False with error message otherwise.
    """
    # Rule 1: Detect operational hold on Christopher without human_decision
    # This is checked FIRST because it applies even when blocker_class is None.
    if reason:
        reason_lower = reason.lower()
        # Check for Christopher hold patterns
        mentions_christopher = any(
            pattern in reason_lower for pattern in CHRISTOPHER_HOLD_PATTERNS
        )
        if mentions_christopher and blocker_class != "human_decision":
            return GovernanceBlockValidation(
                ok=False,
                error=(
                    "Operational hold on Christopher requires human_decision classification. "
                    "Use --blocker-class human_decision with a --decision-class "
                    "(scope_authorization, irreversible_risk_acceptance, policy_override, or principal_intent_ambiguity)."
                ),
            )

    # Rule 2: If blocker_class is explicitly provided, it must be valid
    if blocker_class is not None and blocker_class not in BLOCKER_CLASSES:
        return GovernanceBlockValidation(
            ok=False,
            error=f"Invalid blocker class '{blocker_class}' — must be one of: {', '.join(sorted(BLOCKER_CLASSES))}",
        )

    # Rule 3: human_decision requires a decision_class
    if blocker_class == "human_decision" and not decision_class:
        return GovernanceBlockValidation(
            ok=False,
            error=(
                "human_decision blocks require a decision_class "
                "(scope_authorization, irreversible_risk_acceptance, policy_override, or principal_intent_ambiguity)"
            ),
        )

    # Rule 4: decision_class, if provided, must be valid and only for human_decision
    if decision_class:
        if decision_class not in HUMAN_DECISION_CLASSES:
            return GovernanceBlockValidation(
                ok=False,
                error=f"Invalid decision class '{decision_class}' — must be one of: {', '.join(sorted(HUMAN_DECISION_CLASSES))}",
            )
        if blocker_class != "human_decision":
            return GovernanceBlockValidation(
                ok=False,
                error="decision_class is only valid with blocker_class=human_decision",
            )

    return GovernanceBlockValidation(
        ok=True,
        blocker_class=blocker_class,
        decision_class=decision_class,
    )


@dataclass(frozen=True)
class GovernanceReviewValidation:
    """Result of validating a review-entry transition against governance rules."""

    ok: bool
    error: Optional[str] = None


def validate_review_contract(
    *,
    goal: Optional[str] = None,
    judge: Optional[str] = None,
    evidence_contract: Optional[str] = None,
) -> GovernanceReviewValidation:
    """Validate a review-entry transition against governance invariants.

    A valid review entry requires all three of:
    - goal: what the implementation must achieve
    - judge: who (or what automated process) will review it
    - evidence_contract: what pass/fail evidence demonstrates success

    For backwards compatibility, if ALL three are None (not provided), the
    validation passes and no contract is enforced. However, if ANY of the three
    are provided, then ALL three must be non-empty strings.

    Args:
        goal: The review goal (required if any contract field is provided, must not be empty).
        judge: The reviewer or judge role (required if any contract field is provided, must not be empty).
        evidence_contract: The pass/fail evidence contract (required if any contract field is provided, must not be empty).

    Returns:
        GovernanceReviewValidation with ok=True if validation passes,
        ok=False with error message otherwise.
    """
    # Legacy compatibility: if all three are None, this is a backwards-compatible call
    # from old code that doesn't pass these parameters. Accept it.
    if goal is None and judge is None and evidence_contract is None:
        return GovernanceReviewValidation(ok=True)

    # If ANY field is provided, ALL three must be provided and non-empty
    # Check goal
    if not goal or not (isinstance(goal, str) and goal.strip()):
        return GovernanceReviewValidation(
            ok=False,
            error="Review requires a 'goal' describing what the implementation must achieve.",
        )

    # Check judge
    if not judge or not (isinstance(judge, str) and judge.strip()):
        return GovernanceReviewValidation(
            ok=False,
            error="Review requires a 'judge' (reviewer or automated process).",
        )

    # Check evidence_contract
    if (
        not evidence_contract
        or not (isinstance(evidence_contract, str) and evidence_contract.strip())
    ):
        return GovernanceReviewValidation(
            ok=False,
            error="Review requires an 'evidence_contract' describing pass/fail criteria.",
        )

    return GovernanceReviewValidation(ok=True)


@dataclass(frozen=True)
class GovernanceAnomaly:
    """Detected pathological governance loop requiring escalation."""
    kind: str  # "repeat_review_loop", "illegal_christopher_hold", etc.
    reason: str  # Human-readable description
    task_id: str
    counter_value: int  # The actual counter value that triggered detection


def classify_governance_anomaly(
    task: Any,  # Task object with governance counters
    recent_events: Optional[list] = None,
) -> Optional[GovernanceAnomaly]:
    """Detect governance anomalies that warrant escalation to maintainer defect.
    
    Args:
        task: Task object with governance_review_count and other counters.
        recent_events: Optional list of recent task events (for context).
    
    Returns:
        GovernanceAnomaly if an anomaly is detected, None otherwise.
    """
    # Detect repeat review loop: task has exceeded the threshold for review cycles
    if task.governance_review_count >= REVIEW_LOOP_THRESHOLD:
        return GovernanceAnomaly(
            kind="repeat_review_loop",
            reason=f"Task has been in review {task.governance_review_count} times (threshold: {REVIEW_LOOP_THRESHOLD})",
            task_id=task.id,
            counter_value=task.governance_review_count,
        )
    
    return None


def materialize_maintainer_defect(
    conn: sqlite3.Connection,
    source_task_id: str,
    anomaly: GovernanceAnomaly,
) -> str:
    """Convert a detected governance anomaly into a maintainer defect card.
    
    Creates a new task assigned to 'default' with a title describing the
    governance defect found on the source task.
    
    Args:
        conn: Database connection.
        source_task_id: The task ID that triggered the anomaly detection.
        anomaly: The detected GovernanceAnomaly.
    
    Returns:
        The ID of the newly created defect task.
    """
    # Import here to avoid circular dependency
    from hermes_cli import kanban_db as kb
    
    # Determine defect title and body based on anomaly kind
    if anomaly.kind == "repeat_review_loop":
        defect_title = f"Governance defect: review loop on {source_task_id}"
        defect_body = (
            f"Task {source_task_id} has entered review {anomaly.counter_value} times, "
            f"exceeding threshold of {REVIEW_LOOP_THRESHOLD}.\n\n"
            f"Reason: {anomaly.reason}\n\n"
            f"This may indicate:\n"
            f"- The review goal/judge/evidence_contract is misaligned with implementation\n"
            f"- The implementation path is fundamentally flawed and needs decomposition\n"
            f"- The review criteria are ambiguous or keep changing\n"
        )
    else:
        defect_title = f"Governance defect: {anomaly.kind} on {source_task_id}"
        defect_body = f"Anomaly: {anomaly.reason}"
    
    # Create the defect task assigned to default
    defect_id = kb.create_task(
        conn,
        title=defect_title,
        body=defect_body,
        assignee="default",
    )
    
    return defect_id

