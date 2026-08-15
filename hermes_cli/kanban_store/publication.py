"""Sealed publication intents, exact approvals, dispatch facts, and settlement."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .canonical import canonical_json_bytes, prepare_intent, sha256_hex
from .database import write_txn
from .events import append_event
from .types import (
    ContractError,
    DispatchDisposition,
    DispatchOutcome,
    DraftIntent,
    EventRecord,
    IntentState,
    PreparedIntent,
    PublicationKind,
    TrustedIntentPolicy,
)

PolicyResolver = Callable[[DraftIntent], TrustedIntentPolicy]


def _ledger(conn, table: str, id_column: str, object_id: str, event_type: str, data: Mapping[str, Any]) -> None:
    conn.execute(
        f"INSERT INTO {table}(ledger_uuid, {id_column}, event_type, event_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            object_id,
            event_type,
            canonical_json_bytes(dict(data)).decode("utf-8"),
            int(time.time()),
        ),
    )


def _event(task_id: str, run_id: int, generation: int, event_type: str, payload: Mapping[str, Any]) -> EventRecord:
    return EventRecord(
        event_uuid=str(uuid.uuid4()),
        task_id=task_id,
        run_id=run_id,
        claim_generation=generation,
        event_type=event_type,
        source="kanban.store.publication",
        severity="info",
        retention_class="publication",
        payload=payload,
    )


def _validate_target(kind: PublicationKind, target: Mapping[str, Any]) -> None:
    if kind in {PublicationKind.GITHUB_ISSUE_CREATE, PublicationKind.GITHUB_ISSUE_COMMENT_CREATE}:
        required = {"repository_id", "owner", "repo"}
        allowed = set(required)
        if kind is PublicationKind.GITHUB_ISSUE_COMMENT_CREATE:
            required.add("issue_number")
            allowed.add("issue_number")
        unknown = set(target) - allowed
        if unknown or not required <= set(target):
            raise ContractError("GitHub target does not match the V1 schema")
        if not isinstance(target["repository_id"], int) or int(target["repository_id"]) <= 0:
            raise ContractError("repository_id must be a positive integer")
        for key in ("owner", "repo"):
            if not isinstance(target[key], str) or not target[key] or len(target[key]) > 128:
                raise ContractError(f"invalid GitHub target field: {key}")
        if "issue_number" in target and (
            not isinstance(target["issue_number"], int) or int(target["issue_number"]) <= 0
        ):
            raise ContractError("issue_number must be positive")
        return

    if kind in {
        PublicationKind.HERMES_TASK_COMPLETION_NOTIFY,
        PublicationKind.HERMES_ARTIFACT_DELIVER,
    }:
        required = {"route_version", "platform", "account_id", "conversation_id", "thread_id"}
        if set(target) != required:
            raise ContractError("Hermes delivery target does not match V1 schema")
        for key in required:
            if not isinstance(target[key], str) or not target[key] or len(target[key]) > 256:
                raise ContractError(f"invalid Hermes target field: {key}")
        return
    raise ContractError("unsupported publication kind")


def stage_intent(
    conn,
    *,
    task_id: str,
    run_id: int,
    claim_generation: int,
    draft: DraftIntent,
    policy_resolver: PolicyResolver,
) -> PreparedIntent:
    policy = policy_resolver(draft)
    _validate_target(draft.kind, policy.target)
    if not policy.publisher_principal or len(policy.publisher_principal) > 256:
        raise ContractError("publisher principal is invalid")
    if not policy.adapter_version or len(policy.adapter_version) > 64:
        raise ContractError("adapter version is invalid")
    intent_id = str(uuid.uuid4())
    prepared = prepare_intent(intent_id=intent_id, draft=draft, policy=policy)
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO publication_intents(
            intent_id, task_id, run_id, claim_generation, kind, required,
            state, publisher_principal, adapter_version, target_json,
            payload_json, headers_json, marker, prepared_bytes, request_body_bytes,
            request_body_sha256, wire_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent_id,
            task_id,
            run_id,
            claim_generation,
            draft.kind.value,
            1 if policy.required else 0,
            IntentState.SEALED.value,
            policy.publisher_principal,
            policy.adapter_version,
            canonical_json_bytes(dict(policy.target)).decode("utf-8"),
            canonical_json_bytes(dict(prepared.payload)).decode("utf-8"),
            canonical_json_bytes(dict(prepared.application_headers)).decode("utf-8"),
            prepared.marker,
            prepared.prepared_bytes,
            prepared.request_body_bytes,
            prepared.request_body_sha256,
            prepared.wire_sha256,
            now,
        ),
    )
    _ledger(
        conn,
        "publication_intent_ledger",
        "intent_id",
        intent_id,
        "sealed",
        {
            "wire_sha256": prepared.wire_sha256,
            "kind": draft.kind.value,
            "required": policy.required,
            "publisher_principal": policy.publisher_principal,
            "adapter_version": policy.adapter_version,
        },
    )
    append_event(
        conn,
        _event(
            task_id,
            run_id,
            claim_generation,
            "publication.intent_sealed",
            {
                "intent_id": intent_id,
                "kind": draft.kind.value,
                "required": policy.required,
                "wire_sha256": prepared.wire_sha256,
            },
        ),
    )
    return prepared


def approve_intent(
    conn,
    *,
    intent_id: str,
    wire_sha256: str,
    actor: str,
    decision: str,
    reason: str | None = None,
) -> str:
    if decision not in {"approve", "reject"}:
        raise ContractError("approval decision must be approve or reject")
    with write_txn(conn):
        row = conn.execute(
            "SELECT * FROM publication_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if not row:
            raise KeyError(intent_id)
        if row["state"] != IntentState.SEALED.value:
            raise ContractError("intent is not awaiting exact approval")
        if row["wire_sha256"] != wire_sha256:
            raise ContractError("approval digest does not match the prepared wire")
        target_sha = sha256_hex(str(row["target_json"]).encode("utf-8"))
        approval_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO publication_approvals(
                approval_id, intent_id, wire_sha256, target_sha256,
                adapter_version, publisher_principal, actor, decision,
                reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                intent_id,
                wire_sha256,
                target_sha,
                row["adapter_version"],
                row["publisher_principal"],
                actor,
                decision,
                reason,
                int(time.time()),
            ),
        )
        _ledger(
            conn,
            "publication_approval_ledger",
            "approval_id",
            approval_id,
            decision,
            {"intent_id": intent_id, "wire_sha256": wire_sha256, "actor": actor, "reason": reason},
        )
        next_state = IntentState.APPROVED if decision == "approve" else IntentState.REJECTED
        conn.execute(
            "UPDATE publication_intents SET state=? WHERE intent_id=? AND state=?",
            (next_state.value, intent_id, IntentState.SEALED.value),
        )
        if decision == "reject":
            conn.execute(
                "UPDATE tasks SET status='publication_attention', publication_state='rejected' "
                "WHERE id=? AND status='awaiting_publication'",
                (row["task_id"],),
            )
        append_event(
            conn,
            _event(
                row["task_id"],
                int(row["run_id"]),
                int(row["claim_generation"]),
                f"publication.{decision}d" if decision == "approve" else "publication.rejected",
                {"intent_id": intent_id, "approval_id": approval_id, "actor": actor},
            ),
        )
    return approval_id


def claim_dispatch(conn, *, approval_id: str, controller_id: str) -> str:
    """Create the sole dispatch row for one exact approval.

    A dispatch row is never deleted or recycled.  An ambiguous attempt remains
    attached to this row and cannot be turned into an automatic resend.
    """

    with write_txn(conn):
        row = conn.execute(
            """
            SELECT a.*, i.task_id, i.run_id, i.claim_generation, i.state AS intent_state
              FROM publication_approvals a
              JOIN publication_intents i ON i.intent_id=a.intent_id
             WHERE a.approval_id=?
            """,
            (approval_id,),
        ).fetchone()
        if not row:
            raise KeyError(approval_id)
        if row["decision"] != "approve" or row["intent_state"] != IntentState.APPROVED.value:
            raise ContractError("approval cannot be dispatched")
        existing = conn.execute(
            "SELECT dispatch_id FROM publication_dispatches WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if existing:
            raise ContractError("this approval already has a dispatch fact")
        dispatch_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO publication_dispatches(
                dispatch_id, approval_id, intent_id, controller_id, state, claimed_at
            ) VALUES (?, ?, ?, ?, 'dispatch_claimed', ?)
            """,
            (dispatch_id, approval_id, row["intent_id"], controller_id, int(time.time())),
        )
        conn.execute(
            "UPDATE publication_intents SET state=? WHERE intent_id=? AND state=?",
            (IntentState.DISPATCH_CLAIMED.value, row["intent_id"], IntentState.APPROVED.value),
        )
        _ledger(
            conn,
            "publication_dispatch_ledger",
            "dispatch_id",
            dispatch_id,
            "claimed",
            {"approval_id": approval_id, "controller_id": controller_id},
        )
        append_event(
            conn,
            _event(
                row["task_id"],
                int(row["run_id"]),
                int(row["claim_generation"]),
                "publication.dispatch_claimed",
                {"intent_id": row["intent_id"], "dispatch_id": dispatch_id},
            ),
        )
    return dispatch_id


