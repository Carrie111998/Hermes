"""Generation-fenced safe reclaim coordinator."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable, Mapping

from .canonical import canonical_json_bytes
from .claims import invalidate_claim, verify_fence
from .database import write_txn
from .evidence import classify_evidence, evidence_json
from .events import append_event
from .types import ContractError, EventRecord, ReclaimDecision, RunFence

Resample = Callable[[RunFence], Iterable[Mapping[str, object]]]
FreezeScope = Callable[[RunFence], str]
TerminateScope = Callable[[RunFence, str], None]
ThawScope = Callable[[RunFence, str], None]


def _event(fence: RunFence, event_type: str, payload: Mapping[str, object]) -> EventRecord:
    return EventRecord(
        event_uuid=str(uuid.uuid4()),
        task_id=fence.task_id,
        run_id=fence.run_id,
        claim_generation=fence.claim_generation,
        event_type=event_type,
        source="kanban.store.reclaim",
        severity="warning" if "aborted" in event_type else "info",
        retention_class="reclaim",
        payload=payload,
    )


def reclaim_if_safe(
    conn,
    *,
    fence: RunFence,
    initial_observations: Iterable[Mapping[str, object]],
    resample: Resample,
    freeze_scope: FreezeScope,
    terminate_scope: TerminateScope,
    thaw_scope: ThawScope,
    now: int | None = None,
) -> bool:
    """Reclaim only after a fresh complete resample of the same generation.

    The scope is frozen before the final sample for inert reclaim.  Any change,
    uncertainty, publication/artifact evidence, open operation, or generation
    drift aborts and thaws.  The database generation advances before a
    replacement may be dispatched.
    """

    clock = int(time.time()) if now is None else int(now)
    first = classify_evidence(initial_observations, now=clock)
    if first.decision not in {ReclaimDecision.ELIGIBLE_DEAD, ReclaimDecision.ELIGIBLE_INERT}:
        return False

    probe_id = str(uuid.uuid4())
    with write_txn(conn):
        verify_fence(conn, fence)
        open_effects = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM run_operations WHERE task_id=? AND run_id=?
                 AND claim_generation=? AND state NOT IN ('closed','failed')) +
              (SELECT COUNT(*) FROM publication_intents WHERE task_id=? AND run_id=?
                 AND claim_generation=?) +
              (SELECT COUNT(*) FROM run_artifacts WHERE task_id=? AND run_id=?
                 AND claim_generation=?)
            """,
            (
                fence.task_id, fence.run_id, fence.claim_generation,
                fence.task_id, fence.run_id, fence.claim_generation,
                fence.task_id, fence.run_id, fence.claim_generation,
            ),
        ).fetchone()[0]
        if int(open_effects):
            return False
        conn.execute(
            """
            INSERT INTO reclaim_probes(
                probe_id, task_id, run_id, claim_generation, state,
                evidence_json, started_at
            ) VALUES (?, ?, ?, ?, 'probing', ?, ?)
            """,
            (
                probe_id,
                fence.task_id,
                fence.run_id,
                fence.claim_generation,
                canonical_json_bytes(evidence_json(first)).decode("utf-8"),
                clock,
            ),
        )
        append_event(conn, _event(fence, "reclaim.probe_started", {"probe_id": probe_id}))

    freeze_token: str | None = None
    try:
        if first.decision is ReclaimDecision.ELIGIBLE_INERT:
            freeze_token = freeze_scope(fence)
        second_items = list(resample(fence))
        second = classify_evidence(second_items, now=int(time.time()))
        if second.decision != first.decision:
            raise ContractError("reclaim evidence changed during final probe")
        if set(second.observation_ids) == set(first.observation_ids):
            raise ContractError("final reclaim probe reused stale observations")

        with write_txn(conn):
            verify_fence(conn, fence)
            conn.execute(
                "UPDATE reclaim_probes SET state='terminating', evidence_json=? WHERE probe_id=?",
                (canonical_json_bytes(evidence_json(second)).decode("utf-8"), probe_id),
            )
        terminate_scope(fence, freeze_token or "dead")
        invalidate_claim(
            conn,
            task_id=fence.task_id,
            run_id=fence.run_id,
            claim_generation=fence.claim_generation,
            reason="reclaimed",
            next_state="ready",
        )
        with write_txn(conn):
            conn.execute(
                "UPDATE reclaim_probes SET state='complete', completed_at=? WHERE probe_id=?",
                (int(time.time()), probe_id),
            )
            append_event(conn, _event(fence, "reclaim.completed", {"probe_id": probe_id}))
        return True
    except Exception as exc:
        if freeze_token is not None:
            try:
                thaw_scope(fence, freeze_token)
            except Exception:
                pass
        with write_txn(conn):
            conn.execute(
                "UPDATE reclaim_probes SET state='aborted', completed_at=? WHERE probe_id=?",
                (int(time.time()), probe_id),
            )
            append_event(
                conn,
                _event(
                    fence,
                    "reclaim.aborted",
                    {"probe_id": probe_id, "reason": type(exc).__name__},
                ),
            )
        return False
