"""DDP lifecycle state machine + telemetry emission.

Only declared transitions are legal (spec: "Lifecycle state machine"). Each
transition persists two ledger rows — the advanced request state and a
transition-history row (actor, policy version, optional evidence reference) —
then emits the mapped telemetry event.

Durability ordering (both facts verified against Task 5's ledger): the ledger
`set_state` and `record_transition` calls are two *separate* commits, and the
telemetry emit follows both. Consequences a caller must tolerate:
  * A crash between the two ledger commits leaves the request state advanced
    with its transition-history row missing. The authoritative state
    (`requests.state`) is still correct; only the audit row is absent. True
    single-commit atomicity would need a ledger transaction API and is
    deferred (Stage-2 hardening). Do NOT read the absence of a final
    transition row as "the transition did not happen."
  * A telemetry emit that raises never rolls back the durable state; the
    reconciler (Task 9) republishes lifecycle events idempotently.

`ledger` and `bus` are duck-typed: `ledger` must expose
`get_request/set_state/record_transition`; `bus` must expose `emit` (or be
None to suppress emission). They are passed untyped to avoid importing the
concrete classes into this pure decision module.
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
# Structural invariant, guarded with an explicit raise (not `assert`, which
# `python -O` strips) so a future edit that re-adds a terminal state as a key
# fails loudly at import regardless of optimization flags.
for _t in TERMINAL_STATES:
    if _t in TRANSITIONS:
        raise RuntimeError(f"{_t} is terminal and must have no forward edges")

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

    # Durable state is committed above; telemetry is best-effort. An emit that
    # raises propagates to the caller but the state/transition rows persist —
    # the Task 9 reconciler republishes lifecycle events idempotently, so this
    # layer intentionally does not retry or roll back here.
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
