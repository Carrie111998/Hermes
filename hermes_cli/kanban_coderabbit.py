"""Provider-neutral CodeRabbit evidence for exact-head Kanban review workflows.

The module accepts normalized, read-only review snapshots and reduces them to
one assessment per pull-request head.  It intentionally contains no CodeRabbit
or GitHub client, webhook registration, credentials, external writes, or Kanban
task creation.  A green check is only transport/status evidence: it never means
``clean`` unless a typed review summary also establishes that outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol, Sequence, cast

from hermes_cli import kanban_db as kb


AssessmentState = Literal[
    "pending",
    "clean",
    "actionable",
    "no_actionable_comments",
    "skipped",
    "paused",
    "rate_limited",
    "unavailable",
    "stale",
]
CheckStatus = Literal[
    "pending",
    "success",
    "skipped",
    "paused",
    "rate_limited",
    "unavailable",
]
FindingState = Literal["open", "resolved", "outdated", "superseded"]

ASSESSMENT_STATES = frozenset({
    "pending",
    "clean",
    "actionable",
    "no_actionable_comments",
    "skipped",
    "paused",
    "rate_limited",
    "unavailable",
    "stale",
})
SUMMARY_STATES = ASSESSMENT_STATES - {"stale"}
CHECK_STATUSES = frozenset({
    "pending",
    "success",
    "skipped",
    "paused",
    "rate_limited",
    "unavailable",
})
FINDING_STATES = frozenset({"open", "resolved", "outdated", "superseded"})
DEFAULT_MAX_CORRECTION_ATTEMPTS = 1
MAX_NORMALIZED_SNAPSHOT_BYTES = 128 * 1024
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


class CodeRabbitBoundaryError(ValueError):
    """Normalized evidence violates the exact-head review boundary."""


class CodeRabbitReplayConflict(CodeRabbitBoundaryError):
    """An observation/revision identity was reused for different evidence."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodeRabbitBoundaryError(f"{field} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CodeRabbitBoundaryError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CodeRabbitBoundaryError(
            f"{field} must be a non-negative integer"
        ) from exc
    if parsed < 0:
        raise CodeRabbitBoundaryError(f"{field} must be a non-negative integer")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed < 1:
        raise CodeRabbitBoundaryError(f"{field} must be a positive integer")
    return parsed


def _canonical_repository(value: Any) -> str:
    repository = _required_text(value, "repository").casefold()
    if not _REPOSITORY_RE.fullmatch(repository):
        raise CodeRabbitBoundaryError("repository must use owner/name form")
    return repository


def _full_sha(value: Any, field: str = "head_sha") -> str:
    sha = _required_text(value, field).casefold()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise CodeRabbitBoundaryError(
            f"{field} must be a full 40-character lowercase SHA"
        )
    return sha


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _canonical_json_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CodeRabbitReviewSummary:
    """Typed semantic result from the review summary, not raw prose."""

    state: AssessmentState
    actionable_count: int = 0

    def __post_init__(self) -> None:
        state = _required_text(self.state, "summary.state").casefold()
        if state not in SUMMARY_STATES:
            raise CodeRabbitBoundaryError(
                f"summary.state must be one of {sorted(SUMMARY_STATES)!r}"
            )
        count = _nonnegative_int(self.actionable_count, "summary.actionable_count")
        if state == "actionable" and count < 1:
            raise CodeRabbitBoundaryError(
                "an actionable review summary requires actionable_count > 0"
            )
        if state in {"clean", "no_actionable_comments"} and count:
            raise CodeRabbitBoundaryError(
                f"a {state} review summary cannot report actionable findings"
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "actionable_count", count)

    def normalized_dict(self) -> dict[str, Any]:
        return {"state": self.state, "actionable_count": self.actionable_count}


