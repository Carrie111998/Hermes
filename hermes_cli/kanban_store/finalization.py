"""Atomic, generation-fenced worker finalization.

Filesystem bytes are copied into an unreferenced content-addressed staging set
first.  One ``BEGIN IMMEDIATE`` transaction then verifies the fence, records
that exact artifact set, seals the exact active intent set, closes the run, and
moves the task.  A crash before commit can leave only unreachable immutable
blobs; it cannot expose a partial finalization or publication request.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .artifacts import freeze_artifacts, persist_frozen_artifacts
from .canonical import canonical_json_bytes, sha256_hex
from .claims import verify_fence
from .database import write_txn
from .events import append_event
from .publication import PolicyResolver, stage_intent
from .types import (
    AlreadyFinalized,
    EventRecord,
    FenceConflict,
    FinalizationRequest,
    PreparedIntent,
    RuntimeIdentity,
)

Failpoint = Callable[[str, int], None]


class _FailpointConnection:
    def __init__(self, conn, failpoint: Failpoint | None) -> None:
        self._conn = conn
        self._failpoint = failpoint
        self._statement = 0

    def execute(self, sql, parameters=()):
        result = self._conn.execute(sql, parameters)
        self._statement += 1
        if self._failpoint:
            self._failpoint("after_sql", self._statement)
        return result

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _event(request: FinalizationRequest, kind: str, payload: dict[str, Any]) -> EventRecord:
    fence = request.fence
    return EventRecord(
        event_uuid=str(uuid.uuid4()),
        task_id=fence.task_id,
        run_id=fence.run_id,
        claim_generation=fence.claim_generation,
        event_type=kind,
        source="kanban.store.finalization",
        severity="info",
        retention_class="finalization",
        payload=payload,
    )


def finalize_worker_run(
    conn,
    *,
    request: FinalizationRequest,
    workspace: str | Path,
    artifact_blob_root: str | Path,
    policy_resolver: PolicyResolver,
    trusted_runtime_identity: RuntimeIdentity | None = None,
    failpoint: Failpoint | None = None,
) -> dict[str, Any]:
    """Finalize once; never publish from the worker transaction."""

    metadata = dict(request.metadata)
    if trusted_runtime_identity is not None:
        if "runtime_identity" in metadata:
            raise ValueError("runtime_identity is host-reserved metadata")
        metadata["runtime_identity"] = trusted_runtime_identity.as_dict()
    metadata_json = canonical_json_bytes(metadata).decode("utf-8")

    frozen, artifact_set_sha = freeze_artifacts(
        workspace=workspace,
        blob_root=artifact_blob_root,
        declarations=request.artifacts,
    )
    prepared_intents: list[PreparedIntent] = []
    finalization_id = str(uuid.uuid4())
    with write_txn(conn):
        tx = _FailpointConnection(conn, failpoint)
        verify_fence(tx, request.fence)
        existing = tx.execute(
            """
            SELECT finalization_id FROM finalizations
             WHERE task_id=? AND run_id=? AND claim_generation=?
            """,
            (
                request.fence.task_id,
                request.fence.run_id,
                request.fence.claim_generation,
            ),
        ).fetchone()
        if existing:
            raise AlreadyFinalized(str(existing[0]))

        persist_frozen_artifacts(tx, request.fence, frozen)
        for draft in request.draft_intents:
            prepared_intents.append(
                stage_intent(
                    tx,
                    task_id=request.fence.task_id,
                    run_id=request.fence.run_id,
                    claim_generation=request.fence.claim_generation,
                    draft=draft,
                    policy_resolver=policy_resolver,
                )
            )

        intent_manifest = [
            {
                "intent_id": item.intent_id,
                "kind": item.kind.value,
                "required": item.required,
                "wire_sha256": item.wire_sha256,
            }
            for item in sorted(prepared_intents, key=lambda value: value.intent_id)
        ]
        intent_set_sha = sha256_hex(canonical_json_bytes(intent_manifest))
        required_count = sum(1 for item in prepared_intents if item.required)
        now = int(time.time())
        tx.execute(
            """
            INSERT INTO finalizations(
                finalization_id, task_id, run_id, claim_generation, outcome,
                summary, metadata_json, artifact_set_sha256, intent_set_sha256,
                actor_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'worker', ?)
            """,
            (
                finalization_id,
                request.fence.task_id,
                request.fence.run_id,
                request.fence.claim_generation,
                request.outcome,
                request.summary,
                metadata_json,
                artifact_set_sha,
                intent_set_sha,
                now,
            ),
        )
        run_cur = tx.execute(
            """
            UPDATE task_runs
               SET status='done', outcome=?, summary=?, metadata=?, ended_at=?,
                   finalized_at=?, runtime_provider=?, runtime_model=?,
                   runtime_api_mode=?, runtime_session_id=?, runtime_identity_source=?,
                   claim_token_hash=NULL, claim_expires=NULL
             WHERE id=? AND task_id=? AND claim_generation=? AND status='running'
            """,
            (
                request.outcome,
                request.summary,
                metadata_json,
                now,
                now,
                trusted_runtime_identity.provider if trusted_runtime_identity else None,
                trusted_runtime_identity.model if trusted_runtime_identity else None,
                trusted_runtime_identity.api_mode if trusted_runtime_identity else None,
                trusted_runtime_identity.session_id if trusted_runtime_identity else None,
                trusted_runtime_identity.source if trusted_runtime_identity else None,
                request.fence.run_id,
                request.fence.task_id,
                request.fence.claim_generation,
            ),
        )
        if run_cur.rowcount != 1:
            raise FenceConflict("run finalization lost its fence")

        if required_count:
            next_state = "awaiting_publication"
            publication_state = "pending"
            completed_at = None
        else:
            terminal_states = {
                "completed": "done",
                "blocked": "blocked",
                "review": "review",
                "changes": "ready",
            }
            next_state = terminal_states[request.outcome]
            publication_state = "not_required"
            completed_at = now if next_state == "done" else None
        task_cur = tx.execute(
            """
            UPDATE tasks
               SET status=?, publication_state=?, completed_at=?,
                   claim_token_hash=NULL, claim_expires=NULL, worker_pid=NULL
             WHERE id=? AND current_run_id=? AND claim_generation=?
               AND status='running'
            """,
            (
                next_state,
                publication_state,
                completed_at,
                request.fence.task_id,
                request.fence.run_id,
                request.fence.claim_generation,
            ),
        )
        if task_cur.rowcount != 1:
            raise FenceConflict("task finalization lost its fence")
        append_event(
            tx,
            _event(
                request,
                "run.finalized",
                {
                    "finalization_id": finalization_id,
                    "outcome": request.outcome,
                    "artifact_set_sha256": artifact_set_sha,
                    "intent_set_sha256": intent_set_sha,
                    "required_intents": required_count,
                    "next_state": next_state,
                    "runtime_identity": (
                        trusted_runtime_identity.as_dict()
                        if trusted_runtime_identity else None
                    ),
                },
            ),
        )

    return {
        "finalization_id": finalization_id,
        "task_id": request.fence.task_id,
        "run_id": request.fence.run_id,
        "claim_generation": request.fence.claim_generation,
        "state": next_state,
        "artifact_set_sha256": artifact_set_sha,
        "intent_set_sha256": intent_set_sha,
        "intents": [
            {
                "intent_id": item.intent_id,
                "kind": item.kind.value,
                "required": item.required,
                "wire_sha256": item.wire_sha256,
            }
            for item in prepared_intents
        ],
        "published": False,
    }


def manual_complete(
    conn,
    *,
    task_id: str,
    actor: str,
    summary: str,
    now: int | None = None,
) -> str:
    """Trusted manual lifecycle operation, separate from worker finalization."""

    current_time = int(time.time()) if now is None else int(now)
    finalization_id = str(uuid.uuid4())
    with write_txn(conn):
        task = conn.execute(
            "SELECT current_run_id, claim_generation, status FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if not task:
            raise KeyError(task_id)
        if task["status"] not in {"ready", "running", "blocked", "publication_attention"}:
            raise ValueError("task cannot be manually completed from this state")
        run_id = int(task["current_run_id"] or 0)
        generation = int(task["claim_generation"] or 0)
        conn.execute(
            """
            INSERT INTO finalizations(
                finalization_id, task_id, run_id, claim_generation, outcome,
                summary, metadata_json, artifact_set_sha256, intent_set_sha256,
                actor_type, created_at
            ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, 'operator', ?)
            """,
            (
                finalization_id,
                task_id,
                run_id,
                generation,
                summary,
                json.dumps({"actor": actor}, separators=(",", ":")),
                sha256_hex(b"[]"),
                sha256_hex(b"[]"),
                current_time,
            ),
        )
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, publication_state='manual', "
            "claim_token_hash=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (current_time, task_id),
        )
    return finalization_id
