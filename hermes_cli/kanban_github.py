"""Provider-neutral GitHub readback and exact-head human-review outbox.

GitHub is authoritative for pull-request identity, state, checks, requested
reviewers, reviews, and review threads.  This module normalizes that readback,
requires a fresh exact-head snapshot before each restricted outbound intent,
and persists replay-safe delivery state.  No live adapter is installed by
default, and the transport surface deliberately exposes no merge, approval,
branch-write, push, or auto-merge operation.

A future live adapter must map the semantic surfaces in ``SURFACE_OPERATIONS``
to its provider API.  Reviewer-request permissions and concrete REST/GraphQL
request shapes are intentionally left unverified here instead of being guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Protocol, cast

from hermes_cli import kanban_db as kb


PullRequestState = Literal["open", "closed", "merged"]
CheckStatus = Literal["queued", "in_progress", "completed"]
CheckConclusion = Literal[
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
]
ReviewState = Literal[
    "pending",
    "commented",
    "approved",
    "changes_requested",
    "dismissed",
]
ReviewerKind = Literal["user", "team"]
GitHubSurface = Literal[
    "pull_request",
    "review_requests",
    "pull_request_comments",
]
GitHubOperation = Literal[
    "notify_human_review",
    "request_reviewer",
    "create_comment",
]
OutboxState = Literal[
    "pending",
    "attempting",
    "retry",
    "sent",
    "permanent_failure",
    "superseded",
]
FailureKind = Literal[
    "disabled",
    "network",
    "timeout",
    "rate_limited",
    "server",
    "unavailable",
    "auth",
    "permission",
    "not_found",
    "validation",
    "conflict",
    "unknown",
]
ReadinessState = Literal["ready", "pending", "blocked", "stale", "terminal"]

PULL_REQUEST_STATES = frozenset({"open", "closed", "merged"})
CHECK_STATUSES = frozenset({"queued", "in_progress", "completed"})
CHECK_CONCLUSIONS = frozenset({
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
})
REVIEW_STATES = frozenset({
    "pending",
    "commented",
    "approved",
    "changes_requested",
    "dismissed",
})
REVIEWER_KINDS = frozenset({"user", "team"})
SURFACE_OPERATIONS: Mapping[str, frozenset[str]] = {
    "pull_request": frozenset({"notify_human_review"}),
    "review_requests": frozenset({"request_reviewer"}),
    "pull_request_comments": frozenset({"create_comment"}),
}
OUTBOX_STATES = frozenset({
    "pending",
    "attempting",
    "retry",
    "sent",
    "permanent_failure",
    "superseded",
})
TERMINAL_OUTBOX_STATES = frozenset({"sent", "permanent_failure", "superseded"})
FAILURE_KINDS = frozenset({
    "disabled",
    "network",
    "timeout",
    "rate_limited",
    "server",
    "unavailable",
    "auth",
    "permission",
    "not_found",
    "validation",
    "conflict",
    "unknown",
})
RETRYABLE_FAILURE_KINDS = frozenset({
    "network",
    "timeout",
    "rate_limited",
    "server",
    "unavailable",
})
SUCCESSFUL_CHECK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
BLOCKING_CHECK_CONCLUSIONS = CHECK_CONCLUSIONS - SUCCESSFUL_CHECK_CONCLUSIONS
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 30
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60
MAX_SNAPSHOT_AGE_SECONDS = 300
MAX_SNAPSHOT_FUTURE_SKEW_SECONDS = 60
ATTEMPT_LEASE_SECONDS = 300
MAX_NORMALIZED_SNAPSHOT_BYTES = 256 * 1024
MAX_OUTBOX_PAYLOAD_BYTES = 128 * 1024
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "password",
    "private_key",
    "secret",
    "token",
)


class GitHubBoundaryError(ValueError):
    """Normalized GitHub evidence or intent violates the boundary."""


class GitHubReplayConflict(GitHubBoundaryError):
    """A stable provider/outbox identity was reused for different semantics."""


class GitHubHeadMismatch(GitHubBoundaryError):
    """A trusted PR readback is not for the gate's exact full head SHA."""


class GitHubPRTerminal(GitHubBoundaryError):
    """A closed or merged pull request cannot receive a current delivery."""


class GitHubSnapshotUnavailable(GitHubBoundaryError):
    """The trusted readback is malformed, stale, or implausibly future-dated."""


class GitHubTransportFailure(RuntimeError):
    """Typed provider failure classified without parsing unstable prose."""

    def __init__(
        self,
        message: str,
        *,
        kind: FailureKind,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        normalized_kind = str(kind).strip().casefold()
        if normalized_kind not in FAILURE_KINDS:
            raise ValueError(f"unsupported GitHub failure kind: {kind!r}")
        super().__init__(message)
        self.kind: FailureKind = cast(FailureKind, normalized_kind)
        self.retry_after_seconds: Optional[int] = (
            _nonnegative_int(retry_after_seconds, "retry_after_seconds")
            if retry_after_seconds is not None
            else None
        )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubBoundaryError(f"{field} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise GitHubBoundaryError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GitHubBoundaryError(f"{field} must be a non-negative integer") from exc
    if parsed < 0:
        raise GitHubBoundaryError(f"{field} must be a non-negative integer")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed < 1:
        raise GitHubBoundaryError(f"{field} must be a positive integer")
    return parsed


def _canonical_repository(value: Any) -> str:
    repository = _required_text(value, "repository").casefold()
    if not _REPOSITORY_RE.fullmatch(repository):
        raise GitHubBoundaryError("repository must use owner/name form")
    return repository


def _full_sha(value: Any, field: str = "head_sha") -> str:
    sha = _required_text(value, field).casefold()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise GitHubBoundaryError(f"{field} must be a full 40-character lowercase SHA")
    return sha


def _https_url(value: Any, field: str) -> str:
    url = _required_text(value, field)
    if not url.startswith("https://"):
        raise GitHubBoundaryError(f"{field} must use https")
    return url


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise GitHubBoundaryError("value must be JSON serializable") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ensure_no_sensitive_keys(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise GitHubBoundaryError(
                    f"sensitive field is not allowed in GitHub outbox: {path}.{key}"
                )
            _ensure_no_sensitive_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _ensure_no_sensitive_keys(nested, path=f"{path}[{index}]")


@dataclass(frozen=True)
class GitHubCheck:
    check_id: str
    name: str
    head_sha: str
    status: CheckStatus
    conclusion: Optional[CheckConclusion] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "check_id",
            _required_text(self.check_id, "check.check_id"),
        )
        object.__setattr__(self, "name", _required_text(self.name, "check.name"))
        object.__setattr__(self, "head_sha", _full_sha(self.head_sha, "check.head_sha"))
        status = _required_text(self.status, "check.status").casefold()
        if status not in CHECK_STATUSES:
            raise GitHubBoundaryError(
                f"check.status must be one of {sorted(CHECK_STATUSES)!r}"
            )
        conclusion = (
            _required_text(self.conclusion, "check.conclusion").casefold()
            if self.conclusion is not None
            else None
        )
        if conclusion is not None and conclusion not in CHECK_CONCLUSIONS:
            raise GitHubBoundaryError(
                f"check.conclusion must be one of {sorted(CHECK_CONCLUSIONS)!r}"
            )
        if status == "completed" and conclusion is None:
            raise GitHubBoundaryError("completed checks require a conclusion")
        if status != "completed" and conclusion is not None:
            raise GitHubBoundaryError("incomplete checks cannot have a conclusion")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "conclusion", conclusion)

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "head_sha": self.head_sha,
            "status": self.status,
            "conclusion": self.conclusion,
        }


