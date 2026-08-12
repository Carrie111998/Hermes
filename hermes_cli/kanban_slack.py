"""Provider-neutral Slack notification and acknowledgement boundary.

Slack is deliberately non-authoritative in this workflow. This module stores
exact-head notification intents, validates authoritative pull-request readback
before every send, and records Slack reactions/text/buttons as acknowledgement
receipts only. It exposes no GitHub approval, merge, branch-write, push,
channel-membership, or channel-management operation.

No live adapter is installed by default. Tests inject in-memory transports; a
future approved adapter must preserve the explicit channel and thread routing
stored on each intent instead of consulting ambient gateway state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Protocol, cast

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as gh


SlackSurface = Literal["channel", "thread"]
SlackOperation = Literal["notify_human_review", "reply"]
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
    "not_in_channel",
    "permission",
    "rate_limited",
    "transient",
    "network",
    "timeout",
    "server",
    "unavailable",
    "auth",
    "channel_not_found",
    "thread_not_found",
    "validation",
    "conflict",
    "unknown",
]
AcknowledgementSource = Literal["reaction", "text", "button"]
AcknowledgementAction = Literal[
    "acknowledged",
    "viewed",
    "will_review",
    "ignored",
]

SURFACE_OPERATIONS: Mapping[str, frozenset[str]] = {
    "channel": frozenset({"notify_human_review"}),
    "thread": frozenset({"reply"}),
}
OUTBOX_STATES = frozenset(
    {"pending", "attempting", "retry", "sent", "permanent_failure", "superseded"}
)
TERMINAL_OUTBOX_STATES = frozenset({"sent", "permanent_failure", "superseded"})
FAILURE_KINDS = frozenset(
    {
        "disabled",
        "not_in_channel",
        "permission",
        "rate_limited",
        "transient",
        "network",
        "timeout",
        "server",
        "unavailable",
        "auth",
        "channel_not_found",
        "thread_not_found",
        "validation",
        "conflict",
        "unknown",
    }
)
RETRYABLE_FAILURE_KINDS = frozenset(
    {"rate_limited", "transient", "network", "timeout", "server", "unavailable"}
)
ACKNOWLEDGEMENT_SOURCES = frozenset({"reaction", "text", "button"})
ACKNOWLEDGEMENT_ACTIONS = frozenset(
    {"acknowledged", "viewed", "will_review", "ignored"}
)
ACTIVE_GATE_STATES = frozenset(
    {"pending_delivery", "awaiting_human", "seen", "delivery_failed"}
)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 30
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 60
ATTEMPT_LEASE_SECONDS = 300
MAX_OUTBOX_PAYLOAD_BYTES = 128 * 1024
MAX_ROUTE_COMPONENT_BYTES = 512
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
_ROUTE_PAYLOAD_KEYS = frozenset({"channel", "channel_id", "thread", "thread_ts"})

_REACTION_ACTIONS: Mapping[str, AcknowledgementAction] = {
    "eyes": "viewed",
    "eye": "viewed",
    "white_check_mark": "acknowledged",
    "heavy_check_mark": "acknowledged",
    "check": "acknowledged",
    "+1": "acknowledged",
    "thumbsup": "acknowledged",
    "ack": "acknowledged",
    "hourglass_flowing_sand": "will_review",
    "on_it": "will_review",
    # Approval-like Slack input is explicitly acknowledgement-only evidence.
    "lgtm": "acknowledged",
    "approved": "acknowledged",
    "approve": "acknowledged",
    "merge": "acknowledged",
}
_TEXT_ACTIONS: Mapping[str, AcknowledgementAction] = {
    "seen": "viewed",
    "read": "viewed",
    "viewed": "viewed",
    "ack": "acknowledged",
    "acked": "acknowledged",
    "acknowledge": "acknowledged",
    "acknowledged": "acknowledged",
    "got it": "acknowledged",
    "received": "acknowledged",
    "on it": "will_review",
    "will review": "will_review",
    "reviewing": "will_review",
    # Slack never becomes the source of GitHub approval or merge authority.
    "approve": "acknowledged",
    "approved": "acknowledged",
    "lgtm": "acknowledged",
    "merge": "acknowledged",
    "merge it": "acknowledged",
}
_BUTTON_ACTIONS: Mapping[str, AcknowledgementAction] = {
    "ack": "acknowledged",
    "acknowledge": "acknowledged",
    "seen": "viewed",
    "will_review": "will_review",
    "review": "will_review",
    "approve": "acknowledged",
    "merge": "acknowledged",
}


class SlackBoundaryError(ValueError):
    """Normalized Slack routing, intent, or receipt violates the boundary."""


class SlackReplayConflict(SlackBoundaryError):
    """A stable provider or outbox identity was reused for different semantics."""


class SlackTransportFailure(RuntimeError):
    """Typed Slack failure classified without parsing unstable provider prose."""

    def __init__(
        self,
        message: str,
        *,
        kind: FailureKind,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        normalized_kind = str(kind).strip().casefold()
        if normalized_kind not in FAILURE_KINDS:
            raise ValueError(f"unsupported Slack failure kind: {kind!r}")
        super().__init__(message)
        self.kind: FailureKind = cast(FailureKind, normalized_kind)
        self.retry_after_seconds = (
            _nonnegative_int(retry_after_seconds, "retry_after_seconds")
            if retry_after_seconds is not None
            else None
        )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SlackBoundaryError(f"{field} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SlackBoundaryError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SlackBoundaryError(f"{field} must be a non-negative integer") from exc
    if parsed < 0:
        raise SlackBoundaryError(f"{field} must be a non-negative integer")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed < 1:
        raise SlackBoundaryError(f"{field} must be a positive integer")
    return parsed


def _canonical_repository(value: Any) -> str:
    repository = _required_text(value, "repository").casefold()
    if not _REPOSITORY_RE.fullmatch(repository):
        raise SlackBoundaryError("repository must use owner/name form")
    return repository


def _full_sha(value: Any, field: str = "head_sha") -> str:
    sha = _required_text(value, field).casefold()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise SlackBoundaryError(f"{field} must be a full 40-character lowercase SHA")
    return sha


def _route_component(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if allow_empty and (value is None or value == ""):
        return ""
    route = _required_text(value, field)
    if any(character.isspace() for character in route):
        raise SlackBoundaryError(f"{field} cannot contain whitespace")
    if len(route.encode("utf-8")) > MAX_ROUTE_COMPONENT_BYTES:
        raise SlackBoundaryError(f"{field} exceeds the route component limit")
    return route


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise SlackBoundaryError("value must be JSON serializable") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ensure_no_sensitive_keys(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise SlackBoundaryError(
                    f"sensitive field is not allowed in Slack outbox: {path}.{key}"
                )
            _ensure_no_sensitive_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _ensure_no_sensitive_keys(nested, path=f"{path}[{index}]")


@dataclass(frozen=True)
class SlackDeliveryReceipt:
    external_id: str
    message_ts: str
    thread_ts: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _required_text(self.external_id, "receipt.external_id"))
        object.__setattr__(self, "message_ts", _route_component(self.message_ts, "receipt.message_ts"))
        object.__setattr__(self, "thread_ts", _route_component(self.thread_ts, "receipt.thread_ts"))
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, "receipt.idempotency_key"),
        )


class PullRequestSnapshotProvider(Protocol):
    """Read-only adapter for authoritative current pull-request state."""

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> gh.GitHubPullRequestSnapshot: ...


class SlackDeliveryTransport(Protocol):
    """Restricted Slack sender with no channel-management or review authority."""

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> Optional[SlackDeliveryReceipt]: ...

    def send_intent(self, intent: "SlackOutboxIntent") -> SlackDeliveryReceipt: ...


class SlackAcknowledgementProvider(Protocol):
    """Read-only source of normalized acknowledgement events for one stored thread."""

    def read_acknowledgements(
        self,
        *,
        channel_id: str,
        thread_ts: str,
    ) -> tuple["SlackAcknowledgementEvent", ...]: ...


class DisabledPullRequestSnapshotProvider:
    """Fail-closed default until approved authoritative readback is wired."""

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> gh.GitHubPullRequestSnapshot:
        raise SlackTransportFailure(
            "pull-request snapshot provider is disabled",
            kind="disabled",
        )


class DisabledSlackDeliveryTransport:
    """Fail-closed default; this phase installs no live Slack sender."""

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> Optional[SlackDeliveryReceipt]:
        raise SlackTransportFailure("Slack delivery transport is disabled", kind="disabled")

    def send_intent(self, intent: "SlackOutboxIntent") -> SlackDeliveryReceipt:
        raise SlackTransportFailure("Slack delivery transport is disabled", kind="disabled")


class DisabledSlackAcknowledgementProvider:
    """Fail-closed default; this phase installs no live Slack event reader."""

    def read_acknowledgements(
        self,
        *,
        channel_id: str,
        thread_ts: str,
    ) -> tuple["SlackAcknowledgementEvent", ...]:
        raise SlackTransportFailure(
            "Slack acknowledgement provider is disabled",
            kind="disabled",
        )


@dataclass(frozen=True)
class SlackOutboxIntent:
    id: str
    gate_id: str
    source_intent_id: Optional[str]
    repository: str
    pr_number: int
    head_sha: str
    channel_id: str
    thread_ts: str
    surface: SlackSurface
    operation: SlackOperation
    payload: dict[str, Any]
    payload_sha256: str
    idempotency_key: str
    state: OutboxState
    attempt_count: int
    max_attempts: int
    next_attempt_at: Optional[int]
    external_message_ts: Optional[str]
    delivered_thread_ts: Optional[str]
    last_snapshot_sha256: Optional[str]
    last_snapshot_observed_at: Optional[int]
    last_failure_kind: Optional[FailureKind]
    last_error: Optional[str]
    created_at: int
    updated_at: int
    sent_at: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SlackOutboxIntent":
        try:
            decoded = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SlackReplayConflict(
                "stored Slack outbox payload is not valid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise SlackReplayConflict("stored Slack outbox payload is not an object")
        if _sha256_text(_canonical_json(decoded)) != row["payload_sha256"]:
            raise SlackReplayConflict("stored Slack outbox payload hash does not match")
        expected_key = _idempotency_key(
            row["channel_id"],
            row["thread_ts"],
            row["repository"],
            int(row["pr_number"]),
            row["head_sha"],
            row["surface"],
            row["operation"],
        )
        if row["idempotency_key"] != expected_key or row["id"] != _intent_id(expected_key):
            raise SlackReplayConflict(
                "stored Slack outbox routing identity does not match its replay key"
            )
        if row["state"] not in OUTBOX_STATES:
            raise SlackReplayConflict("stored Slack outbox state is unsupported")
        return cls(
            id=row["id"],
            gate_id=row["gate_id"],
            source_intent_id=row["source_intent_id"],
            repository=row["repository"],
            pr_number=int(row["pr_number"]),
            head_sha=row["head_sha"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            surface=cast(SlackSurface, row["surface"]),
            operation=cast(SlackOperation, row["operation"]),
            payload=decoded,
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
            external_message_ts=row["external_message_ts"],
            delivered_thread_ts=row["delivered_thread_ts"],
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
class SlackOutboxAttempt:
    intent_id: str
    attempt_number: int
    outcome: str
    retryable: bool
    snapshot_sha256: Optional[str]
    external_message_ts: Optional[str]
    failure_kind: Optional[FailureKind]
    error: Optional[str]
    attempted_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SlackOutboxAttempt":
        return cls(
            intent_id=row["intent_id"],
            attempt_number=int(row["attempt_number"]),
            outcome=row["outcome"],
            retryable=bool(row["retryable"]),
            snapshot_sha256=row["snapshot_sha256"],
            external_message_ts=row["external_message_ts"],
            failure_kind=(
                cast(FailureKind, row["failure_kind"])
                if row["failure_kind"] is not None
                else None
            ),
            error=row["error"],
            attempted_at=int(row["attempted_at"]),
        )


@dataclass(frozen=True)
class SlackAcknowledgementEvent:
    provider: str
    event_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    user_id: str
    source: AcknowledgementSource
    value: str
    observed_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "ack.provider").casefold())
        object.__setattr__(self, "event_id", _required_text(self.event_id, "ack.event_id"))
        object.__setattr__(self, "channel_id", _route_component(self.channel_id, "ack.channel_id"))
        object.__setattr__(self, "thread_ts", _route_component(self.thread_ts, "ack.thread_ts"))
        object.__setattr__(self, "message_ts", _route_component(self.message_ts, "ack.message_ts"))
        object.__setattr__(self, "user_id", _route_component(self.user_id, "ack.user_id"))
        normalized_source = _required_text(self.source, "ack.source").casefold()
        if normalized_source not in ACKNOWLEDGEMENT_SOURCES:
            raise SlackBoundaryError(
                f"ack.source must be one of {sorted(ACKNOWLEDGEMENT_SOURCES)!r}"
            )
        object.__setattr__(self, "source", normalized_source)
        object.__setattr__(self, "value", _required_text(self.value, "ack.value"))
        object.__setattr__(
            self,
            "observed_at",
            _nonnegative_int(self.observed_at, "ack.observed_at"),
        )

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "message_ts": self.message_ts,
            "user_id": self.user_id,
            "source": self.source,
            "value": self.value,
        }

    def payload_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.semantic_dict()))


@dataclass(frozen=True)
class SlackAcknowledgementReceipt:
    id: str
    source_intent_id: str
    provider: str
    event_id: str
    gate_id: str
    repository: str
    pr_number: int
    head_sha: str
    channel_id: str
    thread_ts: str
    message_ts: str
    user_id: str
    source: AcknowledgementSource
    normalized_action: AcknowledgementAction
    payload_sha256: str
    observed_at: int
    recorded_at: int

    @property
    def acknowledged(self) -> bool:
        return self.normalized_action != "ignored"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SlackAcknowledgementReceipt":
        return cls(
            id=row["id"],
            source_intent_id=row["source_intent_id"],
            provider=row["provider"],
            event_id=row["event_id"],
            gate_id=row["gate_id"],
            repository=row["repository"],
            pr_number=int(row["pr_number"]),
            head_sha=row["head_sha"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            message_ts=row["message_ts"],
            user_id=row["user_id"],
            source=cast(AcknowledgementSource, row["source"]),
            normalized_action=cast(AcknowledgementAction, row["normalized_action"]),
            payload_sha256=row["payload_sha256"],
            observed_at=int(row["observed_at"]),
            recorded_at=int(row["recorded_at"]),
        )


@dataclass(frozen=True)
class EnqueueReceipt:
    created: bool
    intent: SlackOutboxIntent


@dataclass(frozen=True)
class AcknowledgementRecordResult:
    created: bool
    receipt: SlackAcknowledgementReceipt


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
    if isinstance(error, gh.GitHubSnapshotUnavailable):
        return FailureClassification("unavailable", True, DEFAULT_RETRY_DELAY_SECONDS)
    if isinstance(error, SlackTransportFailure):
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
    thread_ts: str,
) -> tuple[SlackSurface, SlackOperation]:
    normalized_surface = _required_text(surface, "surface").casefold()
    normalized_operation = _required_text(operation, "operation").casefold()
    allowed = SURFACE_OPERATIONS.get(normalized_surface)
    if allowed is None or normalized_operation not in allowed:
        raise SlackBoundaryError(
            "unsupported Slack surface/operation pair: "
            f"{normalized_surface!r}/{normalized_operation!r}"
        )
    if normalized_surface == "channel" and thread_ts:
        raise SlackBoundaryError("top-level channel notifications cannot specify thread_ts")
    if normalized_surface == "thread" and not thread_ts:
        raise SlackBoundaryError("Slack thread replies require stored thread_ts")
    return cast(SlackSurface, normalized_surface), cast(SlackOperation, normalized_operation)


def _idempotency_key(
    channel_id: str,
    thread_ts: str,
    repository: str,
    pr_number: int,
    head_sha: str,
    surface: str,
    operation: str,
) -> str:
    thread_key = thread_ts or "root"
    return (
        f"slack-human-review:v1:channel:{channel_id}:thread:{thread_key}:"
        f"{repository}:pr:{pr_number}:head:{head_sha}:surface:{surface}:"
        f"operation:{operation}"
    )


def _intent_id(idempotency_key: str) -> str:
    return "slo_" + _sha256_text(idempotency_key)[:24]


def get_intent(
    conn: sqlite3.Connection,
    intent_id: str,
) -> Optional[SlackOutboxIntent]:
    row = conn.execute(
        "SELECT * FROM slack_human_review_outbox WHERE id=?",
        (intent_id,),
    ).fetchone()
    return SlackOutboxIntent.from_row(row) if row is not None else None


def list_intents(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
) -> tuple[SlackOutboxIntent, ...]:
    rows = conn.execute(
        "SELECT * FROM slack_human_review_outbox "
        "WHERE repository=? AND pr_number=? ORDER BY created_at, id",
        (_canonical_repository(repository), _positive_int(pr_number, "pr_number")),
    ).fetchall()
    return tuple(SlackOutboxIntent.from_row(row) for row in rows)


def list_attempts(
    conn: sqlite3.Connection,
    intent_id: str,
) -> tuple[SlackOutboxAttempt, ...]:
    rows = conn.execute(
        "SELECT * FROM slack_human_review_attempts "
        "WHERE intent_id=? ORDER BY attempt_number",
        (intent_id,),
    ).fetchall()
    return tuple(SlackOutboxAttempt.from_row(row) for row in rows)


def list_acknowledgements(
    conn: sqlite3.Connection,
    *,
    source_intent_id: str,
) -> tuple[SlackAcknowledgementReceipt, ...]:
    rows = conn.execute(
        "SELECT * FROM slack_human_review_acknowledgements "
        "WHERE source_intent_id=? ORDER BY observed_at, id",
        (source_intent_id,),
    ).fetchall()
    return tuple(SlackAcknowledgementReceipt.from_row(row) for row in rows)


def _supersede_stale_intents_in_txn(
    conn: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    current_head_sha: str,
    now: int,
) -> int:
    updated = conn.execute(
        "UPDATE slack_human_review_outbox SET state='superseded', "
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
    snapshot: gh.GitHubPullRequestSnapshot,
    expected_repository: str,
    expected_pr_number: int,
    expected_head_sha: str,
    channel_id: str,
    thread_ts: Optional[str],
    surface: SlackSurface,
    operation: SlackOperation,
    payload: Mapping[str, Any],
    source_intent_id: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: Optional[int] = None,
    _within_transaction: bool = False,
) -> EnqueueReceipt:
    """Create one exact-route, exact-head Slack notification intent."""
    changed_at = int(time.time()) if now is None else int(now)
    repository = _canonical_repository(expected_repository)
    pr_number = _positive_int(expected_pr_number, "expected_pr_number")
    if not isinstance(snapshot, gh.GitHubPullRequestSnapshot):
        raise SlackBoundaryError(
            "snapshot must be a GitHubPullRequestSnapshot from a trusted provider"
        )
    if snapshot.repository != repository or snapshot.pr_number != pr_number:
        raise SlackBoundaryError("pull-request snapshot identity does not match the gate")
    gh.validate_snapshot_freshness(snapshot, now=changed_at)
    try:
        gh.validate_exact_head(snapshot, expected_head_sha=expected_head_sha)
    except gh.GitHubHeadMismatch:
        supersede_stale_intents(
            conn,
            repository=repository,
            pr_number=pr_number,
            current_head_sha=snapshot.head_sha,
            now=changed_at,
        )
        raise
    if snapshot.state in {"closed", "merged"}:
        raise SlackBoundaryError(
            f"{snapshot.state} pull requests cannot receive a Slack notification"
        )
    if snapshot.is_draft:
        raise SlackBoundaryError("draft pull requests cannot receive a Slack notification")

    normalized_gate = _required_text(gate_id, "gate_id")
    normalized_channel = _route_component(channel_id, "channel_id")
    normalized_thread = _route_component(thread_ts, "thread_ts", allow_empty=True)
    normalized_surface, normalized_operation = _validate_surface_operation(
        surface,
        operation,
        normalized_thread,
    )
    normalized_source_intent_id = (
        _required_text(source_intent_id, "source_intent_id")
        if source_intent_id is not None
        else None
    )
    if normalized_surface == "channel" and normalized_source_intent_id is not None:
        raise SlackBoundaryError(
            "top-level Slack notifications cannot specify source_intent_id"
        )
    if normalized_surface == "thread":
        if normalized_source_intent_id is None:
            raise SlackBoundaryError(
                "Slack thread replies require a sent source notification"
            )
        source_intent = get_intent(conn, normalized_source_intent_id)
        if source_intent is None:
            raise SlackBoundaryError(
                f"Slack source intent {normalized_source_intent_id!r} does not exist"
            )
        if (
            source_intent.surface != "channel"
            or source_intent.operation != "notify_human_review"
            or source_intent.state != "sent"
        ):
            raise SlackBoundaryError(
                "Slack thread replies require a sent top-level notification"
            )
        if (
            source_intent.gate_id != normalized_gate
            or source_intent.repository != snapshot.repository
            or source_intent.pr_number != snapshot.pr_number
            or source_intent.head_sha != snapshot.head_sha
            or source_intent.channel_id != normalized_channel
            or source_intent.delivered_thread_ts != normalized_thread
        ):
            raise SlackBoundaryError(
                "Slack thread route does not match the stored source notification"
            )
    if not isinstance(payload, Mapping):
        raise SlackBoundaryError("payload must be an object")
    payload_copy = dict(payload)
    if any(key in payload_copy for key in _ROUTE_PAYLOAD_KEYS):
        raise SlackBoundaryError(
            "Slack routing belongs in immutable outbox fields, not the payload"
        )
    _required_text(payload_copy.get("body"), "payload.body")
    _ensure_no_sensitive_keys(payload_copy)
    payload_json = _canonical_json(payload_copy)
    if len(payload_json.encode("utf-8")) > MAX_OUTBOX_PAYLOAD_BYTES:
        raise SlackBoundaryError("Slack outbox payload exceeds the 128 KiB audit limit")
    payload_sha256 = _sha256_text(payload_json)
    attempts = _positive_int(max_attempts, "max_attempts")
    key = _idempotency_key(
        normalized_channel,
        normalized_thread,
        snapshot.repository,
        snapshot.pr_number,
        snapshot.head_sha,
        normalized_surface,
        normalized_operation,
    )
    intent_id = _intent_id(key)

    if _within_transaction and not conn.in_transaction:
        raise SlackBoundaryError("internal enqueue requires an active transaction")
    scope = nullcontext() if _within_transaction else kb.write_txn(conn)
    with scope:
        _supersede_stale_intents_in_txn(
            conn,
            repository=snapshot.repository,
            pr_number=snapshot.pr_number,
            current_head_sha=snapshot.head_sha,
            now=changed_at,
        )
        existing_row = conn.execute(
            "SELECT * FROM slack_human_review_outbox "
            "WHERE channel_id=? AND thread_ts=? AND repository=? AND pr_number=? "
            "AND head_sha=? AND surface=? AND operation=?",
            (
                normalized_channel,
                normalized_thread,
                snapshot.repository,
                snapshot.pr_number,
                snapshot.head_sha,
                normalized_surface,
                normalized_operation,
            ),
        ).fetchone()
        if existing_row is not None:
            existing = SlackOutboxIntent.from_row(existing_row)
            if (
                existing.payload_sha256 != payload_sha256
                or existing.gate_id != normalized_gate
                or existing.max_attempts != attempts
            ):
                raise SlackReplayConflict(
                    "Slack outbox identity was reused with a different payload or gate"
                )
            return EnqueueReceipt(False, existing)

        conn.execute(
            """
            INSERT INTO slack_human_review_outbox (
                id, gate_id, source_intent_id, repository, pr_number, head_sha, channel_id,
                thread_ts, surface, operation, payload_json, payload_sha256,
                idempotency_key, state, attempt_count, max_attempts,
                next_attempt_at, external_message_ts, delivered_thread_ts,
                last_snapshot_sha256, last_snapshot_observed_at,
                last_failure_kind, last_error, created_at, updated_at, sent_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL,
                NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL
            )
            """,
            (
                intent_id,
                normalized_gate,
                normalized_source_intent_id,
                snapshot.repository,
                snapshot.pr_number,
                snapshot.head_sha,
                normalized_channel,
                normalized_thread,
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


def _insert_attempt(
    conn: sqlite3.Connection,
    intent: SlackOutboxIntent,
    *,
    outcome: str,
    retryable: bool,
    snapshot_sha256: Optional[str],
    external_message_ts: Optional[str],
    failure_kind: Optional[FailureKind],
    error: Optional[str],
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO slack_human_review_attempts (
            intent_id, attempt_number, outcome, retryable, snapshot_sha256,
            external_message_ts, failure_kind, error, attempted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent.id,
            intent.attempt_count,
            outcome,
            int(retryable),
            snapshot_sha256,
            external_message_ts,
            failure_kind,
            error,
            now,
        ),
    )


def _claim_intent(
    conn: sqlite3.Connection,
    intent_id: str,
    *,
    now: int,
) -> Optional[SlackOutboxIntent]:
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT * FROM slack_human_review_outbox WHERE id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise SlackBoundaryError(f"Slack outbox intent {intent_id!r} does not exist")
        current = SlackOutboxIntent.from_row(row)
        if current.state == "attempting":
            if current.updated_at > now - ATTEMPT_LEASE_SECONDS:
                return None
            _insert_attempt(
                conn,
                current,
                outcome="attempt_lease_expired",
                retryable=True,
                snapshot_sha256=current.last_snapshot_sha256,
                external_message_ts=None,
                failure_kind="unavailable",
                error="delivery attempt lease expired; readback required",
                now=now,
            )
            conn.execute(
                "UPDATE slack_human_review_outbox SET state='retry', "
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
                "UPDATE slack_human_review_outbox "
                "SET state='permanent_failure', last_failure_kind='validation', "
                "last_error='maximum attempts already exhausted', updated_at=? "
                "WHERE id=? AND state=?",
                (now, intent_id, current.state),
            )
            return None
        updated = conn.execute(
            "UPDATE slack_human_review_outbox "
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


def _safe_error(error: BaseException) -> str:
    return str(error).replace("\n", " ")[:500]


def _validate_snapshot_identity(
    intent: SlackOutboxIntent,
    snapshot: gh.GitHubPullRequestSnapshot,
) -> None:
    if snapshot.repository != intent.repository or snapshot.pr_number != intent.pr_number:
        raise SlackTransportFailure(
            "pull-request snapshot identity does not match the Slack intent",
            kind="validation",
        )


def _read_gate_state(
    conn: sqlite3.Connection,
    intent: SlackOutboxIntent,
) -> str:
    row = conn.execute(
        "SELECT repo, pr_number, approved_head_sha, state "
        "FROM human_review_gates WHERE id=?",
        (intent.gate_id,),
    ).fetchone()
    if row is None:
        raise SlackTransportFailure(
            "Slack intent is not bound to a persisted human-review gate",
            kind="validation",
        )
    if (
        str(row["repo"]).casefold() != intent.repository
        or int(row["pr_number"]) != intent.pr_number
        or str(row["approved_head_sha"]).casefold() != intent.head_sha
    ):
        raise SlackTransportFailure(
            "persisted human-review gate identity does not match the Slack intent",
            kind="validation",
        )
    return str(row["state"]).strip().casefold()


def _validate_receipt(
    intent: SlackOutboxIntent,
    receipt: SlackDeliveryReceipt,
) -> SlackDeliveryReceipt:
    if not isinstance(receipt, SlackDeliveryReceipt):
        raise SlackTransportFailure(
            "Slack delivery transport returned an invalid receipt",
            kind="validation",
        )
    if receipt.idempotency_key != intent.idempotency_key:
        raise SlackTransportFailure(
            "Slack delivery receipt idempotency key does not match the intent",
            kind="conflict",
        )
    if intent.surface == "channel" and receipt.thread_ts != receipt.message_ts:
        raise SlackTransportFailure(
            "top-level Slack delivery receipt must use message_ts as thread_ts",
            kind="conflict",
        )
    if intent.thread_ts and receipt.thread_ts != intent.thread_ts:
        raise SlackTransportFailure(
            "Slack delivery receipt changed the stored thread route",
            kind="conflict",
        )
    return receipt


def _record_validated_snapshot(
    conn: sqlite3.Connection,
    intent: SlackOutboxIntent,
    snapshot: gh.GitHubPullRequestSnapshot,
    *,
    now: int,
) -> None:
    conn.execute(
        "UPDATE slack_human_review_outbox "
        "SET last_snapshot_sha256=?, last_snapshot_observed_at=?, updated_at=? "
        "WHERE id=? AND state='attempting'",
        (snapshot.snapshot_sha256(), snapshot.observed_at, now, intent.id),
    )


def _mark_sent(
    conn: sqlite3.Connection,
    intent: SlackOutboxIntent,
    receipt: SlackDeliveryReceipt,
    *,
    snapshot_sha256: str,
    outcome: str,
    now: int,
) -> ProcessIntentResult:
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE slack_human_review_outbox SET state='sent', "
            "external_message_ts=?, delivered_thread_ts=?, next_attempt_at=NULL, "
            "last_failure_kind=NULL, last_error=NULL, updated_at=?, sent_at=? "
            "WHERE id=? AND state='attempting'",
            (receipt.message_ts, receipt.thread_ts, now, now, intent.id),
        )
        if updated.rowcount != 1:
            current = get_intent(conn, intent.id)
            if current is None:
                raise SlackBoundaryError(
                    f"Slack outbox intent {intent.id!r} disappeared while sending"
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
            external_message_ts=receipt.message_ts,
            failure_kind=None,
            error=None,
            now=now,
        )
    return ProcessIntentResult(
        intent.id,
        "sent",
        outcome,
        sent=True,
        deduplicated=outcome in {"already_delivered", "sent_after_readback"},
    )


def _mark_failure(
    conn: sqlite3.Connection,
    intent: SlackOutboxIntent,
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
            "UPDATE slack_human_review_outbox SET state=?, next_attempt_at=?, "
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
                external_message_ts=None,
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
    intent: SlackOutboxIntent,
    snapshot: gh.GitHubPullRequestSnapshot,
    *,
    outcome: str,
    now: int,
) -> ProcessIntentResult:
    snapshot_sha256 = snapshot.snapshot_sha256()
    with kb.write_txn(conn):
        if snapshot.state in {"closed", "merged"}:
            conn.execute(
                "UPDATE slack_human_review_outbox SET state='superseded', "
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
            "UPDATE slack_human_review_outbox SET state='superseded', "
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
            external_message_ts=None,
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
    snapshot_provider: Optional[PullRequestSnapshotProvider] = None,
    delivery_transport: Optional[SlackDeliveryTransport] = None,
    now: Optional[int] = None,
) -> ProcessIntentResult:
    """Revalidate one due intent and deliver it once to its stored Slack route."""
    attempted_at = int(time.time()) if now is None else int(now)
    initial = get_intent(conn, intent_id)
    if initial is None:
        raise SlackBoundaryError(f"Slack outbox intent {intent_id!r} does not exist")
    if initial.state in TERMINAL_OUTBOX_STATES:
        return ProcessIntentResult(initial.id, initial.state, "already_terminal")
    if initial.next_attempt_at is not None and initial.next_attempt_at > attempted_at:
        return ProcessIntentResult(initial.id, initial.state, "not_due")

    claimed = _claim_intent(conn, intent_id, now=attempted_at)
    if claimed is None:
        current = get_intent(conn, intent_id)
        if current is None:
            raise SlackBoundaryError(
                f"Slack outbox intent {intent_id!r} disappeared while claiming"
            )
        outcome = (
            "not_due"
            if current.next_attempt_at is not None
            and current.next_attempt_at > attempted_at
            else (
                "in_progress" if current.state == "attempting" else "already_terminal"
            )
        )
        return ProcessIntentResult(current.id, current.state, outcome)

    provider = snapshot_provider or DisabledPullRequestSnapshotProvider()
    transport = delivery_transport or DisabledSlackDeliveryTransport()
    snapshot: Optional[gh.GitHubPullRequestSnapshot] = None
    try:
        read_snapshot = provider.read_snapshot(
            repository=claimed.repository,
            pr_number=claimed.pr_number,
        )
        if not isinstance(read_snapshot, gh.GitHubPullRequestSnapshot):
            raise SlackTransportFailure(
                "pull-request snapshot provider returned an invalid snapshot",
                kind="validation",
            )
        snapshot = read_snapshot
        _validate_snapshot_identity(claimed, read_snapshot)
        gh.validate_snapshot_freshness(read_snapshot, now=attempted_at)
        gate_state = _read_gate_state(conn, claimed)
        if gate_state not in ACTIVE_GATE_STATES:
            return _mark_superseded(
                conn,
                claimed,
                read_snapshot,
                outcome=f"human_gate_{gate_state or 'invalid'}",
                now=attempted_at,
            )
        if read_snapshot.head_sha != claimed.head_sha:
            return _mark_superseded(
                conn,
                claimed,
                read_snapshot,
                outcome="head_superseded",
                now=attempted_at,
            )
        if read_snapshot.state in {"closed", "merged"} or read_snapshot.is_draft:
            return _mark_superseded(
                conn,
                claimed,
                read_snapshot,
                outcome=(
                    f"pull_request_{read_snapshot.state}"
                    if read_snapshot.state in {"closed", "merged"}
                    else "pull_request_draft"
                ),
                now=attempted_at,
            )
        gh.validate_exact_head(read_snapshot, expected_head_sha=claimed.head_sha)
        snapshot_sha256 = read_snapshot.snapshot_sha256()
        with kb.write_txn(conn):
            _record_validated_snapshot(conn, claimed, read_snapshot, now=attempted_at)

        existing = transport.find_delivery(idempotency_key=claimed.idempotency_key)
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
            receipt = _validate_receipt(claimed, transport.send_intent(claimed))
        except BaseException as send_error:
            try:
                readback = transport.find_delivery(
                    idempotency_key=claimed.idempotency_key
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
            if isinstance(snapshot, gh.GitHubPullRequestSnapshot)
            else None
        )
        return _mark_failure(
            conn,
            claimed,
            error,
            snapshot_sha256=snapshot_sha256,
            now=attempted_at,
        )


def _normalized_ack_value(source: AcknowledgementSource, value: str) -> str:
    normalized = value.strip().casefold().strip(":")
    if source == "reaction":
        return normalized.replace("-", "_").replace(" ", "_")
    if source == "button":
        return normalized.replace("-", "_").replace(" ", "_")
    return " ".join(normalized.replace("_", " ").replace("-", " ").split()).strip(
        " .,!?:;"
    )


def normalize_acknowledgement_action(
    source: AcknowledgementSource,
    value: str,
) -> AcknowledgementAction:
    """Reduce Slack affordances to acknowledgement-only, never approval."""
    normalized_source = _required_text(source, "ack.source").casefold()
    if normalized_source not in ACKNOWLEDGEMENT_SOURCES:
        raise SlackBoundaryError(
            f"ack.source must be one of {sorted(ACKNOWLEDGEMENT_SOURCES)!r}"
        )
    normalized_value = _normalized_ack_value(
        cast(AcknowledgementSource, normalized_source),
        _required_text(value, "ack.value"),
    )
    if normalized_source == "reaction":
        return _REACTION_ACTIONS.get(normalized_value, "ignored")
    if normalized_source == "button":
        return _BUTTON_ACTIONS.get(normalized_value, "ignored")
    return _TEXT_ACTIONS.get(normalized_value, "ignored")


def record_acknowledgement(
    conn: sqlite3.Connection,
    *,
    source_intent_id: str,
    event: SlackAcknowledgementEvent,
    now: Optional[int] = None,
) -> AcknowledgementRecordResult:
    """Persist a replay-safe acknowledgement without changing review authority."""
    recorded_at = int(time.time()) if now is None else int(now)
    source_intent = get_intent(conn, _required_text(source_intent_id, "source_intent_id"))
    if source_intent is None:
        raise SlackBoundaryError(
            f"Slack source intent {source_intent_id!r} does not exist"
        )
    if source_intent.operation != "notify_human_review" or source_intent.surface != "channel":
        raise SlackBoundaryError(
            "Slack acknowledgements must bind to a sent top-level notification"
        )
    if source_intent.state != "sent":
        raise SlackBoundaryError("Slack acknowledgements require a sent notification")
    if not source_intent.delivered_thread_ts or not source_intent.external_message_ts:
        raise SlackBoundaryError("sent Slack notification is missing stored route receipts")
    if event.channel_id != source_intent.channel_id:
        raise SlackBoundaryError("Slack acknowledgement channel does not match stored route")
    if event.thread_ts != source_intent.delivered_thread_ts:
        raise SlackBoundaryError("Slack acknowledgement thread does not match stored thread_ts")

    normalized_action = normalize_acknowledgement_action(event.source, event.value)
    payload_sha256 = event.payload_sha256()
    acknowledgement_id = "sla_" + _sha256_text(
        f"{event.provider}:{event.event_id}"
    )[:24]

    with kb.write_txn(conn):
        existing_event_row = conn.execute(
            "SELECT * FROM slack_human_review_acknowledgements "
            "WHERE provider=? AND event_id=?",
            (event.provider, event.event_id),
        ).fetchone()
        if existing_event_row is not None:
            existing = SlackAcknowledgementReceipt.from_row(existing_event_row)
            if (
                existing.source_intent_id != source_intent.id
                or existing.payload_sha256 != payload_sha256
            ):
                raise SlackReplayConflict(
                    "Slack acknowledgement event ID was reused with different semantics"
                )
            return AcknowledgementRecordResult(False, existing)

        semantic_row = conn.execute(
            "SELECT * FROM slack_human_review_acknowledgements "
            "WHERE source_intent_id=? AND payload_sha256=?",
            (source_intent.id, payload_sha256),
        ).fetchone()
        if semantic_row is not None:
            return AcknowledgementRecordResult(
                False,
                SlackAcknowledgementReceipt.from_row(semantic_row),
            )

        conn.execute(
            """
            INSERT INTO slack_human_review_acknowledgements (
                id, source_intent_id, provider, event_id, gate_id, repository,
                pr_number, head_sha, channel_id, thread_ts, message_ts, user_id,
                source, normalized_action, payload_sha256, observed_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                acknowledgement_id,
                source_intent.id,
                event.provider,
                event.event_id,
                source_intent.gate_id,
                source_intent.repository,
                source_intent.pr_number,
                source_intent.head_sha,
                event.channel_id,
                event.thread_ts,
                event.message_ts,
                event.user_id,
                event.source,
                normalized_action,
                payload_sha256,
                event.observed_at,
                recorded_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM slack_human_review_acknowledgements WHERE id=?",
            (acknowledgement_id,),
        ).fetchone()
        assert row is not None
        return AcknowledgementRecordResult(
            True,
            SlackAcknowledgementReceipt.from_row(row),
        )
