"""Provider-neutral contracts for Hermes-owned conversation sessions.

These types deliberately contain no provider thread or response identifiers.
Provider continuations are optional hints bound to a compiled Hermes revision;
they are never required to reconstruct a turn from the canonical snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from agent.models_dev import ModelCapabilities


Message = Mapping[str, Any]
ToolSchema = Mapping[str, Any]


@dataclass(frozen=True)
class TurnCommand:
    """One idempotent user-turn append against an expected session revision."""

    session_id: str
    turn_id: str
    idempotency_key: str
    expected_revision: int
    user_event: Message

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if not isinstance(self.user_event, Mapping):
            raise ValueError("user_event must be a mapping")
        if self.user_event.get("role") != "user":
            raise ValueError("user_event must have role='user'")


@dataclass(frozen=True)
class CanonicalSessionEvent:
    """One ordered, durable event in the Hermes session journal projection."""

    event_id: str
    sequence: int
    message: Message
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if any(not event_id.strip() for event_id in self.source_event_ids):
            raise ValueError("source_event_ids must not contain empty values")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids must be unique")


@dataclass(frozen=True)
class SessionSnapshot:
    """Read-only input projection of Hermes' canonical session journal."""

    session_id: str
    revision: int
    events: tuple[CanonicalSessionEvent, ...]

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        sequences = tuple(event.sequence for event in self.events)
        if sequences != tuple(sorted(sequences)) or len(set(sequences)) != len(sequences):
            raise ValueError("events must have unique ascending sequence values")
        event_ids = tuple(event.event_id for event in self.events)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("events must have unique event_id values")


@dataclass(frozen=True)
class SessionAuthorization:
    """Explicit session IDs a caller may read or append.

    Lineage/profile resolution belongs at the authenticated boundary that
    constructs this value. Session readers never infer or substitute identity
    from a caller-supplied profile or raw database path.
    """

    principal: str
    allowed_session_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.principal.strip():
            raise ValueError("principal must not be empty")
        if any(not session_id.strip() for session_id in self.allowed_session_ids):
            raise ValueError("allowed_session_ids must not contain empty values")

    def require(self, session_id: str) -> None:
        if session_id not in self.allowed_session_ids:
            raise SessionAuthorizationError(
                f"principal {self.principal!r} is not authorized for session"
            )


class SessionAuthorizationError(PermissionError):
    """The authenticated caller may not access the requested session."""


class StaleSessionRevisionError(RuntimeError):
    """The append expected an older or newer canonical session revision."""


class TurnIdempotencyConflictError(RuntimeError):
    """A turn/idempotency key was reused for a different user event."""


class TurnLeaseConflictError(RuntimeError):
    """A non-expired execution lease is owned by another coordinator."""