@dataclass(frozen=True)
class CodeRabbitComment:
    """One normalized review comment without unstable prose or API payloads."""

    comment_id: str
    head_sha: str
    state: FindingState
    actionable: bool
    thread_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "comment_id",
            _required_text(self.comment_id, "comment.comment_id"),
        )
        object.__setattr__(
            self,
            "head_sha",
            _full_sha(self.head_sha, "comment.head_sha"),
        )
        state = _required_text(self.state, "comment.state").casefold()
        if state not in FINDING_STATES:
            raise CodeRabbitBoundaryError(
                f"comment.state must be one of {sorted(FINDING_STATES)!r}"
            )
        object.__setattr__(self, "state", state)
        if not isinstance(self.actionable, bool):
            raise CodeRabbitBoundaryError("comment.actionable must be a boolean")
        if self.thread_id is not None:
            object.__setattr__(
                self,
                "thread_id",
                _required_text(self.thread_id, "comment.thread_id"),
            )

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "thread_id": self.thread_id,
            "head_sha": self.head_sha,
            "state": self.state,
            "actionable": self.actionable,
        }


@dataclass(frozen=True)
class CodeRabbitThread:
    """One normalized review thread and its authoritative lifecycle state."""

    thread_id: str
    head_sha: str
    state: FindingState
    actionable: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "thread_id",
            _required_text(self.thread_id, "thread.thread_id"),
        )
        object.__setattr__(
            self,
            "head_sha",
            _full_sha(self.head_sha, "thread.head_sha"),
        )
        state = _required_text(self.state, "thread.state").casefold()
        if state not in FINDING_STATES:
            raise CodeRabbitBoundaryError(
                f"thread.state must be one of {sorted(FINDING_STATES)!r}"
            )
        object.__setattr__(self, "state", state)
        if not isinstance(self.actionable, bool):
            raise CodeRabbitBoundaryError("thread.actionable must be a boolean")

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "head_sha": self.head_sha,
            "state": self.state,
            "actionable": self.actionable,
        }


@dataclass(frozen=True)
class CodeRabbitSnapshot:
    """Complete normalized CodeRabbit evidence observed for one PR/head review."""

    provider: str
    observation_id: str
    repository: str
    pr_number: int
    head_sha: str
    review_generation: int
    observed_at: int
    check_status: CheckStatus
    summary: Optional[CodeRabbitReviewSummary] = None
    comments: tuple[CodeRabbitComment, ...] = ()
    threads: tuple[CodeRabbitThread, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _required_text(self.provider, "provider").casefold(),
        )
        object.__setattr__(
            self,
            "observation_id",
            _required_text(self.observation_id, "observation_id"),
        )
        object.__setattr__(self, "repository", _canonical_repository(self.repository))
        object.__setattr__(
            self, "pr_number", _positive_int(self.pr_number, "pr_number")
        )
        object.__setattr__(self, "head_sha", _full_sha(self.head_sha))
        object.__setattr__(
            self,
            "review_generation",
            _nonnegative_int(self.review_generation, "review_generation"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _nonnegative_int(self.observed_at, "observed_at"),
        )
        check_status = _required_text(self.check_status, "check_status").casefold()
        if check_status not in CHECK_STATUSES:
            raise CodeRabbitBoundaryError(
                f"check_status must be one of {sorted(CHECK_STATUSES)!r}"
            )
        object.__setattr__(self, "check_status", check_status)
        if self.summary is not None and not isinstance(
            self.summary,
            CodeRabbitReviewSummary,
        ):
            raise CodeRabbitBoundaryError(
                "summary must be a CodeRabbitReviewSummary or None"
            )

        comments = tuple(self.comments)
        if any(not isinstance(comment, CodeRabbitComment) for comment in comments):
            raise CodeRabbitBoundaryError(
                "comments must contain CodeRabbitComment values"
            )
        if len({comment.comment_id for comment in comments}) != len(comments):
            raise CodeRabbitBoundaryError(
                "comment IDs must be unique within a snapshot"
            )
        object.__setattr__(
            self,
            "comments",
            tuple(sorted(comments, key=lambda comment: comment.comment_id)),
        )

        threads = tuple(self.threads)
        if any(not isinstance(thread, CodeRabbitThread) for thread in threads):
            raise CodeRabbitBoundaryError(
                "threads must contain CodeRabbitThread values"
            )
        if len({thread.thread_id for thread in threads}) != len(threads):
            raise CodeRabbitBoundaryError("thread IDs must be unique within a snapshot")
        object.__setattr__(
            self,
            "threads",
            tuple(sorted(threads, key=lambda thread: thread.thread_id)),
        )

        encoded = _canonical_json(self.normalized_dict()).encode("utf-8")
        if len(encoded) > MAX_NORMALIZED_SNAPSHOT_BYTES:
            raise CodeRabbitBoundaryError(
                "normalized snapshot exceeds the 128 KiB audit limit"
            )

    def semantic_dict(self) -> dict[str, Any]:
        """Evidence identity excluding delivery ID and observation timestamp."""
        return {
            "provider": self.provider,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "review_generation": self.review_generation,
            "check_status": self.check_status,
            "summary": self.summary.normalized_dict() if self.summary else None,
            "comments": [comment.normalized_dict() for comment in self.comments],
            "threads": [thread.normalized_dict() for thread in self.threads],
        }

    def normalized_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "observation_id": self.observation_id,
            "observed_at": self.observed_at,
        }

    def snapshot_sha256(self) -> str:
        return _canonical_json_sha256(self.semantic_dict())

    def payload_sha256(self) -> str:
        return _canonical_json_sha256(self.normalized_dict())


