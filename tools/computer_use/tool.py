"""Entry point for the `computer_use` tool.

Universal (any-model) desktop control across macOS, Windows, and Linux via
cua-driver's background computer-use primitive. Replaces #4562's
Anthropic-native `computer_20251124` approach — the schema here is standard
OpenAI function-calling so every tool-capable model can drive it.

Linux is the most recent runtime (X11 + Wayland, via cua-driver-rs's
AT-SPI tree path); it is enabled here alongside macOS and Windows. When a
host's display server or accessibility stack isn't reachable, cua-driver's
`health_report` (surfaced by `hermes computer-use doctor`) reports the
exact blocked check rather than the toolset silently failing.

Return contract
---------------
For text-only results (wait, key, list_apps, focus_app, failures, etc.):
  JSON string.

For captures / actions with `capture_after=True`:
  A dict wrapped as the OpenAI-style multi-part tool-message content:

      {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "<human-readable summary + SOM index>"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,<b64>"}},
        ],
        "text_summary": "<text used for fallback string content>",
      }

  run_agent.py's tool-message builder inspects `_multimodal` and emits a
  list-shaped `content` for OpenAI-compatible providers. The Anthropic
  adapter splices the base64 image into a `tool_result` block (see
  `agent/anthropic_adapter.py`). Every provider that supports multi-part
  tool content gets the image; text-only providers see the summary only.
"""

from __future__ import annotations

import atexit
import base64
import concurrent.futures
import json
import logging
import os
import queue
import re
import struct
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approval & safety
# ---------------------------------------------------------------------------

_approval_callback = None


def set_approval_callback(cb) -> None:
    """Register a callback for computer_use approval prompts (used by CLI).

    Matches the terminal_tool._approval_callback pattern. The callback
    receives (action, args, summary) and returns one of:
      "approve_once" | "approve_session" | "always_approve" | "deny".
    """
    global _approval_callback
    _approval_callback = cb


# Actions that read, not mutate. Always allowed.
_SAFE_ACTIONS = frozenset({
    "capture", "wait", "list_apps", "list_windows", "cua_browser_state",
})

# Actions that mutate user-visible state. Go through approval.
_DESTRUCTIVE_ACTIONS = frozenset({
    "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "focus_app",
    "cua_browser_prepare", "cua_browser_navigate", "cua_browser_click",
    "cua_browser_type", "cua_browser_pointer", "cua_browser_dialog",
    "cua_browser_set_input_files", "cua_browser_download",
})

# Hard-blocked key combinations. Mirrored from #4562 — these are destructive
# regardless of approval level (e.g. logout kills the session Hermes runs in).
_BLOCKED_KEY_COMBOS = {
    frozenset({"cmd", "shift", "backspace"}),   # empty trash
    frozenset({"cmd", "option", "backspace"}),   # force delete
    frozenset({"cmd", "ctrl", "q"}),             # lock screen
    frozenset({"cmd", "shift", "q"}),            # log out
    frozenset({"cmd", "option", "shift", "q"}),  # force log out
    # Windows secure/session shortcuts. The Windows driver accepts Win-key
    # combos, and Alt is canonicalized to option below, so block the
    # destructive variants before any backend sees them.
    frozenset({"win", "l"}),
    frozenset({"ctrl", "option", "delete"}),
    frozenset({"ctrl", "option", "del"}),
    frozenset({"option", "f4"}),
}

_KEY_ALIASES = {
    "command": "cmd", "control": "ctrl", "alt": "option", "⌘": "cmd", "⌥": "option",
    "windows": "win", "super": "win", "meta": "win",
}


def _canon_key_combo(keys: str) -> frozenset:
    # Split on both "+" and "-": the cua-driver backend's _parse_key_combo
    # accepts hyphen-separated combos too, so "ctrl-alt-delete" executes as
    # the real destructive shortcut. Mirror its separators here, otherwise the
    # _BLOCKED_KEY_COMBOS gate is trivially bypassed with hyphen notation.
    parts = [p.strip().lower() for p in re.split(r"\s*[+\-]\s*", keys) if p.strip()]
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    return frozenset(parts)


# Dangerous text patterns for the `type` action. Same list as #4562.
_BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\s+-[rf]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}", re.IGNORECASE),  # fork bomb
]


def _is_blocked_type(text: str) -> Optional[str]:
    for pat in _BLOCKED_TYPE_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# ---------------------------------------------------------------------------
# Backend selection — env-swappable for tests
# ---------------------------------------------------------------------------

# Per-Hermes-session cached backends. Each backend owns its own cua-driver
# session, native target, typed-browser binding, refs, and grant namespace.
_backend_lock = threading.Lock()
# Backward-compatible empty-session injection hook used by older tests.
# Process-scoped aux-vision routing cache: (provider, model) → bool.
_AUX_VISION_ROUTE_CACHE: Dict[Tuple[str, str], bool] = {}
_backend: Optional[ComputerUseBackend] = None
_backends: Dict[str, ComputerUseBackend] = {}
_backend_call_locks: Dict[str, threading.RLock] = {}
_backend_permission_modes: Dict[str, str] = {}
# Approval state, scoped per conversation/run (keyed by session_id) so a
# gateway serving concurrent sessions can't leak one run's "always approve"
# unlock into another. Falls back to a shared "" bucket for callers that
# don't pass a session_id (e.g. the classic single-run CLI). Values:
#   _session_auto_approve[sid] -> bool   ("always_approve everything")
#   _always_allow[sid]         -> set of (action, delivery_mode) scope keys
# See NousResearch/hermes-agent#67052 gap 4.
_approval_lock = threading.Lock()
_session_auto_approve: Dict[str, bool] = {}
_always_allow: Dict[str, set] = {}


class BackendLifecycleState(str, Enum):
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass
class ComputerUseReleaseResult:
    session_id: str
    generation: Optional[int]
    status: str
    reason: str
    elapsed_ms: float = 0.0
    error: Optional[str] = None

    @property
    def released(self) -> bool:
        return self.status == "released"

    def __bool__(self) -> bool:
        return self.released


@dataclass(frozen=True)
class ComputerUseTerminalTransition:
    session_id: str
    generation: Optional[int]
    invalidated_publications: tuple["ComputerUseSessionPublication", ...] = ()


@dataclass(frozen=True)
class ComputerUseSessionReactivation:
    session_id: str


@dataclass(eq=False)
class ComputerUseSessionPublication:
    session_id: str
    route_key: Optional[str] = None
    route_epoch: int = 0
    active: bool = True


@dataclass
class _BackendRecord:
    session_id: str
    generation: int
    permission_mode: str
    backend: ComputerUseBackend
    call_lock: threading.RLock
    state: BackendLifecycleState = BackendLifecycleState.STARTING
    close_requested_at: Optional[float] = None
    close_reason: Optional[str] = None
    last_error: Optional[str] = None
    close_attempts: int = 0


_backend_records: Dict[str, _BackendRecord] = {}
_terminal_transitions: Dict[str, ComputerUseTerminalTransition] = {}
_session_reactivations: Dict[str, ComputerUseSessionReactivation] = {}
_managed_session_publications: Dict[
    str, set[ComputerUseSessionPublication]
] = {}
_managed_routing_enabled = False
_managed_session_validator = None
_session_route_epoch_sequence = 0
_backend_lookup_state = threading.local()
_backend_generation_counters: OrderedDict[str, int] = OrderedDict()
_backend_generation_sequence = 0
_BACKEND_GENERATION_TOMBSTONE_CAP = 4096
_DEFAULT_RELEASE_TIMEOUT = 5.0
_EXPECTED_GENERATION_UNSET = object()


def _next_backend_generation_locked(sid: str) -> int:
    global _backend_generation_sequence
    _backend_generation_sequence += 1
    generation = _backend_generation_sequence
    _backend_generation_counters[sid] = generation
    _backend_generation_counters.move_to_end(sid)
    while len(_backend_generation_counters) > _BACKEND_GENERATION_TOMBSTONE_CAP:
        for oldest_sid in tuple(_backend_generation_counters):
            if oldest_sid != sid and oldest_sid not in _backend_records:
                _backend_generation_counters.pop(oldest_sid, None)
                break
        else:
            # More active sessions than the tombstone cap: keep their exact
            # generations rather than corrupting ownership or looping forever.
            break
    return generation


def _stamp_backend_lookup_locked(
    sid: str,
    backend: ComputerUseBackend,
    *,
    route_allowed: bool,
) -> None:
    _backend_lookup_state.value = (
        sid,
        id(backend),
        _session_route_epoch_sequence,
        route_allowed,
    )


def _record_from_legacy_cache_locked(sid: str) -> Optional[_BackendRecord]:
    """Normalize direct test/integration cache injection into lifecycle state."""
    record = _backend_records.get(sid)
    if record is not None:
        return record
    backend = _backends.get(sid)
    if backend is None and sid == "":
        backend = _backend
    if backend is None:
        return None
    call_lock = _backend_call_locks.setdefault(sid, threading.RLock())
    record = _BackendRecord(
        session_id=sid,
        generation=_next_backend_generation_locked(sid),
        permission_mode=_backend_permission_modes.get(sid, "standard"),
        backend=backend,
        call_lock=call_lock,
        state=BackendLifecycleState.ACTIVE,
    )
    _backend_records[sid] = record
    return record


def _has_active_publication_locked(sid: str) -> bool:
    return any(
        publication.active
        for publication in _managed_session_publications.get(sid, ())
    )


def set_computer_use_session_validator(validator) -> None:
    """Install the gateway's authoritative current-route validator."""
    global _managed_routing_enabled, _managed_session_validator
    global _session_route_epoch_sequence
    with _backend_lock:
        _managed_session_validator = validator
        _managed_routing_enabled = validator is not None
        _session_route_epoch_sequence += 1


