"""Deterministic progress-aware lifecycle primitives for delegated workers."""
from __future__ import annotations

import enum
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional


_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _safe_label(value: Any, fallback: str = "operation") -> str:
    text = str(value or "").strip()
    return text if _SAFE_LABEL.fullmatch(text) else fallback


def _activity_class(summary: Any) -> str:
    if not isinstance(summary, dict):
        return "unknown"
    desc = str(summary.get("last_activity_desc", "") or "").lower()
    if any(word in desc for word in ("waiting for", "starting api", "requesting", "starting", "executing")):
        return "waiting"
    if any(word in desc for word in ("receiving", "stream", "chunk")):
        return "stream"
    if any(word in desc for word in ("completed", "result", "posted", "finished")):
        return "completed"
    return "activity"


def activity_signature(summary: Any) -> tuple[Any, Any, str, bool]:
    """Return a secret-free observation used to compare adjacent samples."""
    if not isinstance(summary, dict):
        return (0, None, "unknown", False)
    try:
        api_calls = int(summary.get("api_call_count", 0) or 0)
    except (TypeError, ValueError):
        api_calls = 0
    return (
        api_calls,
        summary.get("last_activity_ts"),
        _activity_class(summary),
        bool(summary.get("current_tool")),
    )


def meaningful_activity_token(summary: Any) -> tuple[Any, Any]:
    """Return a canonical marker for the latest substantive progress."""
    signature = activity_signature(summary)
    api_calls, activity_ts, category, has_tool = signature
    if category == "waiting" or (has_tool and category not in {"stream", "completed"}):
        # API-call counters advance at request start in the agent loop. They
        # are therefore not progress while the provider/tool is still waiting.
        return 0, None
    return api_calls, activity_ts


def meaningful_activity_advanced(summary: Any, previous: Any) -> bool:
    """Whether *summary* crossed a meaningful progress boundary.

    ``previous`` is normally an ``activity_signature``. Provider wait/start
    heartbeats are intentionally never progress, even when their timestamp
    changes or an API call has merely started.
    """
    current = activity_signature(summary)
    before = previous if isinstance(previous, tuple) and len(previous) == 4 else activity_signature(previous)
    if current[2] == "waiting":
        return False
    # API-call counts can advance at request start, so only count them once
    # the observation is explicitly a completion/result boundary.
    if current[2] in {"completed", "stream"} and current[0] > int(before[0] or 0):
        return True
    if current[2] in {"stream", "completed"} and current[1] is not None and current[1] != before[1]:
        return True
    return current[2] in {"stream", "completed"} and current[2] != before[2]


class WorkerState(str, enum.Enum):
    RUNNING = "running"
    PROGRESSING = "progressing"
    WAITING_ON_MODEL = "waiting_on_model"
    WAITING_ON_TOOL = "waiting_on_tool"
    STALLED = "stalled"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLATION_PENDING = "cancellation_pending"
    CANCELLATION_CONFIRMED = "cancellation_confirmed"
    SUCCESS = "success"
    FAILED = "failed"
    LATE_SUCCESS = "late_success"
    SUPERSEDED = "superseded"


_TERMINAL = frozenset({
    WorkerState.CANCELLATION_CONFIRMED,
    WorkerState.SUCCESS,
    WorkerState.FAILED,
    WorkerState.LATE_SUCCESS,
    WorkerState.SUPERSEDED,
})


@dataclass
class WorkerProgress:
    child_started_at: float
    last_progress_at: float
    last_progress_kind: str = "started"
    current_operation: Optional[str] = None
    current_operation_started_at: Optional[float] = None
    worker_state: WorkerState = WorkerState.RUNNING
    cancellation_state: str = "not_requested"
    logical_task_id: str = ""
    execution_generation: int = 1
    attempt_number: int = 1
    mutation_possible: bool = False
    mutation_reconciled: bool = False
    progress_count: int = 0
    stall_detected: bool = False
    cancellation_requested_at: Optional[float] = None
    cancellation_confirmed_at: Optional[float] = None
    fenced_at: Optional[float] = None
    late_result: bool = False
    superseded_result: bool = False
    last_token: Any = None


