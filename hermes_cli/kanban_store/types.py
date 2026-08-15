"""Typed contracts for the zero-authority Kanban execution boundary.

This module intentionally contains no database, network, process, or plugin
initialization.  The immutable values here are safe to share between the
store, strict worker runtime, publisher controller, and read-only surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class ContractError(ValueError):
    """A caller supplied a value outside the versioned contract."""


class FenceConflict(RuntimeError):
    """The task/run/generation/token fence no longer identifies the owner."""


class AlreadyFinalized(RuntimeError):
    """The run generation already has a terminal finalization."""


class PublicationKind(str, Enum):
    GITHUB_ISSUE_CREATE = "github.issue.create"
    GITHUB_ISSUE_COMMENT_CREATE = "github.issue.comment.create"
    HERMES_TASK_COMPLETION_NOTIFY = "hermes.task_completion.notify"
    HERMES_ARTIFACT_DELIVER = "hermes.artifact.deliver"


class TaskState(str, Enum):
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    AWAITING_PUBLICATION = "awaiting_publication"
    PUBLICATION_ATTENTION = "publication_attention"


class IntentState(str, Enum):
    SEALED = "sealed"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPATCH_CLAIMED = "dispatch_claimed"
    DISPATCH_STARTED = "dispatch_started"
    RECEIPT = "receipt"
    RECONCILE_REQUIRED = "reconcile_required"
    CONFLICT = "conflict"


class DispatchDisposition(str, Enum):
    SUCCESS = "success"
    DEFINITE_NO_EFFECT = "definite_no_effect"
    AMBIGUOUS = "ambiguous"


class TriState(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class ProcessState(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


class MotionState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    UNKNOWN = "unknown"


class CoverageState(str, Enum):
    STRONG = "strong"
    BEST_EFFORT = "best_effort"
    UNKNOWN = "unknown"


class ReclaimDecision(str, Enum):
    PRESERVE = "preserve"
    ELIGIBLE_DEAD = "eligible_dead"
    ELIGIBLE_INERT = "eligible_inert"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunFence:
    task_id: str
    run_id: int
    claim_generation: int
    claim_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.task_id or len(self.task_id) > 256:
            raise ContractError("task_id must be 1..256 characters")
        if self.run_id <= 0:
            raise ContractError("run_id must be positive")
        if self.claim_generation <= 0:
            raise ContractError("claim_generation must be positive")
        if len(self.claim_token) < 32 or len(self.claim_token) > 512:
            raise ContractError("claim_token length is outside the contract")


@dataclass(frozen=True, slots=True)
class RunBinding:
    board_id: str
    database_id: str
    worker_id: str
    profile: str
    strict: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("board_id", self.board_id),
            ("database_id", self.database_id),
            ("worker_id", self.worker_id),
            ("profile", self.profile),
        ):
            if not value or len(value) > 256:
                raise ContractError(f"{name} must be 1..256 characters")


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Host-resolved provider route bound to one strict worker run."""

    provider: str
    model: str
    api_mode: str
    session_id: str
    source: str = "controller_resolved_provider_route"

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("api_mode", self.api_mode),
            ("session_id", self.session_id),
        ):
            if not value or len(value) > 512:
                raise ContractError(f"runtime identity {name} is invalid")
        if self.source != "controller_resolved_provider_route":
            raise ContractError("runtime identity source is not host-resolved")

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_mode": self.api_mode,
            "session_id": self.session_id,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class WorkerContext:
    fence: RunFence
    binding: RunBinding
    capability_manifest_sha256: str
    runtime_identity: RuntimeIdentity
    workspace: str
    artifact_root: str
    broker_socket: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if len(self.capability_manifest_sha256) != 64:
            raise ContractError("capability manifest digest must be SHA-256 hex")
        if self.schema_version != 1:
            raise ContractError("unsupported worker context schema")


@dataclass(frozen=True, slots=True)
class DraftIntent:
    kind: PublicationKind
    target: Mapping[str, Any]
    payload: Mapping[str, Any]
    client_nonce: str

    def __post_init__(self) -> None:
        if not self.client_nonce or len(self.client_nonce) > 128:
            raise ContractError("client_nonce must be 1..128 characters")


@dataclass(frozen=True, slots=True)
class ArtifactDeclaration:
    relative_path: str
    display_name: str
    media_type: str

    def __post_init__(self) -> None:
        if not self.relative_path or len(self.relative_path) > 1024:
            raise ContractError("artifact relative path is invalid")
        if not self.display_name or len(self.display_name) > 512:
            raise ContractError("artifact display name is invalid")
        if not self.media_type or len(self.media_type) > 255:
            raise ContractError("artifact media type is invalid")


@dataclass(frozen=True, slots=True)
class FinalizationRequest:
    fence: RunFence
    outcome: str
    summary: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    artifacts: Sequence[ArtifactDeclaration] = field(default_factory=tuple)
    draft_intents: Sequence[DraftIntent] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "blocked", "review", "changes"}:
            raise ContractError("unsupported finalization outcome")
        if len(self.summary.encode("utf-8")) > 64 * 1024:
            raise ContractError("summary exceeds 64 KiB")
        if len(self.artifacts) > 128:
            raise ContractError("too many artifacts")
        if len(self.draft_intents) > 32:
            raise ContractError("too many draft intents")


@dataclass(frozen=True, slots=True)
class TrustedIntentPolicy:
    """Trusted projection of a worker draft into an allowed publication.

    The worker does not choose whether an intent is required, the publisher
    principal, adapter version, or repository/platform identity.  Those values
    are supplied by this trusted policy after validating the draft.
    """

    required: bool
    publisher_principal: str
    adapter_version: str
    target: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedIntent:
    intent_id: str
    kind: PublicationKind
    required: bool
    publisher_principal: str
    adapter_version: str
    target: Mapping[str, Any]
    payload: Mapping[str, Any]
    application_headers: Mapping[str, str]
    marker: str
    prepared_bytes: bytes = field(repr=False)
    request_body_bytes: bytes = field(repr=False)
    request_body_sha256: str
    wire_sha256: str


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    disposition: DispatchDisposition
    remote_identity: str | None = None
    status_code: int | None = None
    detail_code: str | None = None
    response_digest: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceVector:
    process: ProcessState
    motion: MotionState
    artifacts: TriState
    publication: TriState
    coverage: CoverageState
    decision: ReclaimDecision
    reason_codes: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_uuid: str
    task_id: str
    run_id: int | None
    claim_generation: int | None
    event_type: str
    source: str
    severity: str
    retention_class: str
    payload: Mapping[str, Any]
    correlation_id: str | None = None
    operation_id: str | None = None
    stream: str | None = None
    stream_seq: int | None = None
    producer_time: int | None = None
    schema_version: int = 1
