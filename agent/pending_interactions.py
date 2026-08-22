"""Process-bound pending interaction service for trusted backend plugins.

The service is deliberately transport-neutral: native approval and clarify
registries remain authoritative, while plugins receive immutable identities and
may request an exact, policy-checked resolution without owning a UI transport.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

PUBLIC_CONTRACT_VERSION = 1
PROCESS_INSTANCE_ID = uuid.uuid4().hex
_TERMINAL_STATUSES = frozenset(
    {"resolved", "denied", "expired", "cancelled", "interrupted", "process_stopping"}
)
_INTERACTION_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pending_interaction_source", default=""
)


@contextmanager
def pending_interaction_source(kind: str):
    """Mark an internal fork so observer metadata cannot mimic a primary turn."""
    token = _INTERACTION_SOURCE.set(str(kind or "internal")[:64])
    try:
        yield
    finally:
        _INTERACTION_SOURCE.reset(token)


@dataclass(frozen=True)
class PendingInteractionTarget:
    """Exact process-local identity of one pending interaction."""

    contract_version: int
    process_instance_id: str
    profile_id: str
    runtime_session_id: str
    request_id: str
    interaction_type: str
    question_id: str | None = None
    registration_id: str = ""


@dataclass(frozen=True)
class PendingInteractionEvent:
    """Immutable lifecycle snapshot delivered to subscribers."""

    contract_version: int
    event: str
    target: PendingInteractionTarget
    occurred_at: float
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    status: str | None = None
    resolved_by: str | None = None


@dataclass(frozen=True)
class PendingInteractionResponse:
    """A resolver's explicit response to a pending interaction."""

    kind: str
    value: Any = None
    resolved_by: str = "plugin"


@dataclass(frozen=True)
class PendingInteractionResolveResult:
    """Stable result of an exact resolution attempt."""

    status: str


@dataclass
class _Record:
    target: PendingInteractionTarget
    resolver: Callable[[PendingInteractionResponse], bool]
    validator: Callable[[PendingInteractionResponse], bool]
    metadata: Mapping[str, Any]


def _active_profile_id() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return str(get_active_profile_name() or "default")
    except Exception:
        return str(os.environ.get("HERMES_PROFILE") or "default")


def current_interaction_metadata(*, surface: str = "") -> dict[str, Any]:
    """Return bounded, authoritative metadata for the current execution context."""
    try:
        from gateway.session_context import get_session_env

        platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "")
        session_id = str(get_session_env("HERMES_SESSION_ID", "") or "")
        source = str(get_session_env("HERMES_SESSION_SOURCE", "") or "")
    except Exception:
        platform = session_id = source = ""
    fork_kind = _INTERACTION_SOURCE.get()
    try:
        from agent.delegation_context import is_delegated_child_context

        delegated = bool(is_delegated_child_context())
    except Exception:
        delegated = False
    if not fork_kind and delegated:
        fork_kind = "subagent"
    internal = bool(fork_kind)
    return {
        "surface": str(surface or platform or "unknown")[:64],
        "platform": platform[:64],
        "session_id": session_id[:256],
        "source": source[:64],
        "primary_user_turn": not internal,
        "internal": internal,
        "fork_kind": fork_kind,
    }


