"""Attempt-local deterministic fallback for timed-out context compression."""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Callable, Iterator
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The host can overlap a fenced remote worker with a network-free retry. Keep
# fallback selection in the retry's copied context instead of shared compressor
# attributes visible to both attempts.
_STATIC_SUMMARY_FALLBACK_REASON: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("hermes_static_summary_fallback_reason", default=None)
)


@contextlib.contextmanager
def static_summary_fallback(reason: str) -> Iterator[None]:
    """Force one built-in compaction to use its deterministic handoff."""
    token = _STATIC_SUMMARY_FALLBACK_REASON.set(
        str(reason or "summary unavailable")
    )
    try:
        yield
    finally:
        _STATIC_SUMMARY_FALLBACK_REASON.reset(token)


def static_summary_fallback_reason() -> Optional[str]:
    """Return the attempt-local reason for bypassing the summary LLM."""
    return _STATIC_SUMMARY_FALLBACK_REASON.get()


def run_static_compression_fallback(
    *,
    worker: Callable[[Any], tuple[list, str]],
    messages: list,
    reason: str,
    telemetry_agent: Any = None,
    new_fence: Optional[Callable[[], Any]] = None,
) -> tuple[bool, Optional[tuple[list, str]]]:
    """Commit the built-in deterministic handoff after a fenced summary stall.

    Imports are local to keep this bounded helper independent of the two
    compression orchestrator modules during module initialization.
    """
    try:
        from agent.context_compressor import ContextCompressor
        from agent.conversation_compression import CompressionCommitFence
    except Exception:
        return False, None

    compressor = getattr(telemetry_agent, "context_compressor", None)
    if not isinstance(compressor, ContextCompressor):
        return False, None

    # From this point the built-in path owns recovery. A hard stop or local
    # failure must degrade without issuing a second provider request.
    hard_cancel = getattr(telemetry_agent, "_hard_interrupt_requested", None)
    if callable(getattr(hard_cancel, "is_set", None)) and hard_cancel.is_set():
        return True, None

    retry_fence = None
    if new_fence is not None:
        try:
            retry_fence = new_fence()
        except Exception:
            logger.warning(
                "static compression fallback fence factory failed",
                exc_info=True,
            )
    if not isinstance(retry_fence, CompressionCommitFence):
        retry_fence = CompressionCommitFence()

    logger.warning(
        "Context compression summary %s — committing the local deterministic "
        "handoff without another LLM request",
        reason,
    )
    try:
        with static_summary_fallback(reason):
            result_msgs, result_prompt = worker(retry_fence)
    except Exception:
        logger.warning(
            "Local deterministic compression fallback failed",
            exc_info=True,
        )
        return True, None
    if result_msgs is messages:
        return True, None

    clear = getattr(compressor, "_clear_compression_failure_cooldown", None)
    if callable(clear):
        try:
            clear()
        except Exception:
            logger.debug("failed to reset superseded timeout cooldown", exc_info=True)

    record = getattr(compressor, "record_timeout_failure", None)
    if callable(record):
        try:
            record(
                f"local fallback after summary {reason}",
                failure_kind=(
                    "ceiling_exhausted" if "total ceiling" in reason else "stalled"
                ),
            )
        except Exception:
            logger.debug("failed to record local fallback cooldown", exc_info=True)

    emit = getattr(telemetry_agent, "_emit_warning", None)
    if callable(emit):
        try:
            emit(
                "⚠ Context compression used a local fallback summary because "
                f"the summary model {reason}. The session remains usable; "
                "future remote summary attempts are temporarily paused."
            )
        except Exception:
            logger.debug("failed to emit local fallback warning", exc_info=True)
    return True, (result_msgs, result_prompt)