@dataclass(frozen=True)
class GitHubReview:
    review_id: str
    author_login: str
    head_sha: str
    state: ReviewState
    submitted_at: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "review_id",
            _required_text(self.review_id, "review.review_id"),
        )
        object.__setattr__(
            self,
            "author_login",
            _required_text(self.author_login, "review.author_login").casefold(),
        )
        object.__setattr__(
            self,
            "head_sha",
            _full_sha(self.head_sha, "review.head_sha"),
        )
        state = _required_text(self.state, "review.state").casefold()
        if state not in REVIEW_STATES:
            raise GitHubBoundaryError(
                f"review.state must be one of {sorted(REVIEW_STATES)!r}"
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "submitted_at",
            _nonnegative_int(self.submitted_at, "review.submitted_at"),
        )

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "author_login": self.author_login,
            "head_sha": self.head_sha,
            "state": self.state,
            "submitted_at": self.submitted_at,
        }


@dataclass(frozen=True)
class GitHubReviewComment:
    comment_id: str
    author_login: str
    head_sha: str
    created_at: int
    actionable: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "comment_id",
            _required_text(self.comment_id, "review_comment.comment_id"),
        )
        object.__setattr__(
            self,
            "author_login",
            _required_text(
                self.author_login,
                "review_comment.author_login",
            ).casefold(),
        )
        object.__setattr__(
            self,
            "head_sha",
            _full_sha(self.head_sha, "review_comment.head_sha"),
        )
        object.__setattr__(
            self,
            "created_at",
            _nonnegative_int(self.created_at, "review_comment.created_at"),
        )
        if not isinstance(self.actionable, bool):
            raise GitHubBoundaryError("review_comment.actionable must be a boolean")

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "author_login": self.author_login,
            "head_sha": self.head_sha,
            "created_at": self.created_at,
            "actionable": self.actionable,
        }


@dataclass(frozen=True)
class GitHubReviewThread:
    thread_id: str
    head_sha: str
    resolved: bool
    outdated: bool
    actionable: bool
    comments: tuple[GitHubReviewComment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "thread_id",
            _required_text(self.thread_id, "review_thread.thread_id"),
        )
        object.__setattr__(
            self,
            "head_sha",
            _full_sha(self.head_sha, "review_thread.head_sha"),
        )
        for field, value in (
            ("resolved", self.resolved),
            ("outdated", self.outdated),
            ("actionable", self.actionable),
        ):
            if not isinstance(value, bool):
                raise GitHubBoundaryError(f"review_thread.{field} must be a boolean")
        comments = tuple(self.comments)
        if any(not isinstance(item, GitHubReviewComment) for item in comments):
            raise GitHubBoundaryError(
                "review_thread.comments must contain GitHubReviewComment values"
            )
        if len({item.comment_id for item in comments}) != len(comments):
            raise GitHubBoundaryError(
                "review comment IDs must be unique within a thread"
            )
        object.__setattr__(
            self,
            "comments",
            tuple(
                sorted(comments, key=lambda item: (item.created_at, item.comment_id))
            ),
        )

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "head_sha": self.head_sha,
            "resolved": self.resolved,
            "outdated": self.outdated,
            "actionable": self.actionable,
            "comments": [item.normalized_dict() for item in self.comments],
        }


@dataclass(frozen=True)
class GitHubRequestedReviewer:
    principal: str
    kind: ReviewerKind

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "principal",
            _required_text(self.principal, "requested_reviewer.principal").casefold(),
        )
        kind = _required_text(self.kind, "requested_reviewer.kind").casefold()
        if kind not in REVIEWER_KINDS:
            raise GitHubBoundaryError(
                f"requested_reviewer.kind must be one of {sorted(REVIEWER_KINDS)!r}"
            )
        object.__setattr__(self, "kind", kind)

    def normalized_dict(self) -> dict[str, Any]:
        return {"principal": self.principal, "kind": self.kind}