class TurnState(str, Enum):
    """Durable lifecycle of one Hermes-owned turn command."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True)
class AppendTurnReceipt:
    """Current durable state of one canonical idempotent turn.

    ``event_revision`` is the immutable revision created by the user-event
    append. ``session_revision`` is the latest active journal revision at read
    time and therefore the value a client uses for its next command. Keeping
    the two explicit avoids changing an idempotent replay's append identity
    merely because assistant/tool events were written later.
    """

    session_id: str
    turn_id: str
    idempotency_key: str
    prior_revision: int
    event_revision: int
    session_revision: int
    event_id: str
    projection_row_id: int
    appended: bool
    state: TurnState = TurnState.ACCEPTED
    attempt: int = 0
    terminal_revision: int | None = None

    @property
    def revision(self) -> int:
        """Backward-compatible alias for the latest session revision."""

        return self.session_revision


@dataclass(frozen=True)
class AcceptedTurn:
    """A command whose user event is already durable in the Hermes journal."""

    command: TurnCommand
    receipt: AppendTurnReceipt

    def __post_init__(self) -> None:
        if self.command.session_id != self.receipt.session_id:
            raise ValueError("accepted turn session identities do not match")
        if self.command.turn_id != self.receipt.turn_id:
            raise ValueError("accepted turn IDs do not match")
        if self.command.idempotency_key != self.receipt.idempotency_key:
            raise ValueError("accepted turn idempotency keys do not match")
        if self.receipt.state not in {TurnState.ACCEPTED, TurnState.RUNNING}:
            raise ValueError("accepted turn receipt must be accepted or running")


@dataclass(frozen=True)
class TurnExecutionLease:
    """Exclusive, expiring authority to execute one accepted turn."""

    session_id: str
    turn_id: str
    owner_id: str
    attempt: int
    lease_expires_at: float


class ToolExecutionState(str, Enum):
    """Crash-recovery state for a model-issued tool call."""

    RUNNING = "running"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ToolExecutionReceipt:
    """Durable decision for one tool call inside a canonical turn.

    ``execute`` is true only for a newly claimed call or a safe no-effect
    retry. A completed call replays its stored result. An effect-capable call
    left running by a dead process becomes ``uncertain`` and is never executed
    again automatically, preventing duplicate external side effects.
    """

    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    payload_hash: str
    state: ToolExecutionState
    attempt: int
    execute: bool
    result: Any = None


@dataclass(frozen=True)
class CompilationMessage:
    """One provider-neutral message candidate for a model invocation.

    ``required`` marks instructions and the complete active-turn suffix.  A
    compiler may omit older, optional history but must never omit a required
    item.  ``source_event_ids`` trace the projection back to Hermes' canonical
    journal; request-only instructions and adapter scaffolding have no source
    event.
    """

    message: Message
    source_event_ids: tuple[str, ...] = ()
    required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.message, Mapping):
            raise ValueError("message must be a mapping")
        if not self.message.get("role"):
            raise ValueError("message role must not be empty")
        if any(not event_id.strip() for event_id in self.source_event_ids):
            raise ValueError("source_event_ids must not contain empty values")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids must be unique per message")


@dataclass(frozen=True)
class ModelInvocation:
    """Canonical input to the shared compiler for any model call.

    Unlike :class:`TurnCommand`, this describes an inference inside an already
    active turn.  Later calls may end in assistant/tool events rather than a
    new user event, which is why the complete required suffix is represented
    by :class:`CompilationMessage` instead of a single ``user_event``.
    """

    session_id: str
    session_revision: int
    turn_id: str
    messages: tuple[CompilationMessage, ...]

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if self.session_revision < 0:
            raise ValueError("session_revision must be non-negative")
        if not self.turn_id.strip():
            raise ValueError("turn_id must not be empty")


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    """Versioned logical tool surface considered for one compilation."""

    version: str
    tools: tuple[ToolSchema, ...] = ()


@dataclass(frozen=True)
class ContextComponentUsage:
    instructions_tokens: int
    history_tokens: int
    current_input_tokens: int
    tool_tokens: int
    fixed_overhead_tokens: int
    output_reserve_tokens: int

    @property
    def input_tokens(self) -> int:
        return (
            self.instructions_tokens
            + self.history_tokens
            + self.current_input_tokens
            + self.tool_tokens
            + self.fixed_overhead_tokens
        )

    @property
    def total_reserved_tokens(self) -> int:
        return self.input_tokens + self.output_reserve_tokens


@dataclass(frozen=True)
class ContextReceipt:
    """Content-free accounting and lineage for a compiled request."""

    source_revision: int
    source_event_count: int
    retained_event_ids: tuple[str, ...]
    omitted_event_ids: tuple[str, ...]
    tool_catalog_version: str
    selected_tool_count: int
    usage: ContextComponentUsage
    estimator: str


@dataclass(frozen=True)
class CompiledTurn:
    """Bounded provider-neutral input accepted by model adapters."""

    session_id: str
    session_revision: int
    turn_id: str
    model: str
    provider: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...]
    capabilities: ModelCapabilities
    receipt: ContextReceipt
    context_fingerprint: str


class ContextCompilationFailureReason(str, Enum):
    MANDATORY_ENVELOPE_EXCEEDS_CAPACITY = "mandatory_envelope_exceeds_capacity"
    CAPACITY_UNKNOWN = "capacity_unknown"
    HISTORY_CANNOT_FIT_WITHOUT_CHECKPOINT = "history_cannot_fit_without_checkpoint"
    INVALID_CURRENT_TURN = "invalid_current_turn"
    UNSUPPORTED_REQUIRED_CONTENT = "unsupported_required_content"


@dataclass(frozen=True)
class ContextCompilationFailure:
    reason: ContextCompilationFailureReason
    session_id: str
    session_revision: int
    turn_id: str
    capacity_tokens: int
    required_tokens: int


@dataclass(frozen=True)
class ContextCompilationResult:
    compiled: CompiledTurn | None = None
    failure: ContextCompilationFailure | None = None

    def __post_init__(self) -> None:
        if (self.compiled is None) == (self.failure is None):
            raise ValueError("exactly one of compiled or failure is required")

    @property
    def ok(self) -> bool:
        return self.compiled is not None


@dataclass(frozen=True)
class CanonicalModelEvent:
    """Provider output normalized before it is appended to the session."""

    turn_id: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderContinuation:
    """Disposable provider optimization tied to exact Hermes state."""

    adapter: str
    handle: str
    session_revision: int
    context_fingerprint: str


class ModelAdapter(Protocol):
    """Provider wire adapter; session selection and persistence stay outside."""

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def run(
        self,
        turn: CompiledTurn,
        *,
        continuation: ProviderContinuation | None = None,
    ) -> Sequence[CanonicalModelEvent]: ...
