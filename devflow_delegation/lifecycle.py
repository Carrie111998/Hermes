"""DDP lifecycle state machine + telemetry emission.

Only declared transitions are legal (spec: "Lifecycle state machine"). Every
transition is a ledger transaction carrying timestamp, actor, policy version,
and optional evidence reference; telemetry is secondary — a failed event emit
never rolls back durable state (reconciliation republishes idempotently).
"""
from __future__ import annotations

from typing import Dict, Optional

from events.schema import EventType

from devflow_delegation.ledger import TERMINAL_STATES


class IllegalTransitionError(ValueError):
    pass


# Canonical machine. REQUESTED..MERGE_PENDING advance strictly in order;
# terminal/side states are reachable from the states listed and have NO
# forward edges of their own. DEPLOYED is a success terminal (in
# TERMINAL_STATES) so it is NOT a key here; a revert is a side exit taken
# from MERGED/AUTO_MERGED/DEPLOYING (before the deploy terminalizes) into
# REVERT_REQUESTED, which itself has forward edges.
TRANSITIONS: Dict[str, frozenset] = {
    "REQUESTED": frozenset({"TRIAGED", "DECLINED", "DUPLICATE", "SUPPRESSED", "CANCELLED"}),
    "TRIAGED": frozenset({"PLANNED", "DECLINED", "CANCELLED"}),
    "PLANNED": frozenset({"BUILDING", "DECLINED", "CANCELLED"}),
    "BUILDING": frozenset({"VALIDATED", "FAILED"}),
    "VALIDATED": frozenset({"PR_OPEN", "FAILED"}),
    "PR_OPEN": frozenset({"MERGE_PENDING", "FAILED", "CANCELLED"}),
    "MERGE_PENDING": frozenset({"MERGED", "AUTO_MERGED", "CANCELLED", "FAILED"}),
    "MERGED": frozenset({"DEPLOYING", "REVERT_REQUESTED"}),
    "AUTO_MERGED": frozenset({"DEPLOYING", "REVERT_REQUESTED"}),
    "DEPLOYING": frozenset({"DEPLOYED", "FAILED", "REVERT_REQUESTED"}),
    "REVERT_REQUESTED": frozenset({"REVERTED", "FAILED"}),
}
for _t in TERMINAL_STATES:
    assert _t not in TRANSITIONS, f"{_t} must stay terminal"

# State -> lifecycle event. None = ledger-only transition (no telemetry type
# exists for it by design: VALIDATED is an internal checkpoint; FAILED/
# CANCELLED/REVERT* carry their context in the ledger's terminal_reason and
# transition history).
STATE_EVENTS: Dict[str, Optional[EventType]] = {
    "TRIAGED": EventType.DEVFLOW_WORK_TRIAGED,
    "PLANNED": EventType.DEVFLOW_WORK_PLANNED,
    "DUPLICATE": EventType.DEVFLOW_WORK_DUPLICATE,
    "DECLINED": EventType.DEVFLOW_WORK_DECLINED,
    "SUPPRESSED": EventType.DEVFLOW_WORK_SUPPRESSED,
    "BUILDING": EventType.DEVFLOW_BUILD_STARTED,      # reused existing member
    "VALIDATED": None,
    "PR_OPEN": EventType.DEVFLOW_PR_OPENED,           # reused existing member
    "MERGE_PENDING": EventType.DEVFLOW_MERGE_PENDING,
    "MERGED": EventType.DEVFLOW_MERGED,
    "AUTO_MERGED": EventType.DEVFLOW_AUTO_MERGED,
    "DEPLOYING": EventType.DEVFLOW_DEPLOY_STARTED,
    "DEPLOYED": EventType.DEVFLOW_DEPLOYED,
    "FAILED": None,
    "CANCELLED": None,
    "REVERT_REQUESTED": None,
    "REVERTED": None,
}


def transition(
    ledger,
    bus,
    request_id: str,
    to_state: str,
    *,
    actor: str,
    policy_version: str = "policy-v1",
    evidence_ref: Optional[str] = None,
) -> str:
    row = ledger.get_request(request_id)
    if row is None:
        raise IllegalTransitionError(f"unknown request: {request_id}")
    from_state = row["state"]
    if to_state not in TRANSITIONS.get(from_state, frozenset()):
        raise IllegalTransitionError(f"illegal transition {from_state} -> {to_state} for {request_id}")

    terminal_reason = to_state if to_state in TERMINAL_STATES else None
    ledger.set_state(request_id, to_state, terminal_reason=terminal_reason)
    ledger.record_transition(request_id, from_state, to_state, actor, policy_version, evidence_ref)

    event_type = STATE_EVENTS.get(to_state)
    if event_type is not None and bus is not None:
        bus.emit(
            event_type=event_type,
            source="ddp.lifecycle",
            payload={
                "request_id": request_id,
                "from_state": from_state,
                "to_state": to_state,
                "actor": actor,
                "policy_version": policy_version,
            },
            correlation_id=request_id,
        )
    return to_state