class WorkerLifecycle:
    """Thread-safe state machine for one logical worker attempt.

    Metadata is intentionally limited to timestamps, bounded labels, counters,
    and generation/fencing facts. Prompts, credentials, tool arguments, and
    provider content never enter this object.
    """

    def __init__(self, *, logical_task_id: str, execution_generation: int = 1,
                 attempt_number: int = 1, now: Optional[float] = None) -> None:
        stamp = time.time() if now is None else float(now)
        self._lock = threading.RLock()
        self._p = WorkerProgress(
            child_started_at=stamp,
            last_progress_at=stamp,
            logical_task_id=str(logical_task_id),
            execution_generation=int(execution_generation),
            attempt_number=int(attempt_number),
        )

    def snapshot(self, *, now: Optional[float] = None) -> dict[str, Any]:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            p = self._p
            return {
                "child_started_at": p.child_started_at,
                "last_progress_at": p.last_progress_at,
                "last_progress_kind": p.last_progress_kind,
                "current_operation": p.current_operation,
                "current_operation_started_at": p.current_operation_started_at,
                "worker_state": p.worker_state.value,
                "cancellation_state": p.cancellation_state,
                "logical_task_id": p.logical_task_id,
                "execution_generation": p.execution_generation,
                "attempt_number": p.attempt_number,
                "mutation_possible": p.mutation_possible,
                "mutation_reconciled": p.mutation_reconciled,
                "progress_count": p.progress_count,
                "stall_detected": p.stall_detected,
                "cancellation_requested_at": p.cancellation_requested_at,
                "cancellation_confirmed_at": p.cancellation_confirmed_at,
                "fenced_at": p.fenced_at,
                "late_result": p.late_result,
                "superseded_result": p.superseded_result,
                "seconds_since_progress": max(0.0, stamp - p.last_progress_at),
            }

    def transition(self, state: WorkerState, *, now: Optional[float] = None) -> bool:
        state = WorkerState(state)
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._p.worker_state in _TERMINAL:
                return False
            self._p.worker_state = state
            if state == WorkerState.STALLED:
                self._p.stall_detected = True
            elif state == WorkerState.CANCELLATION_REQUESTED:
                self._p.cancellation_state = "requested"
                self._p.cancellation_requested_at = stamp
            elif state == WorkerState.CANCELLATION_PENDING:
                self._p.cancellation_state = "pending"
            elif state == WorkerState.CANCELLATION_CONFIRMED:
                self._p.cancellation_state = "confirmed"
                self._p.cancellation_confirmed_at = stamp
            return True

    def record_progress(self, kind: str, *, operation: Optional[str] = None,
                        now: Optional[float] = None) -> bool:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._p.worker_state in _TERMINAL:
                return False
            self._p.last_progress_at = stamp
            self._p.last_progress_kind = _safe_label(kind, "progress")
            self._p.progress_count += 1
            if operation is not None:
                safe_operation = _safe_label(operation)
                if safe_operation != self._p.current_operation:
                    self._p.current_operation = safe_operation
                    self._p.current_operation_started_at = stamp
            if self._p.worker_state in {
                WorkerState.RUNNING, WorkerState.WAITING_ON_MODEL,
                WorkerState.WAITING_ON_TOOL, WorkerState.STALLED,
            }:
                self._p.worker_state = WorkerState.PROGRESSING
            return True

    def observe_tool_call(self, name: str, *, mutating: bool = False,
                          completed: bool = False,
                          now: Optional[float] = None) -> bool:
        """Record a sanitized tool lifecycle event.

        Tool start only changes the wait state. Completion is the meaningful
        progress boundary; a potentially side-effecting tool is fenced before
        execution so uncertain cancellation fails closed.
        """
        if mutating:
            self.mark_mutation_possible()
        if completed:
            return self.record_progress("tool_completed", operation=name, now=now)
        self.mark_tool_wait(name, now=now)
        return False

    def observe_activity(self, summary: dict[str, Any], *, now: Optional[float] = None) -> bool:
        """Map sanitized agent activity to progress.

        A tool becoming active is recorded as a wait-state transition, not as
        indefinite progress. API-call counters, activity timestamps, and
        completion/result descriptions are substantive forward-progress signals.
        """
        if not isinstance(summary, dict):
            return False
        stamp = time.time() if now is None else float(now)
        api_calls = int(summary.get("api_call_count", 0) or 0)
        activity_ts = summary.get("last_activity_ts")
        tool = summary.get("current_tool")
        desc = str(summary.get("last_activity_desc", "") or "")
        with self._lock:
            old = self._p.last_token
            signature = activity_signature(summary)
            token = meaningful_activity_token(summary)
            meaningful = old is None and token != (0, None) and signature[2] != "waiting"
            if old is not None:
                meaningful = meaningful_activity_advanced(summary, old)
            self._p.last_token = signature
            if meaningful:
                self._p.last_progress_at = stamp
                self._p.last_progress_kind = "api_call_completed" if api_calls > 0 else "activity"
                self._p.progress_count += 1
                self._p.current_operation = _safe_label(tool) if tool else None
                self._p.current_operation_started_at = stamp if tool else None
                self._p.worker_state = WorkerState.WAITING_ON_TOOL if tool else WorkerState.WAITING_ON_MODEL
            elif tool and self._p.worker_state == WorkerState.RUNNING:
                self._p.worker_state = WorkerState.WAITING_ON_TOOL
            return meaningful

    def mark_model_wait(self, operation: str = "provider_request", *, now: Optional[float] = None) -> None:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._p.worker_state not in _TERMINAL:
                self._p.worker_state = WorkerState.WAITING_ON_MODEL
                self._p.current_operation = _safe_label(operation)
                self._p.current_operation_started_at = stamp

    def mark_tool_wait(self, operation: str, *, now: Optional[float] = None) -> None:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._p.worker_state not in _TERMINAL:
                self._p.worker_state = WorkerState.WAITING_ON_TOOL
                self._p.current_operation = _safe_label(operation)
                self._p.current_operation_started_at = stamp

    def mark_waiting_on_provider(self, operation: str = "provider_request", *, now: Optional[float] = None) -> None:
        self.mark_model_wait(operation, now=now)

    def mark_waiting_on_tool(self, operation: str, *, now: Optional[float] = None) -> None:
        self.mark_tool_wait(operation, now=now)

    def mark_tool_completed(self, operation: str, *, now: Optional[float] = None) -> bool:
        return self.record_progress("tool_completed", operation=operation, now=now)

    def record_api_response(self, *, streamed: bool = False, completed: bool = True,
                            now: Optional[float] = None) -> bool:
        kind = "model_chunk" if streamed and not completed else "api_call_completed"
        return self.record_progress(kind, operation="provider_request", now=now)

    def record_result_fragment(self, *, now: Optional[float] = None) -> bool:
        return self.record_progress("result_fragment", operation="result", now=now)

    def reconcile_mutation(self) -> None:
        with self._lock:
            self._p.mutation_reconciled = True

    def fence(self, *, now: Optional[float] = None) -> bool:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._p.worker_state in _TERMINAL:
                return False
            self._p.fenced_at = stamp
            self._p.worker_state = WorkerState.CANCELLATION_PENDING
            self._p.cancellation_state = "fenced"
            return True

    def request_cancellation(self, *, now: Optional[float] = None) -> bool:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._p.worker_state in _TERMINAL:
                return False
            self._p.cancellation_requested_at = stamp
            self._p.cancellation_state = "requested"
            self._p.worker_state = WorkerState.CANCELLATION_REQUESTED
            return True

    def mark_cancellation_pending(self) -> bool:
        with self._lock:
            if self._p.worker_state in _TERMINAL:
                return False
            self._p.cancellation_state = "pending"
            self._p.worker_state = WorkerState.CANCELLATION_PENDING
            return True

    def confirm_termination(self, *, now: Optional[float] = None) -> bool:
        """Record a returned worker as terminated, idempotently.

        This method is only a termination observation; it never claims that an
        interrupt stopped an upstream request. Repeated finalizers must not
        rewrite an already accepted result state.
        """
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._p.cancellation_confirmed_at is not None:
                return False
            if self._p.worker_state not in {
                WorkerState.CANCELLATION_REQUESTED,
                WorkerState.CANCELLATION_PENDING,
                WorkerState.STALLED,
                WorkerState.LATE_SUCCESS,
                WorkerState.SUPERSEDED,
            } and self._p.cancellation_requested_at is None:
                # A normal success/failure is a termination observation, not
                # a cancellation confirmation.
                return False
            self._p.cancellation_state = "confirmed"
            self._p.cancellation_confirmed_at = stamp
            if self._p.worker_state in {
                WorkerState.CANCELLATION_REQUESTED,
                WorkerState.CANCELLATION_PENDING,
                WorkerState.STALLED,
            }:
                self._p.worker_state = WorkerState.CANCELLATION_CONFIRMED
            return True

    def mark_mutation_possible(self) -> None:
        with self._lock:
            self._p.mutation_possible = True

    def note_tool_event(self, kind: str, tool: Optional[str] = None, *, completed: bool = False,
                        mutating: bool = False, now: Optional[float] = None) -> bool:
        """Record a sanitized tool boundary from the host tool executor.

        Tool start is only an operation/wait transition. Completion is the
        meaningful boundary. ``mutating`` is supplied by the host policy and
        never inferred from user-controlled arguments here.
        """
        return self.observe_tool_call(
            tool or kind,
            mutating=mutating,
            completed=completed,
            now=now,
        )

    def fence_execution(self, *, now: Optional[float] = None) -> bool:
        """Fence future acceptance after cancellation is not proven safe."""
        return self.fence(now=now)

    def termination_confirmed(self) -> bool:
        with self._lock:
            return self._p.cancellation_confirmed_at is not None

    def retry_decision(self, *, replacement_generation: int) -> str:
        """Return a fail-closed retry decision for orchestration callers."""
        with self._lock:
            if self._p.mutation_possible and not self._p.mutation_reconciled:
                return "reconcile_mutation"
            if self._p.cancellation_confirmed_at is None:
                return "cancellation_pending"
            if replacement_generation <= self._p.execution_generation:
                return "generation_conflict"
            return "allow"

    def set_attempt(self, *, generation: int, attempt_number: int) -> bool:
        with self._lock:
            if generation <= self._p.execution_generation:
                return False
            if self._p.worker_state not in _TERMINAL:
                return False
            self._p.execution_generation = int(generation)
            self._p.attempt_number = int(attempt_number)
            self._p.worker_state = WorkerState.RUNNING
            self._p.cancellation_state = "not_requested"
            self._p.cancellation_confirmed_at = None
            self._p.fenced_at = None
            self._p.late_result = False
            self._p.superseded_result = False
            self._p.last_progress_at = time.time()
            self._p.last_progress_kind = "started"
            self._p.last_token = None
            return True

    def accept_result(self, *, generation: int,
                      authoritative_generation: Optional[int] = None,
                      success: bool = True,
                      mutation_reconciled: bool = False) -> WorkerState:
        with self._lock:
            if self._p.worker_state in {
                WorkerState.SUCCESS, WorkerState.FAILED,
                WorkerState.LATE_SUCCESS, WorkerState.SUPERSEDED,
            }:
                return self._p.worker_state
            if ((authoritative_generation is not None and generation < authoritative_generation)
                    or generation != self._p.execution_generation):
                self._p.superseded_result = True
                self._p.worker_state = WorkerState.SUPERSEDED
                return WorkerState.SUPERSEDED
            if self._p.stall_detected or self._p.fenced_at is not None:
                if self._p.mutation_possible and not (
                    self._p.mutation_reconciled or mutation_reconciled
                ):
                    self._p.superseded_result = True
                    self._p.worker_state = WorkerState.SUPERSEDED
                    return WorkerState.SUPERSEDED
                self._p.late_result = True
                self._p.worker_state = WorkerState.LATE_SUCCESS if success else WorkerState.FAILED
                return self._p.worker_state
            self._p.worker_state = WorkerState.SUCCESS if success else WorkerState.FAILED
            return self._p.worker_state

    def can_retry(self, *, termination_confirmed: bool,
                  replacement_generation: Optional[int] = None) -> bool:
        with self._lock:
            if self._p.mutation_possible and not self._p.mutation_reconciled:
                return False
            if not termination_confirmed:
                return False
            if self._p.worker_state not in {
                WorkerState.CANCELLATION_CONFIRMED,
                WorkerState.FAILED,
                WorkerState.STALLED,
                WorkerState.CANCELLATION_PENDING,
            }:
                return False
            return replacement_generation is None or replacement_generation > self._p.execution_generation

    def annotate(self, target: dict[str, Any]) -> dict[str, Any]:
        target.update(self.snapshot())
        return target


class LifecycleWatchdog:
    """Cheap local decision helper; no polling request or LLM is performed."""

    def __init__(self, lifecycle: WorkerLifecycle, *, inactivity_threshold: float,
                 clock=time.time) -> None:
        self.lifecycle = lifecycle
        self.inactivity_threshold = max(0.01, float(inactivity_threshold))
        self.clock = clock

    def should_stall(self) -> bool:
        snap = self.lifecycle.snapshot(now=self.clock())
        return (snap["worker_state"] not in {state.value for state in _TERMINAL}
                and snap["seconds_since_progress"] >= self.inactivity_threshold)


WorkerLifecycleState = WorkerState
ProgressWatchdog = WorkerLifecycle
WORKER_STATES = tuple(state.value for state in WorkerState)
WORKER_STATE_SET = frozenset(WORKER_STATES)
SECRET_SAFE_METADATA = True
LIFECYCLE_SCHEMA_VERSION = 1


def is_terminal(state: str | WorkerState) -> bool:
    try:
        return WorkerState(state) in _TERMINAL
    except (ValueError, TypeError):
        return False
