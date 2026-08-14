"""Durable, resumable session-turn API support.

This is an API-edge contract, not a model tool.  Turn/outbox metadata and safe
SSE frames share ``state.db`` with SessionDB, while SessionDB messages remain
the only transcript.  No input or final-response columns exist in the turn
ledger.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import hashlib
import io
import json
import logging
import queue
import re
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, TypeVar

from aiohttp import web

from gateway.session_execution_lease import SessionExecutionConflict


logger = logging.getLogger(__name__)
_StoreResult = TypeVar("_StoreResult")

SESSION_TURN_HEARTBEAT_INTERVAL_SECONDS = 15.0
SESSION_TURN_LIFECYCLE_QUEUE_SIZE = 32
SESSION_TURN_LIFECYCLE_WORKERS = 4
SESSION_TURN_LIFECYCLE_PENDING_DISPATCHERS = 32
SESSION_TURN_LIFECYCLE_SLOW_SECONDS = 0.1
SESSION_TURN_LIFECYCLE_FLUSH_SECONDS = 0.05
SESSION_TURN_CALLBACK_QUEUE_SIZE = 32
SESSION_TURN_CALLBACK_ENQUEUE_TIMEOUT_SECONDS = 1.0


class _CallbackEventClass(Enum):
    """Persistence guarantees for events emitted by synchronous agent callbacks."""

    STRUCTURAL = "structural"
    VOLATILE = "volatile"


@dataclass(frozen=True)
class _CallbackPersistenceEvent:
    event_class: _CallbackEventClass
    event_type: str
    data: Dict[str, Any]
    structural_sequence: Optional[int] = None


@dataclass
class _StructuralSubmission:
    """Handshake that makes timeout cancellation and enqueue mutually exclusive."""

    event: _CallbackPersistenceEvent
    lock: threading.Lock
    cancelled: bool = False
    enqueued: bool = False


class _CallbackPersistenceOverloaded(RuntimeError):
    """Stop a producer after retaining an exact structural resync range."""


class _CallbackPersistenceIngress:
    """Bounded sync-callback ingress to one loop-owned persistence pump.

    Structural callbacks apply bounded backpressure. If a callback cannot be
    retained, one out-of-band resync range records every rejected structural
    ordinal; the pump persists that marker after all retained events and before
    terminal truth. Volatile deltas use one coalescing loop notification.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int):
        self.loop = loop
        self.queue: asyncio.Queue[_CallbackPersistenceEvent] = asyncio.Queue(
            maxsize=max(1, maxsize)
        )
        self.done = asyncio.Event()
        self.pump_task: Optional[asyncio.Task[Any]] = None
        self.max_observed = 0
        self.resync_required = False
        self._loop_thread_id = threading.get_ident()
        self._volatile_lock = threading.Lock()
        self._pending_volatile: Optional[_CallbackPersistenceEvent] = None
        self._volatile_notification_pending = False
        self._accepting = True
        self._structural_lock = threading.Lock()
        self._next_structural_sequence = 1
        self._structural_resync_range: Optional[tuple[int, int]] = None
        self._producer_stopped = False

    def bind_pump(self, pump_task: asyncio.Task[Any]) -> None:
        self.pump_task = pump_task

    def append_structural(self, event_type: str, data: Dict[str, Any]) -> None:
        # Serialize producers so the first rejected ordinal closes ingress and
        # every later rejected fact extends one exact contiguous range.
        with self._structural_lock:
            sequence = self._next_structural_sequence
            self._next_structural_sequence += 1
            event = _CallbackPersistenceEvent(
                _CallbackEventClass.STRUCTURAL,
                event_type,
                dict(data),
                structural_sequence=sequence,
            )
            if self._producer_stopped or not self._accepting:
                self._mark_structural_resync(sequence)
                raise _CallbackPersistenceOverloaded("callback persistence closed")
            if threading.get_ident() == self._loop_thread_id:
                try:
                    self.queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    self._mark_structural_resync(sequence)
                    raise _CallbackPersistenceOverloaded(
                        "structural callback persistence saturated"
                    ) from exc
                self._observe_size()
                return
            self._append_structural_from_callback_thread(event, sequence)

    def _append_structural_from_callback_thread(
        self, event: _CallbackPersistenceEvent, sequence: int
    ) -> None:
        if self.loop.is_closed() or not self.loop.is_running():
            self._mark_structural_resync(sequence)
            raise _CallbackPersistenceOverloaded("callback persistence loop unavailable")
        submission = _StructuralSubmission(event=event, lock=threading.Lock())
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._put_structural(submission), self.loop
            )
        except RuntimeError as exc:
            self._mark_structural_resync(sequence)
            raise _CallbackPersistenceOverloaded(
                "callback persistence loop unavailable"
            ) from exc
        deadline = time.monotonic() + SESSION_TURN_CALLBACK_ENQUEUE_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.loop.is_closed() or not self.loop.is_running():
                with submission.lock:
                    if submission.enqueued:
                        return
                    submission.cancelled = True
                future.cancel()
                self._mark_structural_resync(sequence)
                raise _CallbackPersistenceOverloaded(
                    "structural callback persistence unavailable"
                )
            try:
                future.result(timeout=min(0.05, remaining))
                return
            except concurrent.futures.TimeoutError:
                continue
            except Exception as exc:
                with submission.lock:
                    enqueued = submission.enqueued
                if enqueued:
                    return
                self._mark_structural_resync(sequence)
                raise _CallbackPersistenceOverloaded(
                    "callback persistence unavailable"
                ) from exc

    async def _put_structural(self, submission: _StructuralSubmission) -> None:
        while self._accepting:
            if self.pump_task is not None and self.pump_task.done():
                raise _CallbackPersistenceOverloaded("callback persistence unavailable")
            with submission.lock:
                if submission.cancelled:
                    raise _CallbackPersistenceOverloaded(
                        "callback persistence enqueue cancelled"
                    )
                try:
                    self.queue.put_nowait(submission.event)
                except asyncio.QueueFull:
                    pass
                else:
                    submission.enqueued = True
                    self._observe_size()
                    return
            await asyncio.sleep(0.01)
        raise _CallbackPersistenceOverloaded("callback persistence closed")

    def _mark_structural_resync(self, sequence: int) -> None:
        self._producer_stopped = True
        if self._structural_resync_range is None:
            self._structural_resync_range = (sequence, sequence)
        else:
            first, last = self._structural_resync_range
            self._structural_resync_range = (min(first, sequence), max(last, sequence))

    @property
    def structural_resync_range(self) -> Optional[tuple[int, int]]:
        with self._structural_lock:
            return self._structural_resync_range

    def append_volatile(self, event_type: str, data: Dict[str, Any]) -> None:
        event = _CallbackPersistenceEvent(
            _CallbackEventClass.VOLATILE, event_type, dict(data)
        )
        if threading.get_ident() == self._loop_thread_id:
            if not self._accepting:
                return
            try:
                self.queue.put_nowait(event)
                self._observe_size()
            except asyncio.QueueFull:
                self.resync_required = True
            return
        with self._volatile_lock:
            if not self._accepting:
                return
            if self._pending_volatile is not None:
                # Do not concatenate bodies: retained callback memory stays fixed.
                self.resync_required = True
                return
            self._pending_volatile = event
            if self._volatile_notification_pending:
                return
            self._volatile_notification_pending = True
        self.loop.call_soon_threadsafe(self._flush_volatile)

    def _flush_volatile(self) -> None:
        with self._volatile_lock:
            event = self._pending_volatile
            self._pending_volatile = None
            self._volatile_notification_pending = False
        if event is None:
            return
        if not self._accepting:
            self.resync_required = True
            return
        try:
            self.queue.put_nowait(event)
            self._observe_size()
        except asyncio.QueueFull:
            self.resync_required = True

    def close(self) -> None:
        """Run on the loop after the agent exits; retain or mark the last delta."""
        with self._volatile_lock:
            self._accepting = False
            pending = self._pending_volatile
            self._pending_volatile = None
        if pending is not None:
            try:
                self.queue.put_nowait(pending)
                self._observe_size()
            except asyncio.QueueFull:
                self.resync_required = True
        self.done.set()

    def _observe_size(self) -> None:
        self.max_observed = max(self.max_observed, self.queue.qsize())


@dataclass
class _TurnExecutionState:
    """Authoritative phase reached only after the corresponding durable write."""

    entered: bool = False
    started: bool = False
    execution_done: bool = False

    @property
    def cancellation_code(self) -> str:
        if not self.started:
            return "cancelled_before_execution"
        if self.execution_done:
            return "cancelled_after_execution"
        return "cancelled_during_execution"


def _emit_turn_lifecycle(event: str, session_id: str, turn_id: str, **extra: Any) -> bool:
    """Invoke the provider boundary. Call only from an observer worker thread."""
    try:
        from agent.lifecycle_hooks import emit_session_turn_lifecycle

        return emit_session_turn_lifecycle(
            event,
            session_id=session_id,
            turn_id=turn_id,
            **extra,
        )
    except Exception:
        logger.warning(
            "session_turn_lifecycle_failed event=%s reason=invalid_or_callback_error",
            event,
        )
        return False


_lifecycle_metric_lock = threading.Lock()
_lifecycle_metrics = {
    "heartbeat_dropped": 0,
    "observer_failed": 0,
    "observer_slow": 0,
    "observer_capacity_exhausted": 0,
    "terminal_enqueue_failed": 0,
}


def _note_lifecycle_degradation(metric: str, event: str, reason: str) -> None:
    """Record a privacy-safe provider degradation without touching execution."""
    with _lifecycle_metric_lock:
        _lifecycle_metrics[metric] = _lifecycle_metrics.get(metric, 0) + 1
    logger.warning(
        "session_turn_lifecycle_degraded event=%s reason=%s metric=%s",
        event,
        reason,
        metric,
    )


def session_turn_lifecycle_metrics() -> Dict[str, int]:
    """Return process-local safe counters for monitoring/tests."""
    with _lifecycle_metric_lock:
        return dict(_lifecycle_metrics)