@dataclass(frozen=True)
class GitHubPullRequestSnapshot:
    provider: str
    observation_id: str
    repository: str
    pr_number: int
    pr_url: str
    state: PullRequestState
    is_draft: bool
    base_ref: str
    head_ref: str
    head_sha: str
    observed_at: int
    checks: tuple[GitHubCheck, ...] = ()
    reviews: tuple[GitHubReview, ...] = ()
    review_threads: tuple[GitHubReviewThread, ...] = ()
    requested_reviewers: tuple[GitHubRequestedReviewer, ...] = ()

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
        object.__setattr__(self, "pr_url", _https_url(self.pr_url, "pr_url"))
        state = _required_text(self.state, "state").casefold()
        if state not in PULL_REQUEST_STATES:
            raise GitHubBoundaryError(
                f"state must be one of {sorted(PULL_REQUEST_STATES)!r}"
            )
        object.__setattr__(self, "state", state)
        if not isinstance(self.is_draft, bool):
            raise GitHubBoundaryError("is_draft must be a boolean")
        object.__setattr__(self, "base_ref", _required_text(self.base_ref, "base_ref"))
        object.__setattr__(self, "head_ref", _required_text(self.head_ref, "head_ref"))
        object.__setattr__(self, "head_sha", _full_sha(self.head_sha))
        object.__setattr__(
            self,
            "observed_at",
            _nonnegative_int(self.observed_at, "observed_at"),
        )

        checks = tuple(self.checks)
        reviews = tuple(self.reviews)
        threads = tuple(self.review_threads)
        requested = tuple(self.requested_reviewers)
        if any(not isinstance(item, GitHubCheck) for item in checks):
            raise GitHubBoundaryError("checks must contain GitHubCheck values")
        if any(not isinstance(item, GitHubReview) for item in reviews):
            raise GitHubBoundaryError("reviews must contain GitHubReview values")
        if any(not isinstance(item, GitHubReviewThread) for item in threads):
            raise GitHubBoundaryError(
                "review_threads must contain GitHubReviewThread values"
            )
        if any(not isinstance(item, GitHubRequestedReviewer) for item in requested):
            raise GitHubBoundaryError(
                "requested_reviewers must contain GitHubRequestedReviewer values"
            )
        if len({item.check_id for item in checks}) != len(checks):
            raise GitHubBoundaryError("check IDs must be unique within a snapshot")
        if len({item.review_id for item in reviews}) != len(reviews):
            raise GitHubBoundaryError("review IDs must be unique within a snapshot")
        if len({item.thread_id for item in threads}) != len(threads):
            raise GitHubBoundaryError(
                "review thread IDs must be unique within a snapshot"
            )
        if len({(item.kind, item.principal) for item in requested}) != len(requested):
            raise GitHubBoundaryError(
                "requested reviewers must be unique within a snapshot"
            )
        object.__setattr__(
            self,
            "checks",
            tuple(sorted(checks, key=lambda item: item.check_id)),
        )
        object.__setattr__(
            self,
            "reviews",
            tuple(
                sorted(reviews, key=lambda item: (item.submitted_at, item.review_id))
            ),
        )
        object.__setattr__(
            self,
            "review_threads",
            tuple(sorted(threads, key=lambda item: item.thread_id)),
        )
        object.__setattr__(
            self,
            "requested_reviewers",
            tuple(sorted(requested, key=lambda item: (item.kind, item.principal))),
        )
        if len(_canonical_json(self.normalized_dict()).encode("utf-8")) > (
            MAX_NORMALIZED_SNAPSHOT_BYTES
        ):
            raise GitHubBoundaryError(
                "normalized GitHub snapshot exceeds the 256 KiB audit limit"
            )

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "state": self.state,
            "is_draft": self.is_draft,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "checks": [item.normalized_dict() for item in self.checks],
            "reviews": [item.normalized_dict() for item in self.reviews],
            "review_threads": [item.normalized_dict() for item in self.review_threads],
            "requested_reviewers": [
                item.normalized_dict() for item in self.requested_reviewers
            ],
        }

    def normalized_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "observation_id": self.observation_id,
            "observed_at": self.observed_at,
        }

    def snapshot_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.semantic_dict()))

    def to_human_review_mapping(self) -> dict[str, Any]:
        """Return the provider-neutral shape consumed by the existing gate."""
        return {
            "source": "github_readback",
            "verified_at": self.observed_at,
            "repo": self.repository,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "state": self.state.upper(),
            "is_draft": self.is_draft,
            "base_branch": self.base_ref,
            "head_branch": self.head_ref,
            "head_sha": self.head_sha,
        }


class GitHubSnapshotProvider(Protocol):
    """Read-only adapter for authoritative GitHub PR state."""

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> GitHubPullRequestSnapshot: ...


@dataclass(frozen=True)
class GitHubDeliveryReceipt:
    external_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "external_id",
            _required_text(self.external_id, "delivery_receipt.external_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(
                self.idempotency_key,
                "delivery_receipt.idempotency_key",
            ),
        )


class GitHubDeliveryTransport(Protocol):
    """Restricted outbox transport; intentionally has no merge/approval API."""

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> Optional[GitHubDeliveryReceipt]: ...

    def send_intent(self, intent: "GitHubOutboxIntent") -> GitHubDeliveryReceipt: ...


class DisabledGitHubSnapshotProvider:
    """Fail-closed default until an approved live readback adapter is wired."""

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> GitHubPullRequestSnapshot:
        raise GitHubTransportFailure(
            "GitHub snapshot provider is disabled",
            kind="disabled",
        )


class DisabledGitHubDeliveryTransport:
    """Fail-closed default; no live GitHub writes are configured in this phase."""

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> Optional[GitHubDeliveryReceipt]:
        raise GitHubTransportFailure(
            "GitHub delivery transport is disabled",
            kind="disabled",
        )

    def send_intent(self, intent: "GitHubOutboxIntent") -> GitHubDeliveryReceipt:
        raise GitHubTransportFailure(
            "GitHub delivery transport is disabled",
            kind="disabled",
        )


@dataclass(frozen=True)
class HumanReviewDecisionEvidence:
    review_id: str
    reviewer_login: str
    head_sha: str
    state: Literal["approved", "changes_requested"]
    submitted_at: int


@dataclass(frozen=True)
class ReviewReadiness:
    state: ReadinessState
    reasons: tuple[str, ...]
    all_current_checks_green: bool


def validate_exact_head(
    snapshot: GitHubPullRequestSnapshot,
    *,
    expected_head_sha: str,
) -> GitHubPullRequestSnapshot:
    if not isinstance(snapshot, GitHubPullRequestSnapshot):
        raise GitHubBoundaryError(
            "snapshot must be a GitHubPullRequestSnapshot from a trusted provider"
        )
    expected = _full_sha(expected_head_sha, "expected_head_sha")
    if snapshot.head_sha != expected:
        raise GitHubHeadMismatch(
            "GitHub snapshot is not for the gate exact head "
            f"({snapshot.head_sha!r} != {expected!r})"
        )
    return snapshot


