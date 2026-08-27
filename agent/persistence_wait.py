"""Helpers for waiting out transient session-store write contention."""

from __future__ import annotations

from collections.abc import Callable
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_INITIAL_SLEEP_S = 0.25
_DEFAULT_MAX_SLEEP_S = 5.0
_DEFAULT_PROGRESS_INTERVAL_S = 10.0
_DEFAULT_OPERATION_PATIENCE_S = 0.5
_INTERRUPT_POLL_S = 0.2

_WAITING_MESSAGE = "Session storage is busy; waiting to save before continuing."
_MISSING = object()


def _agent_float(agent: Any, attr: str, default: float) -> float:
    try:
        value = float(getattr(agent, attr, default))
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return value


def _classify_persistence_failure(agent: Any, exc: BaseException | None) -> str:
    if exc is not None:
        from hermes_state import classify_persistence_error

        cause = classify_persistence_error(exc)
        agent._last_persistence_error_cause = cause
        return cause

    cause = getattr(agent, "_last_persistence_error_cause", None)
    if cause is None:
        cause = "unknown"
        agent._last_persistence_error_cause = cause
    return cause


def _touch_wait_activity(agent: Any, stage: str) -> None:
    touch = getattr(agent, "_touch_activity", None)
    if not callable(touch):
        return
    try:
        touch(f"waiting for session storage lock ({stage})")
    except Exception:
        logger.debug("session persistence wait activity update failed", exc_info=True)


def _emit_wait_progress(agent: Any, stage: str, attempts: int, elapsed_s: float) -> None:
    emit = getattr(agent, "_emit_status", None)
    if not callable(emit):
        return
    try:
        emit(_WAITING_MESSAGE)
    except Exception:
        logger.debug(
            "session persistence wait progress emit failed "
            "(stage=%s attempts=%d elapsed=%.1fs)",
            stage,
            attempts,
            elapsed_s,
            exc_info=True,
        )


def _begin_wait(agent: Any, stage: str, attempts: int) -> float:
    started_mono = time.monotonic()
    agent._awaiting_session_persistence = True
    agent._awaiting_session_persistence_stage = stage
    agent._session_persistence_wait_attempts = attempts
    _touch_wait_activity(agent, stage)
    _emit_wait_progress(agent, stage, attempts, 0.0)
    logger.warning(
        "Session DB write locked; waiting to retry persistence "
        "(session=%s stage=%s)",
        getattr(agent, "session_id", None) or "none",
        stage,
    )
    return started_mono


def _finish_wait(
    agent: Any,
    *,
    cancelled: bool = False,
    attempts: int = 0,
) -> None:
    agent._awaiting_session_persistence = False
    agent._awaiting_session_persistence_stage = None
    agent._session_persistence_wait_cancelled = bool(cancelled)
    agent._session_persistence_wait_attempts = attempts


def session_persistence_wait_was_cancelled(agent: Any) -> bool:
    return getattr(agent, "_session_persistence_wait_cancelled", False) is True


def session_persistence_write_patience(agent: Any) -> float:
    """Short DB patience for one operation inside the outer interruptible wait."""
    return _agent_float(
        agent,
        "_session_persistence_lock_write_patience_s",
        _DEFAULT_OPERATION_PATIENCE_S,
    )


def wait_for_session_persistence(
    agent: Any,
    operation: Callable[[], Any],
    *,
    stage: str,
    allow_interrupted_start: bool = False,
) -> bool:
    """Retry ``operation`` while SessionDB reports lock/busy contention.

    The operation must be the exact durable-boundary write for the current
    in-memory transcript batch. This helper never regenerates model output and
    never runs tools; it only retries the same persistence callable until the
    store accepts it, a non-lock error occurs, or the turn is interrupted.
    """

    delay_s = _agent_float(
        agent,
        "_session_persistence_lock_wait_initial_s",
        _DEFAULT_INITIAL_SLEEP_S,
    )
    max_sleep_s = _agent_float(
        agent,
        "_session_persistence_lock_wait_max_sleep_s",
        _DEFAULT_MAX_SLEEP_S,
    )
    progress_interval_s = _agent_float(
        agent,
        "_session_persistence_lock_progress_interval_s",
        _DEFAULT_PROGRESS_INTERVAL_S,
    )
    attempts = 0
    started_mono: float | None = None
    last_progress_mono = 0.0
    agent._session_persistence_wait_cancelled = False

    while True:
        if (
            getattr(agent, "_interrupt_requested", False)
            and not allow_interrupted_start
        ):
            _finish_wait(agent, cancelled=True, attempts=attempts)
            return False
        allow_interrupted_start = False

        attempts += 1
        agent._last_persistence_error_cause = None
        previous_patience = getattr(
            agent,
            "_session_persistence_operation_patience_s",
            _MISSING,
        )
        agent._session_persistence_operation_patience_s = (
            session_persistence_write_patience(agent)
        )
        try:
            persisted = operation()
        except Exception as exc:
            cause = _classify_persistence_failure(agent, exc)
            if cause != "locked":
                _finish_wait(agent, attempts=attempts)
                logger.warning(
                    "Session DB persistence failed after %s "
                    "(session=%s cause=%s): %s",
                    stage,
                    getattr(agent, "session_id", None) or "none",
                    cause,
                    exc,
                )
                return False
        else:
            if persisted is not False:
                _finish_wait(agent, attempts=attempts)
                agent._last_persistence_error_cause = None
                return True
            cause = _classify_persistence_failure(agent, None)
            if cause != "locked":
                _finish_wait(agent, attempts=attempts)
                return False
        finally:
            if previous_patience is _MISSING:
                try:
                    delattr(agent, "_session_persistence_operation_patience_s")
                except AttributeError:
                    pass
            else:
                agent._session_persistence_operation_patience_s = previous_patience

        if started_mono is None:
            started_mono = _begin_wait(agent, stage, attempts)
            last_progress_mono = started_mono
        else:
            agent._session_persistence_wait_attempts = attempts
            _touch_wait_activity(agent, stage)

        now = time.monotonic()
        if (
            progress_interval_s == 0
            or now - last_progress_mono >= progress_interval_s
        ):
            _emit_wait_progress(
                agent,
                stage,
                attempts,
                now - started_mono,
            )
            last_progress_mono = now

        if delay_s <= 0:
            continue

        sleep_until = time.monotonic() + delay_s
        while True:
            remaining = sleep_until - time.monotonic()
            if remaining <= 0:
                break
            if getattr(agent, "_interrupt_requested", False):
                _finish_wait(agent, cancelled=True, attempts=attempts)
                return False
            time.sleep(min(_INTERRUPT_POLL_S, remaining))

        if max_sleep_s <= 0:
            delay_s = 0.0
        else:
            delay_s = min(max_sleep_s, delay_s * 2 if delay_s > 0 else max_sleep_s)


__all__ = [
    "session_persistence_write_patience",
    "session_persistence_wait_was_cancelled",
    "wait_for_session_persistence",
]
