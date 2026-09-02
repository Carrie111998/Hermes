"""Public, plugin-safe lifecycle API for delegated Hermes subagents.

This module deliberately exposes immutable contracts, not ``AIAgent`` objects.
It is the supported boundary for plugins that need to supervise fresh child
sessions; plugins must obtain it from ``PluginContext.subagent_lifecycle``.
"""

from __future__ import annotations

import contextvars
import dataclasses
import enum
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from contextlib import contextmanager
from concurrent.futures import Future, TimeoutError
from typing import Any, Callable, Mapping, Optional

from agent.interrupt_compat import request_hard_interrupt

PUBLIC_CONTRACT_VERSION = 1
_MAX_GOAL_CHARS = 16_000
_MAX_CONTEXT_CHARS = 32_000
_MAX_METADATA_BYTES = 8_192
_MAX_RESULT_CHARS = 32_000
_TERMINAL_RETENTION_SECONDS = 3_600
_MAX_INTERRUPTION_EVIDENCE = 8


class SubagentLifecycleError(ValueError):
    """A request cannot be safely accepted by the public lifecycle API."""


class SubagentState(str, enum.Enum):
    PENDING = "PENDING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class SubagentInterruptionCause(str, enum.Enum):
    """Authoritative reason associated with an interruption observation."""

    USER_CANCEL = "user_cancel"
    USER = "user_cancel"  # intentional alias of USER_CANCEL; same wire value
    TIMEOUT = "timeout"
    RESET = "reset"
    LEASE_LOSS = "lease_loss"
    WORKER_INTERRUPT = "worker_interrupt"
    LATE_CALLBACK = "late_callback"
    UNKNOWN = "unknown"


class SubagentInterruptionStage(str, enum.Enum):
    """Progress of an interruption request through the worker boundary."""

    REQUESTED = "requested"
    SIGNAL_ACCEPTED = "signal_accepted"
    SIGNAL = "signal_accepted"  # intentional alias of SIGNAL_ACCEPTED; same wire value
    OBSERVED = "observed"
    ACKNOWLEDGED = "acknowledged"
    VERIFIED_TERMINATION = "verified_termination"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class SubagentInterruptionEvidence:
    """Immutable, bounded evidence for one interruption observation."""

    cause: SubagentInterruptionCause = SubagentInterruptionCause.UNKNOWN
    stage: SubagentInterruptionStage = SubagentInterruptionStage.UNKNOWN
    source: str = "unknown"
    occurred_at: float = dataclasses.field(default_factory=time.time)
    detail: Optional[str] = None
    generation: int = 0
    callback_id: Optional[str] = None

    @property
    def first_cause(self) -> SubagentInterruptionCause:
        """Compatibility-friendly access to the typed cause."""
        return self.cause


@dataclasses.dataclass(frozen=True)
class SubagentLaunchRequest:
    goal: str
    context: Optional[str] = None
    role: str = "leaf"
    model: Optional[str] = None
    allowed_toolsets: Optional[tuple[str, ...]] = None
    blocked_tools: tuple[str, ...] = ()
    working_directory: Optional[str] = None
    parent_session_id: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    timeout_seconds: Optional[float] = None


@dataclasses.dataclass(frozen=True)
class SubagentHandle:
    contract_version: int
    subagent_id: str
    parent_session_id: Optional[str]
    correlation_id: Optional[str]
    created_at: float
    provider: Optional[str]
    model: Optional[str]
    role: str
    depth: int
    capability: str
    # Added at the end so serialized and positional v1 handles remain readable.
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubagentHandle":
        try:
            return cls(**dict(value))
        except (TypeError, ValueError) as exc:
            raise SubagentLifecycleError("Malformed subagent handle.") from exc


