"""Reconciliation for ambiguous publication outcomes.

Reconciliation is deliberately read-only with respect to the remote system.
A no-match result never authorizes a resend.  Exactly one fully validated match
may settle the original dispatch; multiple matches or incompatible matches
freeze the task as a conflict requiring an operator decision.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import canonical_json_bytes
from .database import write_txn
from .events import append_event
from .publication import _event, _ledger, _settle_if_complete
from .types import ContractError, IntentState


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    complete: bool
    matches: tuple[Mapping[str, Any], ...]
    detail_code: str
    detail: Mapping[str, Any]

    def __post_init__(self) -> None:
        if len(self.matches) > 100:
            raise ContractError("reconciliation result is unbounded")
        if not self.detail_code or len(self.detail_code) > 128:
            raise ContractError("invalid reconciliation detail code")


def begin_reconciliation(conn, *, intent_id: str, actor: str) -> str:
    with write_txn(conn):
        row = conn.execute(
            """
            SELECT i.*, d.dispatch_id
              FROM publication_intents i
              JOIN publication_dispatches d ON d.intent_id=i.intent_id
             WHERE i.intent_id=?
            """,
            (intent_id,),
        ).fetchone()
        if not row:
            raise KeyError(intent_id)
        if row["state"] != IntentState.RECONCILE_REQUIRED.value:
            raise ContractError("intent is not awaiting reconciliation")
        active = conn.execute(
            "SELECT reconciliation_id FROM publication_reconciliations "
            "WHERE intent_id=? AND state='running'",
            (intent_id,),
        ).fetchone()
        if active:
            raise ContractError("a reconciliation is already running")
        reconciliation_id = str(uuid.uuid4())
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO publication_reconciliations(
                reconciliation_id, intent_id, dispatch_id, state, outcome,
                match_count, detail_json, created_at
            ) VALUES (?, ?, ?, 'running', NULL, NULL, ?, ?)
            """,
            (
                reconciliation_id,
                intent_id,
                row["dispatch_id"],
                canonical_json_bytes({"actor": actor}).decode("utf-8"),
                now,
            ),
        )
        append_event(
            conn,
            _event(
                row["task_id"],
                int(row["run_id"]),
                int(row["claim_generation"]),
                "publication.reconciliation_started",
                {"intent_id": intent_id, "reconciliation_id": reconciliation_id, "actor": actor},
            ),
        )
    return reconciliation_id


def finish_reconciliation(
    conn,
    *,
    reconciliation_id: str,
    result: ReconciliationResult,
) -> str:
    """Record a read-only lookup result without ever scheduling a resend."""

    with write_txn(conn):
        row = conn.execute(
            """
            SELECT r.*, i.task_id, i.run_id, i.claim_generation, i.marker,
                   i.publisher_principal, i.kind
              FROM publication_reconciliations r
              JOIN publication_intents i ON i.intent_id=r.intent_id
             WHERE r.reconciliation_id=?
            """,
            (reconciliation_id,),
        ).fetchone()
        if not row:
            raise KeyError(reconciliation_id)
        if row["state"] != "running":
            raise ContractError("reconciliation is already terminal")

        verified = []
        for match in result.matches:
            marker = match.get("marker")
            principal = match.get("publisher_principal")
            if marker != row["marker"] or principal != row["publisher_principal"]:
                continue
            verified.append(dict(match))

        if not result.complete:
            outcome = "incomplete"
            intent_state = IntentState.RECONCILE_REQUIRED.value
            task_state = "publication_attention"
        elif len(verified) == 1 and len(result.matches) == 1:
            outcome = "settled"
            intent_state = IntentState.RECEIPT.value
            task_state = None
        elif len(result.matches) > 1 or len(verified) > 1:
            outcome = "conflict"
            intent_state = IntentState.CONFLICT.value
            task_state = "publication_attention"
        else:
            # A complete no-match is evidence, not permission to retry.  The
            # hidden-first-object possibility remains and requires a human.
            outcome = "no_match"
            intent_state = IntentState.RECONCILE_REQUIRED.value
            task_state = "publication_attention"

        detail = {
            "detail_code": result.detail_code,
            "complete": result.complete,
            "reported_matches": len(result.matches),
            "verified_matches": len(verified),
            "detail": dict(result.detail),
            "matches": verified,
        }
        now = int(time.time())
        conn.execute(
            """
            UPDATE publication_reconciliations
               SET state='complete', outcome=?, match_count=?, detail_json=?, completed_at=?
             WHERE reconciliation_id=? AND state='running'
            """,
            (
                outcome,
                len(result.matches),
                canonical_json_bytes(detail).decode("utf-8"),
                now,
                reconciliation_id,
            ),
        )
        conn.execute(
            "UPDATE publication_intents SET state=? WHERE intent_id=?",
            (intent_state, row["intent_id"]),
        )
        if task_state:
            conn.execute(
                "UPDATE tasks SET status=?, publication_state=? WHERE id=?",
                (task_state, f"reconciliation_{outcome}", row["task_id"]),
            )
        _ledger(
            conn,
            "publication_receipt_ledger",
            "receipt_id",
            f"reconciliation:{reconciliation_id}",
            f"reconciliation_{outcome}",
            detail,
        )
        append_event(
            conn,
            _event(
                row["task_id"],
                int(row["run_id"]),
                int(row["claim_generation"]),
                "publication.reconciliation_completed",
                {
                    "intent_id": row["intent_id"],
                    "reconciliation_id": reconciliation_id,
                    "outcome": outcome,
                    "match_count": len(result.matches),
                },
            ),
        )
        if outcome == "settled":
            _settle_if_complete(conn, str(row["task_id"]))
    return outcome