class CodeRabbitSnapshotProvider(Protocol):
    """Read-only adapter protocol; no implementation is installed in this phase."""

    def read_review(
        self,
        *,
        repository: str,
        pr_number: int,
        expected_head_sha: str,
    ) -> CodeRabbitSnapshot: ...


@dataclass(frozen=True)
class CorrectionMetadata:
    correction_work_key: str
    loop_prevention_key: str
    attempt_count: int
    max_attempts: int

    @property
    def remaining_attempts(self) -> int:
        return max(0, self.max_attempts - self.attempt_count)


@dataclass(frozen=True)
class CodeRabbitAssessment:
    repository: str
    pr_number: int
    head_sha: str
    review_generation: int
    observed_at: int
    state: AssessmentState
    reason: str
    actionable_count: int
    unresolved_count: int
    resolved_count: int
    outdated_count: int
    superseded_count: int
    non_blocking_count: int
    actionable_finding_ids: tuple[str, ...]
    snapshot_sha256: str
    correction: CorrectionMetadata
    created_at: int
    updated_at: int

    def to_human_review_disposition(self, *, disposition: str = "") -> dict[str, Any]:
        """Return the typed shape consumed by the existing human-review gate."""
        return {
            "status": self.state,
            "disposition": disposition.strip(),
            "actionable_count": self.actionable_count,
            "unresolved_count": self.unresolved_count,
        }


@dataclass(frozen=True)
class ObservationReceipt:
    observation_id: str
    created: bool
    applied: bool
    outcome: str
    assessment: CodeRabbitAssessment


@dataclass(frozen=True)
class CorrectionAttemptReceipt:
    created: bool
    reason: str
    attempt_number: Optional[int]
    correction: CorrectionMetadata


@dataclass(frozen=True)
class _FindingMetrics:
    actionable_ids: tuple[str, ...]
    resolved_count: int
    outdated_count: int
    superseded_count: int
    non_blocking_count: int
    total_count: int