@dataclasses.dataclass(frozen=True)
class SubagentStatus:
    handle: SubagentHandle
    state: SubagentState
    updated_at: float
    diagnostic: Optional[str] = None
    interruption: Optional[SubagentInterruptionEvidence] = None
    later_interruptions: tuple[SubagentInterruptionEvidence, ...] = ()
    dropped_interruptions: int = 0

    @property
    def generation(self) -> int:
        return self.handle.generation

    @property
    def first_cause(self) -> Optional[SubagentInterruptionCause]:
        return self.interruption.cause if self.interruption else None

    @property
    def later_causes(self) -> tuple[SubagentInterruptionCause, ...]:
        return tuple(evidence.cause for evidence in self.later_interruptions)


@dataclasses.dataclass(frozen=True)
class SubagentTerminalState:
    handle: SubagentHandle
    state: SubagentState
    completed: bool
    timed_out: bool = False
    diagnostic: Optional[str] = None
    interruption: Optional[SubagentInterruptionEvidence] = None
    later_interruptions: tuple[SubagentInterruptionEvidence, ...] = ()
    dropped_interruptions: int = 0

    @property
    def generation(self) -> int:
        return self.handle.generation

    @property
    def first_cause(self) -> Optional[SubagentInterruptionCause]:
        return self.interruption.cause if self.interruption else None

    @property
    def later_causes(self) -> tuple[SubagentInterruptionCause, ...]:
        return tuple(evidence.cause for evidence in self.later_interruptions)


@dataclasses.dataclass(frozen=True)
class SubagentCancelResult:
    accepted: bool
    already_terminal: bool = False
    unknown_handle: bool = False
    unsupported: bool = False
    state: SubagentState = SubagentState.UNKNOWN
    generation: int = 0
    interruption: Optional[SubagentInterruptionEvidence] = None


@dataclasses.dataclass(frozen=True)
class SubagentResult:
    handle: SubagentHandle
    terminal_state: SubagentState
    ready: bool
    summary: Optional[str] = None
    structured_payload: Optional[Mapping[str, Any]] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error_classification: Optional[str] = None
    error_message: Optional[str] = None
    usage_metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    tool_execution_summary: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    result_hash: Optional[str] = None
    interruption: Optional[SubagentInterruptionEvidence] = None
    later_interruptions: tuple[SubagentInterruptionEvidence, ...] = ()
    dropped_interruptions: int = 0

    @property
    def generation(self) -> int:
        return self.handle.generation

    @property
    def first_cause(self) -> Optional[SubagentInterruptionCause]:
        return self.interruption.cause if self.interruption else None

    @property
    def later_causes(self) -> tuple[SubagentInterruptionCause, ...]:
        return tuple(evidence.cause for evidence in self.later_interruptions)


@dataclasses.dataclass(frozen=True)
class SubagentReconnectResult:
    connected: bool
    state: SubagentState
    diagnostic: Optional[str] = None


@dataclasses.dataclass
class _Record:
    handle: SubagentHandle
    state: SubagentState
    updated_at: float
    agent: Any = None
    future: Optional[Future] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[SubagentResult] = None
    generation: int = 1
    interruption: Optional[SubagentInterruptionEvidence] = None
    later_interruptions: list[SubagentInterruptionEvidence] = dataclasses.field(
        default_factory=list
    )
    dropped_interruptions: int = 0
    # Replay fences are deliberately not evicted with presentation evidence.
    callback_digests: dict[str, str] = dataclasses.field(default_factory=dict)