def mark_dispatch_started(conn, dispatch_id: str) -> None:
    with write_txn(conn):
        row = conn.execute(
            """
            SELECT d.*, i.task_id, i.run_id, i.claim_generation
              FROM publication_dispatches d
              JOIN publication_intents i ON i.intent_id=d.intent_id
             WHERE d.dispatch_id=?
            """,
            (dispatch_id,),
        ).fetchone()
        if not row or row["state"] != IntentState.DISPATCH_CLAIMED.value:
            raise ContractError("dispatch is not claimable for start")
        now = int(time.time())
        conn.execute(
            "UPDATE publication_dispatches SET state='dispatch_started', started_at=? "
            "WHERE dispatch_id=? AND state='dispatch_claimed'",
            (now, dispatch_id),
        )
        conn.execute(
            "UPDATE publication_intents SET state='dispatch_started' WHERE intent_id=?",
            (row["intent_id"],),
        )
        _ledger(
            conn,
            "publication_dispatch_ledger",
            "dispatch_id",
            dispatch_id,
            "started",
            {"started_at": now},
        )
        append_event(
            conn,
            _event(
                row["task_id"],
                int(row["run_id"]),
                int(row["claim_generation"]),
                "publication.dispatch_started",
                {"intent_id": row["intent_id"], "dispatch_id": dispatch_id},
            ),
        )