def _validate_managed_publication(sid: str) -> tuple[bool, int]:
    """Validate admission without lock inversion across route epoch changes."""
    while True:
        with _backend_lock:
            epoch = _session_route_epoch_sequence
            if not sid or not _managed_routing_enabled:
                return True, epoch
            if _has_active_publication_locked(sid):
                return True, epoch
            validator = _managed_session_validator
        try:
            # ``None`` requests a current-session-id lookup, not authority to
            # mint a run publication. Publication itself always supplies and
            # validates an exact route key.
            allowed = bool(validator(None, sid)) if callable(validator) else False
        except Exception:
            allowed = False
        with _backend_lock:
            if epoch == _session_route_epoch_sequence:
                return allowed, epoch


def publish_computer_use_session(
    session_id: str,
    *,
    route_key: Optional[str] = None,
) -> ComputerUseSessionPublication:
    """Publish one route-bound run lease after authoritative validation.

    The validator runs outside ``_backend_lock`` to avoid lock inversion with
    SessionStore.  The scalar route epoch is then rechecked under the lock, so
    a terminal transition or reactivation that starts during validation makes
    this attempt retry instead of minting a stale capability.
    """
    global _managed_routing_enabled
    sid = str(session_id or "")
    if not sid:
        raise ValueError("managed computer_use publication requires a session id")
    normalized_route_key = str(route_key or "") or None
    while True:
        with _backend_lock:
            epoch = _session_route_epoch_sequence
            validator = _managed_session_validator
            validator_required = callable(validator)
        if validator_required:
            if normalized_route_key is None:
                raise ValueError(
                    "managed computer_use publication requires an exact route key"
                )
            try:
                allowed = bool(validator(normalized_route_key, sid))
            except Exception:
                allowed = False
        else:
            # Backward-compatible unmanaged CLI/test seam.  The first lease
            # still enables publication-only admission after it is removed.
            allowed = True
        with _backend_lock:
            if epoch != _session_route_epoch_sequence:
                continue
            if sid in _terminal_transitions or sid in _session_reactivations:
                raise RuntimeError(
                    f"computer_use backend for session {sid!r} is fenced by a terminal transition"
                )
            if not allowed:
                raise RuntimeError(
                    f"computer_use session {sid!r} does not match the authoritative route"
                )
            _managed_routing_enabled = True
            publication = ComputerUseSessionPublication(
                session_id=sid,
                route_key=normalized_route_key,
                route_epoch=epoch,
            )
            _managed_session_publications.setdefault(sid, set()).add(publication)
            return publication


def unpublish_computer_use_session(
    publication: ComputerUseSessionPublication,
) -> None:
    """Release one exact run-scoped publication and reclaim its map entry."""
    with _backend_lock:
        publications = _managed_session_publications.get(publication.session_id)
        if publications is None or publication not in publications:
            raise RuntimeError("computer_use session publication token mismatch")
        publications.remove(publication)
        publication.active = False
        if not publications:
            _managed_session_publications.pop(publication.session_id, None)


def get_computer_use_session_generation(session_id: str) -> Optional[int]:
    sid = str(session_id or "")
    if not sid:
        return None
    with _backend_lock:
        record = _record_from_legacy_cache_locked(sid)
        return record.generation if record is not None else None


def begin_computer_use_terminal_transition(
    session_id: str,
) -> ComputerUseTerminalTransition:
    """Fence one session ID against CUA acquisition through route publication."""
    sid = str(session_id or "")
    if not sid:
        raise ValueError("terminal computer_use transition requires a session id")
    global _session_route_epoch_sequence
    with _backend_lock:
        if sid in _terminal_transitions or sid in _session_reactivations:
            raise RuntimeError(
                f"computer_use backend for session {sid!r} already has a lifecycle transition"
            )
        _session_route_epoch_sequence += 1
        invalidated_publications = tuple(
            publication
            for publication in _managed_session_publications.get(sid, ())
            if publication.active
        )
        for publication in invalidated_publications:
            publication.active = False
        record = _record_from_legacy_cache_locked(sid)
        token = ComputerUseTerminalTransition(
            session_id=sid,
            generation=record.generation if record is not None else None,
            invalidated_publications=invalidated_publications,
        )
        _terminal_transitions[sid] = token
        return token


def end_computer_use_terminal_transition(
    transition: ComputerUseTerminalTransition,
    *,
    retire: bool = False,
) -> None:
    """Remove one exact fence after confirmed route publication."""
    global _managed_routing_enabled, _session_route_epoch_sequence
    with _backend_lock:
        current = _terminal_transitions.get(transition.session_id)
        if current is not transition:
            raise RuntimeError("computer_use terminal transition token mismatch")
        _terminal_transitions.pop(transition.session_id, None)
        _session_route_epoch_sequence += 1
        if retire:
            _managed_routing_enabled = True
        else:
            publications = _managed_session_publications.get(
                transition.session_id,
                set(),
            )
            for publication in transition.invalidated_publications:
                if publication in publications:
                    publication.active = True


def is_computer_use_session_retired(session_id: str) -> bool:
    sid = str(session_id or "")
    if not sid:
        return False
    with _backend_lock:
        return _managed_routing_enabled and not _has_active_publication_locked(sid)


def begin_computer_use_session_reactivation(
    session_id: str,
) -> ComputerUseSessionReactivation:
    """Fence and re-arm one explicitly republished historical route ID."""
    sid = str(session_id or "")
    if not sid:
        raise ValueError("computer_use reactivation requires a session id")
    global _session_route_epoch_sequence
    with _backend_lock:
        if _has_active_publication_locked(sid):
            raise RuntimeError(
                f"computer_use session {sid!r} is already published"
            )
        if sid in _terminal_transitions or sid in _session_reactivations:
            raise RuntimeError(
                f"computer_use session {sid!r} already has a lifecycle fence"
            )
        if _record_from_legacy_cache_locked(sid) is not None:
            raise RuntimeError(
                f"computer_use retired session {sid!r} still owns a backend"
            )
        token = ComputerUseSessionReactivation(session_id=sid)
        _session_route_epoch_sequence += 1
        _session_reactivations[sid] = token
        return token


def end_computer_use_session_reactivation(
    reactivation: ComputerUseSessionReactivation,
    *,
    succeeded: bool,
) -> None:
    """Finish one exact reactivation fence or restore retirement on failure."""
    global _session_route_epoch_sequence
    with _backend_lock:
        current = _session_reactivations.get(reactivation.session_id)
        if current is not reactivation:
            raise RuntimeError("computer_use session reactivation token mismatch")
        _session_reactivations.pop(reactivation.session_id, None)
        _session_route_epoch_sequence += 1


