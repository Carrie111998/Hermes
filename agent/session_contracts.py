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
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not self.turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if self.user_event.get("role") != "user":
            raise ValueError("user_event must have role='user'")


@dataclass(frozen=True)
class CanonicalSessionEvent:
    """One ordered, durable event in the Hermes session journal projection."""

    event_id: str
    sequence: int
    message: Message

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")


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