def _finding_metrics(snapshot: CodeRabbitSnapshot) -> _FindingMetrics:
    threads = {thread.thread_id: thread for thread in snapshot.threads}
    comments_by_group: dict[str, list[CodeRabbitComment]] = {}
    for comment in snapshot.comments:
        group = (
            f"thread:{comment.thread_id}"
            if comment.thread_id
            else f"comment:{comment.comment_id}"
        )
        comments_by_group.setdefault(group, []).append(comment)

    group_keys = set(comments_by_group)
    group_keys.update(f"thread:{thread_id}" for thread_id in threads)
    actionable_ids: list[str] = []
    resolved_count = 0
    outdated_count = 0
    superseded_count = 0
    non_blocking_count = 0

    for group_key in sorted(group_keys):
        thread_id = (
            group_key.removeprefix("thread:")
            if group_key.startswith("thread:")
            else None
        )
        thread = threads.get(thread_id) if thread_id else None
        comments = comments_by_group.get(group_key, [])

        if thread is not None and thread.state != "open":
            category = thread.state
        elif thread is not None and thread.head_sha != snapshot.head_sha:
            category = "outdated"
        else:
            current_open_actionable = bool(
                thread is not None
                and thread.state == "open"
                and thread.head_sha == snapshot.head_sha
                and thread.actionable
            ) or any(
                comment.state == "open"
                and comment.head_sha == snapshot.head_sha
                and comment.actionable
                for comment in comments
            )
            current_open = bool(
                thread is not None
                and thread.state == "open"
                and thread.head_sha == snapshot.head_sha
            ) or any(
                comment.state == "open" and comment.head_sha == snapshot.head_sha
                for comment in comments
            )
            if current_open_actionable:
                category = "actionable"
            elif current_open:
                category = "non_blocking"
            else:
                states = {comment.state for comment in comments}
                if any(comment.head_sha != snapshot.head_sha for comment in comments):
                    states.add("outdated")
                if "superseded" in states:
                    category = "superseded"
                elif "outdated" in states:
                    category = "outdated"
                elif "resolved" in states:
                    category = "resolved"
                else:
                    category = "non_blocking"

        if category == "actionable":
            actionable_ids.append(group_key)
        elif category == "resolved":
            resolved_count += 1
        elif category == "outdated":
            outdated_count += 1
        elif category == "superseded":
            superseded_count += 1
        else:
            non_blocking_count += 1

    return _FindingMetrics(
        actionable_ids=tuple(actionable_ids),
        resolved_count=resolved_count,
        outdated_count=outdated_count,
        superseded_count=superseded_count,
        non_blocking_count=non_blocking_count,
        total_count=len(group_keys),
    )


def _correction_work_key(repository: str, pr_number: int, head_sha: str) -> str:
    return f"coderabbit-correction:v1:{repository}:pr:{pr_number}:head:{head_sha}"


def _loop_prevention_key(
    repository: str,
    pr_number: int,
    head_sha: str,
    review_generation: int,
    snapshot_sha256: str,
) -> str:
    return (
        f"coderabbit-review-loop:v1:{repository}:pr:{pr_number}:head:{head_sha}:"
        f"generation:{review_generation}:snapshot:{snapshot_sha256}"
    )


def assess_snapshot(
    snapshot: CodeRabbitSnapshot,
    *,
    current_head_sha: str,
    correction_attempt_count: int = 0,
    max_correction_attempts: int = DEFAULT_MAX_CORRECTION_ATTEMPTS,
    now: Optional[int] = None,
) -> CodeRabbitAssessment:
    """Classify one normalized snapshot against a trusted current full SHA."""
    if not isinstance(snapshot, CodeRabbitSnapshot):
        raise CodeRabbitBoundaryError("snapshot must be a CodeRabbitSnapshot")
    current_head = _full_sha(current_head_sha, "current_head_sha")
    attempt_count = _nonnegative_int(
        correction_attempt_count,
        "correction_attempt_count",
    )
    max_attempts = _nonnegative_int(
        max_correction_attempts,
        "max_correction_attempts",
    )
    if attempt_count > max_attempts:
        raise CodeRabbitBoundaryError(
            "correction_attempt_count cannot exceed max_correction_attempts"
        )

    metrics = _finding_metrics(snapshot)
    summary_count = snapshot.summary.actionable_count if snapshot.summary else 0
    actionable_count = max(len(metrics.actionable_ids), summary_count)
    digest = snapshot.snapshot_sha256()

    if snapshot.head_sha != current_head:
        state: AssessmentState = "stale"
        reason = "snapshot_head_superseded_by_current_pr_head"
    elif actionable_count:
        state = "actionable"
        reason = "current_head_has_actionable_review_findings"
    else:
        summary_state = snapshot.summary.state if snapshot.summary else None
        constrained = next(
            (
                candidate
                for candidate in (
                    summary_state,
                    snapshot.check_status,
                )
                if candidate in {"skipped", "paused", "rate_limited", "unavailable"}
            ),
            None,
        )
        if constrained is not None:
            state = cast(AssessmentState, constrained)
            reason = f"review_evidence_{constrained}"
        elif snapshot.check_status == "pending" or summary_state == "pending":
            state = "pending"
            reason = "review_evidence_pending"
        elif summary_state == "no_actionable_comments":
            state = "no_actionable_comments"
            reason = "typed_summary_reports_no_actionable_comments"
        elif summary_state == "clean":
            if metrics.total_count:
                state = "no_actionable_comments"
                reason = "typed_clean_summary_with_only_non_actionable_history"
            else:
                state = "clean"
                reason = "typed_clean_summary_for_current_head"
        else:
            state = "pending"
            reason = (
                "success_without_semantic_review_summary"
                if (snapshot.check_status == "success")
                else "review_evidence_pending"
            )

    checked_at = int(time.time()) if now is None else int(now)
    correction = CorrectionMetadata(
        correction_work_key=_correction_work_key(
            snapshot.repository,
            snapshot.pr_number,
            snapshot.head_sha,
        ),
        loop_prevention_key=_loop_prevention_key(
            snapshot.repository,
            snapshot.pr_number,
            snapshot.head_sha,
            snapshot.review_generation,
            digest,
        ),
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )
    return CodeRabbitAssessment(
        repository=snapshot.repository,
        pr_number=snapshot.pr_number,
        head_sha=snapshot.head_sha,
        review_generation=snapshot.review_generation,
        observed_at=snapshot.observed_at,
        state=state,
        reason=reason,
        actionable_count=actionable_count,
        unresolved_count=actionable_count,
        resolved_count=metrics.resolved_count,
        outdated_count=metrics.outdated_count,
        superseded_count=metrics.superseded_count,
        non_blocking_count=metrics.non_blocking_count,
        actionable_finding_ids=metrics.actionable_ids,
        snapshot_sha256=digest,
        correction=correction,
        created_at=checked_at,
        updated_at=checked_at,
    )


