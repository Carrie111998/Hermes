"""Linear intent/readback boundary for Kanban orchestration.

Linear events are normalized into a small signed envelope and stored as wakeups.
They never create Kanban tasks or mutate human-review gates directly.  A caller
must separately build a plan from injected read-only providers and then apply
that plan transactionally.  Linear attachments establish issue-to-PR
association only; repository state and exact heads come exclusively from the
trusted pull-request snapshot provider.

This module intentionally contains no Linear/GitHub clients, credentials,
webhook registration, network routing, or external write capability.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

from hermes_cli import kanban_db as kb


EVENT_SCHEMA_VERSION = 1
VALID_EVENT_KINDS = frozenset({"issue", "attachment", "comment"})
VALID_PR_STATES = frozenset({"open", "closed", "merged"})
DEFAULT_SIGNATURE_MAX_AGE_SECONDS = 300
DEFAULT_SIGNATURE_FUTURE_SKEW_SECONDS = 60
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


class LinearBoundaryError(ValueError):
    """The normalized event or provider readback violates the boundary."""


class LinearSignatureError(LinearBoundaryError):
    """The signed event envelope is missing, stale, or invalid."""


class LinearReplayConflict(LinearBoundaryError):
    """A dedupe identity was reused for different semantic input."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LinearBoundaryError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _required_text(value, field)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise LinearBoundaryError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LinearBoundaryError(
            f"{field} must be a non-negative integer"
        ) from exc
    if parsed < 0:
        raise LinearBoundaryError(f"{field} must be a non-negative integer")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed < 1:
        raise LinearBoundaryError(f"{field} must be a positive integer")
    return parsed


def _canonical_repository(repository: str) -> str:
    normalized = _required_text(repository, "repository").casefold()
    if not _REPOSITORY_RE.fullmatch(normalized):
        raise LinearBoundaryError("repository must use owner/name form")
    return normalized


def _https_url(value: Any, field: str) -> str:
    url = _required_text(value, field)
    if not url.startswith("https://"):
        raise LinearBoundaryError(f"{field} must use https")
    return url


def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, order=True)
class PullRequestRef:
    repository: str
    number: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", _canonical_repository(self.repository))
        object.__setattr__(self, "number", _positive_int(self.number, "pr_number"))