def validate_snapshot_freshness(
    snapshot: GitHubPullRequestSnapshot,
    *,
    now: int,
) -> GitHubPullRequestSnapshot:
    checked_at = _nonnegative_int(now, "now")
    if snapshot.observed_at < checked_at - MAX_SNAPSHOT_AGE_SECONDS:
        raise GitHubSnapshotUnavailable(
            "GitHub snapshot is stale; refresh the authoritative PR readback"
        )
    if snapshot.observed_at > checked_at + MAX_SNAPSHOT_FUTURE_SKEW_SECONDS:
        raise GitHubSnapshotUnavailable(
            "GitHub snapshot observation time is implausibly far in the future"
        )
    return snapshot


def read_human_review_decisions(
    snapshot: GitHubPullRequestSnapshot,
    *,
    expected_head_sha: str,
    reviewer_login: Optional[str] = None,
) -> tuple[HumanReviewDecisionEvidence, ...]:
    """Return latest current-head human decisions as read-only evidence."""
    validate_exact_head(snapshot, expected_head_sha=expected_head_sha)
    expected = _full_sha(expected_head_sha, "expected_head_sha")
    expected_reviewer = (
        _required_text(reviewer_login, "reviewer_login").casefold()
        if reviewer_login is not None
        else None
    )
    latest: dict[str, GitHubReview] = {}
    for review in snapshot.reviews:
        if review.head_sha != expected:
            continue
        if review.state not in {"approved", "changes_requested", "dismissed"}:
            continue
        if expected_reviewer is not None and review.author_login != expected_reviewer:
            continue
        prior = latest.get(review.author_login)
        if prior is None or (review.submitted_at, review.review_id) > (
            prior.submitted_at,
            prior.review_id,
        ):
            latest[review.author_login] = review
    return tuple(
        HumanReviewDecisionEvidence(
            review_id=review.review_id,
            reviewer_login=review.author_login,
            head_sha=review.head_sha,
            state=cast(
                Literal["approved", "changes_requested"],
                review.state,
            ),
            submitted_at=review.submitted_at,
        )
        for review in sorted(latest.values(), key=lambda item: item.author_login)
        if review.state in {"approved", "changes_requested"}
    )


def assess_review_readiness(
    snapshot: GitHubPullRequestSnapshot,
    *,
    expected_head_sha: str,
    coderabbit_state: str,
) -> ReviewReadiness:
    """Reduce source evidence without allowing green checks to hide blockers."""
    try:
        validate_exact_head(snapshot, expected_head_sha=expected_head_sha)
    except GitHubHeadMismatch:
        return ReviewReadiness("stale", ("pull_request_head_mismatch",), False)
    if snapshot.state in {"closed", "merged"}:
        return ReviewReadiness(
            "terminal",
            (f"pull_request_{snapshot.state}",),
            False,
        )

    expected = _full_sha(expected_head_sha, "expected_head_sha")
    reasons: list[str] = []
    blocked = False
    pending = False
    normalized_coderabbit = str(coderabbit_state or "").strip().casefold()
    if normalized_coderabbit == "actionable":
        blocked = True
        reasons.append("coderabbit_actionable")
    elif normalized_coderabbit not in {"clean", "no_actionable_comments"}:
        pending = True
        reasons.append(f"coderabbit_{normalized_coderabbit or 'unknown'}")

    decisions = read_human_review_decisions(
        snapshot,
        expected_head_sha=expected,
    )
    if any(item.state == "changes_requested" for item in decisions):
        blocked = True
        reasons.append("human_changes_requested")

    actionable_threads = [
        thread
        for thread in snapshot.review_threads
        if thread.head_sha == expected
        and not thread.resolved
        and not thread.outdated
        and (
            thread.actionable
            or any(
                comment.head_sha == expected and comment.actionable
                for comment in thread.comments
            )
        )
    ]
    if actionable_threads:
        blocked = True
        reasons.append("actionable_review_thread")

    current_checks = [check for check in snapshot.checks if check.head_sha == expected]
    blocking_checks = [
        check
        for check in current_checks
        if check.status == "completed"
        and check.conclusion in BLOCKING_CHECK_CONCLUSIONS
    ]
    incomplete_checks = [
        check for check in current_checks if check.status != "completed"
    ]
    if blocking_checks:
        blocked = True
        reasons.append("current_check_failed")
    if incomplete_checks or not current_checks:
        pending = True
        reasons.append(
            "current_checks_pending" if incomplete_checks else "current_checks_missing"
        )
    all_green = bool(current_checks) and all(
        check.status == "completed" and check.conclusion in SUCCESSFUL_CHECK_CONCLUSIONS
        for check in current_checks
    )
    if snapshot.is_draft:
        pending = True
        reasons.append("pull_request_draft")

    if blocked:
        state: ReadinessState = "blocked"
    elif pending:
        state = "pending"
    else:
        state = "ready"
    return ReviewReadiness(state, tuple(reasons), all_green)