def get_head_assessment(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
) -> Optional[CodeRabbitAssessment]:
    row = conn.execute(
        "SELECT * FROM coderabbit_head_assessments "
        "WHERE repository=? AND pr_number=? AND head_sha=?",
        (
            _canonical_repository(repository),
            _positive_int(pr_number, "pr_number"),
            _full_sha(head_sha),
        ),
    ).fetchone()
    return _assessment_from_row(row) if row is not None else None


def get_current_assessment(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
) -> Optional[CodeRabbitAssessment]:
    canonical_repository = _canonical_repository(repository)
    number = _positive_int(pr_number, "pr_number")
    row = conn.execute(
        "SELECT a.* FROM coderabbit_pr_heads h "
        "LEFT JOIN coderabbit_head_assessments a "
        "ON a.repository=h.repository AND a.pr_number=h.pr_number "
        "AND a.head_sha=h.current_head_sha "
        "WHERE h.repository=? AND h.pr_number=?",
        (canonical_repository, number),
    ).fetchone()
    if row is None or row["repository"] is None:
        return None
    return _assessment_from_row(row)


def _set_current_head(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    current_head_sha: str,
    head_observed_at: int,
    changed_at: int,
) -> str:
    existing = conn.execute(
        "SELECT current_head_sha, observed_at FROM coderabbit_pr_heads "
        "WHERE repository=? AND pr_number=?",
        (repository, pr_number),
    ).fetchone()
    if existing is not None:
        existing_observed_at = int(existing["observed_at"])
        if head_observed_at < existing_observed_at:
            return existing["current_head_sha"]
        if (
            head_observed_at == existing_observed_at
            and existing["current_head_sha"] != current_head_sha
        ):
            raise CodeRabbitReplayConflict(
                "trusted PR head observation timestamp was reused for a different SHA"
            )
    if existing is not None and existing["current_head_sha"] != current_head_sha:
        conn.execute(
            "UPDATE coderabbit_head_assessments SET state='stale', "
            "reason='assessment_head_superseded_by_current_pr_head', updated_at=? "
            "WHERE repository=? AND pr_number=? AND head_sha<>? AND state<>'stale'",
            (changed_at, repository, pr_number, current_head_sha),
        )
    conn.execute(
        """
        INSERT INTO coderabbit_pr_heads (
            repository, pr_number, current_head_sha, observed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(repository, pr_number) DO UPDATE SET
            current_head_sha=excluded.current_head_sha,
            observed_at=excluded.observed_at,
            updated_at=excluded.updated_at
        """,
        (repository, pr_number, current_head_sha, head_observed_at, changed_at),
    )
    return current_head_sha