class _Registry:
    """Thread-safe terminal-retention registry; never returns live records."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.records: dict[str, _Record] = {}
        self.correlations: dict[tuple[Optional[str], str], str] = {}


_REGISTRY = _Registry()
# Daemon worker pool: a wedged/abandoned child must never block interpreter
# exit at atexit-join time (same rationale as _run_single_child's timeout
# executor and the async-delegation registry pool).
from tools.daemon_pool import DaemonThreadPoolExecutor as _DaemonExecutor

_EXECUTOR = _DaemonExecutor(max_workers=8, thread_name_prefix="hermes-lifecycle")
_SECRET = secrets.token_bytes(32)
_ACTIVE_PARENT_AGENT: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "hermes_subagent_lifecycle_parent", default=None
)


@contextmanager
def bind_subagent_parent(parent_agent: Any):
    """Bind the host-owned parent for the current agent turn."""
    token = _ACTIVE_PARENT_AGENT.set(parent_agent)
    try:
        yield
    finally:
        _ACTIVE_PARENT_AGENT.reset(token)


def get_active_subagent_parent() -> Any:
    """Return the parent bound to this execution context, if any."""
    return _ACTIVE_PARENT_AGENT.get()


class SubagentLifecycleService:
    """Stable public service returned by :attr:`PluginContext.subagent_lifecycle`.

    Running children are in-process only.  Completed results remain available
    until process exit; ``reconnect`` accurately reports that a serialized
    handle cannot reconnect after a restart instead of launching work again.
    """

    def __init__(self, parent_agent_resolver: Callable[[], Any]) -> None:
        self._parent_agent_resolver = parent_agent_resolver

    def launch(self, request: SubagentLaunchRequest) -> SubagentHandle:
        parent = self._parent_agent_resolver()
        if parent is None:
            raise SubagentLifecycleError(
                "No active Hermes parent session is available."
            )
        self._validate_request(request, parent)
        parent_session_id = str(getattr(parent, "session_id", "") or "") or None
        if request.parent_session_id and request.parent_session_id != parent_session_id:
            raise SubagentLifecycleError(
                "parent_session_id does not match the active session."
            )
        correlation_key = (parent_session_id, request.correlation_id or "")
        with _REGISTRY.lock:
            self._cleanup_locked()
            if request.correlation_id and correlation_key in _REGISTRY.correlations:
                raise SubagentLifecycleError(
                    "Duplicate correlation_id for this parent session."
                )

        # Delegate construction remains internal so plugin code never imports
        # private delegation helpers or manipulates the active-child registry.
        from tools.delegate_tool import (
            _build_child_preserving_parent_tools,
            DEFAULT_MAX_ITERATIONS,
        )

        child = _build_child_preserving_parent_tools(
            task_index=0,
            goal=request.goal,
            context=request.context,
            toolsets=list(request.allowed_toolsets)
            if request.allowed_toolsets
            else None,
            model=request.model,
            max_iterations=DEFAULT_MAX_ITERATIONS,
            task_count=1,
            parent_agent=parent,
            role=request.role,
        )
        subagent_id = str(getattr(child, "_subagent_id", "") or "")
        if not subagent_id:
            raise SubagentLifecycleError("Hermes failed to assign a child identity.")
        created = time.time()
        handle = SubagentHandle(
            PUBLIC_CONTRACT_VERSION,
            subagent_id,
            parent_session_id,
            request.correlation_id,
            created,
            getattr(child, "provider", None),
            getattr(child, "model", None),
            getattr(child, "_delegate_role", request.role),
            int(getattr(child, "_delegate_depth", 1) or 1),
            self._capability(subagent_id, parent_session_id, created),
            1,
        )
        record = _Record(
            handle, SubagentState.PENDING, created, agent=child, generation=1
        )
        with _REGISTRY.lock:
            _REGISTRY.records[subagent_id] = record
            if request.correlation_id:
                _REGISTRY.correlations[correlation_key] = subagent_id
        record.future = _EXECUTOR.submit(self._run, record, request.goal, parent)
        return handle

    def status(self, handle: SubagentHandle) -> SubagentStatus:
        record = self._record(handle)
        if record is None:
            return SubagentStatus(
                handle, SubagentState.UNKNOWN, time.time(), "UNKNOWN_HANDLE"
            )
        with _REGISTRY.lock:
            return SubagentStatus(
                record.handle,
                record.state,
                record.updated_at,
                interruption=record.interruption,
                later_interruptions=tuple(record.later_interruptions),
                dropped_interruptions=record.dropped_interruptions,
            )

    def wait(
        self, handle: SubagentHandle, *, timeout_seconds: Optional[float] = None
    ) -> SubagentTerminalState:
        record = self._record(handle)
        if record is None:
            return SubagentTerminalState(
                handle, SubagentState.UNKNOWN, True, diagnostic="UNKNOWN_HANDLE"
            )
        future = record.future
        if future is not None:
            try:
                future.result(timeout=timeout_seconds)
            except TimeoutError:
                return SubagentTerminalState(record.handle, record.state, False, True)
            except Exception:
                pass
        with _REGISTRY.lock:
            return SubagentTerminalState(
                record.handle,
                record.state,
                record.result is not None,
                interruption=record.interruption,
                later_interruptions=tuple(record.later_interruptions),
                dropped_interruptions=record.dropped_interruptions,
            )

    def cancel(self, handle: SubagentHandle, *, reason: str) -> SubagentCancelResult:
        record = self._record(handle)
        if record is None:
            return SubagentCancelResult(False, unknown_handle=True)
        with _REGISTRY.lock:
            if record.result is not None:
                return SubagentCancelResult(
                    False, already_terminal=True, state=record.state
                )
            if not isinstance(reason, str):
                raise SubagentLifecycleError("reason must be a string.")
            agent = record.agent
            record.state = SubagentState.CANCEL_REQUESTED
            record.updated_at = time.time()
            first = self._record_interruption_locked(
                record,
                SubagentInterruptionCause.USER_CANCEL,
                SubagentInterruptionStage.REQUESTED,
                source="lifecycle.cancel",
                detail=reason,
            )
        if agent is None:
            return SubagentCancelResult(
                False,
                unsupported=True,
                state=SubagentState.CANCEL_REQUESTED,
                generation=record.generation,
                interruption=first,
            )
        try:
            accepted = request_hard_interrupt(
                agent,
                f"Lifecycle cancellation requested: {reason[:500]}",
                tool_reason="subagent cancellation requested",
            )
        except Exception:
            return SubagentCancelResult(
                False,
                unsupported=True,
                state=SubagentState.CANCEL_REQUESTED,
                generation=record.generation,
                interruption=first,
            )
        if not accepted:
            return SubagentCancelResult(
                False,
                unsupported=True,
                state=SubagentState.CANCEL_REQUESTED,
                generation=record.generation,
                interruption=first,
            )
        with _REGISTRY.lock:
            self._record_interruption_locked(
                record,
                SubagentInterruptionCause.USER_CANCEL,
                SubagentInterruptionStage.SIGNAL_ACCEPTED,
                source="lifecycle.cancel",
                detail=reason,
            )
        return SubagentCancelResult(
            True,
            state=SubagentState.CANCEL_REQUESTED,
            generation=record.generation,
            interruption=first,
        )

    def result(self, handle: SubagentHandle) -> SubagentResult:
        record = self._record(handle)
        if record is None:
            return SubagentResult(
                handle,
                SubagentState.UNKNOWN,
                False,
                error_classification="UNKNOWN_HANDLE",
            )
        with _REGISTRY.lock:
            if record.result is not None:
                return record.result
            return SubagentResult(
                record.handle,
                record.state,
                False,
                error_classification="NOT_READY",
                interruption=record.interruption,
                later_interruptions=tuple(record.later_interruptions),
                dropped_interruptions=record.dropped_interruptions,
            )

    def _finalize_record(
        self,
        record: _Record,
        result: SubagentResult,
        *,
        generation: Optional[int] = None,
        callback_id: Optional[str] = None,
    ) -> SubagentResult:
        """Commit one terminal callback, or return the already committed one.

        The registry lock is the linearization point.  A callback from an older
        generation, or a duplicate callback with a conflicting payload, can
        add bounded diagnostic evidence but can never replace the terminal
        snapshot.  Callback digests are retained for the lifetime of the
        bounded terminal record, independently of presentation evidence.
        """
        if not isinstance(result, SubagentResult):
            raise SubagentLifecycleError("result must be a SubagentResult.")
        if result.terminal_state not in {
            SubagentState.SUCCEEDED,
            SubagentState.FAILED,
            SubagentState.INTERRUPTED,
            SubagentState.CANCELLED,
            SubagentState.UNKNOWN,
        }:
            raise SubagentLifecycleError("Only terminal states can be finalized.")
        callback_generation = generation
        if callback_generation in (None, 0):
            callback_generation = record.generation
        if type(callback_generation) is not int or callback_generation < 0:
            raise SubagentLifecycleError("generation must be a non-negative integer.")
        if callback_id is not None and (
            not isinstance(callback_id, str) or not callback_id
        ):
            raise SubagentLifecycleError("callback_id must be a non-empty string.")

        payload = dataclasses.asdict(result)
        payload.pop("result_hash", None)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        with _REGISTRY.lock:
            if callback_id is not None:
                previous_digest = record.callback_digests.get(callback_id)
                if previous_digest is not None:
                    if previous_digest != digest:
                        self._record_interruption_locked(
                            record,
                            SubagentInterruptionCause.LATE_CALLBACK,
                            SubagentInterruptionStage.UNKNOWN,
                            source="lifecycle.finalize",
                            detail="conflicting duplicate callback payload",
                            generation=callback_generation,
                            callback_id=callback_id,
                            authoritative=False,
                        )
                    return record.result or result

            if callback_generation != record.generation:
                self._record_interruption_locked(
                    record,
                    SubagentInterruptionCause.LATE_CALLBACK,
                    SubagentInterruptionStage.UNKNOWN,
                    source="lifecycle.finalize",
                    detail="stale callback generation",
                    generation=callback_generation,
                    callback_id=callback_id,
                    authoritative=False,
                )
                if callback_id is not None:
                    record.callback_digests[callback_id] = digest
                return record.result or result

            if record.result is not None:
                if callback_id is not None:
                    if callback_id not in record.callback_digests:
                        self._record_interruption_locked(
                            record,
                            SubagentInterruptionCause.LATE_CALLBACK,
                            SubagentInterruptionStage.UNKNOWN,
                            source="lifecycle.finalize",
                            detail="callback after terminal finalization",
                            generation=callback_generation,
                            callback_id=callback_id,
                            authoritative=False,
                        )
                    record.callback_digests[callback_id] = digest
                return record.result

            committed = dataclasses.replace(
                result,
                handle=record.handle,
                result_hash=None,
                interruption=record.interruption,
                later_interruptions=tuple(record.later_interruptions),
                dropped_interruptions=record.dropped_interruptions,
            )
            committed_payload = dataclasses.asdict(committed)
            committed_payload.pop("result_hash", None)
            committed = dataclasses.replace(
                committed,
                result_hash=hashlib.sha256(
                    json.dumps(
                        committed_payload, sort_keys=True, default=str
                    ).encode("utf-8")
                ).hexdigest(),
            )
            if callback_id is not None:
                record.callback_digests[callback_id] = digest
            record.result = committed
            record.state = committed.terminal_state
            record.completed_at = committed.completed_at
            record.updated_at = committed.completed_at or time.time()
            record.agent = None
            return committed

    def _record_interruption_locked(
        self,
        record: _Record,
        cause: SubagentInterruptionCause,
        stage: SubagentInterruptionStage,
        *,
        source: str,
        detail: Optional[str] = None,
        generation: Optional[int] = None,
        callback_id: Optional[str] = None,
        authoritative: bool = True,
    ) -> SubagentInterruptionEvidence:
        """Record first-writer-wins cause and bounded subsequent evidence."""
        if not isinstance(cause, SubagentInterruptionCause):
            try:
                cause = SubagentInterruptionCause(cause)
            except (TypeError, ValueError) as exc:
                raise SubagentLifecycleError("Unknown interruption cause.") from exc
        if not isinstance(stage, SubagentInterruptionStage):
            try:
                stage = SubagentInterruptionStage(stage)
            except (TypeError, ValueError) as exc:
                raise SubagentLifecycleError("Unknown interruption stage.") from exc
        evidence = SubagentInterruptionEvidence(
            cause=cause,
            stage=stage,
            source=str(source)[:128],
            occurred_at=time.time(),
            detail=str(detail)[:_MAX_RESULT_CHARS] if detail is not None else None,
            generation=record.generation if generation in (None, 0) else generation,
            callback_id=callback_id,
        )
        if record.interruption is None and authoritative:
            record.interruption = evidence
        elif len(record.later_interruptions) < _MAX_INTERRUPTION_EVIDENCE:
            record.later_interruptions.append(evidence)
        else:
            record.dropped_interruptions += 1
        record.updated_at = evidence.occurred_at
        return record.interruption

    def reconnect(self, handle: SubagentHandle) -> SubagentReconnectResult:
        record = self._record(handle)
        if record is None:
            return SubagentReconnectResult(
                False, SubagentState.UNKNOWN, "RECONNECT_UNAVAILABLE"
            )
        with _REGISTRY.lock:
            return SubagentReconnectResult(True, record.state)

    def _record(self, handle: SubagentHandle) -> Optional[_Record]:
        if (
            not isinstance(handle, SubagentHandle)
            or type(handle.contract_version) is not int
            or handle.contract_version != PUBLIC_CONTRACT_VERSION
        ):
            return None
        if (
            not isinstance(handle.subagent_id, str)
            or not handle.subagent_id
            or (
                handle.parent_session_id is not None
                and not isinstance(handle.parent_session_id, str)
            )
            or (
                handle.correlation_id is not None
                and not isinstance(handle.correlation_id, str)
            )
            or isinstance(handle.created_at, bool)
            or not isinstance(handle.created_at, (int, float))
            or not math.isfinite(handle.created_at)
            or (handle.provider is not None and not isinstance(handle.provider, str))
            or (handle.model is not None and not isinstance(handle.model, str))
            or not isinstance(handle.role, str)
            or type(handle.depth) is not int
            or not isinstance(handle.capability, str)
            or type(handle.generation) is not int
            or handle.generation < 0
        ):
            return None
        if not hmac.compare_digest(
            handle.capability,
            self._capability(
                handle.subagent_id, handle.parent_session_id, handle.created_at
            ),
        ):
            return None
        parent = self._parent_agent_resolver()
        active_parent_id = str(getattr(parent, "session_id", "") or "") or None
        if active_parent_id != handle.parent_session_id:
            return None
        with _REGISTRY.lock:
            record = _REGISTRY.records.get(handle.subagent_id)
            if record is None:
                return None
            # generation == 0 is a pre-field wildcard for serialized handles.
            # It is not a freshness proof; authenticity is the capability HMAC
            # and the active-parent session check above.
            if handle.generation not in (0, record.generation):
                return None
            return record

    @staticmethod
    def _cleanup_locked() -> None:
        """Retain terminal snapshots for a bounded period, never live work."""
        cutoff = time.time() - _TERMINAL_RETENTION_SECONDS
        expired = [
            subagent_id
            for subagent_id, record in _REGISTRY.records.items()
            if record.result is not None
            and record.completed_at is not None
            and record.completed_at < cutoff
        ]
        for subagent_id in expired:
            record = _REGISTRY.records.pop(subagent_id)
            if record.handle.correlation_id:
                _REGISTRY.correlations.pop(
                    (record.handle.parent_session_id, record.handle.correlation_id),
                    None,
                )

    def _run(self, record: _Record, goal: str, parent: Any) -> None:
        with _REGISTRY.lock:
            if record.state is not SubagentState.CANCEL_REQUESTED:
                record.state = SubagentState.RUNNING
            record.started_at = time.time()
            record.updated_at = record.started_at
        try:
            from tools.delegate_tool import _run_child_lifecycle

            raw = _run_child_lifecycle(0, goal, record.agent, parent)
            status = (
                str(raw.get("status", "error")) if isinstance(raw, dict) else "error"
            )
            if status == "completed":
                state = SubagentState.SUCCEEDED
            elif status == "interrupted":
                with _REGISTRY.lock:
                    if record.interruption is None:
                        self._record_interruption_locked(
                            record,
                            SubagentInterruptionCause.WORKER_INTERRUPT,
                            SubagentInterruptionStage.OBSERVED,
                            source="worker.callback",
                            detail=(
                                raw.get("error")
                                if isinstance(raw, dict)
                                else None
                            ),
                        )
                    else:
                        self._record_interruption_locked(
                            record,
                            record.interruption.cause,
                            SubagentInterruptionStage.OBSERVED,
                            source="worker.callback",
                        )
                    state = (
                        SubagentState.CANCELLED
                        if record.interruption.cause
                        is SubagentInterruptionCause.USER_CANCEL
                        else SubagentState.INTERRUPTED
                    )
            else:
                state = SubagentState.FAILED
            summary = raw.get("summary") if isinstance(raw, dict) else None
            summary = str(summary)[:_MAX_RESULT_CHARS] if summary is not None else None
            error = raw.get("error") if isinstance(raw, dict) else None
            result = SubagentResult(
                record.handle,
                state,
                True,
                summary=summary,
                completed_at=time.time(),
                started_at=record.started_at,
                error_classification=None
                if state == SubagentState.SUCCEEDED
                else status.upper(),
                error_message=str(error)[:_MAX_RESULT_CHARS] if error else None,
                usage_metadata={"api_calls": raw.get("api_calls", 0)}
                if isinstance(raw, dict)
                else {},
                tool_execution_summary={
                    "duration_seconds": raw.get("duration_seconds", 0)
                }
                if isinstance(raw, dict)
                else {},
            )
        except Exception as exc:
            result = SubagentResult(
                record.handle,
                SubagentState.FAILED,
                True,
                started_at=record.started_at,
                completed_at=time.time(),
                error_classification=type(exc).__name__,
                error_message=str(exc)[:_MAX_RESULT_CHARS],
            )
        self._finalize_record(
            record,
            result,
            generation=record.generation,
            callback_id=f"run:{record.handle.subagent_id}",
        )

    @staticmethod
    def _capability(
        subagent_id: str, parent_session_id: Optional[str], created_at: float
    ) -> str:
        value = f"{subagent_id}|{parent_session_id or ''}|{created_at:.6f}".encode(
            "utf-8"
        )
        return hmac.new(_SECRET, value, hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_request(request: SubagentLaunchRequest, parent: Any) -> None:
        if (
            not isinstance(request, SubagentLaunchRequest)
            or not isinstance(request.goal, str)
            or not request.goal.strip()
            or len(request.goal) > _MAX_GOAL_CHARS
        ):
            raise SubagentLifecycleError(
                "goal must be a non-empty string of at most 16000 characters."
            )
        if request.context is not None and (
            not isinstance(request.context, str)
            or len(request.context) > _MAX_CONTEXT_CHARS
        ):
            raise SubagentLifecycleError(
                "context must be a string of at most 32000 characters."
            )
        if request.role not in {"leaf", "orchestrator"}:
            raise SubagentLifecycleError("role must be 'leaf' or 'orchestrator'.")
        if request.timeout_seconds is not None:
            raise SubagentLifecycleError(
                "Per-launch timeout is not supported; configure delegation timeout explicitly."
            )
        if request.working_directory is not None:
            raise SubagentLifecycleError(
                "working_directory is not supported because Hermes delegates use isolated task environments."
            )
        if request.blocked_tools:
            raise SubagentLifecycleError(
                "Per-tool blocking is not supported; use allowed_toolsets. Hermes always blocks unsafe child tools."
            )
        try:
            metadata_bytes = len(
                json.dumps(dict(request.metadata), sort_keys=True).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise SubagentLifecycleError("metadata must be JSON-serializable.") from exc
        if metadata_bytes > _MAX_METADATA_BYTES:
            raise SubagentLifecycleError("metadata exceeds 8192 bytes.")
        if request.allowed_toolsets:
            from toolsets import TOOLSETS

            unknown = set(request.allowed_toolsets) - set(TOOLSETS)
            if unknown:
                raise SubagentLifecycleError(
                    f"Unknown toolsets: {', '.join(sorted(unknown))}."
                )
            enabled = getattr(parent, "enabled_toolsets", None)
            if enabled is not None and not set(request.allowed_toolsets).issubset(
                set(enabled)
            ):
                raise SubagentLifecycleError(
                    "Requested toolsets would broaden parent permissions."
                )