def computer_use_lifecycle_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a secret-free, operator-facing lifecycle snapshot."""
    with _backend_lock:
        return {
            sid: {
                "generation": record.generation,
                "state": record.state.value,
                "permission_mode": record.permission_mode,
                "close_reason": record.close_reason,
                "close_attempts": record.close_attempts,
                "last_error": record.last_error,
            }
            for sid, record in _backend_records.items()
        }


class _CleanupSupervisor:
    """Bounded, tracked executor with deduplicated deferred admission."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_pending: int = 64,
        max_deferred: int = 256,
    ) -> None:
        self._max_pending = max_pending
        self._max_deferred = max_deferred
        self._slots = threading.BoundedSemaphore(max_pending)
        self._lock = threading.Lock()
        self._work_queue: queue.Queue[
            Optional[tuple[tuple[str, Optional[int]], str, Dict[str, Any]]]
        ] = queue.Queue(maxsize=max_pending)
        self._workers: list[threading.Thread] = []
        for index in range(max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"computer-use-cleanup-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        self._active_keys: set[tuple[str, Optional[int]]] = set()
        self._deferred: OrderedDict[
            tuple[str, Optional[int]], tuple[str, Dict[str, Any]]
        ] = OrderedDict()
        self._inflight: Dict[
            tuple[str, Optional[int]], concurrent.futures.Future
        ] = {}
        self._closed = False

    @staticmethod
    def _rejected_result(
        sid: str,
        kwargs: Dict[str, Any],
        error: str,
    ) -> ComputerUseReleaseResult:
        return ComputerUseReleaseResult(
            session_id=sid,
            generation=kwargs.get("expected_generation"),
            status="queue_rejected",
            reason=str(kwargs.get("reason") or "unspecified"),
            error=error,
        )

    def _launch_locked(
        self,
        key: tuple[str, Optional[int]],
        sid: str,
        kwargs: Dict[str, Any],
    ) -> Optional[str]:
        """Launch after one slot was acquired; caller holds ``_lock``."""
        self._active_keys.add(key)
        try:
            self._work_queue.put_nowait((key, sid, kwargs))
        except queue.Full:
            self._active_keys.discard(key)
            self._slots.release()
            return "cleanup worker queue unexpectedly full"
        return None

    def _worker_loop(self) -> None:
        while True:
            item = self._work_queue.get()
            try:
                if item is None:
                    return
                key, sid, kwargs = item
                self._run_release(key, sid, kwargs)
            finally:
                self._work_queue.task_done()

    def _run_release(
        self,
        key: tuple[str, Optional[int]],
        sid: str,
        kwargs: Dict[str, Any],
    ) -> None:
        try:
            result = _release_computer_use_session_with_retries(sid, **kwargs)
        except Exception as exc:
            result = ComputerUseReleaseResult(
                session_id=sid,
                generation=kwargs.get("expected_generation"),
                status="failed",
                reason=str(kwargs.get("reason") or "unspecified"),
                error=f"cleanup worker raised: {exc!r}",
            )
        self._complete(key, result)

    def _complete(
        self,
        key: tuple[str, Optional[int]],
        result: ComputerUseReleaseResult,
    ) -> None:
        completions: list[
            tuple[concurrent.futures.Future, ComputerUseReleaseResult]
        ] = []
        with self._lock:
            external = self._inflight.pop(key, None)
            self._active_keys.discard(key)
            self._slots.release()
            if external is not None:
                completions.append((external, result))

            # A completion admits the oldest deferred exact owner. If executor
            # shutdown races admission, reject it truthfully and continue so no
            # deferred future is left pending forever.
            while self._deferred and self._slots.acquire(blocking=False):
                deferred_key, (sid, kwargs) = self._deferred.popitem(last=False)
                launch_error = self._launch_locked(deferred_key, sid, kwargs)
                if launch_error is None:
                    break
                deferred_future = self._inflight.pop(deferred_key, None)
                if deferred_future is not None:
                    completions.append(
                        (
                            deferred_future,
                            self._rejected_result(sid, kwargs, launch_error),
                        )
                    )
        for future, completion in completions:
            if not future.done():
                future.set_result(completion)

    def submit(self, session_id: str, **kwargs: Any) -> concurrent.futures.Future:
        sid = str(session_id or "")
        key = (sid, kwargs.get("expected_generation"))
        rejected: Optional[ComputerUseReleaseResult] = None
        with self._lock:
            duplicate = self._inflight.get(key)
            if duplicate is not None:
                return duplicate
            external: concurrent.futures.Future = concurrent.futures.Future()
            if self._closed:
                rejected = self._rejected_result(
                    sid, kwargs, "cleanup supervisor closed"
                )
            else:
                self._inflight[key] = external
                if self._slots.acquire(blocking=False):
                    launch_error = self._launch_locked(key, sid, dict(kwargs))
                    if launch_error is not None:
                        self._inflight.pop(key, None)
                        rejected = self._rejected_result(
                            sid, kwargs, launch_error
                        )
                elif len(self._deferred) < self._max_deferred:
                    self._deferred[key] = (sid, dict(kwargs))
                else:
                    self._inflight.pop(key, None)
                    rejected = self._rejected_result(
                        sid,
                        kwargs,
                        "cleanup deferred-admission queue full; exact owner retained",
                    )
        if rejected is not None:
            if "exact owner retained" in str(rejected.error or ""):
                _mark_cleanup_admission_failure(
                    sid,
                    kwargs.get("expected_generation"),
                    str(rejected.error),
                )
            external.set_result(rejected)
            logger.error(
                "computer_use cleanup rejected session=%s generation=%s reason=%s error=%s",
                sid,
                kwargs.get("expected_generation"),
                kwargs.get("reason"),
                rejected.error,
            )
        return external

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                if not self._active_keys and not self._deferred:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pending": len(self._active_keys),
                "deferred": len(self._deferred),
                "capacity": self._max_pending,
                "deferred_capacity": self._max_deferred,
                "closed": self._closed,
            }

    def shutdown(self, timeout: float) -> bool:
        with self._lock:
            self._closed = True
        deadline = time.monotonic() + max(0.0, timeout)
        drained = self.drain(timeout)
        if not drained:
            # Do not enqueue stop sentinels ahead of deferred work. Workers are
            # daemonized, so a wedged backend cannot block interpreter exit;
            # a later shutdown/drain call can still reap them if work unwinds.
            return False
        for _ in self._workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self._work_queue.put(None, timeout=remaining)
            except queue.Full:
                break
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        # Workers are daemonized deliberately: a backend violating its bounded
        # stop contract cannot hold interpreter shutdown hostage. Active exact
        # ownership remains visible in lifecycle state until process exit.
        return drained and not any(worker.is_alive() for worker in self._workers)


def _mark_cleanup_admission_failure(
    sid: str,
    expected_generation: Any,
    error: str,
) -> None:
    """Fail closed if even the bounded deferred queue cannot admit ownership."""
    if not isinstance(expected_generation, int) or isinstance(
        expected_generation, bool
    ):
        return
    with _backend_lock:
        record = _backend_records.get(sid)
        if record is None or record.generation != expected_generation:
            return
        record.state = BackendLifecycleState.FAILED
        record.close_reason = "cleanup_admission_failure"
        record.last_error = error


_cleanup_supervisor = _CleanupSupervisor()


def computer_use_cleanup_snapshot() -> Dict[str, Any]:
    """Return bounded-supervisor queue depth without exposing session content."""
    return _cleanup_supervisor.snapshot()


