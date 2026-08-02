"""Opt-in, tool-wait-only speculative context compression.

The worker side of this module is deliberately detached from the live agent.
It owns only a copied transcript and a freshly constructed built-in compressor.
The foreground path is the only place that can install a candidate or touch
session state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from concurrent.futures import CancelledError, Future, TimeoutError
from dataclasses import dataclass
from queue import Empty, Queue
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_DB_PERSISTED_MARKER = "_db_persisted"
_TAIL_MARKER = "_speculative_tail_marker"


@dataclass(frozen=True)
class SpeculativeCompressionSettings:
    """Normalized feature settings.

    ``during_idle`` is retained as a forward-compatible config field, but v1
    intentionally never schedules idle work.
    """

    enabled: bool = False
    start_ratio: float = 0.70
    hard_ratio: float = 0.85
    max_age_seconds: float = 180.0
    hard_wait_seconds: float = 2.0
    during_tool_wait: bool = True
    during_idle: bool = False


DEFAULT_SPECULATIVE_COMPRESSION_SETTINGS = SpeculativeCompressionSettings()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _finite_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum):
        return default
    return parsed


def normalize_speculative_compression_settings(
    raw: Any,
    *,
    warning_logger: logging.Logger | None = None,
) -> SpeculativeCompressionSettings:
    """Normalize ``compression.speculative`` without raising on bad config."""

    log = warning_logger or logger
    values = raw if isinstance(raw, Mapping) else {}
    defaults = DEFAULT_SPECULATIVE_COMPRESSION_SETTINGS

    raw_start = values.get("start_ratio", defaults.start_ratio)
    raw_hard = values.get("hard_ratio", defaults.hard_ratio)
    try:
        start = float(raw_start)
        hard = float(raw_hard)
        ratios_valid = (
            math.isfinite(start) and math.isfinite(hard) and 0.0 < start < hard < 1.0
        )
    except (TypeError, ValueError):
        ratios_valid = False
        start = hard = 0.0
    if not ratios_valid:
        log.warning(
            "Invalid compression.speculative ratios start_ratio=%r hard_ratio=%r; "
            "using defaults %.2f/%.2f",
            raw_start,
            raw_hard,
            defaults.start_ratio,
            defaults.hard_ratio,
        )
        start = defaults.start_ratio
        hard = defaults.hard_ratio

    max_age = _finite_float(
        values.get("max_age_seconds", defaults.max_age_seconds),
        defaults.max_age_seconds,
        minimum=0.0,
    )
    hard_wait = _finite_float(
        values.get("hard_wait_seconds", defaults.hard_wait_seconds),
        defaults.hard_wait_seconds,
        minimum=0.0,
    )
    return SpeculativeCompressionSettings(
        enabled=_as_bool(values.get("enabled"), defaults.enabled),
        start_ratio=start,
        hard_ratio=hard,
        max_age_seconds=max_age,
        hard_wait_seconds=max(0.0, hard_wait),
        during_tool_wait=_as_bool(
            values.get("during_tool_wait"), defaults.during_tool_wait
        ),
        during_idle=_as_bool(values.get("during_idle"), defaults.during_idle),
    )


def is_builtin_compression_eligible(
    *,
    api_mode: Any,
    context_engine: Any,
) -> bool:
    """Return whether v1 may use the built-in compressor."""

    if str(api_mode or "") == "codex_app_server":
        return False
    try:
        from agent.context_compressor import ContextCompressor

        return isinstance(context_engine, ContextCompressor)
    except Exception:
        return False


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in {_DB_PERSISTED_MARKER, _TAIL_MARKER}
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical_value(item) for item in value), key=repr)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return _canonical_value(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return _canonical_value(vars(value))
        except Exception:
            pass
    return repr(value)


def fingerprint_messages(messages: Iterable[Mapping[str, Any]]) -> str:
    """Hash provider-visible message content deterministically."""

    encoded = json.dumps(
        [_canonical_value(message) for message in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw(item) for item in value}
    return copy.deepcopy(value)


def _message_list(
    snapshot_messages: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    return [_thaw(message) for message in snapshot_messages]


def _boundary_splits_tool_batch(messages: List[Mapping[str, Any]], cut: int) -> bool:
    if cut <= 0 or cut >= len(messages):
        return False
    if messages[cut].get("role") == "tool":
        return True
    previous = messages[cut - 1]
    return bool(previous.get("role") == "assistant" and previous.get("tool_calls"))


@dataclass(frozen=True)
class SpeculativeSnapshot:
    """Immutable copied source state for one candidate job."""

    session_id: str
    messages: Tuple[Mapping[str, Any], ...]
    source_fingerprint: str
    boundary_fingerprint: str
    cut_index: int
    compress_start: int
    original_count: int
    request_tokens: int | None
    captured_at: float

    @property
    def source_message_count(self) -> int:
        return self.original_count


def capture_snapshot(
    messages: List[Dict[str, Any]],
    compressor: Any,
    request_tokens: int | None,
    session_id: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> SpeculativeSnapshot:
    """Capture a stable prefix/tail boundary using compressor-owned helpers."""

    copied = copy.deepcopy(messages)
    if not copied:
        raise ValueError("cannot capture an empty speculative transcript")
    head_end = compressor._protect_head_size(copied)
    cut = compressor._find_tail_cut_by_tokens(copied, head_end)
    cut = compressor._align_boundary_forward(copied, cut)
    if cut <= head_end or cut >= len(copied):
        raise ValueError("speculative transcript has no stable compressible window")
    if _boundary_splits_tool_batch(copied, cut):
        # The aligner is deterministic: a cut that still splits a batch after
        # one alignment has no safe forward move. Reject the snapshot — the
        # synchronous path handles this transcript instead.
        raise ValueError("speculative boundary splits a tool-call/result batch")

    covered = copied[:cut]
    boundary = copied[cut]
    return SpeculativeSnapshot(
        session_id=str(session_id or ""),
        messages=tuple(_freeze(message) for message in copied),
        source_fingerprint=fingerprint_messages(covered),
        boundary_fingerprint=fingerprint_messages([boundary]),
        cut_index=cut,
        compress_start=head_end,
        original_count=len(copied),
        request_tokens=request_tokens,
        captured_at=clock(),
    )


@dataclass(frozen=True)
class SpeculativeCandidate:
    """Uncommitted compressed prefix plus source validation metadata."""

    session_id: str
    source_fingerprint: str
    boundary_fingerprint: str
    cut_index: int
    compress_start: int
    original_count: int
    compressed_prefix: Tuple[Mapping[str, Any], ...]
    created_at: float
    source_tokens: int | None = None
    used_fallback: bool = False
    feasibility_skip: bool = False
    summary_error: str | None = None
    previous_summary: str | None = None
    summary_has_user_turn: bool | None = None
    made_progress: bool | None = None
    savings_pct: float | None = None

    @property
    def source_session_id(self) -> str:
        return self.session_id

    @property
    def compressed_prefix_result(self) -> Tuple[Mapping[str, Any], ...]:
        return self.compressed_prefix

    def is_expired(self, max_age_seconds: float, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now - self.created_at > max(0.0, float(max_age_seconds))

    def matches(
        self, messages: List[Dict[str, Any]], session_id: str | None = None
    ) -> bool:
        if session_id is not None and str(session_id or "") != self.session_id:
            return False
        # A shorter live transcript could be an older/reloaded view. Installing
        # the candidate into it would silently drop the snapshot's preserved
        # suffix, so only equal-or-longer transcripts are eligible.
        if len(messages) < self.original_count or len(messages) < self.cut_index:
            return False
        if fingerprint_messages(messages[: self.cut_index]) != self.source_fingerprint:
            return False
        if self.cut_index < len(messages):
            return (
                fingerprint_messages([messages[self.cut_index]])
                == self.boundary_fingerprint
            )
        return self.cut_index == self.original_count

    def assemble(
        self, messages: List[Dict[str, Any]], session_id: str | None = None
    ) -> List[Dict[str, Any]]:
        if not self.matches(messages, session_id):
            raise ValueError("speculative candidate no longer matches the transcript")
        prefix = _message_list(self.compressed_prefix)
        suffix = copy.deepcopy(messages[self.cut_index :])
        for message in prefix + suffix:
            if isinstance(message, dict):
                message.pop(_TAIL_MARKER, None)
        return prefix + suffix


def build_candidate(
    snapshot: SpeculativeSnapshot,
    compressor_factory: Callable[[], Any],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> SpeculativeCandidate:
    """Generate a candidate without touching the foreground compressor."""

    worker = compressor_factory()
    worker_messages = _message_list(snapshot.messages)
    marker = uuid.uuid4().hex
    worker_messages[snapshot.cut_index][_TAIL_MARKER] = marker
    try:
        compressed = worker.compress(
            worker_messages,
            current_tokens=snapshot.request_tokens,
            force=False,
        )
    except Exception:
        raise
    if not isinstance(compressed, list) or not compressed:
        raise ValueError("speculative compressor returned no transcript")

    marker_index = next(
        (
            index
            for index, message in enumerate(compressed)
            if isinstance(message, dict) and message.get(_TAIL_MARKER) == marker
        ),
        None,
    )
    if marker_index is None:
        raise ValueError("speculative compressor did not preserve the tail marker")

    source_tail_head = _message_list(snapshot.messages)[snapshot.cut_index]
    candidate_tail_head = copy.deepcopy(compressed[marker_index])
    candidate_tail_head.pop(_TAIL_MARKER, None)
    if fingerprint_messages([candidate_tail_head]) != fingerprint_messages([
        source_tail_head
    ]):
        # A summary merged into the first tail message cannot be safely spliced
        # with a later live suffix. Rejecting it preserves the tail verbatim and
        # lets the synchronous path handle the uncommon alternation collision.
        raise ValueError("speculative compression rewrote the protected tail")

    prefix = tuple(
        _freeze(copy.deepcopy(message)) for message in compressed[:marker_index]
    )
    if not prefix:
        raise ValueError("speculative compressor returned an empty compressed prefix")

    return SpeculativeCandidate(
        session_id=snapshot.session_id,
        source_fingerprint=snapshot.source_fingerprint,
        boundary_fingerprint=snapshot.boundary_fingerprint,
        cut_index=snapshot.cut_index,
        compress_start=snapshot.compress_start,
        original_count=snapshot.original_count,
        compressed_prefix=prefix,
        created_at=clock(),
        source_tokens=snapshot.request_tokens,
        used_fallback=bool(getattr(worker, "_last_summary_fallback_used", False)),
        feasibility_skip=bool(getattr(worker, "_last_feasibility_skip", False)),
        summary_error=getattr(worker, "_last_summary_error", None),
        previous_summary=getattr(worker, "_previous_summary", None),
        summary_has_user_turn=getattr(worker, "_summary_has_user_turn", None),
        made_progress=getattr(worker, "_last_compression_made_progress", None),
        savings_pct=getattr(worker, "_last_compression_savings_pct", None),
    )


def clone_builtin_compressor(compressor: Any) -> Any:
    """Build an isolated built-in compressor with the live budget settings."""

    from agent.context_compressor import ContextCompressor

    if not isinstance(compressor, ContextCompressor):
        raise TypeError("speculative compression requires the built-in compressor")
    context_length = getattr(compressor, "_resolved_context_length", None)
    if context_length is None:
        context_length = compressor.context_length
    return ContextCompressor(
        model=compressor.model,
        threshold_percent=compressor.threshold_percent,
        protect_first_n=compressor.protect_first_n,
        protect_last_n=compressor.protect_last_n,
        summary_target_ratio=compressor.summary_target_ratio,
        quiet_mode=True,
        summary_model_override=getattr(compressor, "summary_model", "") or None,
        base_url=getattr(compressor, "base_url", ""),
        api_key=getattr(compressor, "api_key", ""),
        config_context_length=context_length,
        provider=getattr(compressor, "provider", ""),
        api_mode=getattr(compressor, "api_mode", ""),
        abort_on_summary_failure=getattr(compressor, "abort_on_summary_failure", False),
        max_tokens=getattr(compressor, "max_tokens", None),
        model_thresholds=getattr(compressor, "model_thresholds", None),
        threshold_tokens_cap=getattr(compressor, "threshold_tokens_cap", None),
        proactive_prune_tokens=getattr(compressor, "proactive_prune_tokens", 0),
        proactive_prune_min_result_chars=getattr(
            compressor, "proactive_prune_min_result_chars", 8000
        ),
        proactive_prune_min_reclaim_tokens=getattr(
            compressor, "proactive_prune_min_reclaim_tokens", 4096
        ),
        min_tail_user_messages=getattr(compressor, "min_tail_user_messages", 1),
    )


def speculative_thresholds(
    compressor: Any,
    settings: SpeculativeCompressionSettings,
) -> Tuple[int, int]:
    """Return soft/hard trigger budgets from the effective input window."""

    context_length = int(getattr(compressor, "context_length", 0) or 0)
    max_tokens = getattr(compressor, "max_tokens", None)
    compute = getattr(type(compressor), "_compute_threshold_tokens", None)
    if callable(compute) and context_length > 0:
        return (
            int(compute(context_length, settings.start_ratio, max_tokens)),
            int(compute(context_length, settings.hard_ratio, max_tokens)),
        )
    effective = context_length - int(max_tokens or 0)
    if effective <= 0:
        effective = context_length
    return int(effective * settings.start_ratio), int(effective * settings.hard_ratio)


def schedule_tool_wait_candidate(
    agent: Any,
    messages: List[Dict[str, Any]],
    request_tokens: int,
) -> str:
    """Capture and schedule one candidate immediately before tool execution."""

    settings = getattr(agent, "speculative_compression_settings", None)
    manager = getattr(agent, "_speculative_compression_manager", None)
    compressor = getattr(agent, "context_compressor", None)
    session_id = str(getattr(agent, "session_id", "") or "")
    if (
        not getattr(agent, "speculative_compression_enabled", False)
        or not isinstance(settings, SpeculativeCompressionSettings)
        or not settings.during_tool_wait
        or manager is None
        or not session_id
        or not is_builtin_compression_eligible(
            api_mode=getattr(agent, "api_mode", None),
            context_engine=compressor,
        )
    ):
        return "disabled"
    soft_trigger, _hard_trigger = speculative_thresholds(compressor, settings)
    if int(request_tokens or 0) < soft_trigger:
        return "below_soft_trigger"
    try:
        snapshot = capture_snapshot(
            messages,
            compressor,
            int(request_tokens),
            session_id,
        )
        return manager.maybe_start(
            session_id,
            snapshot,
            lambda source=compressor: clone_builtin_compressor(source),
            max_age_seconds=settings.max_age_seconds,
        )
    except Exception:
        logger.debug(
            "speculative compression snapshot unavailable for session=%s",
            session_id,
            exc_info=True,
        )
        return "unavailable"


@dataclass
class _Job:
    snapshot: SpeculativeSnapshot
    future: Future
    cancel_event: threading.Event
    started_at: float
    max_age_seconds: float
    rerun_snapshot: SpeculativeSnapshot | None = None
    rerun_factory: Callable[[], Any] | None = None
    candidate: SpeculativeCandidate | None = None
    error: BaseException | None = None


class _DaemonExecutor:
    """Small daemon-only executor for disposable speculative work."""

    def __init__(self, max_workers: int, thread_name_prefix: str):
        self._queue: Queue[tuple[Future, Callable[..., Any], tuple, dict] | None] = (
            Queue()
        )
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}_{index}",
                daemon=True,
            )
            for index in range(max_workers)
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("speculative executor has been shut down")
            self._queue.put((future, fn, args, kwargs))
        return future

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, fn, args, kwargs = item
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args, **kwargs))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                self._queue.task_done()

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = True) -> None:
        with self._lock:
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except Empty:
                        break
                    if item is not None:
                        item[0].cancel()
                    self._queue.task_done()
            for _thread in self._threads:
                self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


class SpeculativeCompressionManager:
    """Single-flight process-local candidate manager keyed by session id."""

    def __init__(
        self, *, max_workers: int = 2, clock: Callable[[], float] = time.monotonic
    ):
        self._lock = threading.Lock()
        self._entries: Dict[str, _Job] = {}
        self._executor: _DaemonExecutor | None = None
        self._max_workers = max(1, int(max_workers))
        self._clock = clock
        self._shutdown = False

    def _get_executor(self) -> _DaemonExecutor:
        if self._executor is None:
            self._executor = _DaemonExecutor(
                self._max_workers, "hermes-speculative-compression"
            )
            self._executor.start()
        return self._executor

    def maybe_start(
        self,
        session_id: str,
        snapshot: SpeculativeSnapshot,
        compressor_factory: Callable[[], Any],
        *,
        max_age_seconds: float = 180.0,
    ) -> str:
        if not session_id or snapshot.session_id != str(session_id):
            return "rejected"
        with self._lock:
            if self._shutdown:
                return "shutdown"
            existing = self._entries.get(str(session_id))
            if existing is not None and not existing.future.done():
                existing.rerun_snapshot = snapshot
                existing.rerun_factory = compressor_factory
                logger.debug(
                    "speculative compression session=%s disposition=coalesced fingerprint=%s",
                    session_id,
                    snapshot.source_fingerprint[:12],
                )
                return "coalesced"
            if existing is not None and existing.candidate is not None:
                if not existing.candidate.is_expired(
                    max_age_seconds, now=self._clock()
                ):
                    if (
                        existing.candidate.source_fingerprint
                        == snapshot.source_fingerprint
                        and existing.candidate.boundary_fingerprint
                        == snapshot.boundary_fingerprint
                        and existing.candidate.cut_index == snapshot.cut_index
                    ):
                        return "ready"
                    # A completed candidate for a changed prefix cannot be
                    # reused. Drop it so this newer snapshot gets a fresh job.
                self._entries.pop(str(session_id), None)
            cancel_event = threading.Event()
            future = self._get_executor().submit(
                self._run_job,
                snapshot,
                compressor_factory,
                cancel_event,
            )
            job = _Job(
                snapshot=snapshot,
                future=future,
                cancel_event=cancel_event,
                started_at=self._clock(),
                max_age_seconds=max_age_seconds,
            )
            self._entries[str(session_id)] = job
            future.add_done_callback(
                lambda done, sid=str(session_id): self._finish_job(sid, done)
            )
            logger.debug(
                "speculative compression session=%s disposition=started fingerprint=%s source_tokens=%s",
                session_id,
                snapshot.source_fingerprint[:12],
                snapshot.request_tokens,
            )
            return "started"

    @staticmethod
    def _run_job(
        snapshot: SpeculativeSnapshot,
        compressor_factory: Callable[[], Any],
        cancel_event: threading.Event,
    ) -> SpeculativeCandidate:
        if cancel_event.is_set():
            raise CancelledError()
        candidate = build_candidate(snapshot, compressor_factory)
        if cancel_event.is_set():
            raise CancelledError()
        return candidate

    def _finish_job(self, session_id: str, future: Future) -> None:
        with self._lock:
            job = self._entries.get(session_id)
            if job is None or job.future is not future:
                return
            try:
                job.candidate = future.result()
                job.error = None
                logger.debug(
                    "speculative compression session=%s disposition=ready fingerprint=%s",
                    session_id,
                    job.candidate.source_fingerprint[:12],
                )
            except CancelledError:
                job.error = None
                logger.debug(
                    "speculative compression session=%s disposition=cancelled",
                    session_id,
                )
            except BaseException as exc:
                job.error = exc
                logger.debug(
                    "speculative compression session=%s disposition=failed (%s: %s)",
                    session_id,
                    type(exc).__name__,
                    exc,
                )

            pending = job.rerun_snapshot
            factory = job.rerun_factory
            if pending is not None and factory is not None:
                if job.candidate is not None and (
                    pending.source_fingerprint == job.candidate.source_fingerprint
                    and pending.cut_index == job.candidate.cut_index
                ):
                    job.rerun_snapshot = None
                    job.rerun_factory = None
                    return
                if not self._shutdown:
                    cancel_event = threading.Event()
                    next_future = self._get_executor().submit(
                        self._run_job, pending, factory, cancel_event
                    )
                    self._entries[session_id] = _Job(
                        snapshot=pending,
                        future=next_future,
                        cancel_event=cancel_event,
                        started_at=self._clock(),
                        max_age_seconds=job.max_age_seconds,
                    )
                    next_future.add_done_callback(
                        lambda done, sid=session_id: self._finish_job(sid, done)
                    )
                    logger.debug(
                        "speculative compression session=%s disposition=rerun fingerprint=%s",
                        session_id,
                        pending.source_fingerprint[:12],
                    )

    def take_matching_candidate(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        *,
        wait_seconds: float = 0.0,
        max_age_seconds: float = 180.0,
    ) -> SpeculativeCandidate | None:
        sid = str(session_id or "")
        with self._lock:
            job = self._entries.get(sid)
            future = job.future if job is not None and not job.future.done() else None
        if future is not None and wait_seconds > 0:
            try:
                future.result(timeout=max(0.0, float(wait_seconds)))
            except BaseException:
                pass

        with self._lock:
            job = self._entries.get(sid)
            if job is None:
                return None
            candidate = job.candidate
            if candidate is None:
                return None
            if candidate.is_expired(max_age_seconds, now=self._clock()):
                self._entries.pop(sid, None)
                logger.debug(
                    "speculative compression session=%s disposition=stale", sid
                )
                return None
            if not candidate.matches(messages, sid):
                self._entries.pop(sid, None)
                logger.debug(
                    "speculative compression session=%s disposition=stale", sid
                )
                return None
            job.candidate = None
            self._entries.pop(sid, None)
            return candidate

    def invalidate_session(self, session_id: str) -> None:
        sid = str(session_id or "")
        with self._lock:
            job = self._entries.pop(sid, None)
            if job is None:
                return
            job.cancel_event.set()
            job.future.cancel()
            logger.debug(
                "speculative compression session=%s disposition=cancelled", sid
            )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            jobs = list(self._entries.values())
            self._entries.clear()
            executor = self._executor
            for job in jobs:
                job.cancel_event.set()
                job.future.cancel()
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)


_DEFAULT_MANAGER: SpeculativeCompressionManager | None = None
_DEFAULT_MANAGER_LOCK = threading.Lock()


def get_default_manager() -> SpeculativeCompressionManager:
    global _DEFAULT_MANAGER
    with _DEFAULT_MANAGER_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = SpeculativeCompressionManager(max_workers=2)
        return _DEFAULT_MANAGER


def shutdown_default_manager() -> None:
    global _DEFAULT_MANAGER
    with _DEFAULT_MANAGER_LOCK:
        manager = _DEFAULT_MANAGER
        _DEFAULT_MANAGER = None
    if manager is not None:
        manager.shutdown()


__all__ = [
    "DEFAULT_SPECULATIVE_COMPRESSION_SETTINGS",
    "SpeculativeCandidate",
    "SpeculativeCompressionManager",
    "SpeculativeCompressionSettings",
    "SpeculativeSnapshot",
    "build_candidate",
    "capture_snapshot",
    "clone_builtin_compressor",
    "fingerprint_messages",
    "get_default_manager",
    "is_builtin_compression_eligible",
    "normalize_speculative_compression_settings",
    "schedule_tool_wait_candidate",
    "shutdown_default_manager",
    "speculative_thresholds",
]
