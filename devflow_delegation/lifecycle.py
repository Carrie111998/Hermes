"""DDP lifecycle state machine and best-effort telemetry emission.

Every legal transition persists the request state and transition-history record
atomically when the ledger provides transaction(). Telemetry follows that durable
commit and never rolls it back.
"""
from __future__ import annotations

from typing import Dict, Optional

from events.schema import EventType

from devflow_delegation.ledger import TERMINAL_STATES


class IllegalTransitionError(ValueError):
    pass


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
for _terminal in TERMINAL_STATES:
    if _terminal in TRANSITIONS:
        raise RuntimeError(f"{_terminal} is terminal and must have no forward edges")

STATE_EVENTS: Dict[str, Optional[EventType]] = {
    "TRIAGED": EventType.DEVFLOW_WORK_TRIAGED,
    "PLANNED": EventType.DEVFLOW_WORK_PLANNED,
    "DUPLICATE": EventType.DEVFLOW_WORK_DUPLICATE,
    "DECLINED": EventType.DEVFLOW_WORK_DECLINED,
    "SUPPRESSED": EventType.DEVFLOW_WORK_SUPPRESSED,
    "BUILDING": EventType.DEVFLOW_BUILD_STARTED,
    "VALIDATED": None,
    "PR_OPEN": EventType.DEVFLOW_PR_OPENED,
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
    tx = getattr(ledger, "transaction", None)
    if tx is None:
        row = ledger.get_request(request_id)
        if row is None:
            raise IllegalTransitionError(f"unknown request: {request_id}")
        from_state = row["state"]
        if to_state not in TRANSITIONS.get(from_state, frozenset()):
            raise IllegalTransitionError(f"illegal transition {from_state} -> {to_state} for {request_id}")
        terminal_reason = to_state if to_state in TERMINAL_STATES else None
        ledger.set_state(request_id, to_state, terminal_reason=terminal_reason)
        ledger.record_transition(request_id, from_state, to_state, actor, policy_version, evidence_ref)
    else:
        with tx():
            row = ledger.get_request(request_id)
            if row is None:
                raise IllegalTransitionError(f"unknown request: {request_id}")
            from_state = row["state"]
            if to_state not in TRANSITIONS.get(from_state, frozenset()):
                raise IllegalTransitionError(
                    f"illegal transition {from_state} -> {to_state} for {request_id}"
                )
            terminal_reason = to_state if to_state in TERMINAL_STATES else None
            ledger.set_state(request_id, to_state, terminal_reason=terminal_reason)
            ledger.record_transition(request_id, from_state, to_state, actor, policy_version, evidence_ref)

    emit_transition_event(
        bus,
        request_id=request_id,
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        policy_version=policy_version,
    )
    return to_state


def emit_transition_event(
    bus,
    *,
    request_id: str,
    from_state: str,
    to_state: str,
    actor: str,
    policy_version: str = "policy-v1",
) -> None:
    """Emit best-effort lifecycle telemetry after a durable transition."""
    event_type = STATE_EVENTS.get(to_state)
    if event_type is None or bus is None:
        return
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