def computer_use_process_snapshot() -> List[Dict[str, Any]]:
    """Return direct/recursive cua-driver descendants for operator diagnostics."""
    try:
        import psutil

        parent = psutil.Process()
        rows = []
        for child in parent.children(recursive=True):
            try:
                name = child.name()
                if "cua-driver" not in name.lower():
                    continue
                rows.append({
                    "pid": child.pid,
                    "ppid": child.ppid(),
                    "name": name,
                    "create_time": child.create_time(),
                    "status": child.status(),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sorted(rows, key=lambda row: (row["create_time"], row["pid"]))
    except Exception:
        logger.debug("computer_use process snapshot failed", exc_info=True)
        return []


def write_computer_use_runtime_attestation(
    extra_callables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Atomically bind the live gateway PID to loaded CUA source/code bytes."""
    import hashlib
    import inspect
    import marshal
    from datetime import datetime, timezone
    from pathlib import Path

    from hermes_constants import get_hermes_home

    callables: Dict[str, Any] = {
        "publish_computer_use_session": publish_computer_use_session,
        "_get_backend": _get_backend,
        "_acquire_backend_for_call": _acquire_backend_for_call,
        "release_computer_use_session_result": release_computer_use_session_result,
    }
    callables.update(extra_callables or {})
    code_rows = {}
    module_paths = set()
    for name, value in callables.items():
        target = getattr(value, "__func__", value)
        code = getattr(target, "__code__", None)
        source_path = inspect.getsourcefile(target)
        if source_path:
            module_paths.add(str(Path(source_path).resolve()))
        code_rows[name] = {
            "source_path": str(Path(source_path).resolve()) if source_path else None,
            "first_line": getattr(code, "co_firstlineno", None),
            "code_sha256": (
                hashlib.sha256(marshal.dumps(code)).hexdigest()
                if code is not None
                else None
            ),
        }
    modules = {}
    for source_path in sorted(module_paths):
        path = Path(source_path)
        modules[source_path] = {
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    try:
        import psutil

        process_create_time = psutil.Process(os.getpid()).create_time()
    except Exception:
        # Process creation time is required for PID-reuse-safe external
        # verification. Fail instead of publishing a weaker receipt.
        raise RuntimeError("could not attest gateway process creation time")
    output = get_hermes_home() / "runtime" / "cua_gateway_attestation.json"
    archive_dir = output.parent / "cua_gateway_attestations"
    archive = archive_dir / (
        f"{os.getpid()}-{int(process_create_time * 1_000_000)}.json"
    )
    receipt = {
        "schema": 2,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "process_create_time": process_create_time,
        "executable": sys.executable,
        "argv": list(sys.argv),
        "modules": modules,
        "callables": code_rows,
        "archive_path": str(archive),
        "path": str(output),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(receipt, indent=2)
    archive_temporary = archive.with_suffix(".tmp")
    archive_temporary.write_text(encoded, encoding="utf-8")
    os.replace(archive_temporary, archive)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, output)
    return receipt


def _cua_permission_mode(session_id: str) -> str:
    """Map Hermes's explicit approval bypass onto Cua's immutable mode.

    Hermes has TWO session-identity namespaces: the tool-dispatch path passes
    the DB ``session_id`` (``agent.session_id``), while gateway ``/yolo``
    keys approval state off the gateway ``session_key`` (set per turn via the
    ``set_current_session_key`` contextvar in tools/approval.py). CLI and TUI
    use the DB id for both. Checking ONLY ``session_id`` here would make a
    gateway ``/yolo`` toggle silently invisible to computer_use (works in
    CLI, dead on messaging platforms), so we consult both namespaces —
    bypass in either means the user explicitly opted out of approvals for
    this run. Fails closed on any resolution error.
    """
    try:
        from tools.approval import (
            get_current_session_key,
            is_approval_bypass_active_for_session,
        )

        if is_approval_bypass_active_for_session(session_id):
            return "unrestricted"
        current_key = get_current_session_key(default="")
        if current_key and is_approval_bypass_active_for_session(current_key):
            return "unrestricted"
    except Exception:
        # Approval state must fail closed if it cannot be resolved.
        pass
    return "standard"


def _get_backend(session_id: str = "") -> ComputerUseBackend:
    global _backend
    sid = str(session_id or "")
    while True:
        release_generation: Optional[int] = None
        route_allowed, route_epoch = _validate_managed_publication(sid)
        with _backend_lock:
            if route_epoch != _session_route_epoch_sequence:
                continue
            if sid in _terminal_transitions or sid in _session_reactivations:
                raise RuntimeError(
                    f"computer_use backend for session {sid!r} is fenced by a terminal transition"
                )
            if not route_allowed:
                raise RuntimeError(
                    f"computer_use backend for retired or unpublished session {sid!r} is unavailable"
                )
            permission_mode = _cua_permission_mode(sid)
            if sid == "" and _backend is not None and sid not in _backends:
                _backends[sid] = _backend
                _backend_call_locks[sid] = threading.RLock()
                _backend_permission_modes[sid] = permission_mode
            record = _record_from_legacy_cache_locked(sid)
            if record is not None:
                if record.state == BackendLifecycleState.ACTIVE:
                    if record.permission_mode == permission_mode:
                        _stamp_backend_lookup_locked(
                            sid,
                            record.backend,
                            route_allowed=route_allowed,
                        )
                        return record.backend
                    release_generation = record.generation
                elif record.state == BackendLifecycleState.CLOSING:
                    raise RuntimeError(
                        f"computer_use backend for session {sid!r} is closing"
                    )
                elif record.state == BackendLifecycleState.FAILED:
                    raise RuntimeError(
                        f"computer_use backend for session {sid!r} cleanup failed; "
                        "retry release before recreating it"
                    )
                else:
                    raise RuntimeError(
                        f"computer_use backend for session {sid!r} is "
                        f"{record.state.value.lower()}"
                    )
            else:
                backend_name = os.environ.get(
                    "HERMES_COMPUTER_USE_BACKEND", "cua"
                ).lower()
                if backend_name in {"cua", "cua-driver", ""}:
                    from tools.computer_use.cua_backend import CuaDriverBackend

                    backend = CuaDriverBackend(permission_mode=permission_mode)
                elif backend_name == "noop":  # pragma: no cover
                    backend = _NoopBackend()
                else:
                    raise RuntimeError(
                        f"Unknown HERMES_COMPUTER_USE_BACKEND={backend_name!r}"
                    )
                call_lock = threading.RLock()
                record = _BackendRecord(
                    session_id=sid,
                    generation=_next_backend_generation_locked(sid),
                    permission_mode=permission_mode,
                    backend=backend,
                    call_lock=call_lock,
                )
                _backend_records[sid] = record
                _backends[sid] = backend
                _backend_call_locks[sid] = call_lock
                _backend_permission_modes[sid] = permission_mode
                if sid == "":
                    _backend = backend
                try:
                    backend.start()
                except Exception as exc:
                    record.state = BackendLifecycleState.FAILED
                    record.last_error = repr(exc)
                    try:
                        backend.stop()
                    except Exception as cleanup_exc:
                        record.last_error = (
                            f"start={exc!r}; cleanup={cleanup_exc!r}"
                        )
                        logger.error(
                            "computer_use startup cleanup failed session=%s "
                            "generation=%s error=%s",
                            sid,
                            record.generation,
                            cleanup_exc,
                        )
                    else:
                        _backend_records.pop(sid, None)
                        _backends.pop(sid, None)
                        _backend_call_locks.pop(sid, None)
                        _backend_permission_modes.pop(sid, None)
                        if sid == "" and _backend is backend:
                            _backend = None
                    raise
                record.state = BackendLifecycleState.ACTIVE
                logger.info(
                    "computer_use lifecycle session=%s generation=%s state=ACTIVE",
                    sid,
                    record.generation,
                )
                _stamp_backend_lookup_locked(
                    sid,
                    backend,
                    route_allowed=route_allowed,
                )
                return backend

        # Permission-mode changes are hard lifecycle boundaries. The exact
        # generation stays installed while it closes, so no same-SID backend can
        # be recreated until stop has succeeded.
        result = release_computer_use_session_result(
            sid,
            expected_generation=release_generation,
            timeout=_DEFAULT_RELEASE_TIMEOUT,
            reason="permission_mode_change",
            allow_empty_session=(sid == ""),
        )
        if not result.released:
            raise RuntimeError(
                f"computer_use permission-mode transition failed for {sid!r}: "
                f"{result.status}: {result.error or ''}"
            )


def _acquire_backend_for_call(
    session_id: str,
) -> tuple[ComputerUseBackend, threading.RLock]:
    """Acquire an action lease that cannot outlive its exact active backend."""
    global _backend
    sid = str(session_id or "")
    for _ in range(3):
        backend = _get_backend(session_id=sid)
        lookup = getattr(_backend_lookup_state, "value", None)
        expected_route_epoch = (
            lookup[2]
            if (
                isinstance(lookup, tuple)
                and len(lookup) == 4
                and lookup[0] == sid
                and lookup[1] == id(backend)
                and lookup[3] is True
            )
            else None
        )
        with _backend_lock:
            record = _record_from_legacy_cache_locked(sid)
            if record is None and sid == "":
                # Preserve the long-standing test/plugin seam where _get_backend
                # itself is replaced and returns a pre-started backend. Restrict
                # this normalization to the legacy empty-session namespace so
                # hard session cleanup still requires authoritative ownership.
                call_lock = threading.RLock()
                record = _BackendRecord(
                    session_id=sid,
                    generation=_next_backend_generation_locked(sid),
                    permission_mode=_cua_permission_mode(sid),
                    backend=backend,
                    call_lock=call_lock,
                    state=BackendLifecycleState.ACTIVE,
                )
                _backend_records[sid] = record
                _backends[sid] = backend
                _backend_call_locks[sid] = call_lock
                _backend_permission_modes[sid] = record.permission_mode
                _backend = backend
            if record is None or record.backend is not backend:
                if (
                    expected_route_epoch is not None
                    and expected_route_epoch != _session_route_epoch_sequence
                ):
                    raise RuntimeError(
                        f"computer_use backend for session {sid!r} changed while acquiring action lease"
                    )
                continue
            call_lock = record.call_lock
        call_lock.acquire()
        with _backend_lock:
            current = _backend_records.get(sid)
            current_route_epoch = _session_route_epoch_sequence
            if (
                current is record
                and current.backend is backend
                and current.state == BackendLifecycleState.ACTIVE
                and sid not in _terminal_transitions
                and sid not in _session_reactivations
                and (
                    not sid
                    or (
                        expected_route_epoch is not None
                        and expected_route_epoch == current_route_epoch
                    )
                )
            ):
                return backend, call_lock
        call_lock.release()
        if (
            expected_route_epoch is not None
            and expected_route_epoch != current_route_epoch
        ):
            raise RuntimeError(
                f"computer_use backend for session {sid!r} changed while acquiring action lease"
            )
    raise RuntimeError(
        f"computer_use backend for session {sid!r} changed while acquiring action lease"
    )


def release_computer_use_session_result(
    session_id: str,
    *,
    expected_generation: Any = _EXPECTED_GENERATION_UNSET,
    timeout: float = _DEFAULT_RELEASE_TIMEOUT,
    reason: str = "explicit",
    allow_empty_session: bool = False,
) -> ComputerUseReleaseResult:
    """Close one exact backend generation with bounded, truthful lifecycle state.

    Omitting ``expected_generation`` preserves the historical public call shape,
    but it never authorizes wildcard teardown: the exact active generation is
    snapshotted under ``_backend_lock``. Passing ``None`` explicitly remains an
    invalid generation.
    """
    global _backend
    sid = str(session_id or "")
    started = time.monotonic()
    generation_omitted = expected_generation is _EXPECTED_GENERATION_UNSET
    if (
        not generation_omitted
        and (type(expected_generation) is not int or expected_generation <= 0)
    ):
        return ComputerUseReleaseResult(
            session_id=sid,
            generation=expected_generation,
            status="mismatch",
            reason=reason,
            error="expected_generation must be a positive integer",
        )
    if not sid and not allow_empty_session:
        return ComputerUseReleaseResult(
            session_id=sid,
            generation=None if generation_omitted else expected_generation,
            status="mismatch",
            reason=reason,
            error="empty session id rejected",
        )

    with _backend_lock:
        record = _record_from_legacy_cache_locked(sid)
        if generation_omitted:
            if record is None:
                return ComputerUseReleaseResult(
                    session_id=sid,
                    generation=None,
                    status="already_absent",
                    reason=reason,
                )
            expected_generation = record.generation
        if record is None:
            last_generation = _backend_generation_counters.get(sid)
            idempotent_absence = (
                expected_generation is None
                or (
                    last_generation is not None
                    and expected_generation == last_generation
                )
            )
            status = "already_absent" if idempotent_absence else "mismatch"
            return ComputerUseReleaseResult(
                session_id=sid,
                generation=expected_generation,
                status=status,
                reason=reason,
                error=None if status == "already_absent" else "generation absent",
            )
        if expected_generation is not None and record.generation != expected_generation:
            return ComputerUseReleaseResult(
                session_id=sid,
                generation=record.generation,
                status="mismatch",
                reason=reason,
                error=f"expected generation {expected_generation}",
            )
        if record.state == BackendLifecycleState.CLOSING:
            return ComputerUseReleaseResult(
                session_id=sid,
                generation=record.generation,
                status="already_closing",
                reason=reason,
            )
        if record.state == BackendLifecycleState.CLOSED:
            return ComputerUseReleaseResult(
                session_id=sid,
                generation=record.generation,
                status="already_absent",
                reason=reason,
            )
        record.state = BackendLifecycleState.CLOSING
        record.close_requested_at = time.monotonic()
        record.close_reason = reason
        record.close_attempts += 1
        record.last_error = None
        generation = record.generation
        backend = record.backend
        call_lock = record.call_lock

    with _approval_lock:
        _session_auto_approve.pop(sid, None)
        _always_allow.pop(sid, None)

    logger.info(
        "computer_use lifecycle session=%s generation=%s state=CLOSING reason=%s",
        sid,
        generation,
        reason,
    )
    acquired = False
    try:
        acquired = call_lock.acquire(timeout=max(0.0, timeout))
        if not acquired:
            error = f"in-flight call did not quiesce within {timeout:.3f}s"
            with _backend_lock:
                current = _backend_records.get(sid)
                if current is record:
                    record.state = BackendLifecycleState.FAILED
                    record.last_error = error
            status = "timed_out"
        else:
            try:
                backend.stop()
            except Exception as exc:
                error = repr(exc)
                with _backend_lock:
                    current = _backend_records.get(sid)
                    if current is record:
                        record.state = BackendLifecycleState.FAILED
                        record.last_error = error
                status = "failed"
            else:
                error = None
                with _backend_lock:
                    current = _backend_records.get(sid)
                    if current is not record:
                        status = "mismatch"
                        error = "lifecycle record changed during close"
                    else:
                        record.state = BackendLifecycleState.CLOSED
                        _backend_records.pop(sid, None)
                        _backends.pop(sid, None)
                        _backend_call_locks.pop(sid, None)
                        _backend_permission_modes.pop(sid, None)
                        if sid == "" and _backend is backend:
                            _backend = None
                        status = "released"
    finally:
        if acquired:
            call_lock.release()

    elapsed_ms = (time.monotonic() - started) * 1000.0
    log = logger.info if status == "released" else logger.error
    log(
        "computer_use lifecycle session=%s generation=%s status=%s "
        "reason=%s elapsed_ms=%.1f error=%s",
        sid,
        generation,
        status,
        reason,
        elapsed_ms,
        error,
    )
    return ComputerUseReleaseResult(
        session_id=sid,
        generation=generation,
        status=status,
        reason=reason,
        elapsed_ms=elapsed_ms,
        error=error,
    )


def _release_computer_use_session_with_retries(
    session_id: str,
    *,
    expected_generation: int,
    timeout: float,
    reason: str,
    allow_empty_session: bool,
    max_attempts: int = 3,
    retry_delay: float = 0.1,
) -> ComputerUseReleaseResult:
    """Run bounded retries for transient quiescence/stop failures."""
    result: Optional[ComputerUseReleaseResult] = None
    for attempt in range(1, max(1, max_attempts) + 1):
        result = release_computer_use_session_result(
            session_id,
            expected_generation=expected_generation,
            timeout=timeout,
            reason=f"{reason}:attempt-{attempt}",
            allow_empty_session=allow_empty_session,
        )
        if result.status not in {"timed_out", "failed"}:
            return result
        if attempt < max_attempts:
            time.sleep(max(0.0, retry_delay) * attempt)
    assert result is not None
    return result


def release_computer_use_session(session_id: str) -> bool:
    """Backward-compatible bool adapter that snapshots one exact generation."""
    sid = str(session_id or "")
    with _backend_lock:
        record = _record_from_legacy_cache_locked(sid)
        if record is None:
            return False
        generation = record.generation
    result = release_computer_use_session_result(
        sid,
        expected_generation=generation,
        timeout=_DEFAULT_RELEASE_TIMEOUT,
        reason="explicit",
        allow_empty_session=True,
    )
    return result.released


def submit_computer_use_session_release(
    session_id: str,
    *,
    expected_generation: int,
    timeout: float = _DEFAULT_RELEASE_TIMEOUT,
    reason: str,
    max_attempts: int = 3,
    retry_delay: float = 0.1,
) -> concurrent.futures.Future:
    """Submit one exact generation close to the bounded cleanup supervisor."""
    if type(expected_generation) is not int or expected_generation <= 0:
        raise ValueError("expected_generation must be a positive integer")
    return _cleanup_supervisor.submit(
        session_id,
        expected_generation=expected_generation,
        timeout=timeout,
        reason=reason,
        allow_empty_session=False,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )


def drain_computer_use_cleanup(timeout: float = _DEFAULT_RELEASE_TIMEOUT) -> bool:
    return _cleanup_supervisor.drain(timeout)


def _shutdown_backend_atexit() -> None:
    """Boundedly drain tracked cleanup, then close every remaining generation."""
    try:
        drain_computer_use_cleanup(timeout=_DEFAULT_RELEASE_TIMEOUT)
    except Exception:
        logger.debug("computer_use cleanup drain failed during shutdown", exc_info=True)

    with _backend_lock:
        records = [
            (sid, record.generation)
            for sid, record in _backend_records.items()
        ]
        # Preserve direct legacy injection even if it has not been normalized.
        for sid in tuple(_backends):
            record = _record_from_legacy_cache_locked(sid)
            if record is not None and (sid, record.generation) not in records:
                records.append((sid, record.generation))
        if _backend is not None and "" not in _backends:
            record = _record_from_legacy_cache_locked("")
            if record is not None and ("", record.generation) not in records:
                records.append(("", record.generation))

    for sid, generation in records:
        try:
            release_computer_use_session_result(
                sid,
                expected_generation=generation,
                timeout=_DEFAULT_RELEASE_TIMEOUT,
                reason="process_shutdown",
                allow_empty_session=True,
            )
        except Exception:
            logger.debug(
                "computer_use shutdown release failed session=%s generation=%s",
                sid,
                generation,
                exc_info=True,
            )

    with _approval_lock:
        _session_auto_approve.clear()
        _always_allow.clear()


def _finalize_computer_use_atexit() -> None:
    try:
        _shutdown_backend_atexit()
    finally:
        try:
            _cleanup_supervisor.shutdown(timeout=_DEFAULT_RELEASE_TIMEOUT)
        except Exception:
            pass


atexit.register(_finalize_computer_use_atexit)


def reset_backend_for_tests() -> None:  # pragma: no cover
    """Test helper — tear down cached backends and reset lifecycle state."""
    global _backend, _backend_generation_sequence
    global _managed_routing_enabled, _managed_session_validator
    global _session_route_epoch_sequence
    _shutdown_backend_atexit()
    with _backend_lock:
        _backend = None
        _backends.clear()
        _backend_call_locks.clear()
        _backend_permission_modes.clear()
        _backend_records.clear()
        _terminal_transitions.clear()
        _session_reactivations.clear()
        _managed_session_publications.clear()
        _managed_routing_enabled = False
        _managed_session_validator = None
        _session_route_epoch_sequence = 0
        _backend_generation_counters.clear()
        _backend_generation_sequence = 0
    with _approval_lock:
        _session_auto_approve.clear()
        _always_allow.clear()
    _AUX_VISION_ROUTE_CACHE.clear()


class _NoopBackend(ComputerUseBackend):  # pragma: no cover
    """Test/CI stub. Records calls; returns trivial results."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._started = False

    def start(self) -> None: self._started = True
    def stop(self) -> None: self._started = False
    def is_available(self) -> bool: return True

    def capture(
        self,
        mode: str = "som",
        app: Optional[str] = None,
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
    ) -> CaptureResult:
        self.calls.append((
            "capture",
            {"mode": mode, "app": app, "pid": pid, "window_id": window_id},
        ))
        return CaptureResult(mode=mode, width=1024, height=768, png_b64=None,
                             elements=[], app=app or "", window_title="")

    def click(self, **kw) -> ActionResult:
        self.calls.append(("click", kw))
        return ActionResult(ok=True, action="click")

    def drag(self, **kw) -> ActionResult:
        self.calls.append(("drag", kw))
        return ActionResult(ok=True, action="drag")

    def scroll(self, **kw) -> ActionResult:
        self.calls.append(("scroll", kw))
        return ActionResult(ok=True, action="scroll")

    def type_text(self, text: str, **kw) -> ActionResult:
        self.calls.append(("type", {"text": text, **kw}))
        return ActionResult(ok=True, action="type")

    def key(self, keys: str, **kw) -> ActionResult:
        self.calls.append(("key", {"keys": keys, **kw}))
        return ActionResult(ok=True, action="key")

    def list_apps(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_apps", {}))
        return []

    def list_windows(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_windows", {}))
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        self.calls.append(("focus_app", {"app": app, "raise": raise_window}))
        return ActionResult(ok=True, action="focus_app")

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        self.calls.append(("set_value", {"value": value, "element": element}))
        return ActionResult(ok=True, action="set_value")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle_computer_use(args: Dict[str, Any], **kwargs) -> Any:
    """Main entry point — dispatched by tools.registry.

    Returns either a JSON string (text-only) or a dict marked `_multimodal`
    (image + summary) which run_agent.py wraps into the tool message.
    """
    action = (args.get("action") or "").strip().lower()
    if not action:
        return json.dumps({"error": "missing `action`"})
    # Per-run key for approval-state and daemon-mode isolation across
    # concurrent sessions.
    session_id = str(kwargs.get("session_id") or "")

    # Safety: validate actions before approval prompt.
    if action in {"type", "cua_browser_type"}:
        text = args.get("text", "")
        pat = _is_blocked_type(text)
        if pat:
            return json.dumps({
                "error": f"blocked pattern in type text: {pat!r}",
                "hint": "Dangerous shell patterns cannot be typed via computer_use.",
            })

    if action == "key":
        keys = args.get("keys", "")
        combo = _canon_key_combo(keys)
        for blocked in _BLOCKED_KEY_COMBOS:
            if blocked.issubset(combo) and len(blocked) <= len(combo):
                return json.dumps({
                    "error": f"blocked key combo: {sorted(blocked)}",
                    "hint": "Destructive system shortcuts are hard-blocked.",
                })

    if args.get("bring_to_front") and args.get("delivery_mode") != "foreground":
        return json.dumps({
            "error": "bring_to_front requires delivery_mode='foreground'",
            "code": "bring_to_front_requires_foreground",
        })

    # Approval gate (destructive actions only).
    if action in _DESTRUCTIVE_ACTIONS:
        err = _request_approval(action, args, session_id)
        if err is not None:
            return err
    # Persistent focus is a separate, visible side effect from the input
    # itself. Keep its approval scope distinct even when the input rung has
    # already been approved for this session.
    if args.get("bring_to_front") or (
        action == "focus_app" and args.get("raise_window")
    ):
        err = _request_approval("bring_to_front", args, session_id)
        if err is not None:
            return err

    # Dispatch through an exact-generation action lease. Teardown can mark the
    # record CLOSING concurrently, but cannot stop the backend until this lease
    # releases its per-session call lock.
    call_lock = None
    try:
        backend, call_lock = _acquire_backend_for_call(session_id)
    except Exception as e:
        return json.dumps({
            "error": f"computer_use backend unavailable: {e}",
            "hint": "If the cua-driver binary is missing, run `hermes computer-use install`. "
                    "If a Python dependency is missing, the error above shows the exact install command.",
        })
    try:
        return _dispatch(backend, action, args)
    except Exception as e:
        if getattr(e, "cua_action_termination_unconfirmed", False):
            # The action lease is about to be released, but the exact driver
            # coroutine may still be running. Poison this generation before
            # releasing the lock so no successor action can overlap it. Only
            # exact-generation teardown may clear the failed record.
            with _backend_lock:
                record = _backend_records.get(session_id)
                if (
                    record is not None
                    and record.backend is backend
                    and record.state == BackendLifecycleState.ACTIVE
                ):
                    record.state = BackendLifecycleState.FAILED
                    record.close_reason = "action_termination_unconfirmed"
                    record.last_error = repr(e)
        logger.exception("computer_use %s failed", action)
        return json.dumps({"error": f"{action} failed: {e}"})
    finally:
        if call_lock is not None:
            call_lock.release()


def _request_approval(action: str, args: Dict[str, Any],
                      session_id: str = "") -> Optional[str]:
    """Return None if approved, or a JSON error string if denied.

    Approval is scoped by (action, delivery_mode) AND by session_id.
    Foreground delivery is a visible focus change, so a prior background
    approval — even ``approve_session`` on the same action — must NOT
    silently authorize it (NousResearch/hermes-agent#67052).
    ``always_approve`` (the blanket "auto-approve everything" unlock) still
    covers foreground, since the user explicitly opted into unattended
    operation. State is keyed on session_id so concurrent runs don't leak
    unlocks into one another.
    """
    is_foreground = args.get("delivery_mode") == "foreground"
    scope_key = (action, "foreground" if is_foreground else "background")
    with _approval_lock:
        if _session_auto_approve.get(session_id):
            return None
        if scope_key in _always_allow.get(session_id, set()):
            return None
    cb = _approval_callback
    if cb is None:
        # No CLI approval wired — default allow. Gateway approval is handled
        # one layer out via the normal tool-approval infra.
        return None
    summary = _summarize_action(action, args)
    try:
        verdict = cb(action, args, summary)
    except Exception as e:
        logger.warning("approval callback failed: %s", e)
        verdict = "deny"
    if verdict == "approve_once":
        return None
    if verdict == "approve_session" or verdict == "always_approve":
        with _approval_lock:
            _always_allow.setdefault(session_id, set()).add(scope_key)
            if verdict == "always_approve":
                _session_auto_approve[session_id] = True
        return None
    if verdict == "timeout":
        return json.dumps({
            "error": (
                "approval prompt timed out — the user did not respond. "
                "Silence is not consent; do not retry without the user."
            ),
            "action": action,
        })
    return json.dumps({"error": "denied by user", "action": action})


def _summarize_action(action: str, args: Dict[str, Any]) -> str:
    fg = " [FOREGROUND — briefly raises the window / changes focus]" \
        if args.get("delivery_mode") == "foreground" else ""
    if action in {"click", "double_click", "right_click", "middle_click"}:
        if args.get("element") is not None:
            return f"{action} element #{args['element']}{fg}"
        coord = args.get("coordinate")
        if coord:
            return f"{action} at {tuple(coord)}{fg}"
        return action + fg
    if action == "drag":
        src = args.get("from_element") or args.get("from_coordinate")
        dst = args.get("to_element") or args.get("to_coordinate")
        return f"drag {src} → {dst}{fg}"
    if action == "scroll":
        return f"scroll {args.get('direction', '?')} x{args.get('amount', 3)}{fg}"
    if action == "type":
        text = args.get("text", "")
        return f"type {text[:60]!r}" + ("..." if len(text) > 60 else "") + fg
    if action == "key":
        return f"key {args.get('keys', '')!r}{fg}"
    if action == "focus_app":
        return f"focus {args.get('app', '')!r}" + (" (raise)" if args.get("raise_window") else "")
    return action + fg


def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:
    capture_after = bool(args.get("capture_after"))

    if action == "capture":
        mode = str(args.get("mode", "som"))
        if mode not in {"som", "vision", "ax"}:
            return json.dumps({"error": f"bad mode {mode!r}; use som|vision|ax"})
        capture_kwargs: Dict[str, Any] = {"mode": mode, "app": args.get("app")}
        if args.get("pid") is not None or args.get("window_id") is not None:
            capture_kwargs.update({
                "pid": args.get("pid"),
                "window_id": args.get("window_id"),
            })
        cap = backend.capture(**capture_kwargs)
        return _capture_response(cap, max_elements=_coerce_max_elements(args.get("max_elements")))

    if action == "wait":
        seconds = float(args.get("seconds", 1.0))
        res = backend.wait(seconds)
        return _text_response(res)

    if action == "list_apps":
        apps = backend.list_apps()
        return json.dumps({"apps": apps, "count": len(apps)})

    if action == "list_windows":
        windows = backend.list_windows()
        return json.dumps({"windows": windows, "count": len(windows)})

    if action == "focus_app":
        app = args.get("app")
        if not app:
            return json.dumps({"error": "focus_app requires `app`"})
        res = backend.focus_app(app, raise_window=bool(args.get("raise_window")))
        return _maybe_follow_capture(backend, res, capture_after)

    # cua-driver's typed browser surface is namespaced inside the existing
    # computer_use tool so it cannot collide with native browser/MCP tools.
    # The backend owns the opaque driver session, target, tab and ref state;
    # none of those capabilities can be supplied across Hermes sessions.
    if action == "cua_browser_state":
        state_args: Dict[str, Any] = {}
        for public, internal in (
            ("pid", "pid"),
            ("window_id", "window_id"),
            ("tab_id", "tab_id"),
            ("snapshot_format", "snapshot_format"),
            ("query", "query"),
            ("scope_ref", "scope_ref"),
            ("continuation", "continuation"),
        ):
            if args.get(public) is not None:
                state_args[internal] = args[public]
        return json.dumps(backend.typed_browser_state(**state_args))

    if action == "cua_browser_prepare":
        return json.dumps(backend.typed_browser_prepare(
            pid=args.get("pid"),
            window_id=args.get("window_id"),
            profile_mode=args.get("profile_mode", "isolated_new"),
            profile_name=args.get("profile_name"),
            allow_launch=bool(args.get("allow_launch")),
        ))

    browser_tools = {
        "cua_browser_navigate": "browser_navigate",
        "cua_browser_click": "browser_click",
        "cua_browser_type": "browser_type",
        "cua_browser_pointer": "browser_pointer",
        "cua_browser_dialog": "browser_dialog",
        "cua_browser_set_input_files": "browser_set_input_files",
        "cua_browser_download": "browser_download",
    }
    driver_tool = browser_tools.get(action)
    if driver_tool is not None:
        call_args: Dict[str, Any] = {}
        allowed_fields = {
            "browser_navigate": ("url",),
            "browser_click": ("ref", "input_route", "x", "y"),
            "browser_type": ("ref", "text"),
            "browser_pointer": (
                "ref", "destination_ref", "input_route", "x", "y",
                "to_x", "to_y", "delta_x", "delta_y",
            ),
            "browser_dialog": (
                "dialog_id", "prompt_text", "delivery_mode",
            ),
            "browser_set_input_files": ("ref", "files"),
            "browser_download": ("ref", "destination_root"),
        }
        for field in allowed_fields[driver_tool]:
            if args.get(field) is not None:
                call_args[field] = args[field]
        if (
            driver_tool in {"browser_click", "browser_pointer"}
            and args.get("coordinate") is not None
        ):
            coordinate = args["coordinate"]
            if isinstance(coordinate, (list, tuple)) and len(coordinate) == 2:
                call_args["x"], call_args["y"] = coordinate
        pointer_action = args.get("browser_pointer_action")
        dialog_action = args.get("browser_dialog_action")
        # Direct adapter callers may omit the public discriminator from args;
        # retain this narrow compatibility path without making it usable to
        # override the namespaced action selected by handle_computer_use.
        nested_action = args.get("action")
        if nested_action not in browser_tools:
            if driver_tool == "browser_pointer" and pointer_action is None:
                pointer_action = nested_action
            if driver_tool == "browser_dialog" and dialog_action is None:
                dialog_action = nested_action
        if pointer_action is not None:
            call_args["action"] = pointer_action
        if dialog_action is not None:
            call_args["action"] = dialog_action
        if args.get("browser_type_mode") is not None:
            call_args["mode"] = args["browser_type_mode"]
        return json.dumps(backend.typed_browser_action(
            driver_tool,
            tab_id=args.get("tab_id"),
            args=call_args,
        ))

    # delivery_mode / bring_to_front thread through every input action so the
    # model can escalate background → foreground per cua-driver's ladder.
    delivery_mode = args.get("delivery_mode")
    bring_to_front = bool(args.get("bring_to_front"))

    if action in {"click", "double_click", "right_click", "middle_click"}:
        button = args.get("button")
        click_count = 1
        if action == "double_click":
            click_count = 2
        elif action == "right_click":
            button = "right"
        elif action == "middle_click":
            button = "middle"
        else:
            button = button or "left"
        element = args.get("element")
        coord = args.get("coordinate") or (None, None)
        x, y = (coord[0], coord[1]) if coord and coord[0] is not None else (None, None)
        res = backend.click(
            element=element if element is not None else None,
            x=x, y=y, button=button or "left", click_count=click_count,
            modifiers=args.get("modifiers"),
            delivery_mode=delivery_mode, bring_to_front=bring_to_front,
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "drag":
        has_elements = args.get("from_element") is not None and args.get("to_element") is not None
        has_coords = args.get("from_coordinate") and args.get("to_coordinate")
        if not has_elements and not has_coords:
            return json.dumps({
                "error": "drag requires from_coordinate/to_coordinate or from_element/to_element",
            })
        res = backend.drag(
            from_element=args.get("from_element"),
            to_element=args.get("to_element"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            button=args.get("button", "left"),
            modifiers=args.get("modifiers"),
            delivery_mode=delivery_mode, bring_to_front=bring_to_front,
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "scroll":
        coord = args.get("coordinate") or (None, None)
        res = backend.scroll(
            direction=args.get("direction", "down"),
            amount=int(args.get("amount", 3)),
            element=args.get("element"),
            x=coord[0] if coord and coord[0] is not None else None,
            y=coord[1] if coord and coord[1] is not None else None,
            modifiers=args.get("modifiers"),
            delivery_mode=delivery_mode, bring_to_front=bring_to_front,
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "type":
        res = backend.type_text(args.get("text", ""),
                                delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "key":
        res = backend.key(args.get("keys", ""),
                          delivery_mode=delivery_mode, bring_to_front=bring_to_front)
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "set_value":
        value = args.get("value")
        if value is None:
            return json.dumps({"error": "set_value requires `value`"})
        res = backend.set_value(value=str(value), element=args.get("element"))
        return _maybe_follow_capture(backend, res, capture_after)

    return json.dumps({"error": f"unknown action {action!r}"})


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

def _classify_action_result(res: ActionResult) -> Dict[str, Any]:
    """Choose the next ladder step from semantic evidence, in precedence order.

    An escalation recommendation is advisory. It never overrides a confirmed
    effect and it never turns an unverifiable action into permission to repeat
    input. The model must first obtain fresh evidence.
    """
    if res.effect == "confirmed" or res.verified is True:
        return {"decision": "done"}
    if res.effect == "unverifiable":
        return {"decision": "verify_fresh_state"}
    if res.effect == "suspected_noop" or not res.ok or res.code is not None:
        decision: Dict[str, Any] = {"decision": "escalate"}
        if isinstance(res.escalation, dict):
            decision["recommended"] = res.escalation.get("recommended")
        return decision
    # Transport success without semantic proof is not proof of effect.
    return {"decision": "verify_fresh_state"}


def _action_payload(res: ActionResult) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    # Surface cua-driver's structured verdict additively so the model can
    # follow the verify → escalate ladder. Only include fields the driver
    # actually returned (None = old driver / not carried). ok is transport
    # success; effect/escalation are the semantic verdict.
    if res.verified is not None:
        payload["verified"] = res.verified
    if res.effect is not None:
        payload["effect"] = res.effect
    if res.escalation is not None:
        payload["escalation"] = res.escalation
    if res.path is not None:
        payload["path"] = res.path
    if res.degraded is not None:
        payload["degraded"] = res.degraded
    if res.delivery_mode is not None:
        payload["delivery_mode"] = res.delivery_mode
    if res.code is not None:
        payload["code"] = res.code
    if res.meta:
        payload["meta"] = res.meta
    payload["verdict"] = _classify_action_result(res)
    return payload


def _text_response(res: ActionResult) -> str:
    return json.dumps(_action_payload(res))


# Default cap for the AX `elements` array returned by capture. Dense UIs
# (Electron apps, Obsidian, JetBrains IDEs) can publish 500+ AX nodes, which
# can exhaust session context after a single capture. The model-facing
# `max_elements` argument lets callers raise this when they need the full tree.
_DEFAULT_MAX_ELEMENTS = 100
# Hard upper bound on caller-supplied `max_elements`. Without this, a tool
# call passing a very large integer would silently disable the safeguard and
# reintroduce the original unbounded behavior.
_MAX_ALLOWED_MAX_ELEMENTS = 1000
_MIN_PROVIDER_IMAGE_DIMENSION = 8


def _image_dimensions_from_b64(image_b64: str) -> Optional[Tuple[int, int]]:
    """Return (width, height) for common inline screenshot formats.

    Some providers reject images below 8x8 before the model sees the tool
    result. Inspecting the encoded bytes here lets computer_use fall back to
    its AX/SOM text payload instead of sending an unusable placeholder.
    """
    if not image_b64:
        return None
    try:
        raw = base64.b64decode(image_b64, validate=False)
    except Exception:
        return None

    # PNG: signature + IHDR width/height.
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        try:
            width, height = struct.unpack(">II", raw[16:24])
            return int(width), int(height)
        except Exception:
            return None

    # JPEG: scan for SOF markers that carry dimensions.
    if raw.startswith(b"\xff\xd8") and len(raw) > 4:
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            i += 2
            while marker == 0xFF and i < len(raw):
                marker = raw[i]
                i += 1
            if marker in {0xD8, 0xD9}:
                continue
            if marker == 0xDA:
                break
            if i + 2 > len(raw):
                break
            segment_len = int.from_bytes(raw[i:i + 2], "big")
            if segment_len < 2 or i + segment_len > len(raw):
                break
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            } and segment_len >= 7:
                height = int.from_bytes(raw[i + 3:i + 5], "big")
                width = int.from_bytes(raw[i + 5:i + 7], "big")
                return int(width), int(height)
            i += segment_len
    return None


def _coerce_max_elements(value: Any) -> int:
    """Validate the caller-supplied ``max_elements``.

    Falls back to :data:`_DEFAULT_MAX_ELEMENTS` for missing / non-integer /
    sub-1 inputs so the cap can never be silently disabled by a malformed
    tool-call argument. Clamps oversized values to
    :data:`_MAX_ALLOWED_MAX_ELEMENTS` so a caller cannot bypass the
    safeguard by passing a very large integer.
    """
    if value is None:
        return _DEFAULT_MAX_ELEMENTS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ELEMENTS
    if n < 1:
        return _DEFAULT_MAX_ELEMENTS
    if n > _MAX_ALLOWED_MAX_ELEMENTS:
        return _MAX_ALLOWED_MAX_ELEMENTS
    return n


def _capture_response(cap: CaptureResult, max_elements: int = _DEFAULT_MAX_ELEMENTS) -> Any:
    total_elements = len(cap.elements)
    visible_elements = cap.elements[:max_elements]
    truncated_elements = max(0, total_elements - len(visible_elements))
    image_dimensions = _image_dimensions_from_b64(cap.png_b64 or "") if cap.png_b64 else None
    response_width = image_dimensions[0] if image_dimensions else cap.width
    response_height = image_dimensions[1] if image_dimensions else cap.height
    image_too_small = bool(
        image_dimensions
        and (
            image_dimensions[0] < _MIN_PROVIDER_IMAGE_DIMENSION
            or image_dimensions[1] < _MIN_PROVIDER_IMAGE_DIMENSION
        )
    )

    # Index only what's actually surfaced in the response — otherwise the
    # human-readable summary references element indices the model cannot
    # find in the JSON `elements` array (e.g. max_elements=10 vs the default
    # 40-line index window).
    element_index = _format_elements(visible_elements)
    summary_lines = [
        f"capture mode={cap.mode} {response_width}x{response_height}"
        + (f" app={cap.app}" if cap.app else "")
        + (f" window={cap.window_title!r}" if cap.window_title else ""),
        f"{total_elements} interactable element(s):",
    ]
    if element_index:
        summary_lines.extend(element_index)
    # Multimodal and AX paths both reference `summary`; build it once up-front
    # so the aux-vision routing branch (which fires before either path is
    # selected) has a valid value to hand to _route_capture_through_aux_vision.
    # The AX path appends the "truncated to N of M" note to summary_lines
    # below and rebuilds; the multimodal path keeps this version untouched.
    if image_too_small:
        summary_lines.append(
            f"  (screenshot omitted: {image_dimensions[0]}x{image_dimensions[1]} "
            f"is below the {_MIN_PROVIDER_IMAGE_DIMENSION}x{_MIN_PROVIDER_IMAGE_DIMENSION} "
            "provider minimum)"
        )
    summary = "\n".join(summary_lines)

    if cap.png_b64 and cap.mode != "ax" and not image_too_small:
        # Decide whether to hand the screenshot to the auxiliary.vision
        # pipeline (text-only result) or keep the multimodal envelope (main
        # model handles vision natively). Issue #24015: previously the
        # multimodal envelope was returned unconditionally, so non-vision
        # main models tripped HTTP 404 / 400 at the provider boundary even
        # when auxiliary.vision was explicitly configured to handle this.
        if _should_route_through_aux_vision():
            routed = _route_capture_through_aux_vision(cap, summary)
            if routed is not None:
                return routed
            # Aux routing was requested but failed (vision node down, aux call
            # raised, empty analysis, etc.). Routing being requested means the
            # main model may not be able to consume images; falling through to
            # the multimodal envelope can break the capture with a provider
            # error. Degrade to the AX/SOM text payload instead so element
            # indices remain usable while vision is unavailable.
            summary_lines.append(
                "  (vision unavailable: the auxiliary vision model could not "
                "be reached; screenshot omitted. Element-index actions still "
                "work — drive via the element list above.)"
            )
            if truncated_elements:
                summary_lines.append(
                    f"  (response truncated to {len(visible_elements)} of "
                    f"{total_elements} elements; raise max_elements or pass "
                    "app= to narrow)"
                )
            payload = {
                "mode": cap.mode,
                "width": response_width,
                "height": response_height,
                "app": cap.app,
                "window_title": cap.window_title,
                "elements": [_element_to_dict(e) for e in visible_elements],
                "total_elements": total_elements,
                "summary": "\n".join(summary_lines),
                "vision_unavailable": True,
            }
            if truncated_elements:
                payload["truncated_elements"] = truncated_elements
            return json.dumps(payload)

        # Prefer the explicit MIME type cua-driver attaches to its image
        # parts (Surface 7 of NousResearch/hermes-agent#47072 — trycua/cua#1961
        # made `mimeType` part of every MCP image-part response). Fall back
        # to base64-prefix sniffing for older cua-driver builds that didn't
        # carry the field. JPEG base64 starts with /9j/; PNG with iVBOR.
        _mime = cap.image_mime_type
        if not _mime:
            _b64_prefix = cap.png_b64[:8]
            _mime = "image/jpeg" if _b64_prefix.startswith("/9j/") else "image/png"
        # The multimodal response carries the screenshot, not the AX
        # elements array, so a "response truncated to N of M elements"
        # note would be inaccurate — skip it on this branch.
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url",
                 "image_url": {"url": f"data:{_mime};base64,{cap.png_b64}"}},
            ],
            "text_summary": summary,
            "meta": {"mode": cap.mode, "width": response_width, "height": response_height,
                     "elements": total_elements, "png_bytes": cap.png_bytes_len},
        }
    # AX-only (or image-missing fallback): text path actually carries the
    # `elements` array, so the truncation note applies here.
    if truncated_elements:
        summary_lines.append(
            f"  (response truncated to {len(visible_elements)} of {total_elements} elements; "
            f"raise max_elements or pass app= to narrow)"
        )
    summary = "\n".join(summary_lines)
    payload: Dict[str, Any] = {
        "mode": cap.mode,
        "width": response_width,
        "height": response_height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in visible_elements],
        "total_elements": total_elements,
        "summary": summary,
    }
    if truncated_elements:
        payload["truncated_elements"] = truncated_elements
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# auxiliary.vision routing for captured screenshots (#24015)
# ---------------------------------------------------------------------------

# Longest image side handed to the aux vision model. Full-resolution desktop
# captures tokenize heavily and can overflow small local-model context windows;
# ~1456px keeps SOM badges legible while cutting per-capture vision latency.
_MAX_VISION_DIM = 1456


def _shrink_capture_for_vision(raw: bytes, ext: str,
                               max_dim: int = _MAX_VISION_DIM) -> bytes:
    """Downscale encoded image bytes so the longest side is <= max_dim.

    Returns the original bytes unchanged when the image already fits or when
    Pillow is unavailable/fails — no worse than the pre-shrink behavior.
    """
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if max(img.size) <= max_dim:
            return raw
        img.thumbnail((max_dim, max_dim))
        out = BytesIO()
        img.save(out, format="JPEG" if ext == ".jpg" else "PNG")
        return out.getvalue()
    except Exception as exc:
        logger.debug("computer_use: vision downscale skipped: %s", exc)
        return raw

def _should_route_through_aux_vision() -> bool:
    """Return True when ``_capture_response`` should hand the PNG to aux vision.

    Reads the active main provider/model and the loaded config and asks the
    routing helper. Any failure (config import, runtime override missing,
    etc.) returns False so the existing multimodal envelope continues to be
    returned — fail open on the routing decision so a broken config can
    never silently drop the screenshot for vision-capable main models.
    """
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider
        from hermes_cli.config import load_config
        from tools.computer_use.vision_routing import (
            should_route_capture_to_aux_vision,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing import failed: %s", exc)
        return False
    try:
        provider = _read_main_provider() or ""
        model = _read_main_model() or ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing config read failed: %s", exc)
        return False
    cache_key = (str(provider), str(model))
    cached = _AUX_VISION_ROUTE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        cfg = load_config()
        decision = bool(should_route_capture_to_aux_vision(provider, model, cfg))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing decision failed: %s", exc)
        return False
    _AUX_VISION_ROUTE_CACHE[cache_key] = decision
    return decision


def _capture_after_mode() -> str:
    """Mode for ``capture_after`` follow-ups. Default ``som`` (screenshot)."""
    try:
        from hermes_cli.config import load_config

        raw = ((load_config() or {}).get("computer_use") or {}).get(
            "capture_after_mode", "som"
        )
    except Exception:
        return "som"
    mode = str(raw or "som").strip().lower()
    return mode if mode in {"som", "vision", "ax"} else "som"


def _route_capture_through_aux_vision(
    cap: CaptureResult,
    summary: str,
) -> Optional[str]:
    """Pre-analyse the captured PNG via ``vision_analyze`` and return a text result.

    The captured base64 PNG is materialised to ``$HERMES_HOME/cache/vision/``
    and handed to ``vision_analyze_tool`` with a generic describe prompt.
    The resulting text description is merged into the existing AX/SOM
    summary so the main model receives a single text payload that mentions
    every interactable element AND a description of what the screenshot
    looked like.

    Returns:
      A JSON-encoded text response on success.
      ``None`` on failure (caller falls back to the multimodal envelope).
    """
    if not cap.png_b64:
        return None
    try:
        import base64 as _base64
        import os as _os
        import uuid as _uuid

        from hermes_constants import get_hermes_dir
        from model_tools import _run_async
        from tools.vision_tools import vision_analyze_tool
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision import failed: %s", exc)
        return None

    temp_image_path = None
    try:
        try:
            raw = _base64.b64decode(cap.png_b64, validate=False)
        except Exception as exc:
            logger.debug("computer_use: failed to decode capture base64: %s", exc)
            return None

        # Pick an extension that matches the on-disk bytes so vision_analyze's
        # MIME sniffing returns the right content-type.
        # Surface 7: prefer the explicit MIME type cua-driver supplied.
        _mime_for_ext = cap.image_mime_type or ""
        if _mime_for_ext == "image/jpeg" or (not _mime_for_ext and cap.png_b64[:8].startswith("/9j/")):
            ext = ".jpg"
        else:
            ext = ".png"
        cache_dir = get_hermes_dir("cache/vision", "temp_vision_images")
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_image_path = cache_dir / f"computer_use_{_uuid.uuid4().hex}{ext}"
        raw = _shrink_capture_for_vision(raw, ext)
        temp_image_path.write_bytes(raw)

        prompt = (
            "Describe what is visible in this desktop application screenshot in "
            "concise but specific terms. Mention the app name and window "
            "title if visible, the overall layout, any labelled buttons, "
            "menus or text fields, and any prominent text content the user "
            "would need to know about. Do not invent details that are not "
            "actually visible.\n\n"
            f"AX/SOM index for cross-reference:\n{summary}"
        )

        result_json = _run_async(
            vision_analyze_tool(str(temp_image_path), prompt)
        )
    except Exception as exc:
        logger.warning(
            "computer_use: auxiliary.vision pre-analysis failed (%s); "
            "returning to caller without aux analysis",
            exc,
        )
        return None
    finally:
        if temp_image_path is not None:
            try:
                _os.unlink(str(temp_image_path))
            except Exception:
                pass

    analysis_text = ""
    if isinstance(result_json, str):
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                analysis_text = str(parsed.get("analysis") or "").strip()
        except (TypeError, json.JSONDecodeError):
            analysis_text = result_json.strip()

    if not analysis_text:
        return None

    return json.dumps({
        "mode": cap.mode,
        "width": cap.width,
        "height": cap.height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in cap.elements],
        "summary": summary,
        "vision_analysis": analysis_text,
        "vision_analysis_routed_via": "auxiliary.vision",
    })


def _maybe_follow_capture(
    backend: ComputerUseBackend, res: ActionResult, do_capture: bool,
) -> Any:
    if not do_capture:
        return _text_response(res)
    # Skip the follow-up capture when the action itself failed: showing a
    # normal-looking screenshot after a failure misleads the model into thinking
    # the action succeeded. Return the error text instead.
    if not res.ok:
        return _text_response(res)
    try:
        # Preserve the exact selected window when possible. Linux may expose a
        # generic app name for several unrelated windows, so app-only recapture
        # can silently switch targets after a successful action.
        target = getattr(backend, "_last_target", None) or {}
        pid = target.get("pid")
        window_id = target.get("window_id")
        mode = _capture_after_mode()
        if pid is not None and window_id is not None:
            cap = backend.capture(mode=mode, pid=pid, window_id=window_id)
        else:
            cap = backend.capture(mode=mode, app=getattr(backend, "_last_app", None))
    except Exception as e:
        logger.warning("follow-up capture failed: %s", e)
        return _text_response(res)
    # Combine action summary with the capture.
    resp = _capture_response(cap)
    if isinstance(resp, dict) and resp.get("_multimodal"):
        # Keep the complete evidence/verdict contract visible when an image is
        # attached; otherwise capture_after would accidentally discard the
        # very signal that governs whether repeating input is allowed.
        prefix = json.dumps(_action_payload(res))
        resp["content"][0]["text"] = prefix + "\n\n" + resp["content"][0]["text"]
        resp["text_summary"] = prefix + "\n\n" + resp["text_summary"]
        resp["action_result"] = _action_payload(res)
        return resp
    # Fallback: action + text capture merged.
    try:
        data = json.loads(resp)
    except (TypeError, json.JSONDecodeError):
        data = {"capture": resp}
    data.update(_action_payload(res))
    return json.dumps(data)


def _format_elements(elements: List[UIElement], max_lines: int = 40) -> List[str]:
    out: List[str] = []
    for e in elements[:max_lines]:
        label = e.label.replace("\n", " ")[:60]
        out.append(f"  #{e.index} {e.role} {label!r} @ {e.bounds}"
                   + (f" [{e.app}]" if e.app else ""))
    if len(elements) > max_lines:
        out.append(f"  ... +{len(elements) - max_lines} more (call capture with app= to narrow)")
    return out


def _element_to_dict(e: UIElement) -> Dict[str, Any]:
    return {
        "index": e.index,
        "role": e.role,
        "label": e.label,
        "bounds": list(e.bounds),
        "app": e.app,
    }


# ---------------------------------------------------------------------------
# Availability check (used by the tool registry check_fn)
# ---------------------------------------------------------------------------

def check_computer_use_requirements() -> bool:
    """Return True iff computer_use can run on this host.

    Conditions: macOS, Windows, or Linux + cua-driver binary installed (or
    override via env). cua-driver runs on all three; the Linux path is
    headed/X11 today (Wayland via XWayland), pure-Wayland progress tracked
    upstream. Linux users see specific blocked checks via
    `hermes computer-use doctor` if their session is incomplete (e.g. no
    DISPLAY set).
    """
    if sys.platform not in ("darwin", "win32", "linux"):
        return False
    from tools.computer_use.cua_backend import cua_driver_binary_available
    return cua_driver_binary_available()


def get_computer_use_schema() -> Dict[str, Any]:
    from tools.computer_use.schema import COMPUTER_USE_SCHEMA
    return COMPUTER_USE_SCHEMA