def _upsert_assessment(
    conn: sqlite3.Connection,
    assessment: CodeRabbitAssessment,
    *,
    changed_at: int,
) -> CodeRabbitAssessment:
    existing = get_head_assessment(
        conn,
        repository=assessment.repository,
        pr_number=assessment.pr_number,
        head_sha=assessment.head_sha,
    )
    correction_attempt_count = (
        existing.correction.attempt_count if existing is not None else 0
    )
    max_correction_attempts = (
        existing.correction.max_attempts
        if existing is not None
        else assessment.correction.max_attempts
    )
    created_at = existing.created_at if existing is not None else changed_at
    conn.execute(
        """
        INSERT INTO coderabbit_head_assessments (
            repository, pr_number, head_sha, review_generation, observed_at,
            state, reason, actionable_count, unresolved_count, resolved_count,
            outdated_count, superseded_count, non_blocking_count,
            actionable_finding_ids_json, snapshot_sha256, correction_work_key,
            loop_prevention_key, correction_attempt_count,
            max_correction_attempts, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository, pr_number, head_sha) DO UPDATE SET
            review_generation=excluded.review_generation,
            observed_at=excluded.observed_at,
            state=excluded.state,
            reason=excluded.reason,
            actionable_count=excluded.actionable_count,
            unresolved_count=excluded.unresolved_count,
            resolved_count=excluded.resolved_count,
            outdated_count=excluded.outdated_count,
            superseded_count=excluded.superseded_count,
            non_blocking_count=excluded.non_blocking_count,
            actionable_finding_ids_json=excluded.actionable_finding_ids_json,
            snapshot_sha256=excluded.snapshot_sha256,
            loop_prevention_key=excluded.loop_prevention_key,
            updated_at=excluded.updated_at
        """,
        (
            assessment.repository,
            assessment.pr_number,
            assessment.head_sha,
            assessment.review_generation,
            assessment.observed_at,
            assessment.state,
            assessment.reason,
            assessment.actionable_count,
            assessment.unresolved_count,
            assessment.resolved_count,
            assessment.outdated_count,
            assessment.superseded_count,
            assessment.non_blocking_count,
            _canonical_json({"ids": list(assessment.actionable_finding_ids)}),
            assessment.snapshot_sha256,
            assessment.correction.correction_work_key,
            assessment.correction.loop_prevention_key,
            correction_attempt_count,
            max_correction_attempts,
            created_at,
            changed_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM coderabbit_head_assessments "
        "WHERE repository=? AND pr_number=? AND head_sha=?",
        (assessment.repository, assessment.pr_number, assessment.head_sha),
    ).fetchone()
    assert row is not None
    return _assessment_from_row(row)


def _decode_finding_ids(row: sqlite3.Row) -> tuple[str, ...]:
    try:
        decoded = json.loads(row["actionable_finding_ids_json"])
    except (TypeError, json.JSONDecodeError):
        return ()
    if isinstance(decoded, dict):
        raw_ids = decoded.get("ids", [])
    else:
        raw_ids = decoded
    if not isinstance(raw_ids, list):
        return ()
    return tuple(str(value) for value in raw_ids)


def _assessment_from_row(row: sqlite3.Row) -> CodeRabbitAssessment:
    correction = CorrectionMetadata(
        correction_work_key=row["correction_work_key"],
        loop_prevention_key=row["loop_prevention_key"],
        attempt_count=int(row["correction_attempt_count"]),
        max_attempts=int(row["max_correction_attempts"]),
    )
    return CodeRabbitAssessment(
        repository=row["repository"],
        pr_number=int(row["pr_number"]),
        head_sha=row["head_sha"],
        review_generation=int(row["review_generation"]),
        observed_at=int(row["observed_at"]),
        state=row["state"],
        reason=row["reason"],
        actionable_count=int(row["actionable_count"]),
        unresolved_count=int(row["unresolved_count"]),
        resolved_count=int(row["resolved_count"]),
        outdated_count=int(row["outdated_count"]),
        superseded_count=int(row["superseded_count"]),
        non_blocking_count=int(row["non_blocking_count"]),
        actionable_finding_ids=_decode_finding_ids(row),
        snapshot_sha256=row["snapshot_sha256"],
        correction=correction,
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def record_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot: CodeRabbitSnapshot,
    current_head_sha: str,
    current_head_observed_at: int,
    now: Optional[int] = None,
) -> ObservationReceipt:
    """Persist one observation and update its PR/head aggregate idempotently."""
    if not isinstance(snapshot, CodeRabbitSnapshot):
        raise CodeRabbitBoundaryError("snapshot must be a CodeRabbitSnapshot")
    proposed_current_head = _full_sha(current_head_sha, "current_head_sha")
    head_observed_at = _nonnegative_int(
        current_head_observed_at,
        "current_head_observed_at",
    )
    received_at = int(time.time()) if now is None else int(now)
    snapshot_sha256 = snapshot.snapshot_sha256()
    payload_sha256 = snapshot.payload_sha256()
    snapshot_json = _canonical_json(snapshot.normalized_dict())

    with kb.write_txn(conn):
        current_head = _set_current_head(
            conn,
            repository=snapshot.repository,
            pr_number=snapshot.pr_number,
            current_head_sha=proposed_current_head,
            head_observed_at=head_observed_at,
            changed_at=received_at,
        )

        existing_observation = conn.execute(
            "SELECT * FROM coderabbit_review_observations "
            "WHERE provider=? AND observation_id=?",
            (snapshot.provider, snapshot.observation_id),
        ).fetchone()
        if existing_observation is not None:
            if existing_observation["payload_sha256"] != payload_sha256:
                raise CodeRabbitReplayConflict(
                    "provider observation_id was reused for different evidence"
                )
            assessment = get_head_assessment(
                conn,
                repository=snapshot.repository,
                pr_number=snapshot.pr_number,
                head_sha=snapshot.head_sha,
            )
            if assessment is None:
                raise CodeRabbitBoundaryError(
                    "stored observation is missing its head assessment"
                )
            return ObservationReceipt(
                observation_id=snapshot.observation_id,
                created=False,
                applied=False,
                outcome="duplicate_observation",
                assessment=assessment,
            )

        existing = get_head_assessment(
            conn,
            repository=snapshot.repository,
            pr_number=snapshot.pr_number,
            head_sha=snapshot.head_sha,
        )
        if existing is not None and (
            snapshot.review_generation == existing.review_generation
            and snapshot.observed_at == existing.observed_at
            and snapshot_sha256 != existing.snapshot_sha256
        ):
            raise CodeRabbitReplayConflict(
                "review generation and observed_at were reused for different evidence"
            )

        out_of_order = existing is not None and (
            snapshot.review_generation < existing.review_generation
            or (
                snapshot.review_generation == existing.review_generation
                and snapshot.observed_at <= existing.observed_at
            )
        )
        duplicate_snapshot = (
            out_of_order
            and existing is not None
            and snapshot_sha256 == existing.snapshot_sha256
        )
        classified = assess_snapshot(
            snapshot,
            current_head_sha=current_head,
            correction_attempt_count=(
                existing.correction.attempt_count if existing is not None else 0
            ),
            max_correction_attempts=(
                existing.correction.max_attempts
                if existing is not None
                else DEFAULT_MAX_CORRECTION_ATTEMPTS
            ),
            now=received_at,
        )

        if duplicate_snapshot:
            outcome = "duplicate_snapshot"
            applied = False
            result_assessment = existing
            assert result_assessment is not None
            observation_state = result_assessment.state
            observation_reason = "duplicate_current_snapshot"
        elif out_of_order:
            outcome = "out_of_order"
            applied = False
            result_assessment = existing
            assert result_assessment is not None
            observation_state = "stale"
            observation_reason = "out_of_order_observation"
        else:
            result_assessment = _upsert_assessment(
                conn,
                classified,
                changed_at=received_at,
            )
            applied = snapshot.head_sha == current_head
            outcome = "applied" if applied else "head_superseded"
            observation_state = result_assessment.state
            observation_reason = result_assessment.reason

        conn.execute(
            """
            INSERT INTO coderabbit_review_observations (
                provider, observation_id, repository, pr_number, head_sha,
                review_generation, observed_at, check_status, snapshot_json,
                snapshot_sha256, payload_sha256, assessment_state,
                assessment_reason, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.provider,
                snapshot.observation_id,
                snapshot.repository,
                snapshot.pr_number,
                snapshot.head_sha,
                snapshot.review_generation,
                snapshot.observed_at,
                snapshot.check_status,
                snapshot_json,
                snapshot_sha256,
                payload_sha256,
                observation_state,
                observation_reason,
                received_at,
            ),
        )
        return ObservationReceipt(
            observation_id=snapshot.observation_id,
            created=True,
            applied=applied,
            outcome=outcome,
            assessment=result_assessment,
        )


def reserve_correction_attempt(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    loop_prevention_key: str,
    now: Optional[int] = None,
) -> CorrectionAttemptReceipt:
    """Reserve bounded correction metadata; never create a Kanban task/card."""
    canonical_repository = _canonical_repository(repository)
    number = _positive_int(pr_number, "pr_number")
    exact_head = _full_sha(head_sha)
    loop_key = _required_text(loop_prevention_key, "loop_prevention_key")
    changed_at = int(time.time()) if now is None else int(now)

    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT current_head_sha FROM coderabbit_pr_heads "
            "WHERE repository=? AND pr_number=?",
            (canonical_repository, number),
        ).fetchone()
        if current is None or current["current_head_sha"] != exact_head:
            raise CodeRabbitBoundaryError(
                "correction attempt head is not the trusted current PR head"
            )
        assessment = get_head_assessment(
            conn,
            repository=canonical_repository,
            pr_number=number,
            head_sha=exact_head,
        )
        if assessment is None:
            raise CodeRabbitBoundaryError("current head has no CodeRabbit assessment")
        if loop_key != assessment.correction.loop_prevention_key:
            raise CodeRabbitBoundaryError(
                "correction loop key does not match the current assessment"
            )

        duplicate = conn.execute(
            "SELECT attempt_number FROM coderabbit_correction_attempts "
            "WHERE repository=? AND pr_number=? AND head_sha=? "
            "AND loop_prevention_key=?",
            (canonical_repository, number, exact_head, loop_key),
        ).fetchone()
        if duplicate is not None:
            return CorrectionAttemptReceipt(
                created=False,
                reason="duplicate_loop_key",
                attempt_number=int(duplicate["attempt_number"]),
                correction=assessment.correction,
            )
        if assessment.state != "actionable":
            return CorrectionAttemptReceipt(
                created=False,
                reason="assessment_not_actionable",
                attempt_number=None,
                correction=assessment.correction,
            )
        if assessment.correction.attempt_count >= assessment.correction.max_attempts:
            return CorrectionAttemptReceipt(
                created=False,
                reason="max_attempts_reached",
                attempt_number=None,
                correction=assessment.correction,
            )

        attempt_number = assessment.correction.attempt_count + 1
        conn.execute(
            """
            INSERT INTO coderabbit_correction_attempts (
                repository, pr_number, head_sha, review_generation,
                correction_work_key, loop_prevention_key, attempt_number,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_repository,
                number,
                exact_head,
                assessment.review_generation,
                assessment.correction.correction_work_key,
                loop_key,
                attempt_number,
                changed_at,
            ),
        )
        conn.execute(
            "UPDATE coderabbit_head_assessments "
            "SET correction_attempt_count=?, updated_at=? "
            "WHERE repository=? AND pr_number=? AND head_sha=?",
            (attempt_number, changed_at, canonical_repository, number, exact_head),
        )
        refreshed = get_head_assessment(
            conn,
            repository=canonical_repository,
            pr_number=number,
            head_sha=exact_head,
        )
        assert refreshed is not None
        return CorrectionAttemptReceipt(
            created=True,
            reason="reserved",
            attempt_number=attempt_number,
            correction=refreshed.correction,
        )


def list_observation_ids(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
) -> tuple[str, ...]:
    rows: Sequence[sqlite3.Row] = conn.execute(
        "SELECT observation_id FROM coderabbit_review_observations "
        "WHERE repository=? AND pr_number=? ORDER BY id",
        (_canonical_repository(repository), _positive_int(pr_number, "pr_number")),
    ).fetchall()
    return tuple(row["observation_id"] for row in rows)