def record_dispatch_outcome(conn, dispatch_id: str, outcome: DispatchOutcome) -> str:
    with write_txn(conn):
        row = conn.execute(
            """
            SELECT d.*, i.task_id, i.run_id, i.claim_generation, i.required
              FROM publication_dispatches d
              JOIN publication_intents i ON i.intent_id=d.intent_id
             WHERE d.dispatch_id=?
            """,
            (dispatch_id,),
        ).fetchone()
        if not row:
            raise KeyError(dispatch_id)
        if row["state"] != IntentState.DISPATCH_STARTED.value:
            raise ContractError("outcome requires a prior dispatch_started fact")
        if conn.execute(
            "SELECT 1 FROM publication_receipts WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone():
            raise ContractError("dispatch already has a terminal receipt")
        receipt_id = str(uuid.uuid4())
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO publication_receipts(
                receipt_id, dispatch_id, intent_id, disposition, remote_identity,
                status_code, detail_code, response_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                dispatch_id,
                row["intent_id"],
                outcome.disposition.value,
                outcome.remote_identity,
                outcome.status_code,
                outcome.detail_code,
                outcome.response_digest,
                now,
            ),
        )
        conn.execute(
            "UPDATE publication_dispatches SET state='receipt', completed_at=? WHERE dispatch_id=?",
            (now, dispatch_id),
        )
        _ledger(
            conn,
            "publication_receipt_ledger",
            "receipt_id",
            receipt_id,
            outcome.disposition.value,
            {
                "dispatch_id": dispatch_id,
                "remote_identity": outcome.remote_identity,
                "status_code": outcome.status_code,
                "detail_code": outcome.detail_code,
            },
        )
        if outcome.disposition is DispatchDisposition.SUCCESS:
            next_state = IntentState.RECEIPT.value
            publication_state = "settled"
        elif outcome.disposition is DispatchDisposition.DEFINITE_NO_EFFECT:
            next_state = IntentState.REJECTED.value
            publication_state = "definite_no_effect"
        else:
            next_state = IntentState.RECONCILE_REQUIRED.value
            publication_state = "reconcile_required"
        conn.execute(
            "UPDATE publication_intents SET state=? WHERE intent_id=?",
            (next_state, row["intent_id"]),
        )
        if outcome.disposition is not DispatchDisposition.SUCCESS:
            conn.execute(
                "UPDATE tasks SET status='publication_attention', publication_state=? "
                "WHERE id=? AND status='awaiting_publication'",
                (publication_state, row["task_id"]),
            )
        append_event(
            conn,
            _event(
                row["task_id"],
                int(row["run_id"]),
                int(row["claim_generation"]),
                "publication.receipt" if outcome.disposition is DispatchDisposition.SUCCESS else "publication.attention",
                {
                    "intent_id": row["intent_id"],
                    "dispatch_id": dispatch_id,
                    "receipt_id": receipt_id,
                    "disposition": outcome.disposition.value,
                },
            ),
        )
        _settle_if_complete(conn, str(row["task_id"]))
    return receipt_id