class _SessionTurnLifecycleExecutor:
    """Process-wide fixed observer capacity with a bounded pending queue.

    Python cannot forcibly stop a plugin callback. A hung callback therefore
    occupies one of a fixed number of daemon workers, while the bounded queue
    prevents completed turns from accumulating behind it forever. Once both
    bounds are exhausted, new lifecycle streams explicitly degrade fail-open.
    """

    def __init__(self, workers: int, pending: int):
        self.worker_count = max(1, workers)
        self.pending_capacity = max(1, pending)
        self._queue: queue.Queue[SessionTurnLifecycleDispatcher] = queue.Queue(
            maxsize=self.pending_capacity
        )
        self._start_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._started = False
        self._active = 0
        self._next_observation = 0
        self._observations: Dict[int, tuple[str, float, bool]] = {}
        # Pay the fixed worker startup cost at module initialization, never on
        # a latency-sensitive turn-admission path.
        self._start_workers()
        threading.Thread(
            target=self._watch_slow_callbacks,
            name="session-lifecycle-watchdog",
            daemon=True,
        ).start()

    def submit(self, dispatcher: "SessionTurnLifecycleDispatcher") -> bool:
        try:
            self._queue.put_nowait(dispatcher)
            return True
        except queue.Full:
            return False

    def stats(self) -> Dict[str, int]:
        with self._stats_lock:
            active = self._active
        return {
            "workers": self.worker_count,
            "active": active,
            "pending": self._queue.qsize(),
            "pending_capacity": self.pending_capacity,
        }

    def observe_callback_started(self, event: str) -> int:
        with self._stats_lock:
            self._next_observation += 1
            token = self._next_observation
            self._observations[token] = (event, time.monotonic(), False)
            return token

    def observe_callback_finished(self, token: int) -> None:
        with self._stats_lock:
            self._observations.pop(token, None)

    def _start_workers(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            for index in range(self.worker_count):
                threading.Thread(
                    target=self._worker,
                    name=f"session-turn-lifecycle-{index}",
                    daemon=True,
                ).start()
            self._started = True

    def _worker(self) -> None:
        while True:
            dispatcher = self._queue.get()
            with self._stats_lock:
                self._active += 1
            try:
                dispatcher._run()
            finally:
                with self._stats_lock:
                    self._active -= 1
                self._queue.task_done()

    def _watch_slow_callbacks(self) -> None:
        """Report hung callbacks from one fixed watchdog, never per-turn timers."""
        while True:
            time.sleep(max(0.01, SESSION_TURN_LIFECYCLE_SLOW_SECONDS / 2))
            now = time.monotonic()
            slow_events = []
            with self._stats_lock:
                for token, (event, started_at, reported) in list(
                    self._observations.items()
                ):
                    if not reported and now - started_at >= SESSION_TURN_LIFECYCLE_SLOW_SECONDS:
                        self._observations[token] = (event, started_at, True)
                        slow_events.append(event)
            for event in slow_events:
                _note_lifecycle_degradation(
                    "observer_slow", event, "callback_slow"
                )


_lifecycle_executor = _SessionTurnLifecycleExecutor(
    SESSION_TURN_LIFECYCLE_WORKERS,
    SESSION_TURN_LIFECYCLE_PENDING_DISPATCHERS,
)


def session_turn_lifecycle_executor_stats() -> Dict[str, int]:
    """Return privacy-safe fixed-capacity observer executor diagnostics."""
    return _lifecycle_executor.stats()


class SessionTurnLifecycleDispatcher:
    """Ordered, bounded, non-blocking observer delivery for one durable turn.

    Plugin code runs only on the process-wide fixed daemon pool. Heartbeats are
    lossy under backpressure, while the structural registered/started/terminal
    facts are retained. A slow or permanently hung plugin therefore cannot
    hold aiohttp, the coordinator, or the persistent execution lease.
    """

    def __init__(self, session_id: str, turn_id: str):
        self.session_id = session_id
        self.turn_id = turn_id
        self._queue: queue.Queue[tuple[str, Dict[str, Any]]] = queue.Queue(
            maxsize=max(4, SESSION_TURN_LIFECYCLE_QUEUE_SIZE)
        )
        self._closed = False
        self._drained = threading.Event()
        # Registered is admitted before the worker can observe later events.
        self._queue.put_nowait(("registered", {}))
        self._admitted = _lifecycle_executor.submit(self)
        if not self._admitted:
            self._closed = True
            self._drained.set()
            _note_lifecycle_degradation(
                "observer_capacity_exhausted", "registered", "executor_saturated"
            )

    def emit(self, event: str, **extra: Any) -> bool:
        """Queue an event without waiting; return whether it was retained."""
        if self._closed:
            _note_lifecycle_degradation("observer_failed", event, "dispatcher_closed")
            return False
        try:
            self._queue.put_nowait((event, dict(extra)))
            return True
        except queue.Full:
            if event == "heartbeat":
                _note_lifecycle_degradation("heartbeat_dropped", event, "queue_full")
                return False
            # Structural events must not be displaced by observer latency.
            # At capacity after registered/started, at least one queued item is
            # a heartbeat; evict the oldest heartbeat without blocking.
            with self._queue.mutex:
                for index, item in enumerate(self._queue.queue):
                    if item[0] == "heartbeat":
                        del self._queue.queue[index]
                        self._queue.not_full.notify()
                        break
                else:
                    _note_lifecycle_degradation(
                        "terminal_enqueue_failed" if event == "terminal" else "observer_failed",
                        event,
                        "structural_queue_full",
                    )
                    return False
            self._queue.put_nowait((event, dict(extra)))
            return True

    def close(self, terminal_outcome: str) -> bool:
        """Queue exactly one terminal and reject all later events."""
        if self._closed:
            return False
        accepted = self.emit("terminal", terminal_outcome=terminal_outcome)
        self._closed = True
        return accepted

    def abort(self) -> bool:
        """Stop delivery without inventing a non-durable terminal fact."""
        if self._closed:
            return False
        accepted = self.emit("_close")
        self._closed = True
        return accepted

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait for terminal delivery. Never call directly on the event loop."""
        return self._drained.wait(timeout)

    def _run(self) -> None:
        while True:
            event, extra = self._queue.get()
            if event == "_close":
                self._drained.set()
                return
            observation = _lifecycle_executor.observe_callback_started(event)
            try:
                delivered = _emit_turn_lifecycle(
                    event,
                    self.session_id,
                    self.turn_id,
                    **extra,
                )
                if not delivered:
                    _note_lifecycle_degradation(
                        "observer_failed", event, "callback_error"
                    )
            except Exception:
                # Defensive: _emit_turn_lifecycle is already fail-open.
                _note_lifecycle_degradation("observer_failed", event, "callback_error")
            finally:
                _lifecycle_executor.observe_callback_finished(observation)
            if event == "terminal":
                self._drained.set()
                return


DELIVERY_MODES = frozenset({"occ_only", "slack_only", "both"})
ACTIVE_STATUSES = frozenset({"queued", "running", "stopping"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})
MAX_TURN_ID_LENGTH = 128
MAX_INPUT_TEXT_LENGTH = 65_536
MAX_INLINE_IMAGES = 4
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_INLINE_IMAGE_TOTAL_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_FRAMES = 256
MAX_SLACK_ROUTED_CHARS = 3_000
INLINE_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_TURN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DATA_IMAGE_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)$",
    re.IGNORECASE,
)


class TurnInputError(ValueError):
    """The submitted turn body is invalid."""


class ConflictingTurn(RuntimeError):
    """A turn id was reused with a different request fingerprint."""


class TurnConflict(RuntimeError):
    """A session already has an active turn."""


class SlackBindingError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SlackBinding:
    chat_id: str
    thread_id: Optional[str]
    metadata: Dict[str, Any]


def _safe_tool_label(tool_name: Any) -> str:
    name = str(tool_name or "tool")
    name = re.sub(r"[^A-Za-z0-9 _.-]", "", name)[:64].strip() or "tool"
    return name.replace("_", " ").replace("-", " ").title()


def project_safe_tool_event(event_type: str, **event: Any) -> Dict[str, Any]:
    """Project an internal callback to the deliberately tiny public schema."""
    statuses = {
        "tool.started": "running",
        "tool.completed": "completed",
        "tool.failed": "failed",
    }
    data: Dict[str, Any] = {
        "tool_call_id": str(event.get("tool_call_id") or "")[:128],
        "label": _safe_tool_label(event.get("tool_name")),
        "status": statuses.get(event_type, "failed"),
    }
    if event_type == "tool.failed":
        data["error_code"] = "tool_failed"
    return {"event": event_type, "data": data}


def _validated_image_dimensions(decoded: bytes, declared_mime: str) -> tuple[int, int]:
    """Verify the container and fully decode pixels with Pillow."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise TurnInputError("Inline image decoder is unavailable") from exc

    expected_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/gif": "GIF",
        "image/webp": "WEBP",
    }[declared_mime]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(decoded)) as image:
                if image.format != expected_format:
                    raise TurnInputError("Image magic/MIME mismatch")
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width > MAX_IMAGE_DIMENSION
                    or height > MAX_IMAGE_DIMENSION
                ):
                    raise TurnInputError("Image dimensions exceed the safe limit")
                if width * height > MAX_IMAGE_PIXELS:
                    raise TurnInputError("Image dimensions exceed the safe pixel limit")
                image.verify()
            # ``verify`` checks container integrity but deliberately does not
            # decode image data. Re-open and load every frame/pixel so corrupt,
            # truncated, and invalid compressed payloads fail before admission.
            with Image.open(io.BytesIO(decoded)) as image:
                if image.format != expected_format or image.size != (width, height):
                    raise TurnInputError("Image payload changed during verification")
                frame_count = int(getattr(image, "n_frames", 1) or 1)
                if frame_count > MAX_IMAGE_FRAMES:
                    raise TurnInputError("Image frame count exceeds the safe limit")
                for frame in range(frame_count):
                    image.seek(frame)
                    image.load()
    except TurnInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        # Pillow rejects extreme pixel counts while opening the container,
        # before our explicit width/height checks can run.  Keep that bounded
        # input failure deterministic instead of misclassifying it as corrupt
        # image data.
        raise TurnInputError("Image dimensions exceed the safe pixel limit") from exc
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise TurnInputError("Image payload is malformed, truncated, or unsafe") from exc
    return width, height


