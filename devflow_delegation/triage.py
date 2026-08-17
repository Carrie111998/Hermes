"""DDP triage — the deterministic driver of the REQUESTED -> TRIAGED edge.

Stage 1's control plane already knows how to *accept* work (emitter -> ledger)
and how to *transition* it (lifecycle.transition), but nothing programmatically
walks freshly-REQUESTED rows onto the TRIAGED (human-approval) gate. `cli
transition` does it one row at a time; this module does it for the whole
REQUESTED frontier in one deterministic, re-runnable pass.

Design constraints (spec: "Lifecycle state machine" + conservative rollout):
  * No model calls, no heuristics — triage is pure selection + the existing
    audited `lifecycle.transition` edge (ledger state + history + telemetry).
  * Idempotent / crash-safe: only rows whose authoritative `requests.state` is
    still REQUESTED are advanced, so a re-run after a partial tick is a no-op on
    already-advanced rows. `requests.state` is authoritative (a crash in
    lifecycle.transition's documented two-commit gap can leave a TRIAGED row
    with no history row — never the reverse), so selecting on state is safe.
  * TRIAGED is the human-approval gate: TRIAGED -> PLANNED is an operator act
    (`cli transition --to PLANNED`), never taken here. Triage parks work at the
    gate; it does not approve it.
  * Never raises for a per-row outcome — a row that has already left REQUESTED
    is counted as skipped, any other per-row failure is counted as an error and
    the pass continues.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from devflow_delegation.contract import SEVERITY_RANK
from devflow_delegation.lifecycle import IllegalTransitionError, transition

# Most-urgent priority first. Unknown/missing priority sorts last.
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _priority_of(row: Dict[str, Any]) -> str:
    """Priority lives in the envelope JSON, not as a ledger column."""
    try:
        return str(json.loads(row.get("envelope_json") or "{}").get("priority", ""))
    except (ValueError, TypeError):
        return ""


def triage_order(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic triage ranking of REQUESTED ledger rows (pure — the input
    list is not mutated). Order: highest severity first, then most-urgent
    priority (P0<P1<...), then oldest `created_at` first, with `request_id` as a
    final stable tiebreak so the order is total and reproducible.
    """
    return sorted(
        rows,
        key=lambda r: (
            -SEVERITY_RANK.get(str(r.get("severity", "")), 0),
            _PRIORITY_RANK.get(_priority_of(r), 9),
            str(r.get("created_at", "")),
            str(r.get("request_id", "")),
        ),
    )


def run_triage(
    ledger,
    bus,
    *,
    actor: str = "ddp.triage",
    limit: int = 50,
    policy_version: str = "policy-v1",
) -> Dict[str, int]:
    """Advance the REQUESTED frontier to TRIAGED, deterministically and
    idempotently.

    Fetches up to `limit` REQUESTED rows (the emit gate keeps this frontier
    small under the conservative rollout), orders them via `triage_order`, and
    applies the legal REQUESTED -> TRIAGED transition to each. `bus=None`
    suppresses telemetry while still advancing durable state (mirrors
    `lifecycle.transition`'s contract).

    Returns counts: `considered` (rows fetched), `triaged` (advanced this pass),
    `errors` (per-row failures, pass continued). A row already past REQUESTED is
    silently skipped and counted in neither `triaged` nor `errors`.
    """
    rows = ledger.list_requests(state="REQUESTED", limit=limit)
    triaged = 0
    errors = 0
    for row in triage_order(rows):
        request_id = row["request_id"]
        try:
            transition(ledger, bus, request_id, "TRIAGED",
                       actor=actor, policy_version=policy_version)
            triaged += 1
        except IllegalTransitionError:
            # Row left REQUESTED between the fetch and now (concurrent tick / a
            # prior partial pass). Not an error — just already handled.
            continue
        except Exception:
            # Durable state may already be committed (transition commits before
            # telemetry); count it and keep going rather than aborting the batch.
            errors += 1
    return {"considered": len(rows), "triaged": triaged, "errors": errors}