@dataclass(frozen=True)
class LinearIssueSnapshot:
    """Read-only Linear issue state normalized by a trusted provider.

    ``attachments=None`` means the provider could not establish a complete
    attachment list.  It is distinct from ``attachments=()`` and never causes
    existing historical issue-to-PR links to be removed.
    """

    issue_id: str
    identifier: str
    title: str
    issue_url: str
    source_revision: int
    attachments: Optional[tuple[PullRequestRef, ...]]
    state: Optional[str] = None
    state_type: Optional[str] = None
    labels: tuple[str, ...] = ()
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    observation_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", _required_text(self.issue_id, "issue_id"))
        object.__setattr__(
            self,
            "identifier",
            _required_text(self.identifier, "identifier"),
        )
        object.__setattr__(self, "title", _required_text(self.title, "title"))
        object.__setattr__(self, "issue_url", _https_url(self.issue_url, "issue_url"))
        object.__setattr__(
            self,
            "source_revision",
            _nonnegative_int(self.source_revision, "source_revision"),
        )
        for field_name in (
            "state",
            "state_type",
            "team_id",
            "team_name",
            "project_id",
            "project_name",
            "observation_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        if isinstance(self.labels, (str, bytes, bytearray)):
            raise LinearBoundaryError("labels must contain strings")
        try:
            labels = tuple(
                sorted({_required_text(label, "label") for label in self.labels})
            )
        except TypeError as exc:
            raise LinearBoundaryError("labels must contain strings") from exc
        object.__setattr__(self, "labels", labels)
        if self.attachments is not None:
            refs = tuple(sorted(set(self.attachments)))
            if any(not isinstance(ref, PullRequestRef) for ref in refs):
                raise LinearBoundaryError(
                    "attachments must contain PullRequestRef values"
                )
            object.__setattr__(self, "attachments", refs)

    @property
    def attachments_complete(self) -> bool:
        return self.attachments is not None

    def digest(self) -> str:
        payload: dict[str, Any] = {
            "issue_id": self.issue_id,
            "identifier": self.identifier,
            "title": self.title,
            "issue_url": self.issue_url,
            "source_revision": self.source_revision,
            "attachments_complete": self.attachments_complete,
            "attachments": [
                {"repository": ref.repository, "number": ref.number}
                for ref in (self.attachments or ())
            ],
        }
        # Preserve the exact legacy digest when every MCP extension field is
        # unset. Existing coordinator rows use that digest for replay
        # detection, so adding optional normalization metadata must not turn a
        # previously applied observation into a replay conflict.
        if any((
            self.state,
            self.state_type,
            self.labels,
            self.team_id,
            self.team_name,
            self.project_id,
            self.project_name,
            self.observation_id,
        )):
            payload["linear_mcp"] = {
                "state": self.state,
                "state_type": self.state_type,
                "labels": list(self.labels),
                "team_id": self.team_id,
                "team_name": self.team_name,
                "project_id": self.project_id,
                "project_name": self.project_name,
                "observation_id": self.observation_id,
            }
        return _canonical_json_sha256(payload)


@dataclass(frozen=True)
class PullRequestSnapshot:
    """Trusted read-only repository state for one PR aggregate/head."""

    ref: PullRequestRef
    pr_url: str
    state: Literal["open", "closed", "merged"]
    is_draft: bool
    base_branch: str
    head_branch: str
    head_sha: str
    provider_revision: int
    observed_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.ref, PullRequestRef):
            raise LinearBoundaryError("ref must be a PullRequestRef")
        object.__setattr__(self, "pr_url", _https_url(self.pr_url, "pr_url"))
        state = _required_text(self.state, "state").casefold()
        if state not in VALID_PR_STATES:
            raise LinearBoundaryError(
                f"state must be one of {sorted(VALID_PR_STATES)!r}"
            )
        object.__setattr__(self, "state", state)
        if not isinstance(self.is_draft, bool):
            raise LinearBoundaryError("is_draft must be a boolean")
        object.__setattr__(
            self,
            "base_branch",
            _required_text(self.base_branch, "base_branch"),
        )
        object.__setattr__(
            self,
            "head_branch",
            _required_text(self.head_branch, "head_branch"),
        )
        head_sha = _required_text(self.head_sha, "head_sha").casefold()
        if not _FULL_SHA_RE.fullmatch(head_sha):
            raise LinearBoundaryError(
                "head_sha must be a full 40-character lowercase SHA"
            )
        object.__setattr__(self, "head_sha", head_sha)
        object.__setattr__(
            self,
            "provider_revision",
            _nonnegative_int(self.provider_revision, "provider_revision"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _nonnegative_int(self.observed_at, "observed_at"),
        )

    def digest(self) -> str:
        return _canonical_json_sha256(
            {
                "repository": self.ref.repository,
                "pr_number": self.ref.number,
                "pr_url": self.pr_url,
                "state": self.state,
                "is_draft": self.is_draft,
                "base_branch": self.base_branch,
                "head_branch": self.head_branch,
                "head_sha": self.head_sha,
                "provider_revision": self.provider_revision,
            }
        )


class LinearIssueSnapshotProvider(Protocol):
    """Read-only live Linear issue snapshot provider."""

    def read_issue(self, issue_id: str) -> LinearIssueSnapshot: ...


class PullRequestSnapshotProvider(Protocol):
    """Read-only trusted repository snapshot provider."""

    def read_pull_requests(
        self,
        refs: tuple[PullRequestRef, ...],
    ) -> Sequence[PullRequestSnapshot]: ...


@dataclass(frozen=True)
class LinearEventEnvelope:
    schema_version: int
    provider: str
    event_id: str
    event_kind: str
    issue_id: str
    source_key: str
    source_revision: int

    @classmethod
    def from_body(cls, body: bytes) -> "LinearEventEnvelope":
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LinearBoundaryError("event body must be a UTF-8 JSON object") from exc
        if not isinstance(decoded, dict):
            raise LinearBoundaryError("event body must be a JSON object")
        allowed = {
            "schema_version",
            "provider",
            "event_id",
            "event_kind",
            "issue_id",
            "source_key",
            "source_revision",
        }
        unknown = set(decoded) - allowed
        if unknown:
            raise LinearBoundaryError(
                f"event body has unsupported fields: {sorted(unknown)!r}"
            )
        version = _nonnegative_int(decoded.get("schema_version"), "schema_version")
        if version != EVENT_SCHEMA_VERSION:
            raise LinearBoundaryError(
                f"schema_version must be {EVENT_SCHEMA_VERSION}"
            )
        provider = _required_text(decoded.get("provider"), "provider").casefold()
        if provider != "linear":
            raise LinearBoundaryError("provider must be 'linear'")
        event_kind = _required_text(decoded.get("event_kind"), "event_kind").casefold()
        if event_kind not in VALID_EVENT_KINDS:
            raise LinearBoundaryError(
                f"event_kind must be one of {sorted(VALID_EVENT_KINDS)!r}"
            )
        return cls(
            schema_version=version,
            provider=provider,
            event_id=_required_text(decoded.get("event_id"), "event_id"),
            event_kind=event_kind,
            issue_id=_required_text(decoded.get("issue_id"), "issue_id"),
            source_key=_required_text(decoded.get("source_key"), "source_key"),
            source_revision=_nonnegative_int(
                decoded.get("source_revision"),
                "source_revision",
            ),
        )


@dataclass(frozen=True)
class InboxReceipt:
    inbox_id: int
    event_id: str
    created: bool
    duplicate_reason: Optional[str]
    status: str


@dataclass(frozen=True)
class LinearReconciliationPlan:
    inbox_id: int
    event_id: str
    event_payload_sha256: str
    inbox_source_revision: int
    issue_snapshot: LinearIssueSnapshot
    pr_snapshots: tuple[PullRequestSnapshot, ...]
    built_at: int


@dataclass(frozen=True)
class ApplyResult:
    inbox_id: int
    event_id: str
    outcome: str
    changed: bool
    coordinator_revision: Optional[int]
    pr_aggregates_observed: int


@dataclass(frozen=True)
class LinearIssueCoordinator:
    issue_id: str
    identifier: str
    title: str
    issue_url: str
    source_revision: int
    snapshot_sha256: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "LinearIssueCoordinator":
        return cls(
            issue_id=row["linear_issue_id"],
            identifier=row["linear_identifier"],
            title=row["title"],
            issue_url=row["issue_url"],
            source_revision=int(row["source_revision"]),
            snapshot_sha256=row["snapshot_sha256"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class PullRequestAggregate:
    ref: PullRequestRef
    pr_url: str
    state: str
    is_draft: bool
    base_branch: str
    head_branch: str
    current_head_sha: str
    provider_revision: int
    snapshot_sha256: str
    observed_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PullRequestAggregate":
        return cls(
            ref=PullRequestRef(row["repository"], int(row["pr_number"])),
            pr_url=row["pr_url"],
            state=row["state"],
            is_draft=bool(row["is_draft"]),
            base_branch=row["base_branch"],
            head_branch=row["head_branch"],
            current_head_sha=row["current_head_sha"],
            provider_revision=int(row["provider_revision"]),
            snapshot_sha256=row["snapshot_sha256"],
            observed_at=int(row["observed_at"]),
            updated_at=int(row["updated_at"]),
        )


def _secret_bytes(secret: str | bytes) -> bytes:
    value = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(value, bytes) or not value:
        raise LinearSignatureError("event signing secret must not be empty")
    return value


def sign_event_body(secret: str | bytes, timestamp: int | str, body: bytes) -> str:
    """Sign the internal normalized envelope contract over exact body bytes."""
    timestamp_text = str(timestamp)
    signed = timestamp_text.encode("ascii") + b"." + body
    digest = hmac.new(_secret_bytes(secret), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_event_signature(
    *,
    secret: str | bytes,
    timestamp: int | str,
    signature: str,
    body: bytes,
    now: Optional[int] = None,
    max_age_seconds: int = DEFAULT_SIGNATURE_MAX_AGE_SECONDS,
    future_skew_seconds: int = DEFAULT_SIGNATURE_FUTURE_SKEW_SECONDS,
) -> None:
    """Verify freshness and HMAC for the normalized event inbox boundary."""
    try:
        parsed_timestamp = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise LinearSignatureError("event timestamp must be integer seconds") from exc
    checked_at = int(time.time()) if now is None else int(now)
    if parsed_timestamp < checked_at - max_age_seconds:
        raise LinearSignatureError("event signature timestamp is stale")
    if parsed_timestamp > checked_at + future_skew_seconds:
        raise LinearSignatureError("event signature timestamp is too far in the future")
    expected = sign_event_body(secret, parsed_timestamp, body)
    try:
        valid = hmac.compare_digest(signature.encode("ascii"), expected.encode("ascii"))
    except (AttributeError, UnicodeEncodeError):
        valid = False
    if not valid:
        raise LinearSignatureError("event signature is invalid")


def _receipt(row: sqlite3.Row, *, created: bool, reason: Optional[str]) -> InboxReceipt:
    return InboxReceipt(
        inbox_id=int(row["id"]),
        event_id=row["event_id"],
        created=created,
        duplicate_reason=reason,
        status=row["status"],
    )


def ingest_signed_event(
    conn: sqlite3.Connection,
    *,
    body: bytes,
    timestamp: int | str,
    signature: str,
    secret: str | bytes,
    now: Optional[int] = None,
) -> InboxReceipt:
    """Authenticate and enqueue one normalized Linear wakeup idempotently."""
    received_at = int(time.time()) if now is None else int(now)
    verify_event_signature(
        secret=secret,
        timestamp=timestamp,
        signature=signature,
        body=body,
        now=received_at,
    )
    event = LinearEventEnvelope.from_body(body)
    payload_sha256 = hashlib.sha256(body).hexdigest()
    with kb.write_txn(conn):
        existing_id = conn.execute(
            "SELECT * FROM linear_event_inbox WHERE provider=? AND event_id=?",
            (event.provider, event.event_id),
        ).fetchone()
        if existing_id is not None:
            if (
                existing_id["payload_sha256"] != payload_sha256
                or existing_id["linear_issue_id"] != event.issue_id
                or existing_id["event_kind"] != event.event_kind
                or existing_id["source_key"] != event.source_key
                or int(existing_id["source_revision"]) != event.source_revision
            ):
                raise LinearReplayConflict(
                    "provider event_id was reused for a different event"
                )
            return _receipt(existing_id, created=False, reason="event_id")

        existing_revision = conn.execute(
            "SELECT * FROM linear_event_inbox "
            "WHERE provider=? AND source_key=? AND source_revision=?",
            (event.provider, event.source_key, event.source_revision),
        ).fetchone()
        if existing_revision is not None:
            if (
                existing_revision["linear_issue_id"] != event.issue_id
                or existing_revision["event_kind"] != event.event_kind
            ):
                raise LinearReplayConflict(
                    "source revision identity was reused for a different event"
                )
            return _receipt(
                existing_revision,
                created=False,
                reason="source_revision",
            )

        cursor = conn.execute(
            """
            INSERT INTO linear_event_inbox (
                provider, event_id, event_kind, linear_issue_id, source_key,
                source_revision, payload_sha256, status, read_attempt_count,
                received_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                event.provider,
                event.event_id,
                event.event_kind,
                event.issue_id,
                event.source_key,
                event.source_revision,
                payload_sha256,
                received_at,
                received_at,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Linear inbox insert did not return a row ID")
        row = conn.execute(
            "SELECT * FROM linear_event_inbox WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
        assert row is not None
        return _receipt(row, created=True, reason=None)


def build_reconciliation_plan(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    issue_provider: LinearIssueSnapshotProvider,
    pr_provider: PullRequestSnapshotProvider,
    now: Optional[int] = None,
) -> LinearReconciliationPlan:
    """Perform live readback without mutating coordinator or PR state."""
    row = conn.execute(
        "SELECT * FROM linear_event_inbox WHERE provider='linear' AND event_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise LinearBoundaryError(f"Linear event {event_id!r} does not exist")
    if row["status"] != "pending":
        raise LinearBoundaryError(
            f"Linear event {event_id!r} is already {row['status']}"
        )

    issue_snapshot = issue_provider.read_issue(row["linear_issue_id"])
    if not isinstance(issue_snapshot, LinearIssueSnapshot):
        raise LinearBoundaryError(
            "Linear issue provider returned a non-LinearIssueSnapshot"
        )
    if issue_snapshot.issue_id != row["linear_issue_id"]:
        raise LinearBoundaryError(
            "Linear issue provider returned a different stable issue ID"
        )

    pr_snapshots: tuple[PullRequestSnapshot, ...] = ()
    if issue_snapshot.attachments is not None:
        returned = tuple(
            pr_provider.read_pull_requests(issue_snapshot.attachments)
        )
        requested = set(issue_snapshot.attachments)
        by_ref: dict[PullRequestRef, PullRequestSnapshot] = {}
        for snapshot in returned:
            if not isinstance(snapshot, PullRequestSnapshot):
                raise LinearBoundaryError(
                    "PR provider returned a non-PullRequestSnapshot"
                )
            if snapshot.ref not in requested:
                raise LinearBoundaryError(
                    "PR provider returned a PR not present in Linear readback"
                )
            if snapshot.ref in by_ref:
                raise LinearBoundaryError(
                    "PR provider returned duplicate snapshots for one aggregate"
                )
            by_ref[snapshot.ref] = snapshot
        pr_snapshots = tuple(by_ref[ref] for ref in sorted(by_ref))

    return LinearReconciliationPlan(
        inbox_id=int(row["id"]),
        event_id=row["event_id"],
        event_payload_sha256=row["payload_sha256"],
        inbox_source_revision=int(row["source_revision"]),
        issue_snapshot=issue_snapshot,
        pr_snapshots=pr_snapshots,
        built_at=int(time.time()) if now is None else int(now),
    )


def _upsert_pr_aggregate(
    conn: sqlite3.Connection,
    snapshot: PullRequestSnapshot,
    *,
    changed_at: int,
) -> bool:
    existing = conn.execute(
        "SELECT * FROM linear_pr_aggregates WHERE repository=? AND pr_number=?",
        (snapshot.ref.repository, snapshot.ref.number),
    ).fetchone()
    digest = snapshot.digest()
    if existing is not None:
        existing_revision = int(existing["provider_revision"])
        if existing_revision > snapshot.provider_revision:
            return False
        if existing_revision == snapshot.provider_revision:
            if existing["snapshot_sha256"] != digest:
                raise LinearBoundaryError(
                    "PR provider reused a revision for different aggregate state"
                )
            return False

    conn.execute(
        """
        INSERT OR IGNORE INTO linear_pr_head_generations (
            repository, pr_number, head_sha, first_observed_at,
            provider_revision, state_at_observation
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.ref.repository,
            snapshot.ref.number,
            snapshot.head_sha,
            snapshot.observed_at,
            snapshot.provider_revision,
            snapshot.state,
        ),
    )
    conn.execute(
        """
        INSERT INTO linear_pr_aggregates (
            repository, pr_number, pr_url, state, is_draft, base_branch,
            head_branch, current_head_sha, provider_revision, snapshot_sha256,
            observed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository, pr_number) DO UPDATE SET
            pr_url=excluded.pr_url,
            state=excluded.state,
            is_draft=excluded.is_draft,
            base_branch=excluded.base_branch,
            head_branch=excluded.head_branch,
            current_head_sha=excluded.current_head_sha,
            provider_revision=excluded.provider_revision,
            snapshot_sha256=excluded.snapshot_sha256,
            observed_at=excluded.observed_at,
            updated_at=excluded.updated_at
        """,
        (
            snapshot.ref.repository,
            snapshot.ref.number,
            snapshot.pr_url,
            snapshot.state,
            int(snapshot.is_draft),
            snapshot.base_branch,
            snapshot.head_branch,
            snapshot.head_sha,
            snapshot.provider_revision,
            digest,
            snapshot.observed_at,
            changed_at,
        ),
    )
    return True


def apply_reconciliation_plan(
    conn: sqlite3.Connection,
    plan: LinearReconciliationPlan,
    *,
    now: Optional[int] = None,
) -> ApplyResult:
    """Atomically apply trusted readbacks after a separate event wakeup/read."""
    changed_at = int(time.time()) if now is None else int(now)
    with kb.write_txn(conn):
        event = conn.execute(
            "SELECT * FROM linear_event_inbox WHERE id=?",
            (plan.inbox_id,),
        ).fetchone()
        if event is None or event["event_id"] != plan.event_id:
            raise LinearBoundaryError("reconciliation plan no longer matches its inbox row")
        if event["payload_sha256"] != plan.event_payload_sha256:
            raise LinearReplayConflict("inbox payload changed after readback")
        if event["linear_issue_id"] != plan.issue_snapshot.issue_id:
            raise LinearBoundaryError("reconciliation issue ID does not match inbox")
        if event["status"] != "pending":
            coordinator = get_issue_coordinator(conn, plan.issue_snapshot.issue_id)
            return ApplyResult(
                inbox_id=plan.inbox_id,
                event_id=plan.event_id,
                outcome=event["status"],
                changed=False,
                coordinator_revision=(
                    coordinator.source_revision if coordinator is not None else None
                ),
                pr_aggregates_observed=0,
            )

        if plan.issue_snapshot.source_revision < plan.inbox_source_revision:
            error = (
                "live Linear source revision "
                f"{plan.issue_snapshot.source_revision} is behind inbox revision "
                f"{plan.inbox_source_revision}"
            )
            conn.execute(
                "UPDATE linear_event_inbox SET read_attempt_count=read_attempt_count+1, "
                "last_error=?, updated_at=? WHERE id=? AND status='pending'",
                (error, changed_at, plan.inbox_id),
            )
            coordinator = get_issue_coordinator(conn, plan.issue_snapshot.issue_id)
            return ApplyResult(
                inbox_id=plan.inbox_id,
                event_id=plan.event_id,
                outcome="source_not_visible",
                changed=False,
                coordinator_revision=(
                    coordinator.source_revision if coordinator is not None else None
                ),
                pr_aggregates_observed=0,
            )

        existing = conn.execute(
            "SELECT * FROM linear_issue_coordinators WHERE linear_issue_id=?",
            (plan.issue_snapshot.issue_id,),
        ).fetchone()
        issue_digest = plan.issue_snapshot.digest()
        if (
            existing is not None
            and int(existing["source_revision"]) == plan.issue_snapshot.source_revision
            and existing["snapshot_sha256"] != issue_digest
        ):
            raise LinearBoundaryError(
                "Linear provider reused a source revision for different issue state"
            )

        stale_issue = (
            existing is not None
            and int(existing["source_revision"]) > plan.issue_snapshot.source_revision
        )
        changed = False
        if not stale_issue and (
            existing is None
            or int(existing["source_revision"]) < plan.issue_snapshot.source_revision
        ):
            created_at = changed_at if existing is None else int(existing["created_at"])
            conn.execute(
                """
                INSERT INTO linear_issue_coordinators (
                    linear_issue_id, linear_identifier, title, issue_url,
                    source_revision, snapshot_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(linear_issue_id) DO UPDATE SET
                    linear_identifier=excluded.linear_identifier,
                    title=excluded.title,
                    issue_url=excluded.issue_url,
                    source_revision=excluded.source_revision,
                    snapshot_sha256=excluded.snapshot_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    plan.issue_snapshot.issue_id,
                    plan.issue_snapshot.identifier,
                    plan.issue_snapshot.title,
                    plan.issue_snapshot.issue_url,
                    plan.issue_snapshot.source_revision,
                    issue_digest,
                    created_at,
                    changed_at,
                ),
            )
            changed = True

        if not stale_issue and plan.issue_snapshot.attachments is not None:
            for ref in plan.issue_snapshot.attachments:
                cursor = conn.execute(
                    """
                    INSERT INTO linear_issue_pr_links (
                        linear_issue_id, repository, pr_number,
                        first_seen_revision, last_seen_revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(linear_issue_id, repository, pr_number) DO UPDATE SET
                        last_seen_revision=MAX(
                            last_seen_revision,
                            excluded.last_seen_revision
                        ),
                        updated_at=excluded.updated_at
                    WHERE linear_issue_pr_links.last_seen_revision
                          < excluded.last_seen_revision
                    """,
                    (
                        plan.issue_snapshot.issue_id,
                        ref.repository,
                        ref.number,
                        plan.issue_snapshot.source_revision,
                        plan.issue_snapshot.source_revision,
                        changed_at,
                        changed_at,
                    ),
                )
                changed = changed or cursor.rowcount > 0

        pr_count = 0
        if not stale_issue:
            for snapshot in plan.pr_snapshots:
                if _upsert_pr_aggregate(conn, snapshot, changed_at=changed_at):
                    changed = True
                    pr_count += 1

        status = "stale" if stale_issue else "processed"
        conn.execute(
            "UPDATE linear_event_inbox SET status=?, "
            "read_attempt_count=read_attempt_count+1, last_error=NULL, "
            "processed_at=?, updated_at=? WHERE id=? AND status='pending'",
            (status, changed_at, changed_at, plan.inbox_id),
        )
        coordinator = get_issue_coordinator(conn, plan.issue_snapshot.issue_id)
        if stale_issue:
            outcome = "stale"
        elif existing is None:
            outcome = "created"
        elif changed:
            outcome = "updated"
        else:
            outcome = "refreshed"
        return ApplyResult(
            inbox_id=plan.inbox_id,
            event_id=plan.event_id,
            outcome=outcome,
            changed=changed,
            coordinator_revision=(
                coordinator.source_revision if coordinator is not None else None
            ),
            pr_aggregates_observed=pr_count,
        )


def get_issue_coordinator(
    conn: sqlite3.Connection,
    issue_id: str,
) -> Optional[LinearIssueCoordinator]:
    row = conn.execute(
        "SELECT * FROM linear_issue_coordinators WHERE linear_issue_id=?",
        (issue_id,),
    ).fetchone()
    return LinearIssueCoordinator.from_row(row) if row is not None else None


def list_issue_pr_refs(
    conn: sqlite3.Connection,
    issue_id: str,
) -> tuple[PullRequestRef, ...]:
    rows = conn.execute(
        "SELECT repository, pr_number FROM linear_issue_pr_links "
        "WHERE linear_issue_id=? ORDER BY repository, pr_number",
        (issue_id,),
    ).fetchall()
    return tuple(PullRequestRef(row["repository"], row["pr_number"]) for row in rows)


def resolve_current_pr_aggregates(
    conn: sqlite3.Connection,
    issue_id: str,
) -> tuple[PullRequestAggregate, ...]:
    """Resolve open PRs only from trusted aggregate snapshots, never prose."""
    rows = conn.execute(
        """
        SELECT aggregate.*
          FROM linear_issue_pr_links AS link
          JOIN linear_pr_aggregates AS aggregate
            ON aggregate.repository = link.repository
           AND aggregate.pr_number = link.pr_number
         WHERE link.linear_issue_id=? AND aggregate.state='open'
         ORDER BY aggregate.repository, aggregate.pr_number
        """,
        (issue_id,),
    ).fetchall()
    return tuple(PullRequestAggregate.from_row(row) for row in rows)


def list_head_generations(
    conn: sqlite3.Connection,
    ref: PullRequestRef,
) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT head_sha FROM linear_pr_head_generations "
        "WHERE repository=? AND pr_number=? "
        "ORDER BY first_observed_at, head_sha",
        (ref.repository, ref.number),
    ).fetchall()
    return tuple(row["head_sha"] for row in rows)
