"""Durable Epic-member integration intent state and claim authority."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, TypeAlias, cast

from hermes_cli.kanban_product_outcomes import (
    ApprovedCandidate,
    CandidateEligibility,
    CandidateEligibilityError,
    PassedTest,
    candidate_eligibility,
)
from hermes_cli.kanban_repository import (
    RepositoryContract,
    VerificationResult,
    verification_receipt_matches,
    verification_result_payload,
)


IntegrationStatus: TypeAlias = Literal[
    "pending",
    "running",
    "prepared",
    "rework_required",
    "attention_required",
    "integrated",
    "superseded",
]

_INTEGRATION_STATUSES = frozenset(
    {
        "pending",
        "running",
        "prepared",
        "rework_required",
        "attention_required",
        "integrated",
        "superseded",
    }
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
Row: TypeAlias = Mapping[str, object] | sqlite3.Row


@dataclass(frozen=True)
class IntegrationKey:
    epic_id: str
    story_id: str
    source_sha: str


@dataclass(frozen=True)
class IntegrationIntent:
    key: IntegrationKey
    source_branch: str
    review_run_id: int
    review_base_sha: str
    status: IntegrationStatus
    claim_lock: str | None
    claim_expires: int | None
    attempt_count: int
    target_pre_sha: str | None
    candidate_sha: str | None
    candidate_ref: str | None
    # Audit-only after the composite integration fact is durable. Later claim,
    # recovery, readiness, and invalidation paths must not depend on this event.
    verification_event_id: int | None
    last_failure_code: str | None
    created_at: int
    updated_at: int


def _value(row: Row, field: str) -> object:
    try:
        return row[field]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"integration intent row is missing {field}") from exc


def _text(row: Row, field: str) -> str:
    value = _value(row, field)
    if not isinstance(value, str):
        raise ValueError(f"integration intent {field} must be text")
    return value


def _nullable_text(row: Row, field: str) -> str | None:
    value = _value(row, field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"integration intent {field} must be text or null")
    return value


def _integer(row: Row, field: str) -> int:
    value = _value(row, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"integration intent {field} must be an integer")
    return value


def _nullable_integer(row: Row, field: str) -> int | None:
    value = _value(row, field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"integration intent {field} must be an integer or null")
    return value


def _full_sha(row: Row, field: str, *, nullable: bool = False) -> str | None:
    value = _value(row, field)
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"integration intent {field} must be a full lowercase SHA")
    return value


def integration_intent_from_row(row: Row) -> IntegrationIntent:
    """Parse one persisted intent without normalizing malformed authority facts."""

    status = _text(row, "status")
    if status not in _INTEGRATION_STATUSES:
        raise ValueError(f"invalid integration intent status: {status!r}")

    source_sha = _full_sha(row, "source_sha")
    review_base_sha = _full_sha(row, "review_base_sha")
    assert source_sha is not None and review_base_sha is not None

    return IntegrationIntent(
        key=IntegrationKey(
            epic_id=_text(row, "epic_id"),
            story_id=_text(row, "story_id"),
            source_sha=source_sha,
        ),
        source_branch=_text(row, "source_branch"),
        review_run_id=_integer(row, "review_run_id"),
        review_base_sha=review_base_sha,
        status=cast(IntegrationStatus, status),
        claim_lock=_nullable_text(row, "claim_lock"),
        claim_expires=_nullable_integer(row, "claim_expires"),
        attempt_count=_integer(row, "attempt_count"),
        target_pre_sha=_full_sha(row, "target_pre_sha", nullable=True),
        candidate_sha=_full_sha(row, "candidate_sha", nullable=True),
        candidate_ref=_nullable_text(row, "candidate_ref"),
        verification_event_id=_nullable_integer(row, "verification_event_id"),
        last_failure_code=_nullable_text(row, "last_failure_code"),
        created_at=_integer(row, "created_at"),
        updated_at=_integer(row, "updated_at"),
    )


def claim_next_intent(
    conn: sqlite3.Connection,
    owner: str,
    lease_seconds: int,
    *,
    board: str | None = None,
    repository_check: Callable[
        [RepositoryContract, ApprovedCandidate, PassedTest], CandidateEligibility
    ]
    | None = None,
) -> IntegrationIntent | None:
    """Claim one intent and re-derive its authority before repository access.

    An unexpired running row excludes every other intent on the board. Expired
    work is selected before new work so recovery stays on the same composite
    intent. Persisted intent fields are comparison inputs, never authority.
    """

    from hermes_cli import kanban_db as kb

    owner_text = str(owner or "").strip()
    if not owner_text:
        raise ValueError("integration claim owner is required")
    if isinstance(lease_seconds, bool) or int(lease_seconds) <= 0:
        raise ValueError("integration claim lease must be positive")

    now = int(time.time())
    claim_lock = f"{owner_text}:{secrets.token_hex(8)}"
    with kb.authorized_governance_write(), kb.write_txn(conn):
        active = conn.execute(
            "SELECT 1 FROM story_integration_intents "
            "WHERE status='running' "
            "AND (claim_expires IS NULL OR claim_expires>?) LIMIT 1",
            (now,),
        ).fetchone()
        if active is not None:
            return None
        row = conn.execute(
            "SELECT * FROM story_integration_intents "
            "WHERE status='pending' "
            "OR (status='running' AND claim_expires IS NOT NULL AND claim_expires<=?) "
            "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, created_at, "
            "epic_id, story_id, source_sha LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        updated = conn.execute(
            "UPDATE story_integration_intents SET status='running', claim_lock=?, "
            "claim_expires=?, attempt_count=attempt_count+1, updated_at=? "
            "WHERE epic_id=? AND story_id=? AND source_sha=? "
            "AND (status='pending' OR (status='running' AND claim_expires IS NOT NULL "
            "AND claim_expires<=?))",
            (
                claim_lock,
                now + int(lease_seconds),
                now,
                row["epic_id"],
                row["story_id"],
                row["source_sha"],
                now,
            ),
        )
        if updated.rowcount != 1:
            return None
        claimed_row = conn.execute(
            "SELECT * FROM story_integration_intents "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (row["epic_id"], row["story_id"], row["source_sha"]),
        ).fetchone()
        if claimed_row is None:
            raise RuntimeError("integration claim was not durable")
        intent = integration_intent_from_row(claimed_row)

    approved: ApprovedCandidate | None = None
    passed: PassedTest | None = None
    with kb.authorized_governance_write(), kb.write_txn(conn):
        current = conn.execute(
            "SELECT * FROM story_integration_intents "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (intent.key.epic_id, intent.key.story_id, intent.key.source_sha),
        ).fetchone()
        membership = conn.execute(
            "SELECT 1 FROM epic_memberships WHERE epic_id=? AND task_id=?",
            (intent.key.epic_id, intent.key.story_id),
        ).fetchone()
        story = conn.execute(
            "SELECT workflow_template_id, current_step_key, status, assignee, "
            "running, blocked, current_run_id, branch_name FROM tasks WHERE id=?",
            (intent.key.story_id,),
        ).fetchone()
        epic = conn.execute(
            "SELECT work_item_kind, workflow_template_id, current_step_key, status, "
            "running, blocked, current_run_id FROM tasks WHERE id=?",
            (intent.key.epic_id,),
        ).fetchone()
        records = kb._terminal_run_records(conn, intent.key.story_id)
        approved = kb.latest_review_authority(records)
        passed = (
            kb.latest_test_authority(records, intent.key.source_sha)
            if approved is not None
            else None
        )
        fresh = (
            current is not None
            and current["status"] == "running"
            and current["claim_lock"] == claim_lock
            and membership is not None
            and story is not None
            and story["workflow_template_id"] == "product"
            and story["current_step_key"] == "integration_pending"
            and story["status"] == "review"
            and story["assignee"] is None
            and not bool(story["running"])
            and not bool(story["blocked"])
            and story["current_run_id"] is None
            and story["branch_name"] == intent.source_branch
            and epic is not None
            and epic["work_item_kind"] == "epic"
            and epic["workflow_template_id"] == "product_epic"
            and epic["current_step_key"] == "collecting_members"
            and epic["status"] == "todo"
            and not bool(epic["running"])
            and not bool(epic["blocked"])
            and epic["current_run_id"] is None
            and approved is not None
            and passed is not None
            and approved.run_id == intent.review_run_id
            and approved.branch == intent.source_branch
            and approved.base_sha == intent.review_base_sha
            and approved.source_sha == intent.key.source_sha
            and passed.branch == intent.source_branch
            and passed.source_sha == intent.key.source_sha
            and kb._agent_compare_key(passed.writer_provider)
            == kb._agent_compare_key(approved.writer_provider)
            and kb.active_rework_directive(conn, intent.key.story_id) is None
        )
        if not fresh:
            conn.execute(
                "UPDATE story_integration_intents SET status='superseded', "
                "claim_lock=NULL, claim_expires=NULL, updated_at=? "
                "WHERE epic_id=? AND story_id=? AND source_sha=? "
                "AND status='running' AND claim_lock=?",
                (
                    now,
                    intent.key.epic_id,
                    intent.key.story_id,
                    intent.key.source_sha,
                    claim_lock,
                ),
            )
            return None

    slug = board if board is not None else kb._known_board_slug_for_connection(conn)
    metadata = kb.product_board_metadata(slug)
    try:
        contract = (
            kb.repository_contract_for_metadata(metadata)
            if metadata is not None
            else None
        )
    except Exception:
        contract = None
    if contract is None or "story_integration" not in contract.verification:
        return None

    assert approved is not None and passed is not None
    check = repository_check or (
        lambda contract, approved, passed: candidate_eligibility(
            contract.repo_root, approved, passed
        )
    )
    try:
        eligibility = check(contract, approved, passed)
    except CandidateEligibilityError:
        return None
    if (
        not isinstance(eligibility, CandidateEligibility)
        or eligibility.source_sha != intent.key.source_sha
        or not eligibility.non_empty
    ):
        return None
    return intent


def _current_intent(
    conn: sqlite3.Connection, key: IntegrationKey
) -> IntegrationIntent:
    row = conn.execute(
        "SELECT * FROM story_integration_intents "
        "WHERE epic_id=? AND story_id=? AND source_sha=?",
        (key.epic_id, key.story_id, key.source_sha),
    ).fetchone()
    if row is None:
        raise ValueError("integration intent no longer exists")
    return integration_intent_from_row(row)


def _prepared_receipt_is_exact(
    conn: sqlite3.Connection,
    intent: IntegrationIntent,
    contract: RepositoryContract,
) -> bool:
    if (
        intent.status != "prepared"
        or intent.target_pre_sha is None
        or intent.candidate_sha is None
        or not intent.candidate_ref
        or intent.verification_event_id is None
    ):
        return False
    row = conn.execute(
        "SELECT payload FROM task_events WHERE id=? AND task_id=? "
        "AND kind='repository_verification'",
        (intent.verification_event_id, intent.key.story_id),
    ).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row["payload"]) if row["payload"] else None
    except (TypeError, ValueError):
        return False
    return isinstance(payload, Mapping) and verification_receipt_matches(
        payload,
        source_sha=intent.key.source_sha,
        candidate_sha=intent.candidate_sha,
        contract_digest=contract.digest,
        gate_kind="story_integration",
        subject_id=intent.key.story_id,
        profile_name="story_integration",
    )


def prepare_claimed_intent(
    conn: sqlite3.Connection,
    intent: IntegrationIntent,
    *,
    board: str | None = None,
    candidate_builder: Callable[..., object] | None = None,
) -> IntegrationIntent:
    """Build, verify, and durably prepare one exact claimed story candidate.

    Repository work runs before the state transaction.  The transaction then
    records the exact verification event and prepared row together; it never
    moves the Epic ref or finalizes an integration fact.
    """

    from hermes_cli import kanban_db as kb

    if not isinstance(intent, IntegrationIntent):
        raise ValueError("claimed integration intent is required")
    slug = board if board is not None else kb._known_board_slug_for_connection(conn)
    metadata = kb.product_board_metadata(slug)
    contract = (
        kb.repository_contract_for_metadata(metadata)
        if metadata is not None
        else None
    )
    if contract is None or "story_integration" not in contract.verification:
        raise ValueError("story integration repository contract is required")

    current = _current_intent(conn, intent.key)
    if current.status == "prepared":
        if not _prepared_receipt_is_exact(conn, current, contract):
            raise ValueError("prepared integration receipt does not match")
        return current
    if (
        current != intent
        or current.status != "running"
        or not current.claim_lock
    ):
        raise ValueError("integration claim changed before preparation")
    if conn.in_transaction:
        raise ValueError("candidate preparation requires no active DB transaction")

    target_branch = kb.epic_branch_for(intent.key.epic_id)
    builder = candidate_builder or kb._build_verified_merge_candidate
    candidate = builder(
        contract.repo_root,
        target_branch,
        intent.source_branch,
        f"integrate story {intent.key.story_id}",
        expected_source_sha=intent.key.source_sha,
        verification_profile=contract.verification["story_integration"],
        verification_contract_digest=contract.digest,
        verification_scope="story_integration",
        verification_subject_id=intent.key.story_id,
        verification_profile_name="story_integration",
        verification_generated_policy_digest=contract.generated_policy_digest,
    )
    if conn.in_transaction:
        raise ValueError("candidate builder opened a DB transaction")
    if not isinstance(candidate, kb.IntegrationCandidate):
        raise ValueError("candidate builder returned an invalid candidate")
    verification = candidate.verification_result
    if (
        candidate.repo_root.resolve() != contract.repo_root
        or candidate.target_branch != target_branch
        or candidate.source_branch != intent.source_branch
        or candidate.source_sha != intent.key.source_sha
        or _FULL_SHA_RE.fullmatch(candidate.pre_sha) is None
        or _FULL_SHA_RE.fullmatch(candidate.candidate_sha) is None
        or not candidate.candidate_ref.startswith(
            "refs/hermes/integration-candidates/"
        )
        or not isinstance(verification, VerificationResult)
        or verification.status != "passed"
    ):
        raise ValueError("candidate builder returned mismatched preparation evidence")
    payload = verification_result_payload(
        verification,
        scope="story_integration",
        subject_id=intent.key.story_id,
    )
    if not verification_receipt_matches(
        payload,
        source_sha=intent.key.source_sha,
        candidate_sha=candidate.candidate_sha,
        contract_digest=contract.digest,
        gate_kind="story_integration",
        subject_id=intent.key.story_id,
        profile_name="story_integration",
    ):
        raise ValueError("candidate verification receipt does not match")

    now = int(time.time())
    with kb.authorized_governance_write(), kb.write_txn(conn):
        locked = _current_intent(conn, intent.key)
        if locked != current:
            raise ValueError("integration claim changed during preparation")
        event_id = kb._append_event(
            conn,
            intent.key.story_id,
            "repository_verification",
            payload,
        )
        updated = conn.execute(
            "UPDATE story_integration_intents SET status='prepared', "
            "claim_lock=NULL, claim_expires=NULL, target_pre_sha=?, "
            "candidate_sha=?, candidate_ref=?, verification_event_id=?, "
            "last_failure_code=NULL, updated_at=? "
            "WHERE epic_id=? AND story_id=? AND source_sha=? "
            "AND status='running' AND claim_lock=?",
            (
                candidate.pre_sha,
                candidate.candidate_sha,
                candidate.candidate_ref,
                event_id,
                now,
                intent.key.epic_id,
                intent.key.story_id,
                intent.key.source_sha,
                current.claim_lock,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("integration claim changed during preparation")
        prepared = _current_intent(conn, intent.key)
        if not _prepared_receipt_is_exact(conn, prepared, contract):
            raise ValueError("prepared integration receipt was not durable")
        return prepared


def enqueue_approved_story(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    story_id: str,
    approved: ApprovedCandidate,
    passed: PassedTest,
    eligibility: CandidateEligibility,
    expected_run_id: int,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> IntegrationIntent:
    """Atomically close Review and enqueue one approved Epic-member source.

    The supplied immutable authority values are comparison inputs, not proof:
    the transaction re-reads membership, task/run ownership, and the complete
    ended Test/Review history before it writes the intent.  No repository or
    Git operation belongs in this state-only transition.
    """

    from hermes_cli import kanban_db as kb

    if not isinstance(approved, ApprovedCandidate):
        raise ValueError("approved authority must be an ApprovedCandidate")
    if not isinstance(passed, PassedTest):
        raise ValueError("test authority must be a PassedTest")
    if not isinstance(eligibility, CandidateEligibility):
        raise ValueError("eligibility must be a CandidateEligibility")
    if (
        approved.source_sha != passed.source_sha
        or approved.source_sha != eligibility.source_sha
        or approved.branch != passed.branch
        or not eligibility.non_empty
        or approved.run_id != expected_run_id
    ):
        raise ValueError("integration authority does not identify one eligible candidate")

    now = int(time.time())
    with kb.authorized_governance_write(), kb.write_txn(conn):
        membership = conn.execute(
            "SELECT 1 FROM epic_memberships WHERE epic_id=? AND task_id=?",
            (epic_id, story_id),
        ).fetchone()
        if membership is None:
            raise ValueError("story is not a current member of the Epic")

        epic = conn.execute(
            "SELECT work_item_kind, workflow_template_id, current_step_key, status "
            "FROM tasks WHERE id=?",
            (epic_id,),
        ).fetchone()
        if (
            epic is None
            or epic["work_item_kind"] != "epic"
            or epic["status"] in {"done", "archived"}
        ):
            raise ValueError("integration parent is not a collecting Epic")

        task = conn.execute(
            "SELECT workflow_template_id, current_step_key, status, current_run_id "
            "FROM tasks WHERE id=?",
            (story_id,),
        ).fetchone()
        if task is None:
            raise ValueError("story does not exist")

        existing = conn.execute(
            "SELECT * FROM story_integration_intents "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (epic_id, story_id, approved.source_sha),
        ).fetchone()
        replay = (
            existing is not None
            and task["workflow_template_id"] == "product"
            and task["current_step_key"] == "integration_pending"
            and task["current_run_id"] is None
            and epic["workflow_template_id"] == "product_epic"
            and epic["current_step_key"] == "collecting_members"
        )

        if replay:
            records = kb._terminal_run_records(conn, story_id)
            if (
                kb.latest_review_authority(records) != approved
                or kb.latest_test_authority(records, approved.source_sha) != passed
                or kb.active_rework_directive(conn, story_id) is not None
            ):
                raise ValueError("stored integration authority is stale")
            return integration_intent_from_row(existing)

        if (
            task["workflow_template_id"] != "product"
            or task["current_step_key"] != "review"
            or task["status"] != "running"
            or task["current_run_id"] != expected_run_id
        ):
            raise ValueError("review run ownership changed")
        run_id = kb._end_run(
            conn,
            story_id,
            outcome="advanced",
            status="completed",
            summary=summary,
            metadata=metadata,
            expected_run_id=expected_run_id,
        )
        if run_id != expected_run_id:
            raise ValueError("review run ownership changed")

        records = kb._terminal_run_records(conn, story_id)
        if (
            kb.latest_review_authority(records) != approved
            or kb.latest_test_authority(records, approved.source_sha) != passed
            or kb.active_rework_directive(conn, story_id) is not None
        ):
            raise ValueError("latest Test/Review authority is not eligible")

        conn.execute(
            "UPDATE story_integration_intents SET status='superseded', updated_at=? "
            "WHERE epic_id=? AND story_id=? AND source_sha<>? "
            "AND status IN ('pending', 'attention_required')",
            (now, epic_id, story_id, approved.source_sha),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO story_integration_intents (
                epic_id, story_id, source_sha, source_branch, review_run_id,
                review_base_sha, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                epic_id,
                story_id,
                approved.source_sha,
                approved.branch,
                approved.run_id,
                approved.base_sha,
                now,
                now,
            ),
        )
        updated = conn.execute(
            "UPDATE tasks SET workflow_template_id='product', "
            "current_step_key='integration_pending', status='review', assignee=NULL, "
            "running=0, blocked=0, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL, result=? "
            "WHERE id=? AND current_step_key='review' AND current_run_id IS NULL",
            (summary, story_id),
        )
        if updated.rowcount != 1:
            raise ValueError("story phase changed during integration enqueue")
        parent_updated = conn.execute(
            "UPDATE tasks SET workflow_template_id='product_epic', "
            "current_step_key='collecting_members', status='todo', assignee=NULL, "
            "running=0, blocked=0, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
            "WHERE id=? AND work_item_kind='epic' "
            "AND status NOT IN ('done', 'archived')",
            (epic_id,),
        )
        if parent_updated.rowcount != 1:
            raise ValueError("Epic state changed during integration enqueue")
        kb._append_event(
            conn,
            story_id,
            "story_integration_enqueued",
            {
                "epic_id": epic_id,
                "story_id": story_id,
                "source_sha": approved.source_sha,
                "review_run_id": approved.run_id,
            },
            run_id=run_id,
        )
        row = conn.execute(
            "SELECT * FROM story_integration_intents "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (epic_id, story_id, approved.source_sha),
        ).fetchone()
        if row is None:
            raise RuntimeError("integration intent insert was not durable")
        return integration_intent_from_row(row)
