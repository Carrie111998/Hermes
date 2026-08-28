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

# First-class duplicate resolution kinds
DUPLICATE_RESOLUTION_KINDS = {
    "duplicate_of",  # Task is a duplicate of another
    "intentional_sibling_of",  # Task is intentionally related but distinct
    "distinct",  # Task is distinct despite similarity
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


def classify_blocker(
    *,
    reason: str,
    task_title: str = "",
    task_body: str = "",
    attachments_checked: bool = False,
    parents_checked: bool = False,
    manuals_checked: bool = False,
    sessions_checked: bool = False,
    has_existing_artifact: bool = False,
) -> str:
    """Return one of BLOCKER_CLASSES from board-native evidence.

    Check-order flags are accepted so callers can record the context ladder
    they actually walked. They do not, by themselves, force a class.
    """
    del attachments_checked, parents_checked, manuals_checked, sessions_checked
    text = f"{task_title}\n{task_body}\n{reason}".lower()
    if "christopher" in text:
        return "human_decision"
    if has_existing_artifact or "parent" in text or "attachment" in text:
        return "dependency_wait"
    if "review" in text and "again" in text:
        return "review_fix"
    if "hook" in text or "dispatcher" in text or "routing" in text:
        return "infra_default"
    return "lane_work"


def build_exception_packet(
    task_id: str,
    board: str,
    blocker_class: str,
    reason: str,
    checks: list[str],
    decision_prompt: str,
) -> str:
    """Decision-shaped exception packet. Not a research replay."""
    checks_md = "\n".join(f"- {item}" for item in checks)
    return (
        f"# Kanban exception for {task_id}\n\n"
        f"Board: {board}\n"
        f"Blocker class: {blocker_class}\n"
        f"Reason: {reason}\n\n"
        f"Checks already performed:\n{checks_md}\n\n"
        f"Decision needed: {decision_prompt}\n"
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


def _defect_fingerprint(source_task_id: str, anomaly: GovernanceAnomaly) -> str:
    return f"{source_task_id}:{anomaly.kind}"


def _defect_title(source_task_id: str, anomaly: GovernanceAnomaly) -> str:
    if anomaly.kind == "repeat_review_loop":
        return f"Governance defect: review loop on {source_task_id}"
    return f"Governance defect: {anomaly.kind} on {source_task_id}"


def _existing_maintainer_defect(
    conn: sqlite3.Connection,
    source_task_id: str,
    anomaly: GovernanceAnomaly,
) -> Optional[str]:
    """Return an open default-owned defect for this source+kind, or None.

    request_review previously called materialize_maintainer_defect on every
    increment past the threshold. t_a0723481 minted t_a93d6dc8 at count 4
    and t_f21333d5 at count 5. Scan-path _ensure_maintainer_defect was
    fingerprint-idempotent; this lookup is the shared fence.
    Live cards (t_a93d6dc8) had no fingerprint line — title match still
    counts as the same defect.
    """
    from hermes_cli import kanban_db as kb

    fingerprint = _defect_fingerprint(source_task_id, anomaly)
    expected_title = _defect_title(source_task_id, anomaly)
    for task in kb.list_tasks(conn, assignee="default"):
        if task.status in {"archived", "cancelled"}:
            continue
        body = task.body or ""
        title = task.title or ""
        if f"fingerprint: {fingerprint}" in body:
            return task.id
        if title == expected_title:
            return task.id
    return None


def materialize_maintainer_defect(
    conn: sqlite3.Connection,
    source_task_id: str,
    anomaly: GovernanceAnomaly,
) -> str:
    """Convert a detected governance anomaly into a maintainer defect card.

    Idempotent on ``{source_task_id}:{anomaly.kind}``. A 5th review_requested
    on the same looping task returns the existing default-owned defect
    instead of minting a sibling (t_a93d6dc8 vs t_f21333d5).
    """
    from hermes_cli import kanban_db as kb

    existing = _existing_maintainer_defect(conn, source_task_id, anomaly)
    if existing is not None:
        return existing

    fingerprint = _defect_fingerprint(source_task_id, anomaly)
    defect_title = _defect_title(source_task_id, anomaly)
    if anomaly.kind == "repeat_review_loop":
        defect_body = (
            f"Task {source_task_id} has entered review {anomaly.counter_value} times, "
            f"exceeding threshold of {REVIEW_LOOP_THRESHOLD}.\n\n"
            f"Reason: {anomaly.reason}\n\n"
            f"This may indicate:\n"
            f"- The review goal/judge/evidence_contract is misaligned with implementation\n"
            f"- The implementation path is fundamentally flawed and needs decomposition\n"
            f"- The review criteria are ambiguous or keep changing\n\n"
            f"fingerprint: {fingerprint}\n"
        )
    else:
        defect_body = (
            f"Anomaly: {anomaly.reason}\n\n"
            f"fingerprint: {fingerprint}\n"
        )

    return kb.create_task(
        conn,
        title=defect_title,
        body=defect_body,
        assignee="default",
        idempotency_key=f"gov-defect:{fingerprint}",
    )


def scan_board_for_governance_defects(
    conn: sqlite3.Connection,
) -> list[GovernanceAnomaly]:
    """Scan a board for governance defects and return detected anomalies.
    
    Iterates through all tasks on the board, detects governance anomalies,
    and materializes maintainer defects for anomalies that haven't already
    been defected (idempotently).
    
    Args:
        conn: Database connection.
    
    Returns:
        List of detected GovernanceAnomaly objects.
    """
    # Import here to avoid circular dependency
    from hermes_cli import kanban_db as kb
    
    anomalies = []
    
    # Get all tasks on this board
    tasks = kb.list_tasks(conn)
    
    for task in tasks:
        # Check for governance anomalies
        anomaly = classify_governance_anomaly(task)
        if anomaly:
            anomalies.append(anomaly)
            
            # Materialize the defect if it doesn't already exist
            _ensure_maintainer_defect(conn, task.id, anomaly)
    
    return anomalies


def _ensure_maintainer_defect(
    conn: sqlite3.Connection,
    source_task_id: str,
    anomaly: GovernanceAnomaly,
) -> None:
    """Ensure a maintainer defect exists for the anomaly (idempotent)."""
    materialize_maintainer_defect(conn, source_task_id, anomaly)