def _settle_if_complete(conn, task_id: str) -> bool:
    outstanding = conn.execute(
        """
        SELECT COUNT(*)
          FROM publication_intents
         WHERE task_id=? AND required=1 AND state != 'receipt'
        """,
        (task_id,),
    ).fetchone()[0]
    required = conn.execute(
        "SELECT COUNT(*) FROM publication_intents WHERE task_id=? AND required=1",
        (task_id,),
    ).fetchone()[0]
    if required and not outstanding:
        outcome_row = conn.execute(
            "SELECT outcome FROM finalizations WHERE task_id=? "
            "ORDER BY created_at DESC, finalization_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        outcome = str(outcome_row[0]) if outcome_row else "completed"
        terminal_states = {
            "completed": "done",
            "blocked": "blocked",
            "review": "review",
            "changes": "ready",
        }
        terminal = terminal_states.get(outcome, "publication_attention")
        completed_at = int(time.time()) if terminal == "done" else None
        conn.execute(
            "UPDATE tasks SET status=?, publication_state='settled', completed_at=? "
            "WHERE id=? AND status IN ('awaiting_publication','publication_attention')",
            (terminal, completed_at, task_id),
        )
        row = conn.execute(
            "SELECT current_run_id, claim_generation FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        append_event(
            conn,
            _event(
                task_id,
                int(row["current_run_id"] or 0),
                int(row["claim_generation"] or 0),
                "publication.task_settled",
                {"required_intents": int(required), "terminal_state": terminal},
            ),
        )
        return True
    return False


def load_dispatch_contract(conn, dispatch_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT d.dispatch_id, d.controller_id, a.approval_id, a.wire_sha256 AS approved_sha,
               a.target_sha256, a.adapter_version AS approved_adapter,
               a.publisher_principal AS approved_principal,
               i.*
          FROM publication_dispatches d
          JOIN publication_approvals a ON a.approval_id=d.approval_id
          JOIN publication_intents i ON i.intent_id=d.intent_id
         WHERE d.dispatch_id=?
        """,
        (dispatch_id,),
    ).fetchone()
    if not row:
        raise KeyError(dispatch_id)
    contract = dict(row)
    if contract["approved_sha"] != contract["wire_sha256"]:
        raise ContractError("approved digest drifted from intent")
    if contract["approved_adapter"] != contract["adapter_version"]:
        raise ContractError("approved adapter drifted from intent")
    if contract["approved_principal"] != contract["publisher_principal"]:
        raise ContractError("approved principal drifted from intent")
    if contract["target_sha256"] != sha256_hex(str(contract["target_json"]).encode("utf-8")):
        raise ContractError("approved target drifted from intent")
    contract["target"] = json.loads(contract["target_json"])
    contract["payload"] = json.loads(contract["payload_json"])
    contract["application_headers"] = json.loads(contract["headers_json"])
    return contract
