"""Kanban governance contracts for blocked-card enforcement.

Validates typed blocker classes, decision classes, and operational rules
to ensure blocked cards follow governance invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Required blocker classes for typed blocks
BLOCKER_CLASSES = {
    "infra_default",
    "lane_work",
    "review_fix",
    "dependency_wait",
    "human_decision",
}

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