@dataclass(frozen=True)
class GitHubOutboxIntent:
    id: str
    gate_id: str
    repository: str
    pr_number: int
    head_sha: str
    surface: GitHubSurface
    operation: GitHubOperation
    payload: dict[str, Any]
    payload_sha256: str
    idempotency_key: str
    state: OutboxState
    attempt_count: int
    max_attempts: int
    next_attempt_at: Optional[int]
    external_id: Optional[str]
    last_snapshot_sha256: Optional[str]
    last_snapshot_observed_at: Optional[int]
    last_failure_kind: Optional[FailureKind]
    last_error: Optional[str]
    created_at: int
    updated_at: int
    sent_at: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GitHubOutboxIntent":
        try:
            decoded = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            decoded = {}
        payload = decoded if isinstance(decoded, dict) else {}
        return cls(
            id=row["id"],
            gate_id=row["gate_id"],
            repository=row["repository"],
            pr_number=int(row["pr_number"]),
            head_sha=row["head_sha"],
            surface=cast(GitHubSurface, row["surface"]),
            operation=cast(GitHubOperation, row["operation"]),
            payload=payload,
            payload_sha256=row["payload_sha256"],
            idempotency_key=row["idempotency_key"],
            state=cast(OutboxState, row["state"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=(
                int(row["next_attempt_at"])
                if row["next_attempt_at"] is not None
                else None
            ),
            external_id=row["external_id"],
            last_snapshot_sha256=row["last_snapshot_sha256"],
            last_snapshot_observed_at=(
                int(row["last_snapshot_observed_at"])
                if row["last_snapshot_observed_at"] is not None
                else None
            ),
            last_failure_kind=(
                cast(FailureKind, row["last_failure_kind"])
                if row["last_failure_kind"] is not None
                else None
            ),
            last_error=row["last_error"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            sent_at=int(row["sent_at"]) if row["sent_at"] is not None else None,
        )


@dataclass(frozen=True)
class GitHubOutboxAttempt:
    intent_id: str
    attempt_number: int
    outcome: str
    retryable: bool
    snapshot_sha256: Optional[str]
    external_id: Optional[str]
    failure_kind: Optional[FailureKind]
    error: Optional[str]
    attempted_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GitHubOutboxAttempt":
        return cls(
            intent_id=row["intent_id"],
            attempt_number=int(row["attempt_number"]),
            outcome=row["outcome"],
            retryable=bool(row["retryable"]),
            snapshot_sha256=row["snapshot_sha256"],
            external_id=row["external_id"],
            failure_kind=(
                cast(FailureKind, row["failure_kind"])
                if row["failure_kind"] is not None
                else None
            ),
            error=row["error"],
            attempted_at=int(row["attempted_at"]),
        )


@dataclass(frozen=True)
class EnqueueReceipt:
    created: bool
    intent: GitHubOutboxIntent


@dataclass(frozen=True)
class FailureClassification:
    kind: FailureKind
    retryable: bool
    retry_delay_seconds: Optional[int]


@dataclass(frozen=True)
class ProcessIntentResult:
    intent_id: str
    state: OutboxState
    outcome: str
    sent: bool = False
    deduplicated: bool = False
    superseded: bool = False
    retryable: bool = False


def classify_transport_failure(error: BaseException) -> FailureClassification:
    if isinstance(error, GitHubSnapshotUnavailable):
        return FailureClassification(
            "unavailable",
            True,
            DEFAULT_RETRY_DELAY_SECONDS,
        )
    if isinstance(error, GitHubTransportFailure):
        kind = error.kind
        retryable = kind in RETRYABLE_FAILURE_KINDS
        if not retryable:
            delay = None
        elif error.retry_after_seconds is not None:
            delay = error.retry_after_seconds
        elif kind == "rate_limited":
            delay = DEFAULT_RATE_LIMIT_DELAY_SECONDS
        else:
            delay = DEFAULT_RETRY_DELAY_SECONDS
        return FailureClassification(kind, retryable, delay)
    return FailureClassification("unknown", False, None)


def _validate_surface_operation(
    surface: Any,
    operation: Any,
) -> tuple[GitHubSurface, GitHubOperation]:
    normalized_surface = _required_text(surface, "surface").casefold()
    normalized_operation = _required_text(operation, "operation").casefold()
    allowed = SURFACE_OPERATIONS.get(normalized_surface)
    if allowed is None or normalized_operation not in allowed:
        raise GitHubBoundaryError(
            "unsupported GitHub human-review surface/operation pair: "
            f"{normalized_surface!r}/{normalized_operation!r}"
        )
    return (
        cast(GitHubSurface, normalized_surface),
        cast(GitHubOperation, normalized_operation),
    )


def _idempotency_key(
    repository: str,
    pr_number: int,
    head_sha: str,
    surface: str,
    operation: str,
) -> str:
    return (
        f"github-human-review:v1:{repository}:pr:{pr_number}:head:{head_sha}:"
        f"surface:{surface}:operation:{operation}"
    )


def _intent_id(idempotency_key: str) -> str:
    return "gho_" + _sha256_text(idempotency_key)[:24]


def get_intent(
    conn: sqlite3.Connection,
    intent_id: str,
) -> Optional[GitHubOutboxIntent]:
    row = conn.execute(
        "SELECT * FROM github_human_review_outbox WHERE id=?",
        (intent_id,),
    ).fetchone()
    return GitHubOutboxIntent.from_row(row) if row is not None else None


def list_intents(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
) -> tuple[GitHubOutboxIntent, ...]:
    rows = conn.execute(
        "SELECT * FROM github_human_review_outbox "
        "WHERE repository=? AND pr_number=? ORDER BY created_at, id",
        (_canonical_repository(repository), _positive_int(pr_number, "pr_number")),
    ).fetchall()
    return tuple(GitHubOutboxIntent.from_row(row) for row in rows)


def list_attempts(
    conn: sqlite3.Connection,
    intent_id: str,
) -> tuple[GitHubOutboxAttempt, ...]:
    rows = conn.execute(
        "SELECT * FROM github_human_review_attempts "
        "WHERE intent_id=? ORDER BY attempt_number",
        (intent_id,),
    ).fetchall()
    return tuple(GitHubOutboxAttempt.from_row(row) for row in rows)


def _supersede_stale_intents_in_txn(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    current_head_sha: str,
    now: int,
) -> int:
    updated = conn.execute(
        "UPDATE github_human_review_outbox SET state='superseded', "
        "next_attempt_at=NULL, updated_at=? "
        "WHERE repository=? AND pr_number=? AND head_sha<>? "
        "AND state IN ('pending', 'attempting', 'retry', 'permanent_failure')",
        (now, repository, pr_number, current_head_sha),
    )
    return int(updated.rowcount)


def supersede_stale_intents(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    current_head_sha: str,
    now: Optional[int] = None,
) -> int:
    changed_at = int(time.time()) if now is None else int(now)
    with kb.write_txn(conn):
        return _supersede_stale_intents_in_txn(
            conn,
            repository=_canonical_repository(repository),
            pr_number=_positive_int(pr_number, "pr_number"),
            current_head_sha=_full_sha(current_head_sha, "current_head_sha"),
            now=changed_at,
        )


def enqueue_intent(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    snapshot: GitHubPullRequestSnapshot,
    expected_repository: str,
    expected_pr_number: int,
    expected_head_sha: str,
    surface: GitHubSurface,
    operation: GitHubOperation,
    payload: Mapping[str, Any],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: Optional[int] = None,
) -> EnqueueReceipt:
    """Create one exact-head intent, deduplicated by semantic GitHub identity."""
    changed_at = int(time.time()) if now is None else int(now)
    expected_repository_key = _canonical_repository(expected_repository)
    expected_number = _positive_int(expected_pr_number, "expected_pr_number")
    if (
        snapshot.repository != expected_repository_key
        or snapshot.pr_number != expected_number
    ):
        raise GitHubBoundaryError(
            "GitHub snapshot repository/PR identity does not match the gate"
        )
    validate_snapshot_freshness(snapshot, now=changed_at)
    try:
        validate_exact_head(snapshot, expected_head_sha=expected_head_sha)
    except GitHubHeadMismatch:
        supersede_stale_intents(
            conn,
            repository=expected_repository_key,
            pr_number=expected_number,
            current_head_sha=snapshot.head_sha,
            now=changed_at,
        )
        raise
    if snapshot.state in {"closed", "merged"}:
        raise GitHubPRTerminal(
            f"{snapshot.state} pull requests cannot receive a current human-review intent"
        )
    if snapshot.is_draft:
        raise GitHubBoundaryError(
            "draft pull requests cannot receive a current human-review intent"
        )
    normalized_gate = _required_text(gate_id, "gate_id")
    normalized_surface, normalized_operation = _validate_surface_operation(
        surface,
        operation,
    )
    if not isinstance(payload, Mapping):
        raise GitHubBoundaryError("payload must be an object")
    payload_copy = dict(payload)
    _ensure_no_sensitive_keys(payload_copy)
    payload_json = _canonical_json(payload_copy)
    if len(payload_json.encode("utf-8")) > MAX_OUTBOX_PAYLOAD_BYTES:
        raise GitHubBoundaryError(
            "GitHub outbox payload exceeds the 128 KiB audit limit"
        )
    payload_sha256 = _sha256_text(payload_json)
    attempts = _positive_int(max_attempts, "max_attempts")
    key = _idempotency_key(
        snapshot.repository,
        snapshot.pr_number,
        snapshot.head_sha,
        normalized_surface,
        normalized_operation,
    )
    intent_id = _intent_id(key)

    with kb.write_txn(conn):
        _supersede_stale_intents_in_txn(
            conn,
            repository=snapshot.repository,
            pr_number=snapshot.pr_number,
            current_head_sha=snapshot.head_sha,
            now=changed_at,
        )
        existing_row = conn.execute(
            "SELECT * FROM github_human_review_outbox "
            "WHERE repository=? AND pr_number=? AND head_sha=? "
            "AND surface=? AND operation=?",
            (
                snapshot.repository,
                snapshot.pr_number,
                snapshot.head_sha,
                normalized_surface,
                normalized_operation,
            ),
        ).fetchone()
        if existing_row is not None:
            existing = GitHubOutboxIntent.from_row(existing_row)
            if (
                existing.payload_sha256 != payload_sha256
                or existing.gate_id != normalized_gate
                or existing.max_attempts != attempts
            ):
                raise GitHubReplayConflict(
                    "GitHub outbox identity was reused with a different payload or gate"
                )
            return EnqueueReceipt(False, existing)

        conn.execute(
            """
            INSERT INTO github_human_review_outbox (
                id, gate_id, repository, pr_number, head_sha, surface, operation,
                payload_json, payload_sha256, idempotency_key, state,
                attempt_count, max_attempts, next_attempt_at, external_id,
                last_snapshot_sha256, last_snapshot_observed_at,
                last_failure_kind, last_error, created_at, updated_at, sent_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL,
                NULL, NULL, NULL, NULL, ?, ?, NULL
            )
            """,
            (
                intent_id,
                normalized_gate,
                snapshot.repository,
                snapshot.pr_number,
                snapshot.head_sha,
                normalized_surface,
                normalized_operation,
                payload_json,
                payload_sha256,
                key,
                attempts,
                changed_at,
                changed_at,
            ),
        )
        created = get_intent(conn, intent_id)
        assert created is not None
        return EnqueueReceipt(True, created)


def _claim_intent(
    conn: sqlite3.Connection,
    intent_id: str,
    *,
    now: int,
) -> Optional[GitHubOutboxIntent]:
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT * FROM github_human_review_outbox WHERE id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise GitHubBoundaryError(
                f"GitHub outbox intent {intent_id!r} does not exist"
            )
        current = GitHubOutboxIntent.from_row(row)
        if current.state == "attempting":
            if current.updated_at > now - ATTEMPT_LEASE_SECONDS:
                return None
            _insert_attempt(
                conn,
                current,
                outcome="attempt_lease_expired",
                retryable=True,
                snapshot_sha256=current.last_snapshot_sha256,
                external_id=None,
                failure_kind="unavailable",
                error="delivery attempt lease expired; readback required",
                now=now,
            )
            conn.execute(
                "UPDATE github_human_review_outbox SET state='retry', "
                "next_attempt_at=?, last_failure_kind='unavailable', "
                "last_error='delivery attempt lease expired; readback required', "
                "updated_at=? WHERE id=? AND state='attempting' AND updated_at=?",
                (now, now, intent_id, current.updated_at),
            )
            refreshed = get_intent(conn, intent_id)
            if refreshed is None or refreshed.state != "retry":
                return None
            current = refreshed
        if current.state not in {"pending", "retry"}:
            return None
        if current.next_attempt_at is not None and current.next_attempt_at > now:
            return None
        if current.attempt_count >= current.max_attempts:
            conn.execute(
                "UPDATE github_human_review_outbox "
                "SET state='permanent_failure', last_failure_kind='validation', "
                "last_error='maximum attempts already exhausted', updated_at=? "
                "WHERE id=? AND state=?",
                (now, intent_id, current.state),
            )
            return None
        updated = conn.execute(
            "UPDATE github_human_review_outbox "
            "SET state='attempting', attempt_count=attempt_count+1, "
            "next_attempt_at=NULL, updated_at=? "
            "WHERE id=? AND state=? AND attempt_count=?",
            (now, intent_id, current.state, current.attempt_count),
        )
        if updated.rowcount != 1:
            return None
        claimed = get_intent(conn, intent_id)
        assert claimed is not None
        return claimed


def _insert_attempt(
    conn: sqlite3.Connection,
    intent: GitHubOutboxIntent,
    *,
    outcome: str,
    retryable: bool,
    snapshot_sha256: Optional[str],
    external_id: Optional[str],
    failure_kind: Optional[FailureKind],
    error: Optional[str],
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO github_human_review_attempts (
            intent_id, attempt_number, outcome, retryable, snapshot_sha256,
            external_id, failure_kind, error, attempted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent.id,
            intent.attempt_count,
            outcome,
            int(retryable),
            snapshot_sha256,
            external_id,
            failure_kind,
            error,
            now,
        ),
    )


def _safe_error(error: BaseException) -> str:
    return str(error).replace("\n", " ")[:500]


def _validate_snapshot_identity(
    intent: GitHubOutboxIntent,
    snapshot: GitHubPullRequestSnapshot,
) -> None:
    if (
        snapshot.repository != intent.repository
        or snapshot.pr_number != intent.pr_number
    ):
        raise GitHubTransportFailure(
            "GitHub snapshot identity does not match the outbox intent",
            kind="validation",
        )


def _validate_receipt(
    intent: GitHubOutboxIntent,
    receipt: GitHubDeliveryReceipt,
) -> GitHubDeliveryReceipt:
    if not isinstance(receipt, GitHubDeliveryReceipt):
        raise GitHubTransportFailure(
            "GitHub delivery transport returned an invalid receipt",
            kind="validation",
        )
    if receipt.idempotency_key != intent.idempotency_key:
        raise GitHubTransportFailure(
            "GitHub delivery receipt idempotency key does not match the intent",
            kind="conflict",
        )
    return receipt


def _record_validated_snapshot(
    conn: sqlite3.Connection,
    intent: GitHubOutboxIntent,
    snapshot: GitHubPullRequestSnapshot,
    *,
    now: int,
) -> None:
    conn.execute(
        "UPDATE github_human_review_outbox "
        "SET last_snapshot_sha256=?, last_snapshot_observed_at=?, updated_at=? "
        "WHERE id=? AND state='attempting'",
        (snapshot.snapshot_sha256(), snapshot.observed_at, now, intent.id),
    )


def _mark_sent(
    conn: sqlite3.Connection,
    intent: GitHubOutboxIntent,
    receipt: GitHubDeliveryReceipt,
    *,
    snapshot_sha256: str,
    outcome: str,
    now: int,
) -> ProcessIntentResult:
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE github_human_review_outbox SET state='sent', external_id=?, "
            "next_attempt_at=NULL, last_failure_kind=NULL, last_error=NULL, "
            "updated_at=?, sent_at=? WHERE id=? AND state='attempting'",
            (receipt.external_id, now, now, intent.id),
        )
        if updated.rowcount != 1:
            current = get_intent(conn, intent.id)
            if current is None:
                raise GitHubBoundaryError(
                    f"GitHub outbox intent {intent.id!r} disappeared while sending"
                )
            return ProcessIntentResult(
                current.id,
                current.state,
                "concurrent_state_change",
            )
        _insert_attempt(
            conn,
            intent,
            outcome=outcome,
            retryable=False,
            snapshot_sha256=snapshot_sha256,
            external_id=receipt.external_id,
            failure_kind=None,
            error=None,
            now=now,
        )
    return ProcessIntentResult(
        intent.id,
        "sent",
        outcome,
        sent=True,
        deduplicated=outcome
        in {
            "already_delivered",
            "requested_reviewer_present",
            "sent_after_readback",
        },
    )


def _mark_failure(
    conn: sqlite3.Connection,
    intent: GitHubOutboxIntent,
    error: BaseException,
    *,
    snapshot_sha256: Optional[str],
    now: int,
) -> ProcessIntentResult:
    classification = classify_transport_failure(error)
    retryable = classification.retryable and intent.attempt_count < intent.max_attempts
    state: OutboxState = "retry" if retryable else "permanent_failure"
    outcome = "retry_scheduled" if retryable else "permanent_failure"
    next_attempt_at = (
        now + int(classification.retry_delay_seconds or 0) if retryable else None
    )
    safe_error = _safe_error(error)
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE github_human_review_outbox SET state=?, next_attempt_at=?, "
            "last_failure_kind=?, last_error=?, updated_at=? "
            "WHERE id=? AND state='attempting'",
            (
                state,
                next_attempt_at,
                classification.kind,
                safe_error,
                now,
                intent.id,
            ),
        )
        if updated.rowcount == 1:
            _insert_attempt(
                conn,
                intent,
                outcome=outcome,
                retryable=retryable,
                snapshot_sha256=snapshot_sha256,
                external_id=None,
                failure_kind=classification.kind,
                error=safe_error,
                now=now,
            )
    return ProcessIntentResult(
        intent.id,
        state,
        outcome,
        retryable=retryable,
    )


def _mark_superseded(
    conn: sqlite3.Connection,
    intent: GitHubOutboxIntent,
    snapshot: GitHubPullRequestSnapshot,
    *,
    outcome: str,
    now: int,
) -> ProcessIntentResult:
    snapshot_sha256 = snapshot.snapshot_sha256()
    with kb.write_txn(conn):
        if (
            snapshot.repository == intent.repository
            and snapshot.pr_number == intent.pr_number
        ):
            if snapshot.state in {"closed", "merged"}:
                conn.execute(
                    "UPDATE github_human_review_outbox SET state='superseded', "
                    "next_attempt_at=NULL, updated_at=? "
                    "WHERE repository=? AND pr_number=? "
                    "AND state IN ('pending', 'attempting', 'retry', 'permanent_failure')",
                    (now, intent.repository, intent.pr_number),
                )
            else:
                _supersede_stale_intents_in_txn(
                    conn,
                    repository=intent.repository,
                    pr_number=intent.pr_number,
                    current_head_sha=snapshot.head_sha,
                    now=now,
                )
        conn.execute(
            "UPDATE github_human_review_outbox SET state='superseded', "
            "next_attempt_at=NULL, updated_at=? WHERE id=? "
            "AND state IN ('pending', 'attempting', 'retry', 'permanent_failure')",
            (now, intent.id),
        )
        _insert_attempt(
            conn,
            intent,
            outcome=outcome,
            retryable=False,
            snapshot_sha256=snapshot_sha256,
            external_id=None,
            failure_kind=None,
            error=None,
            now=now,
        )
    return ProcessIntentResult(
        intent.id,
        "superseded",
        outcome,
        superseded=True,
    )


def process_intent(
    conn: sqlite3.Connection,
    intent_id: str,
    *,
    snapshot_provider: Optional[GitHubSnapshotProvider] = None,
    delivery_transport: Optional[GitHubDeliveryTransport] = None,
    now: Optional[int] = None,
) -> ProcessIntentResult:
    """Validate one due intent against GitHub readback and deliver it once."""
    attempted_at = int(time.time()) if now is None else int(now)
    initial = get_intent(conn, intent_id)
    if initial is None:
        raise GitHubBoundaryError(f"GitHub outbox intent {intent_id!r} does not exist")
    if initial.state in TERMINAL_OUTBOX_STATES:
        return ProcessIntentResult(initial.id, initial.state, "already_terminal")
    if initial.next_attempt_at is not None and initial.next_attempt_at > attempted_at:
        return ProcessIntentResult(initial.id, initial.state, "not_due")

    claimed = _claim_intent(conn, intent_id, now=attempted_at)
    if claimed is None:
        current = get_intent(conn, intent_id)
        if current is None:
            raise GitHubBoundaryError(
                f"GitHub outbox intent {intent_id!r} disappeared while claiming"
            )
        outcome = (
            "not_due"
            if (
                current.next_attempt_at is not None
                and current.next_attempt_at > attempted_at
            )
            else (
                "in_progress" if current.state == "attempting" else "already_terminal"
            )
        )
        return ProcessIntentResult(current.id, current.state, outcome)

    provider = snapshot_provider or DisabledGitHubSnapshotProvider()
    transport = delivery_transport or DisabledGitHubDeliveryTransport()
    snapshot: Optional[GitHubPullRequestSnapshot] = None
    try:
        snapshot = provider.read_snapshot(
            repository=claimed.repository,
            pr_number=claimed.pr_number,
        )
        if not isinstance(snapshot, GitHubPullRequestSnapshot):
            raise GitHubTransportFailure(
                "GitHub snapshot provider returned an invalid snapshot",
                kind="validation",
            )
        _validate_snapshot_identity(claimed, snapshot)
        validate_snapshot_freshness(snapshot, now=attempted_at)
        if snapshot.head_sha != claimed.head_sha:
            return _mark_superseded(
                conn,
                claimed,
                snapshot,
                outcome="head_superseded",
                now=attempted_at,
            )
        if snapshot.state in {"closed", "merged"} or snapshot.is_draft:
            return _mark_superseded(
                conn,
                claimed,
                snapshot,
                outcome=(
                    f"pull_request_{snapshot.state}"
                    if snapshot.state in {"closed", "merged"}
                    else "pull_request_draft"
                ),
                now=attempted_at,
            )
        validate_exact_head(snapshot, expected_head_sha=claimed.head_sha)
        snapshot_sha256 = snapshot.snapshot_sha256()
        with kb.write_txn(conn):
            _record_validated_snapshot(
                conn,
                claimed,
                snapshot,
                now=attempted_at,
            )

        if claimed.operation == "request_reviewer":
            principal = str(claimed.payload.get("reviewer_principal") or "").strip()
            prefix, separator, login = principal.partition(":")
            if prefix == "github" and separator and login:
                normalized_login = login.casefold()
                if any(
                    reviewer.kind == "user" and reviewer.principal == normalized_login
                    for reviewer in snapshot.requested_reviewers
                ):
                    receipt = GitHubDeliveryReceipt(
                        external_id=f"readback:requested-reviewer:{normalized_login}",
                        idempotency_key=claimed.idempotency_key,
                    )
                    return _mark_sent(
                        conn,
                        claimed,
                        receipt,
                        snapshot_sha256=snapshot_sha256,
                        outcome="requested_reviewer_present",
                        now=attempted_at,
                    )

        existing = transport.find_delivery(
            idempotency_key=claimed.idempotency_key,
        )
        if existing is not None:
            receipt = _validate_receipt(claimed, existing)
            return _mark_sent(
                conn,
                claimed,
                receipt,
                snapshot_sha256=snapshot_sha256,
                outcome="already_delivered",
                now=attempted_at,
            )

        try:
            receipt = _validate_receipt(
                claimed,
                transport.send_intent(claimed),
            )
        except BaseException as send_error:
            try:
                readback = transport.find_delivery(
                    idempotency_key=claimed.idempotency_key,
                )
            except BaseException:
                readback = None
            if readback is not None:
                receipt = _validate_receipt(claimed, readback)
                return _mark_sent(
                    conn,
                    claimed,
                    receipt,
                    snapshot_sha256=snapshot_sha256,
                    outcome="sent_after_readback",
                    now=attempted_at,
                )
            return _mark_failure(
                conn,
                claimed,
                send_error,
                snapshot_sha256=snapshot_sha256,
                now=attempted_at,
            )
        return _mark_sent(
            conn,
            claimed,
            receipt,
            snapshot_sha256=snapshot_sha256,
            outcome="sent",
            now=attempted_at,
        )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        snapshot_sha256 = (
            snapshot.snapshot_sha256()
            if isinstance(snapshot, GitHubPullRequestSnapshot)
            else None
        )
        return _mark_failure(
            conn,
            claimed,
            error,
            snapshot_sha256=snapshot_sha256,
            now=attempted_at,
        )