class PendingInteractionService:
    """Versioned, profile-scoped observer and exact resolver service."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[PendingInteractionTarget, _Record] = {}
        self._terminal_targets: set[PendingInteractionTarget] = set()
        self._terminal_order: deque[PendingInteractionTarget] = deque()
        self._subscribers: dict[str, Callable[[PendingInteractionEvent], None]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="hermes-pending-interaction"
        )
        self._dispatch_slots = threading.BoundedSemaphore(128)
        self._stopping = False

    def subscribe(
        self, callback: Callable[[PendingInteractionEvent], None]
    ) -> Callable[[], None]:
        """Subscribe without attaching to, replacing, or retaining any transport."""
        if not callable(callback):
            raise TypeError("pending interaction subscriber must be callable")
        subscription_id = uuid.uuid4().hex
        with self._lock:
            if self._stopping:
                raise RuntimeError("pending interaction service is stopping")
            self._subscribers[subscription_id] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(subscription_id, None)

        return unsubscribe

    def register(
        self,
        *,
        runtime_session_id: str,
        request_id: str,
        interaction_type: str,
        resolver: Callable[[PendingInteractionResponse], bool],
        validator: Callable[[PendingInteractionResponse], bool],
        question_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PendingInteractionTarget:
        """Register only after the native request identity is authoritative."""
        target = PendingInteractionTarget(
            PUBLIC_CONTRACT_VERSION,
            PROCESS_INSTANCE_ID,
            _active_profile_id(),
            str(runtime_session_id),
            str(request_id),
            str(interaction_type),
            str(question_id) if question_id is not None else None,
            uuid.uuid4().hex,
        )
        frozen_metadata = MappingProxyType(dict(metadata or {}))
        with self._lock:
            if self._stopping:
                raise RuntimeError("pending interaction service is stopping")
            if target in self._records:
                raise ValueError("pending interaction target is already registered")
            self._records[target] = _Record(target, resolver, validator, frozen_metadata)
        self._emit("pending_interaction.registered", target, frozen_metadata)
        return target

    def resolve(
        self,
        target: PendingInteractionTarget,
        response: PendingInteractionResponse,
    ) -> PendingInteractionResolveResult:
        """Atomically resolve one exact native request, never a heuristic fallback."""
        if not isinstance(target, PendingInteractionTarget):
            return PendingInteractionResolveResult("not_found")
        if target.contract_version != PUBLIC_CONTRACT_VERSION:
            return PendingInteractionResolveResult("unsupported_source")
        if target.process_instance_id != PROCESS_INSTANCE_ID:
            return PendingInteractionResolveResult("process_mismatch")
        if target.profile_id != _active_profile_id():
            return PendingInteractionResolveResult("policy_denied")
        if not isinstance(response, PendingInteractionResponse):
            return PendingInteractionResolveResult("invalid_response")
        with self._lock:
            record = self._records.get(target)
            if record is None:
                status = (
                    "already_resolved"
                    if target in self._terminal_targets
                    else "not_found"
                )
                return PendingInteractionResolveResult(status)
            if not record.validator(response):
                return PendingInteractionResolveResult("invalid_response")
            try:
                accepted = bool(record.resolver(response))
            except Exception:
                logger.exception("Pending interaction native resolver failed")
                return PendingInteractionResolveResult("unsupported_source")
            if not accepted:
                return PendingInteractionResolveResult("already_resolved")
            self._records.pop(target, None)
            self._remember_terminal_locked(target)
        status = "denied" if response.kind == "deny" else "resolved"
        self._emit_terminal(record, status, response.resolved_by)
        return PendingInteractionResolveResult("accepted")

    def terminal(
        self,
        target: PendingInteractionTarget,
        status: str,
        *,
        resolved_by: str | None = None,
    ) -> bool:
        """Close a natively terminated request without exposing answer content."""
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"unsupported pending interaction terminal status: {status}")
        with self._lock:
            record = self._records.pop(target, None)
            if record is not None:
                self._remember_terminal_locked(target)
        if record is None:
            return False
        self._emit_terminal(record, status, resolved_by)
        return True

    def _remember_terminal_locked(self, target: PendingInteractionTarget) -> None:
        self._terminal_targets.add(target)
        self._terminal_order.append(target)
        while len(self._terminal_order) > 4096:
            expired = self._terminal_order.popleft()
            self._terminal_targets.discard(expired)

    def _emit_terminal(
        self, record: _Record, status: str, resolved_by: str | None
    ) -> None:
        self._emit(
            "pending_interaction.terminal",
            record.target,
            record.metadata,
            status=status,
            resolved_by=(str(resolved_by)[:64] if resolved_by else None),
        )

    def _emit(
        self,
        event_name: str,
        target: PendingInteractionTarget,
        metadata: Mapping[str, Any],
        *,
        status: str | None = None,
        resolved_by: str | None = None,
    ) -> None:
        event = PendingInteractionEvent(
            PUBLIC_CONTRACT_VERSION,
            event_name,
            target,
            time.time(),
            metadata,
            status,
            resolved_by,
        )
        with self._lock:
            callbacks = tuple(self._subscribers.values())
        for callback in callbacks:
            if not self._dispatch_slots.acquire(blocking=False):
                logger.warning("Dropping pending interaction event: subscriber queue full")
                continue
            try:
                self._executor.submit(self._deliver_with_slot, callback, event)
            except RuntimeError:
                self._dispatch_slots.release()
                break

    def _deliver_with_slot(
        self,
        callback: Callable[[PendingInteractionEvent], None],
        event: PendingInteractionEvent,
    ) -> None:
        try:
            self._deliver(callback, event)
        finally:
            self._dispatch_slots.release()

    @staticmethod
    def _deliver(
        callback: Callable[[PendingInteractionEvent], None],
        event: PendingInteractionEvent,
    ) -> None:
        try:
            callback(event)
        except Exception:
            logger.exception("Pending interaction subscriber failed")

    def shutdown(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            records = tuple(self._records.values())
            self._records.clear()
        for record in records:
            self._emit_terminal(record, "process_stopping", None)
        with self._lock:
            self._subscribers.clear()
        self._executor.shutdown(wait=False, cancel_futures=False)


class PluginPendingInteractionService:
    """Plugin-owned view that removes subscriptions during plugin unload."""

    def __init__(
        self,
        service: PendingInteractionService,
        track_unsubscribe: Callable[[Callable[[], None]], None],
    ) -> None:
        self._service = service
        self._track_unsubscribe = track_unsubscribe

    def subscribe(
        self, callback: Callable[[PendingInteractionEvent], None]
    ) -> Callable[[], None]:
        unsubscribe = self._service.subscribe(callback)
        self._track_unsubscribe(unsubscribe)
        return unsubscribe

    def resolve(
        self,
        target: PendingInteractionTarget,
        response: PendingInteractionResponse,
    ) -> PendingInteractionResolveResult:
        return self._service.resolve(target, response)


_SERVICE = PendingInteractionService()
atexit.register(_SERVICE.shutdown)


def get_pending_interaction_service() -> PendingInteractionService:
    return _SERVICE