def _image_decoder_readiness_probe() -> bool:
    try:
        from PIL import Image

        probe = io.BytesIO()
        Image.new("RGB", (1, 1), "black").save(probe, format="PNG")
        return _validated_image_dimensions(probe.getvalue(), "image/png") == (1, 1)
    except Exception:
        return False


IMAGE_DECODER_READY = _image_decoder_readiness_probe()


def _normalize_inline_image(value: Any) -> tuple[str, int]:
    if not IMAGE_DECODER_READY:
        raise TurnInputError("Inline image decoder is unavailable")
    if isinstance(value, dict):
        value = value.get("data_url") or value.get("url") or value.get("image_url")
    if not isinstance(value, str) or not value.lower().startswith("data:image/"):
        raise TurnInputError("Images must be bounded inline data:image/...;base64 payloads")
    match = _DATA_IMAGE_RE.fullmatch(value.strip())
    if not match:
        raise TurnInputError("Invalid inline image encoding or unsupported image MIME type")
    mime = match.group(1).lower()
    if mime not in INLINE_IMAGE_MIME_TYPES:
        raise TurnInputError("Unsupported inline image MIME type")
    try:
        decoded = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TurnInputError("Invalid inline image base64") from exc
    size = len(decoded)
    if size <= 0 or size > MAX_INLINE_IMAGE_BYTES:
        raise TurnInputError("Inline image exceeds the per-image size limit")
    _validated_image_dimensions(decoded, mime)
    canonical = f"data:{mime};base64,{base64.b64encode(decoded).decode('ascii')}"
    return canonical, size


def normalize_turn_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise TurnInputError("Request body must be an object")
    allowed = {"turn_id", "input", "images", "delivery_mode"}
    unknown = set(body) - allowed
    if unknown:
        raise TurnInputError(f"Unsupported turn field: {sorted(unknown)[0]}")
    mode = str(body.get("delivery_mode") or "occ_only").strip().lower()
    if mode not in DELIVERY_MODES:
        raise TurnInputError("delivery_mode must be occ_only, slack_only, or both")

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        text = raw_input
        raw_images = body.get("images") or []
    elif isinstance(raw_input, dict):
        unknown_input = set(raw_input) - {"text", "images"}
        if unknown_input:
            raise TurnInputError(f"Unsupported input field: {sorted(unknown_input)[0]}")
        text = raw_input.get("text") or ""
        raw_images = raw_input.get("images") or []
    else:
        raise TurnInputError("input must be text or an object with text and images")
    if not isinstance(text, str):
        raise TurnInputError("input.text must be a string")
    if len(text) > MAX_INPUT_TEXT_LENGTH:
        raise TurnInputError("input.text exceeds the length limit")
    if not isinstance(raw_images, list):
        raise TurnInputError("input.images must be an array")
    if len(raw_images) > MAX_INLINE_IMAGES:
        raise TurnInputError("Too many inline images")

    images = []
    total = 0
    for image in raw_images:
        normalized, size = _normalize_inline_image(image)
        total += size
        if total > MAX_INLINE_IMAGE_TOTAL_BYTES:
            raise TurnInputError("Inline images exceed the total size limit")
        images.append(normalized)
    if not text.strip() and not images:
        raise TurnInputError("Turn input cannot be empty")
    return {"input": {"text": text, "images": images}, "delivery_mode": mode}


def _fingerprint(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _agent_input(payload: Dict[str, Any]) -> Any:
    text = payload["input"]["text"]
    images = payload["input"]["images"]
    if not images:
        return text
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend({"type": "image_url", "image_url": {"url": image}} for image in images)
    return parts


class SessionTurnStore:
    """Transactional turn/event/outbox ledger stored inside SessionDB.state.db."""

    def __init__(self, session_db: Any):
        self.db = session_db
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        def create(conn):
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_session_turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    delivery_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    safe_error_code TEXT,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    effective_session_id TEXT,
                    assistant_message_id TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_api_session_turns_one_active
                    ON api_session_turns(session_id)
                    WHERE status IN ('queued', 'running', 'stopping');
                CREATE INDEX IF NOT EXISTS idx_api_session_turns_session_created
                    ON api_session_turns(session_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS api_session_turn_events (
                    turn_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(turn_id, seq),
                    FOREIGN KEY(turn_id) REFERENCES api_session_turns(turn_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS api_session_turn_outbox (
                    turn_id TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    delivery_id TEXT,
                    provider_message_id TEXT,
                    state TEXT NOT NULL,
                    safe_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(turn_id, destination),
                    FOREIGN KEY(turn_id) REFERENCES api_session_turns(turn_id) ON DELETE CASCADE
                );
                """
            )
            outbox_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(api_session_turn_outbox)").fetchall()
            }
            if "delivery_id" not in outbox_columns:
                conn.execute("ALTER TABLE api_session_turn_outbox ADD COLUMN delivery_id TEXT")
            if "provider_message_id" not in outbox_columns:
                conn.execute(
                    "ALTER TABLE api_session_turn_outbox ADD COLUMN provider_message_id TEXT"
                )
            turn_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(api_session_turns)").fetchall()
            }
            if "assistant_message_id" not in turn_columns:
                conn.execute(
                    "ALTER TABLE api_session_turns ADD COLUMN assistant_message_id TEXT"
                )
        self.db._execute_write(create)

    @staticmethod
    def validate_turn_id(turn_id: Any) -> str:
        value = str(turn_id or "").strip()
        if len(value) > MAX_TURN_ID_LENGTH or not _TURN_ID_RE.fullmatch(value):
            raise TurnInputError("Invalid turn_id / Idempotency-Key")
        return value

    def reserve(self, session_id: str, turn_id: str, body: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        turn_id = self.validate_turn_id(turn_id)
        payload = normalize_turn_payload(body)
        fingerprint = _fingerprint(payload)
        now = time.time()

        def reserve_tx(conn):
            existing = conn.execute(
                "SELECT * FROM api_session_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing:
                row = dict(existing)
                if row["session_id"] != session_id or row["fingerprint"] != fingerprint:
                    raise ConflictingTurn(turn_id)
                return row, False
            session = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise KeyError(session_id)
            active = conn.execute(
                "SELECT turn_id FROM api_session_turns WHERE session_id = ? "
                "AND status IN ('queued','running','stopping') LIMIT 1",
                (session_id,),
            ).fetchone()
            if active:
                raise TurnConflict(str(active["turn_id"]))
            try:
                conn.execute(
                    "INSERT INTO api_session_turns "
                    "(turn_id, session_id, fingerprint, delivery_mode, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                    (turn_id, session_id, fingerprint, payload["delivery_mode"], now, now),
                )
            except Exception as exc:
                if "idx_api_session_turns_one_active" in str(exc) or "UNIQUE constraint" in str(exc):
                    raise TurnConflict(session_id) from exc
                raise
            row = conn.execute(
                "SELECT * FROM api_session_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            return dict(row), True

        return self.db._execute_write(reserve_tx)

    def get(self, turn_id: str) -> Optional[Dict[str, Any]]:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT * FROM api_session_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        return dict(row) if row else None

    def public_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        row = self.get(turn_id)
        if not row:
            return None
        keys = (
            "turn_id", "session_id", "delivery_mode", "status", "safe_error_code",
            "created_at", "updated_at", "started_at", "finished_at", "effective_session_id",
            "assistant_message_id",
        )
        result = {key: row.get(key) for key in keys}
        with self.db._lock:
            deliveries = self.db._conn.execute(
                "SELECT destination, delivery_id, provider_message_id, state, safe_error_code, "
                "created_at, updated_at "
                "FROM api_session_turn_outbox WHERE turn_id = ? ORDER BY destination",
                (turn_id,),
            ).fetchall()
        result["deliveries"] = [dict(item) for item in deliveries]
        result["provenance"] = {
            "runtime": "hermes",
            "edge": "api_server",
            "execution": "single_run",
            "transcript": "session_db",
        }
        result["object"] = "hermes.session.turn"
        return result

    def _set_status(self, turn_id: str, status: str, safe_error_code: Optional[str] = None,
                    effective_session_id: Optional[str] = None) -> bool:
        now = time.time()
        terminal = status in TERMINAL_STATUSES

        def update(conn):
            row = conn.execute(
                "SELECT status FROM api_session_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES:
                return False
            conn.execute(
                "UPDATE api_session_turns SET status = ?, safe_error_code = ?, updated_at = ?, "
                "started_at = CASE WHEN ? = 'running' THEN COALESCE(started_at, ?) ELSE started_at END, "
                "finished_at = CASE WHEN ? THEN ? ELSE finished_at END, "
                "effective_session_id = COALESCE(?, effective_session_id) WHERE turn_id = ?",
                (status, safe_error_code, now, status, now, int(terminal), now,
                 effective_session_id, turn_id),
            )
            return True
        return bool(self.db._execute_write(update))

    def set_running(self, turn_id: str) -> bool:
        return self._set_status(turn_id, "running")

    def finish(self, turn_id: str, status: str, *, safe_error_code: Optional[str] = None,
               effective_session_id: Optional[str] = None) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError("finish requires a terminal status")
        return self._set_status(turn_id, status, safe_error_code, effective_session_id)

    def message_high_water(self) -> int:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS high FROM messages"
            ).fetchone()
        return int(row["high"])

    def anchor_completed_assistant(
        self, turn_id: str, effective_session_id: str, *, after_message_id: int
    ) -> Optional[str]:
        """Persist the exact final assistant row created after turn admission."""
        def anchor(conn):
            tail = conn.execute(
                "SELECT id, role FROM messages WHERE session_id = ? AND active = 1 "
                "AND id > ? ORDER BY id DESC LIMIT 1",
                (effective_session_id, after_message_id),
            ).fetchone()
            if tail is None or tail["role"] != "assistant":
                return None
            message_id = str(tail["id"])
            cursor = conn.execute(
                "UPDATE api_session_turns SET assistant_message_id = ?, "
                "effective_session_id = ?, updated_at = ? WHERE turn_id = ? "
                "AND status IN ('running','stopping') AND assistant_message_id IS NULL",
                (message_id, effective_session_id, time.time(), turn_id),
            )
            return message_id if cursor.rowcount == 1 else None
        return self.db._execute_write(anchor)

    def anchored_assistant_content(self, turn_id: str) -> Optional[str]:
        """Fetch only the assistant row durably anchored to this turn."""
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT m.content FROM api_session_turns t JOIN messages m "
                "ON CAST(m.id AS TEXT) = t.assistant_message_id "
                "AND m.session_id = t.effective_session_id "
                "WHERE t.turn_id = ? AND m.role = 'assistant' AND m.active = 1",
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        content = self.db._decode_content(row["content"])
        return content if isinstance(content, str) else None

    def request_stop(
        self, turn_id: str
    ) -> Optional[tuple[Dict[str, Any], bool]]:
        """Atomically request stop, record its edge once, and report ownership."""
        now = time.time()

        def stop(conn):
            row = conn.execute(
                "SELECT * FROM api_session_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                return None
            transitioned = row["status"] in {"queued", "running"}
            if transitioned:
                conn.execute(
                    "UPDATE api_session_turns SET stop_requested = 1, status = 'stopping', "
                    "updated_at = ? WHERE turn_id = ?", (now, turn_id)
                )
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS seq "
                    "FROM api_session_turn_events WHERE turn_id = ?", (turn_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO api_session_turn_events "
                    "(turn_id, seq, event_type, data_json, created_at) "
                    "VALUES (?, ?, 'turn.stopping', ?, ?)",
                    (turn_id, int(seq_row["seq"]), '{\"status\": \"stopping\"}', now),
                )
            result = conn.execute(
                "SELECT * FROM api_session_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            return dict(result), transitioned
        return self.db._execute_write(stop)

    def stop_requested(self, turn_id: str) -> bool:
        row = self.get(turn_id)
        return bool(row and row.get("stop_requested"))

    def append_event(self, turn_id: str, event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        if event_type == "assistant.delta":
            clean_data = {"volatile": True}
        else:
            forbidden = {
                "content", "delta", "text", "input", "prompt", "response",
                "reasoning", "output", "preview", "args", "messages",
            }
            clean_data = {key: value for key, value in data.items() if key not in forbidden}

        def append(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM api_session_turn_events WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            seq = int(row["seq"])
            conn.execute(
                "INSERT INTO api_session_turn_events (turn_id, seq, event_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (turn_id, seq, event_type, json.dumps(clean_data, ensure_ascii=False), now),
            )
            return {"seq": seq, "event": event_type, "data": clean_data, "created_at": now}
        return self.db._execute_write(append)

    def event_bounds(self, turn_id: str) -> tuple[int, int, int]:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT COALESCE(MIN(seq), 0) AS low, COALESCE(MAX(seq), 0) AS high, "
                "COUNT(*) AS count FROM api_session_turn_events WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return int(row["low"]), int(row["high"]), int(row["count"])

    def events_after(self, turn_id: str, sequence: int) -> list[Dict[str, Any]]:
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT seq, event_type, data_json, created_at FROM api_session_turn_events "
                "WHERE turn_id = ? AND seq > ? ORDER BY seq", (turn_id, sequence)
            ).fetchall()
        return [
            {"seq": int(row["seq"]), "event": row["event_type"],
             "data": json.loads(row["data_json"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def begin_delivery(self, turn_id: str, destination: str) -> bool:
        now = time.time()

        def begin(conn):
            existing = conn.execute(
                "SELECT state FROM api_session_turn_outbox WHERE turn_id = ? AND destination = ?",
                (turn_id, destination),
            ).fetchone()
            if existing:
                return False
            delivery_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"hermes-session-turn-delivery:{turn_id}:{destination}"
            ))
            conn.execute(
                "INSERT INTO api_session_turn_outbox "
                "(turn_id, destination, delivery_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, 'sending', ?, ?)",
                (turn_id, destination, delivery_id, now, now),
            )
            return True
        return bool(self.db._execute_write(begin))

    def finish_delivery(
        self,
        turn_id: str,
        destination: str,
        state: str,
        safe_error_code: Optional[str] = None,
        *,
        provider_message_id: Optional[str] = None,
    ) -> None:
        now = time.time()
        self.db._execute_write(lambda conn: conn.execute(
            "UPDATE api_session_turn_outbox SET state = ?, safe_error_code = ?, "
            "provider_message_id = COALESCE(?, provider_message_id), updated_at = ? "
            "WHERE turn_id = ? AND destination = ?",
            (
                state,
                safe_error_code,
                provider_message_id,
                now,
                turn_id,
                destination,
            ),
        ))

    def get_delivery(self, turn_id: str, destination: str) -> Optional[Dict[str, Any]]:
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT * FROM api_session_turn_outbox WHERE turn_id = ? AND destination = ?",
                (turn_id, destination),
            ).fetchone()
        return dict(row) if row else None

    def reconcile_uncertain(self) -> int:
        """Interrupt only work whose persistent execution owner is not live."""
        now = time.time()

        def reconcile(conn):
            turns = conn.execute(
                "SELECT turn_id, session_id FROM api_session_turns "
                "WHERE status IN ('queued','running','stopping')"
            ).fetchall()
            interrupted = 0
            for row in turns:
                turn_id = row["turn_id"]
                lease = conn.execute(
                    "SELECT * FROM session_execution_leases WHERE session_id = ?",
                    (row["session_id"],),
                ).fetchone()
                if lease is not None:
                    owner_state = self.db.session_execution_lease_owner_state(
                        lease, now=now
                    )
                    if owner_state == "live":
                        continue
                    conn.execute(
                        "DELETE FROM session_execution_leases "
                        "WHERE session_id = ? AND owner_id = ?",
                        (row["session_id"], lease["owner_id"]),
                    )
                conn.execute(
                    "UPDATE api_session_turns SET status='interrupted', "
                    "safe_error_code='process_restarted', updated_at=?, finished_at=? "
                    "WHERE turn_id=? AND status IN ('queued','running','stopping')",
                    (now, now, turn_id),
                )
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS seq "
                    "FROM api_session_turn_events WHERE turn_id=?",
                    (turn_id,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO api_session_turn_events "
                    "(turn_id, seq, event_type, data_json, created_at) "
                    "VALUES (?, ?, 'turn.interrupted', ?, ?)",
                    (
                        turn_id,
                        int(seq_row["seq"]),
                        json.dumps({
                            "status": "interrupted",
                            "error_code": "process_restarted",
                        }),
                        now,
                    ),
                )
                interrupted += 1
            conn.execute(
                "UPDATE api_session_turn_outbox SET state='needs_manual_retry', "
                "safe_error_code='delivery_outcome_unknown', updated_at=? "
                "WHERE state='sending'",
                (now,),
            )
            return interrupted
        return int(self.db._execute_write(reconcile) or 0)



def _json_object(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _add_slack_evidence(
    evidence: Dict[str, set[str]], value: Dict[str, Any], *, session_key: str = ""
) -> None:
    source = value.get("source")
    if isinstance(source, dict):
        _add_slack_evidence(evidence, source, session_key=session_key)
    elif source:
        evidence["platform"].add(str(source).strip().lower())
    platform = value.get("platform")
    if platform:
        evidence["platform"].add(str(platform).strip().lower())
    for key in ("chat_id", "channel_id"):
        if value.get(key):
            evidence["chat"].add(str(value[key]).strip())
    for key in ("thread_id", "thread_ts"):
        if value.get(key):
            evidence["thread"].add(str(value[key]).strip())
    for key in ("scope_id", "team_id", "workspace_id", "slack_team_id"):
        if value.get(key):
            evidence["workspace"].add(str(value[key]).strip())
    key = str(value.get("session_key") or session_key or "")
    match = re.search(r":slack:([^:]+):(?:channel|chat):([^:]+)(?::thread:([^:]+))?", key)
    if match:
        evidence["platform"].add("slack")
        evidence["workspace"].add(match.group(1))
        evidence["chat"].add(match.group(2))
        if match.group(3):
            evidence["thread"].add(match.group(3))


def resolve_slack_binding(session_db: Any, session_id: Optional[str] = None) -> SlackBinding:
    """Resolve the nearest unique Slack-bound lineage row, checking all durable metadata."""
    if session_id is None and isinstance(session_db, dict):
        rows = [session_db]
        routing: list[Dict[str, Any]] = []
    else:
        with session_db._lock:
            rows = [dict(row) for row in session_db._conn.execute(
                """WITH RECURSIVE lineage(id, depth) AS (
                     SELECT ?, 0 UNION ALL
                     SELECT s.parent_session_id, lineage.depth + 1
                     FROM lineage JOIN sessions s ON s.id = lineage.id
                     WHERE s.parent_session_id IS NOT NULL
                   )
                   SELECT s.*, lineage.depth FROM lineage JOIN sessions s ON s.id=lineage.id
                   ORDER BY lineage.depth""",
                (session_id,),
            ).fetchall()]
            routing_rows = session_db._conn.execute(
                "SELECT session_key, entry_json FROM gateway_routing"
            ).fetchall()
        routing = []
        lineage_ids = {row["id"] for row in rows}
        for route_row in routing_rows:
            entry = _json_object(route_row["entry_json"])
            if entry and str(entry.get("session_id") or "") in lineage_ids:
                entry = dict(entry)
                entry.setdefault("session_key", route_row["session_key"])
                routing.append(entry)

    for row in rows:
        evidence = {
            key: set() for key in ("platform", "chat", "thread", "workspace")
        }
        _add_slack_evidence(evidence, row)
        origin = _json_object(row.get("origin_json"))
        if origin:
            _add_slack_evidence(
                evidence,
                origin,
                session_key=str(row.get("session_key") or ""),
            )
        for entry in routing:
            if str(entry.get("session_id") or "") == str(row.get("id") or ""):
                _add_slack_evidence(evidence, entry)

        # A continuation row may retain source='slack' while intentionally
        # inheriting routing from its parent. It becomes the nearest *bound*
        # row only when durable target evidence exists on that row.
        if not any(evidence[key] for key in ("chat", "thread", "workspace")):
            continue
        if evidence["platform"] != {"slack"}:
            code = (
                "ambiguous_slack_binding"
                if len(evidence["platform"]) > 1
                else "no_slack_binding"
            )
            raise SlackBindingError(code)
        if len(evidence["chat"]) != 1 or len(evidence["workspace"]) != 1:
            code = (
                "ambiguous_slack_binding"
                if len(evidence["chat"]) > 1 or len(evidence["workspace"]) > 1
                else "no_slack_binding"
            )
            raise SlackBindingError(code)
        if len(evidence["thread"]) > 1:
            raise SlackBindingError("ambiguous_slack_binding")

        chat_id = next(iter(evidence["chat"]))
        thread_id = next(iter(evidence["thread"]), None)
        workspace = next(iter(evidence["workspace"]))
        metadata: Dict[str, Any] = {"scope_id": workspace}
        if thread_id:
            metadata["thread_id"] = thread_id
        return SlackBinding(chat_id, thread_id, metadata)

    raise SlackBindingError("no_slack_binding")



def _resync_error(message: str, code: str, *, high_water: int) -> web.Response:
    return web.json_response(
        {"error": {
            "message": message,
            "type": "event_cursor_error",
            "code": code,
            "resync_required": True,
            "high_water": high_water,
        }},
        status=409,
    )


def _error(message: str, code: str, status: int) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status=status,
    )


class SessionTurnService:
    """aiohttp-facing coordinator; APIServerAdapter delegates thinly here."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._stores: Dict[int, SessionTurnStore] = {}
        self._store_lock = asyncio.Lock()
        # SessionDB serializes SQLite with its own lock. This lock additionally
        # preserves submission order for all loop-originated store operations;
        # the operation itself always runs off the aiohttp loop.
        self._store_operation_lock = asyncio.Lock()
        self._tasks: Dict[str, asyncio.Task[Any]] = {}
        self._agent_refs: Dict[str, list[Any]] = {}
        # Stop requests have two distinct pieces of process-local ownership.
        # The handoff survives its HTTP waiter, while an interruption owner
        # prevents concurrent/recovery handoffs from spawning duplicate
        # interrupts for the same durable stopping edge.  The interruption
        # owner is a per-turn lifetime marker: completing the synchronous
        # interrupt call (or its watcher) does not mean the worker has exited.
        self._stop_handoffs: Dict[
            tuple[str, str], asyncio.Task[tuple[Optional[Dict[str, Any]], bool]]
        ] = {}
        self._interruption_owners: Dict[str, asyncio.Task[Any]] = {}
        self._lifecycle_dispatchers: Dict[str, SessionTurnLifecycleDispatcher] = {}
        self._lifecycle_cleanup_owners: set[str] = set()
        # Assistant text deltas are intentionally process-local. Canonical
        # SessionDB messages are the only durable transcript authority.
        self._volatile_events: Dict[str, Dict[int, Dict[str, Any]]] = {}

    async def _store(self) -> Optional[SessionTurnStore]:
        db = await self.adapter._ensure_session_db_async()
        if db is None:
            return None
        key = id(db)
        if key in self._stores:
            return self._stores[key]
        async with self._store_lock:
            if key not in self._stores:
                def initialize_store() -> SessionTurnStore:
                    store = SessionTurnStore(db)
                    store.reconcile_uncertain()
                    return store

                store = await self._store_operation(initialize_store)
                self._stores[key] = store
        return self._stores[key]

    async def reconcile_startup(self) -> None:
        """Initialize the ledger and interrupt crash-uncertain prior work."""
        await self._store()

    async def _store_operation(
        self,
        operation: Callable[[], _StoreResult],
        *,
        propagate_cancellation: bool = True,
    ) -> _StoreResult:
        """Run one ordered store operation off-loop and never abandon its thread."""

        async def run_ordered() -> _StoreResult:
            async with self._store_operation_lock:
                return await asyncio.to_thread(operation)

        operation_task = asyncio.create_task(run_ordered())
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(operation_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                continue
        if cancelled and propagate_cancellation:
            raise asyncio.CancelledError
        return result

    async def _finish_turn(
        self,
        store: SessionTurnStore,
        turn_id: str,
        status: str,
        event_type: str,
        error_code: Optional[str] = None,
        effective_session_id: Optional[str] = None,
    ) -> bool:
        """Persist terminal row and matching ledger event as one ordered unit."""

        def finish_and_append() -> bool:
            changed = store.finish(
                turn_id,
                status,
                safe_error_code=error_code,
                effective_session_id=effective_session_id,
            )
            if changed:
                data = {"status": status}
                if error_code is not None:
                    data["error_code"] = error_code
                store.append_event(turn_id, event_type, data)
            return changed

        # Terminal ownership must survive cancellation through both writes.
        return await self._store_operation(
            finish_and_append, propagate_cancellation=False
        )

    async def _finish_after_callback_store_failure(
        self, store: SessionTurnStore, turn_id: str
    ) -> None:
        """Terminate without appending behind structural events the pump lost."""
        await self._store_operation(
            lambda: store.finish(
                turn_id, "failed", safe_error_code="event_store_failed"
            ),
            propagate_cancellation=False,
        )

    @staticmethod
    def _turn_id(request: web.Request, body: Dict[str, Any]) -> str:
        header_id = request.headers.get("Idempotency-Key", "").strip()
        body_id = str(body.get("turn_id") or "").strip()
        if header_id and body_id and header_id != body_id:
            raise TurnInputError("Idempotency-Key and turn_id must match")
        return SessionTurnStore.validate_turn_id(header_id or body_id)

    async def submit(self, request: web.Request) -> web.Response:
        auth_err = self.adapter._check_auth(request)
        if auth_err:
            return auth_err
        body, body_err = await self.adapter._read_json_body(request)
        if body_err:
            return body_err
        session_id = request.match_info["session_id"]
        session, session_err = await self.adapter._get_existing_session_or_404(session_id)
        if session_err:
            return session_err
        try:
            turn_id = self._turn_id(request, body)
            # Full payload validation decodes every inline image with Pillow.
            # That CPU-heavy work must never run on the aiohttp event loop —
            # a single large submit would freeze every in-flight request.
            payload = await asyncio.to_thread(normalize_turn_payload, body)
        except TurnInputError as exc:
            return _error(str(exc), "invalid_turn", 400)

        store = await self._store()
        if store is None:
            return _error("Session database unavailable", "session_db_unavailable", 503)

        # An accepted idempotency key is durable truth. Browser retries must
        # reuse it even if the Slack adapter's current connectivity changed.
        existing = await self._store_operation(lambda: store.get(turn_id))
        if existing is not None:
            try:
                _turn, _created = await self._store_operation(
                    lambda: store.reserve(session_id, turn_id, payload)
                )
            except ConflictingTurn:
                return _error(
                    "turn_id was already used for a different request",
                    "idempotency_conflict",
                    409,
                )
            except TurnInputError as exc:
                return _error(str(exc), "invalid_turn", 400)
            return web.json_response(
                {
                    "object": "hermes.session.turn.reused",
                    "turn": await self._store_operation(
                        lambda: store.public_turn(turn_id)
                    ),
                },
                status=200,
            )

        binding = None
        slack_adapter = None
        if payload["delivery_mode"] in {"slack_only", "both"}:
            try:
                binding = await self._store_operation(
                    lambda: resolve_slack_binding(store.db, session_id)
                )
            except SlackBindingError as exc:
                return _error("Session has no unique Slack binding", exc.code, 409)
            slack_adapter = self.adapter._get_platform_callback_adapter(request, "slack")
            if slack_adapter is None:
                return _error("Slack adapter is not connected", "slack_unavailable", 503)

        try:
            turn, created = await self._store_operation(
                lambda: store.reserve(session_id, turn_id, payload)
            )
        except ConflictingTurn:
            return _error("turn_id was already used for a different request", "idempotency_conflict", 409)
        except TurnConflict:
            return _error("Session already has an active turn", "active_turn_conflict", 409)
        except TurnInputError as exc:
            return _error(str(exc), "invalid_turn", 400)

        if created:
            # reserve() committed the runnable identity before this observation.
            self._admit_execution(
                store, turn_id, session_id, payload, binding, slack_adapter
            )
        public = await self._store_operation(lambda: store.public_turn(turn_id))
        return web.json_response(
            {"object": "hermes.session.turn.accepted" if created else "hermes.session.turn.reused", "turn": public},
            status=202 if created else 200,
        )

    @staticmethod
    def _consume_background_task_exception(task: asyncio.Task[Any]) -> None:
        """Retrieve failures and log only a stable, privacy-safe classification."""
        if task.cancelled():
            return
        try:
            failure = task.exception()
        except asyncio.CancelledError:
            return
        if failure is not None:
            logger.error(
                "session_turn_background_task_failed reason=internal_task_error"
            )

    def _track_background_task(self, task: asyncio.Task[Any]) -> None:
        # Exception retrieval is unconditional even when an adapter does not
        # expose the optional ownership set.
        task.add_done_callback(self._consume_background_task_exception)
        try:
            self.adapter._background_tasks.add(task)
            task.add_done_callback(self.adapter._background_tasks.discard)
        except (AttributeError, TypeError):
            pass

    def _admit_execution(
        self,
        store: SessionTurnStore,
        turn_id: str,
        session_id: str,
        payload: Dict[str, Any],
        binding: Optional[SlackBinding],
        slack_adapter: Any,
    ) -> asyncio.Task[Any]:
        """Own task admission and its never-entered cancellation seam."""
        # Construct before scheduling: registered is already first in its FIFO.
        dispatcher = SessionTurnLifecycleDispatcher(session_id, turn_id)
        self._lifecycle_dispatchers[turn_id] = dispatcher
        phase = _TurnExecutionState()
        task = asyncio.create_task(
            self._execute(
                store,
                turn_id,
                session_id,
                payload,
                binding,
                slack_adapter,
                phase,
            )
        )
        self._tasks[turn_id] = task
        self._track_background_task(task)

        def execution_done(done: asyncio.Task[Any]) -> None:
            # Explicitly consume the execution exception here as well as in the
            # generic tracker. Reading it does not alter cleanup ownership.
            self._consume_background_task_exception(done)
            if done.cancelled() and not phase.entered:
                # A coroutine cancelled before its first instruction cannot run
                # _execute's try/finally. Transfer all ownership to the same
                # cleanup seam used by the normal execution epilogue.
                cleanup = asyncio.create_task(
                    self._finalize_execution(
                        store,
                        turn_id,
                        cancelled_before_entry=True,
                    )
                )
                self._tasks[turn_id] = cleanup
                self._track_background_task(cleanup)
                cleanup.add_done_callback(
                    lambda finished, tid=turn_id: (
                        self._consume_background_task_exception(finished),
                        self._remove_task_if_current(tid, finished),
                    )
                )
            else:
                self._remove_task_if_current(turn_id, done)

        task.add_done_callback(execution_done)
        return task

    def _remove_task_if_current(
        self, turn_id: str, task: asyncio.Task[Any]
    ) -> None:
        if self._tasks.get(turn_id) is task:
            self._tasks.pop(turn_id, None)

    async def _execute(self, store: SessionTurnStore, turn_id: str, session_id: str,
                       payload: Dict[str, Any], binding: Optional[SlackBinding], slack_adapter: Any,
                       phase: Optional[_TurnExecutionState] = None) -> None:
        phase = phase or _TurnExecutionState()
        phase.entered = True
        agent_ref: list[Any] = [None]
        execution_lease = None
        heartbeat_task: Optional[asyncio.Task[Any]] = None
        agent_task: Optional[asyncio.Task[Any]] = None
        callback_event_task: Optional[asyncio.Task[Any]] = None
        callback_ingress: Optional[_CallbackPersistenceIngress] = None
        callback_events_drained = False
        drain_callback_events: Optional[Callable[[], Any]] = None
        dispatcher = self._lifecycle_dispatchers.get(turn_id)
        self._agent_refs[turn_id] = agent_ref
        try:
            def interrupt_on_lease_loss() -> None:
                agent = agent_ref[0]
                if agent is not None:
                    agent.interrupt("Persistent session execution lease lost")

            try:
                execution_lease = await self.adapter._acquire_session_execution_lease(
                    session_id,
                    owner_prefix=f"api:durable-turn:{turn_id}",
                    on_lost=interrupt_on_lease_loss,
                )
            except SessionExecutionConflict:
                await self._finish_turn(
                    store, turn_id, "failed", "turn.failed", "active_session_execution"
                )
                return
            if await self._store_operation(lambda: store.stop_requested(turn_id)):
                await self._finish_turn(
                    store, turn_id, "interrupted", "turn.interrupted", "stopped_before_start"
                )
                return
            def start_turn() -> bool:
                if not store.set_running(turn_id):
                    return False
                store.append_event(turn_id, "turn.started", {"status": "running"})
                # Set in the same off-loop ownership unit as the durable write.
                # If the coordinator is cancelled while awaiting this worker,
                # _store_operation waits for it and the catch below sees truth.
                phase.started = True
                return True

            if not await self._store_operation(start_turn):
                return
            if dispatcher is not None:
                dispatcher.emit("started")

            async def emit_heartbeats() -> None:
                while True:
                    await asyncio.sleep(SESSION_TURN_HEARTBEAT_INTERVAL_SECONDS)
                    if dispatcher is not None:
                        dispatcher.emit("heartbeat")

            heartbeat_task = asyncio.create_task(emit_heartbeats())
            try:
                history = await self.adapter._conversation_history_for_session(session_id)
            except Exception:
                await self._finish_turn(
                    store, turn_id, "failed", "turn.failed", "history_read_failed"
                )
                return
            if await self._store_operation(lambda: store.stop_requested(turn_id)):
                await self._finish_turn(
                    store, turn_id, "interrupted", "turn.interrupted",
                    "stopped_before_execution",
                )
                return
            message_high_water = await self._store_operation(store.message_high_water)

            callback_ingress = _CallbackPersistenceIngress(
                asyncio.get_running_loop(), SESSION_TURN_CALLBACK_QUEUE_SIZE
            )

            async def persist_callback_events() -> None:
                assert callback_ingress is not None
                while True:
                    if callback_ingress.done.is_set() and callback_ingress.queue.empty():
                        break
                    try:
                        event = await asyncio.wait_for(
                            callback_ingress.queue.get(), timeout=0.05
                        )
                    except asyncio.TimeoutError:
                        continue
                    durable = await self._store_operation(
                        lambda event=event: store.append_event(
                            turn_id, event.event_type, event.data
                        )
                    )
                    if (
                        event.event_class is _CallbackEventClass.VOLATILE
                        and payload["delivery_mode"] != "slack_only"
                    ):
                        self._volatile_events.setdefault(turn_id, {})[
                            durable["seq"]
                        ] = {**durable, "data": dict(event.data)}
                if callback_ingress.resync_required:
                    # A durable body-free delta marker drives all replay paths
                    # through the canonical-transcript resync contract.
                    await self._store_operation(
                        lambda: store.append_event(
                            turn_id, "assistant.delta", {"volatile": True}
                        )
                    )
                structural_range = callback_ingress.structural_resync_range
                if structural_range is not None:
                    missing_from, missing_through = structural_range
                    await self._store_operation(
                        lambda: store.append_event(
                            turn_id,
                            "session.resync_required",
                            {
                                "reason": "structural_events_lost",
                                "missing_from": missing_from,
                                "missing_through": missing_through,
                            },
                        )
                    )

            callback_event_task = asyncio.create_task(persist_callback_events())
            callback_ingress.bind_pump(callback_event_task)

            async def _drain_callback_events() -> bool:
                nonlocal callback_events_drained
                if not callback_events_drained:
                    assert callback_ingress is not None
                    callback_ingress.close()
                    callback_events_drained = True
                assert callback_event_task is not None
                # Shield the single owned pump and re-await through repeated
                # cancellation. No retained structural event may be overtaken by
                # terminal persistence, and the pump exception is consumed here.
                while not callback_event_task.done():
                    try:
                        await asyncio.shield(callback_event_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if callback_event_task.cancelled():
                    error: Optional[BaseException] = asyncio.CancelledError()
                else:
                    error = callback_event_task.exception()
                if error is not None:
                    logger.error(
                        "session_turn_background_task_failed "
                        "reason=callback_event_store_error"
                    )
                    return False
                return True

            drain_callback_events = _drain_callback_events

            def on_delta(delta: Any) -> None:
                if (
                    payload["delivery_mode"] != "slack_only"
                    and isinstance(delta, str)
                    and delta
                ):
                    assert callback_ingress is not None
                    callback_ingress.append_volatile(
                        "assistant.delta", {"delta": delta}
                    )

            def on_tool_start(tool_call_id: Any, function_name: Any, function_args: Any) -> None:
                projected = project_safe_tool_event(
                    "tool.started", tool_call_id=tool_call_id, tool_name=function_name, args=function_args
                )
                assert callback_ingress is not None
                callback_ingress.append_structural(
                    projected["event"], projected["data"]
                )

            def on_tool_complete(tool_call_id: Any, function_name: Any, function_args: Any, function_result: Any) -> None:
                try:
                    from agent.display import _detect_tool_failure

                    failed, _safe_internal_detail = _detect_tool_failure(
                        str(function_name or ""), function_result
                    )
                except Exception:
                    failed = False
                event_type = "tool.failed" if failed else "tool.completed"
                projected = project_safe_tool_event(
                    event_type, tool_call_id=tool_call_id, tool_name=function_name,
                    args=function_args, output=function_result,
                )
                assert callback_ingress is not None
                callback_ingress.append_structural(
                    projected["event"], projected["data"]
                )

            agent_task = asyncio.create_task(
                self.adapter._run_agent(
                    user_message=_agent_input(payload),
                    conversation_history=history,
                    session_id=session_id,
                    stream_delta_callback=on_delta,
                    tool_start_callback=on_tool_start,
                    tool_complete_callback=on_tool_complete,
                    agent_ref=agent_ref,
                    authoritative_turn_id=turn_id,
                )
            )
            while True:
                try:
                    # Cancelling a Task waiting directly on run_in_executor
                    # abandons the still-live thread. Shield the child and keep
                    # this coordinator as monitoring owner until real exit.
                    result, _usage = await asyncio.shield(agent_task)
                    phase.execution_done = True
                    break
                except asyncio.CancelledError:
                    if agent_task.done():
                        # A child cancellation is a real execution outcome, not
                        # a request to abandon coordinator ownership.
                        await agent_task
                    await self._store_operation(
                        lambda: store.request_stop(turn_id),
                        propagate_cancellation=False,
                    )
                    if self._interruption_owners.get(turn_id) is None:
                        agent = agent_ref[0]
                        if agent is not None:
                            current = asyncio.current_task()
                            assert current is not None
                            self._interruption_owners[turn_id] = current
                            try:
                                agent.interrupt(
                                    "Session turn coordinator cancellation requested"
                                )
                            except Exception:
                                pass
                        else:
                            watcher = asyncio.create_task(
                                self._interrupt_when_available(turn_id)
                            )
                            self._interruption_owners[turn_id] = watcher
                            self._track_background_task(watcher)
                    # Continue heartbeat and lease ownership until the executor
                    # future itself resolves. Repeated cancellation is handled
                    # by the same bounded, non-abandoning loop.
            assert drain_callback_events is not None
            if not await drain_callback_events():
                await self._finish_after_callback_store_failure(store, turn_id)
                return
            effective_id = result.get("session_id", session_id) if isinstance(result, dict) else session_id
            if execution_lease is None or not await execution_lease.still_owned():
                await self._finish_turn(
                    store, turn_id, "interrupted", "turn.interrupted",
                    "execution_lease_lost", effective_id,
                )
                return
            if await self._store_operation(lambda: store.stop_requested(turn_id)):
                await self._finish_turn(
                    store, turn_id, "interrupted", "turn.interrupted",
                    "stop_requested", effective_id,
                )
                return
            if not isinstance(result, dict) or result.get("failed"):
                await self._finish_turn(
                    store, turn_id, "failed", "turn.failed", "run_failed", effective_id
                )
                return

            assistant_message_id = await self._store_operation(
                lambda: store.anchor_completed_assistant(
                    turn_id, effective_id, after_message_id=message_high_water
                )
            )
            anchored_content = await self._store_operation(
                lambda: store.anchored_assistant_content(turn_id)
            )
            final_response = result.get("final_response")
            if (
                assistant_message_id is None
                or not isinstance(anchored_content, str)
                or not isinstance(final_response, str)
                or anchored_content != final_response
            ):
                await self._finish_turn(
                    store, turn_id, "failed", "turn.failed",
                    "assistant_anchor_mismatch", effective_id,
                )
                return
            await self._store_operation(
                lambda: store.append_event(
                    turn_id,
                    "assistant.completed",
                    {"completed": True, "assistant_message_id": assistant_message_id},
                )
            )
            if payload["delivery_mode"] in {"slack_only", "both"}:
                assert binding is not None and slack_adapter is not None
                await self._deliver_slack(
                    store, turn_id, binding, slack_adapter, anchored_content
                )
            await self._finish_turn(
                store, turn_id, "completed", "turn.completed",
                effective_session_id=effective_id,
            )
        except asyncio.CancelledError:
            # Phase changes only after its corresponding durable truth exists,
            # so cancellation can never contradict a persisted turn.started.
            if agent_task is not None and agent_task.done():
                phase.execution_done = True
            if callback_event_task is not None:
                assert drain_callback_events is not None
                if not await drain_callback_events():
                    await self._finish_after_callback_store_failure(store, turn_id)
                    return
            await self._finish_turn(
                store, turn_id, "interrupted", "turn.interrupted",
                phase.cancellation_code,
            )
        except Exception:
            if callback_event_task is not None:
                assert drain_callback_events is not None
                if not await drain_callback_events():
                    await self._finish_after_callback_store_failure(store, turn_id)
                    return
            await self._finish_turn(
                store, turn_id, "failed", "turn.failed", "run_failed"
            )
        finally:
            await self._finalize_execution(
                store,
                turn_id,
                execution_lease=execution_lease,
                heartbeat_task=heartbeat_task,
            )

    async def _finalize_execution(
        self,
        store: SessionTurnStore,
        turn_id: str,
        *,
        cancelled_before_entry: bool = False,
        execution_lease: Any = None,
        heartbeat_task: Optional[asyncio.Task[Any]] = None,
    ) -> None:
        """Idempotently clean up either admission or normal execution exit."""
        if turn_id in self._lifecycle_cleanup_owners:
            return
        self._lifecycle_cleanup_owners.add(turn_id)
        dispatcher = self._lifecycle_dispatchers.get(turn_id)
        try:
            if cancelled_before_entry:
                await self._finish_turn(
                    store,
                    turn_id,
                    "interrupted",
                    "turn.interrupted",
                    "cancelled_before_execution",
                )

            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # Awaiting retrieves the exception; never expose its text.
                    logger.error(
                        "session_turn_background_task_failed reason=heartbeat_error"
                    )

            # The durable ledger alone authorizes public terminal truth. If it
            # cannot supply a terminal row, stop the private dispatcher without
            # synthesizing a lifecycle terminal.
            row = await self._store_operation(
                lambda: store.get(turn_id), propagate_cancellation=False
            )
            status = row.get("status") if row else None
            outcome = (
                {
                    "completed": "succeeded",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(status)
                if isinstance(status, str)
                else None
            )
            if dispatcher is not None:
                if outcome is None:
                    dispatcher.abort()
                else:
                    dispatcher.close(outcome)

            if execution_lease is not None:
                # Re-await the lease's single shielded release operation through
                # repeated coordinator cancellation.
                while True:
                    try:
                        await execution_lease.release()
                        break
                    except asyncio.CancelledError:
                        continue

            if dispatcher is not None:
                flush_task = asyncio.create_task(
                    asyncio.to_thread(
                        dispatcher.flush, SESSION_TURN_LIFECYCLE_FLUSH_SECONDS
                    )
                )
                while True:
                    try:
                        await asyncio.shield(flush_task)
                        break
                    except asyncio.CancelledError:
                        continue
        finally:
            # A successful interrupt call is only a request.  Retain its owner
            # until this authoritative execution/worker cleanup seam so a
            # repeated stop while the turn is still ``stopping`` cannot issue
            # another interrupt.  This also covers never-entered coordinators,
            # and ``finally`` prevents a cleanup failure from leaking ownership.
            self._interruption_owners.pop(turn_id, None)
            self._agent_refs.pop(turn_id, None)
            self._lifecycle_dispatchers.pop(turn_id, None)
            self._lifecycle_cleanup_owners.discard(turn_id)

    async def _deliver_slack(self, store: SessionTurnStore, turn_id: str, binding: SlackBinding,
                             slack_adapter: Any, content: str) -> None:
        def begin_and_read_delivery() -> Optional[Dict[str, Any]]:
            if not store.begin_delivery(turn_id, "slack"):
                return None
            return store.get_delivery(turn_id, "slack") or {}

        async def record_outcome(
            state: str,
            event_type: str,
            error_code: Optional[str] = None,
            *,
            provider_message_id: Optional[str] = None,
            cancellation_resistant: bool = False,
        ) -> None:
            def finish_and_append() -> None:
                store.finish_delivery(
                    turn_id,
                    "slack",
                    state,
                    error_code,
                    provider_message_id=provider_message_id,
                )
                data = {"destination": "slack", "status": state}
                if error_code is not None:
                    data["error_code"] = error_code
                store.append_event(turn_id, event_type, data)

            await self._store_operation(
                finish_and_append,
                propagate_cancellation=not cancellation_resistant,
            )

        async def record_cancelled_sending() -> None:
            # Own exactly one ordered transition task and retrieve every error.
            transition = asyncio.create_task(
                record_outcome(
                    "needs_manual_retry",
                    "delivery.failed",
                    "delivery_outcome_unknown",
                    cancellation_resistant=True,
                )
            )
            while not transition.done():
                try:
                    await asyncio.shield(transition)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not transition.cancelled() and transition.exception() is not None:
                logger.error(
                    "session_turn_delivery_transition_failed "
                    "destination=slack reason=store_error"
                )

        try:
            delivery = await self._store_operation(begin_and_read_delivery)
        except asyncio.CancelledError:
            # _store_operation finishes its worker before propagating, so inspect
            # the resulting durable state rather than guessing whether begin ran.
            current = await self._store_operation(
                lambda: store.get_delivery(turn_id, "slack"),
                propagate_cancellation=False,
            )
            if current is not None and current.get("state") == "sending":
                await record_cancelled_sending()
            raise
        if delivery is None:
            return

        if len(content) > MAX_SLACK_ROUTED_CHARS:
            await record_outcome(
                "rejected", "delivery.failed", "delivery_too_large"
            )
            return
        metadata = dict(binding.metadata)
        # The durable delivery id is also Slack's idempotency key. The bounded
        # content contract guarantees the adapter performs exactly one call.
        metadata["client_msg_id"] = str(delivery["delivery_id"])
        try:
            result = await slack_adapter.send(
                binding.chat_id, content, reply_to=binding.thread_id, metadata=metadata
            )
        except asyncio.CancelledError:
            # chat.postMessage may already have reached Slack. Own one shielded,
            # ordered write through repeated cancellation before propagating;
            # neither provider text nor message content enters the ledger/log.
            await record_cancelled_sending()
            raise
        except Exception:
            await record_outcome(
                "needs_manual_retry",
                "delivery.failed",
                "delivery_outcome_unknown",
            )
            return
        if getattr(result, "success", False):
            provider_message_id = getattr(result, "message_id", None)
            if not isinstance(provider_message_id, str) or not provider_message_id:
                await record_outcome(
                    "needs_manual_retry",
                    "delivery.failed",
                    "provider_receipt_missing",
                )
                return
            await record_outcome(
                "delivered",
                "delivery.completed",
                provider_message_id=provider_message_id,
            )
        else:
            # Once chat.postMessage was attempted, a timeout/error can be
            # ambiguous.  Never retry automatically.
            await record_outcome(
                "needs_manual_retry",
                "delivery.failed",
                "delivery_outcome_unknown",
            )

    async def status(self, request: web.Request) -> web.Response:
        auth_err = self.adapter._check_auth(request)
        if auth_err:
            return auth_err
        store = await self._store()
        if store is None:
            return _error("Session database unavailable", "session_db_unavailable", 503)
        turn = await self._store_operation(
            lambda: store.public_turn(request.match_info["turn_id"])
        )
        if not turn or turn["session_id"] != request.match_info["session_id"]:
            return _error("Turn not found", "turn_not_found", 404)
        return web.json_response({"object": "hermes.session.turn.status", "turn": turn})

    async def events(self, request: web.Request) -> web.StreamResponse:
        auth_err = self.adapter._check_auth(request)
        if auth_err:
            return auth_err
        store = await self._store()
        if store is None:
            return _error("Session database unavailable", "session_db_unavailable", 503)
        turn_id = request.match_info["turn_id"]
        turn = await self._store_operation(lambda: store.get(turn_id))
        if not turn or turn["session_id"] != request.match_info["session_id"]:
            return _error("Turn not found", "turn_not_found", 404)
        raw_sequence = request.headers.get("Last-Event-ID") or request.query.get("after") or "0"
        try:
            sequence = int(raw_sequence)
            if sequence < 0:
                raise ValueError
        except (TypeError, ValueError):
            return _error("Last-Event-ID must be a non-negative integer", "invalid_last_event_id", 400)
        low_water, high_water, event_count = await self._store_operation(
            lambda: store.event_bounds(turn_id)
        )
        if sequence > high_water:
            return _resync_error(
                "Last-Event-ID is ahead of the durable event high-water mark",
                "event_cursor_ahead",
                high_water=high_water,
            )
        if event_count and (low_water != 1 or event_count != high_water):
            return _resync_error(
                "Durable event history contains a gap; canonical resync is required",
                "event_gap",
                high_water=high_water,
            )
        initial_rows = await self._store_operation(
            lambda: store.events_after(turn_id, sequence)
        )
        volatile = self._volatile_events.get(turn_id, {})
        missing_volatile = any(
            row["event"] == "assistant.delta" and row["seq"] not in volatile
            for row in initial_rows
        )
        if missing_volatile and turn["status"] in TERMINAL_STATUSES:
            return _resync_error(
                "Volatile assistant deltas were missed; reload the canonical transcript",
                "volatile_events_lost",
                high_water=high_water,
            )
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        try:
            while True:
                def read_stream_state() -> tuple[
                    list[Dict[str, Any]], Optional[Dict[str, Any]]
                ]:
                    return store.events_after(turn_id, sequence), store.get(turn_id)

                rows, current = await self._store_operation(read_stream_state)
                missing_volatile = any(
                    row["event"] == "assistant.delta"
                    and row["seq"] not in self._volatile_events.get(turn_id, {})
                    for row in rows
                )
                if missing_volatile:
                    if current and current["status"] not in TERMINAL_STATUSES:
                        await asyncio.sleep(0.01)
                        continue
                    wire = json.dumps(
                        {
                            "error_code": "volatile_events_lost",
                            "resync_required": True,
                        }
                    )
                    await response.write(
                        f"event: session.resync_required\ndata: {wire}\n\n".encode("utf-8")
                    )
                    break
                for row in rows:
                    if row["event"] == "assistant.delta":
                        row = self._volatile_events[turn_id][row["seq"]]
                    sequence = row["seq"]
                    wire = json.dumps(row["data"], ensure_ascii=False)
                    await response.write(
                        f"id: {sequence}\nevent: {row['event']}\ndata: {wire}\n\n".encode("utf-8")
                    )
                if current and current["status"] in TERMINAL_STATUSES and not rows:
                    break
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    raise
        except (ConnectionResetError, BrokenPipeError):
            pass
        return response

    async def deliver(self, request: web.Request) -> web.Response:
        """Idempotently perform an explicit Slack delivery when no attempt exists."""
        auth_err = self.adapter._check_auth(request)
        if auth_err:
            return auth_err
        body, body_err = await self.adapter._read_json_body(request)
        if body_err:
            return body_err
        if set(body) - {"destination"} or body.get("destination") != "slack":
            return _error("destination must be slack", "invalid_delivery", 400)
        store = await self._store()
        if store is None:
            return _error("Session database unavailable", "session_db_unavailable", 503)
        session_id = request.match_info["session_id"]
        turn_id = request.match_info["turn_id"]
        turn = await self._store_operation(lambda: store.get(turn_id))
        if not turn or turn["session_id"] != session_id:
            return _error("Turn not found", "turn_not_found", 404)
        if turn["status"] != "completed":
            return _error("Turn is not completed", "turn_not_completed", 409)
        existing = await self._store_operation(
            lambda: store.get_delivery(turn_id, "slack")
        )
        if existing:
            if existing["state"] == "delivered":
                return web.json_response({"object": "hermes.session.turn.delivery", "delivery": existing})
            return _error(
                "Slack delivery was already attempted; outcome cannot be safely retried",
                "delivery_not_retryable",
                409,
            )
        try:
            binding = await self._store_operation(
                lambda: resolve_slack_binding(store.db, session_id)
            )
        except SlackBindingError as exc:
            return _error("Session has no unique Slack binding", exc.code, 409)
        slack_adapter = self.adapter._get_platform_callback_adapter(request, "slack")
        if slack_adapter is None:
            return _error("Slack adapter is not connected", "slack_unavailable", 503)
        try:
            content = await self._store_operation(
                lambda: store.anchored_assistant_content(turn_id)
            )
        except Exception:
            return _error("Session history unavailable", "session_history_unavailable", 503)
        if not isinstance(content, str):
            return _error(
                "Completed assistant anchor is missing or mismatched",
                "assistant_anchor_unavailable",
                409,
            )
        await self._deliver_slack(
            store, turn_id, binding, slack_adapter, content
        )
        delivery = await self._store_operation(
            lambda: store.get_delivery(turn_id, "slack")
        )
        status = 200 if delivery and delivery["state"] == "delivered" else 409
        return web.json_response(
            {"object": "hermes.session.turn.delivery", "delivery": delivery}, status=status
        )

    async def stop(self, request: web.Request) -> web.Response:
        auth_err = self.adapter._check_auth(request)
        if auth_err:
            return auth_err
        store = await self._store()
        if store is None:
            return _error("Session database unavailable", "session_db_unavailable", 503)
        turn_id = request.match_info["turn_id"]
        session_id = request.match_info["session_id"]
        public, _transitioned = await self._join_stop_handoff(
            store, session_id, turn_id
        )
        if public is None:
            return _error("Turn not found", "turn_not_found", 404)
        if public["status"] in TERMINAL_STATUSES:
            return web.json_response(
                {"object": "hermes.session.turn.stop", "turn": public}
            )
        return web.json_response(
            {"object": "hermes.session.turn.stop", "turn": public}, status=202
        )

    async def _join_stop_handoff(
        self, store: SessionTurnStore, session_id: str, turn_id: str
    ) -> tuple[Optional[Dict[str, Any]], bool]:
        """Join one cancellation-resistant durable-stop/interruption handoff."""

        key = (session_id, turn_id)
        handoff = self._stop_handoffs.get(key)
        if handoff is None:
            handoff = asyncio.create_task(
                self._perform_stop_handoff(store, session_id, turn_id)
            )
            self._stop_handoffs[key] = handoff
            self._track_background_task(handoff)

            def handoff_done(done: asyncio.Task[Any]) -> None:
                if self._stop_handoffs.get(key) is done:
                    self._stop_handoffs.pop(key, None)

            handoff.add_done_callback(handoff_done)

        # Client disconnect/request cancellation only abandons this waiter. The
        # tracked handoff keeps ownership through durable commit and interrupt.
        return await asyncio.shield(handoff)

    async def _perform_stop_handoff(
        self, store: SessionTurnStore, session_id: str, turn_id: str
    ) -> tuple[Optional[Dict[str, Any]], bool]:
        """Commit/read stop truth, then recover or claim interruption ownership."""

        # Let a just-admitted task finish durable lease/history admission so a
        # stop does not race it into a false "stopped before start" result.
        # Bounded and event-loop friendly; genuinely queued work still stops.
        for _ in range(10):
            ref = self._agent_refs.get(turn_id)
            if (ref and ref[0] is not None) or turn_id not in self._tasks:
                break
            await asyncio.sleep(0.01)
        def stop_and_read() -> tuple[Optional[Dict[str, Any]], bool]:
            existing = store.get(turn_id)
            if existing is None or existing["session_id"] != session_id:
                return None, False
            if existing["status"] in TERMINAL_STATUSES:
                return store.public_turn(turn_id), False
            result = store.request_stop(turn_id)
            if result is None:
                return None, False
            _row, transitioned = result
            return store.public_turn(turn_id), transitioned

        # This task, rather than an HTTP request task, owns the ordered store
        # operation. shield() in the caller ensures a disconnected request can
        # neither cancel the commit nor strand its interruption handoff.
        public, transitioned = await self._store_operation(
            stop_and_read, propagate_cancellation=False
        )
        if public is not None and (
            transitioned or public["status"] == "stopping"
        ):
            if self._interruption_owners.get(turn_id) is not None:
                return public, transitioned

            ref = self._agent_refs.get(turn_id)
            agent = ref[0] if ref else None
            if agent is not None:
                current = asyncio.current_task()
                assert current is not None
                self._interruption_owners[turn_id] = current
                try:
                    agent.interrupt("Stop requested via session turn API")
                except Exception:
                    pass
            else:
                watcher = asyncio.create_task(self._interrupt_when_available(turn_id))
                self._interruption_owners[turn_id] = watcher
                self._track_background_task(watcher)

                def watcher_done(done: asyncio.Task[Any]) -> None:
                    # Normally _finalize_execution owns removal.  A durable
                    # queued row can also be stopped without ever being admitted
                    # in this process; no execution finalizer exists in that
                    # case, so the completed watcher must release its marker.
                    if (
                        turn_id not in self._tasks
                        and self._interruption_owners.get(turn_id) is done
                    ):
                        self._interruption_owners.pop(turn_id, None)

                watcher.add_done_callback(watcher_done)
        return public, transitioned

    async def _interrupt_when_available(self, turn_id: str) -> None:
        """Close the small race between stop admission and agent construction."""
        while True:
            task = self._tasks.get(turn_id)
            if task is None or task.done():
                return
            ref = self._agent_refs.get(turn_id)
            agent = ref[0] if ref else None
            if agent is not None:
                try:
                    agent.interrupt("Stop requested via session turn API")
                except Exception:
                    pass
                return
            await asyncio.sleep(0.01)

    async def wait_for_turn(self, turn_id: str) -> None:
        task = self._tasks.get(turn_id)
        if task is not None:
            await task


SESSION_TURN_CAPABILITIES = {
    "durable": True,
    "idempotency": {"header": "Idempotency-Key", "body_field": "turn_id"},
    "delivery_modes": ["occ_only", "slack_only", "both"],
    "routing": "session_origin_only",
    "events": {"transport": "sse", "resumable": True, "last_event_id": True, "sequence": True},
    "safe_progress": True,
    "input_text": {"max_chars": MAX_INPUT_TEXT_LENGTH},
    "inline_images": {
        "remote_urls": False,
        "max_count": MAX_INLINE_IMAGES,
        "max_bytes_each": MAX_INLINE_IMAGE_BYTES,
        "max_bytes_total": MAX_INLINE_IMAGE_TOTAL_BYTES,
        "max_dimension": MAX_IMAGE_DIMENSION,
        "max_pixels": MAX_IMAGE_PIXELS,
        "max_frames": MAX_IMAGE_FRAMES,
        "mime_types": sorted(INLINE_IMAGE_MIME_TYPES),
    },
    "cancellation": "cooperative_truthful",
    "delivery": {
        "manual_operation": "POST /api/sessions/{session_id}/turns/{turn_id}/deliver",
        "delivery_id": True,
        "automatic_retry": False,
        "ambiguous_retry": False,
        "slack_atomic_max_chars": MAX_SLACK_ROUTED_CHARS,
    },
    "restart_reconciliation": "interrupt_uncertain_no_auto_execute",
}
