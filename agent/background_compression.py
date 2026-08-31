"""Background context compaction with durable splice-merge adoption.

A job summarizes a stable transcript snapshot on a daemon thread. The existing
in-place compression transaction publishes that snapshot with a start watermark,
so rows appended while the summary is running are cloned after the compacted
handoff atomically. The live agent adopts the committed transcript at the next
turn boundary; it never waits for an in-flight job.
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class BackgroundCompressionJob:
    session_id: str
    done: threading.Event = field(default_factory=threading.Event)
    error: Optional[BaseException] = None
    committed: bool = False
    compressor_state: dict[str, Any] = field(default_factory=dict)


def _background_pressure_tokens(compressor: Any) -> int:
    for name in ("last_prompt_tokens", "last_real_prompt_tokens", "last_compression_rough_tokens"):
        value = getattr(compressor, name, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return 0


def _run_background_compression(
    agent: Any,
    job: BackgroundCompressionJob,
    snapshot: list[dict[str, Any]],
    system_message: str,
    approx_tokens: int,
) -> None:
    try:
        worker = copy.copy(agent)
        worker.context_compressor = copy.copy(agent.context_compressor)
        worker._background_compression_enabled = False
        worker.status_callback = None
        worker._emit_status = lambda *_args, **_kwargs: None
        worker._emit_warning = lambda *_args, **_kwargs: None
        worker._session_messages = snapshot

        compressed, _system_prompt = worker._compress_context(
            snapshot,
            system_message,
            approx_tokens=approx_tokens,
        )
        job.committed = bool(
            getattr(worker, "_last_compaction_in_place", False)
            and compressed is not snapshot
        )
        if job.committed:
            from agent.conversation_compression import _snapshot_compressor_attempt_state

            job.compressor_state = _snapshot_compressor_attempt_state(
                worker.context_compressor
            )
    except BaseException as exc:
        job.error = exc
        logger.warning(
            "Background compression failed for session %s: %s",
            job.session_id,
            exc,
            exc_info=True,
        )
    finally:
        job.done.set()


def maybe_start_background_compression(
    agent: Any,
    messages: list[dict[str, Any]],
    system_message: str = "",
) -> bool:
    """Start one eligible background compaction without blocking the caller."""
    if getattr(agent, "_background_compression_enabled", False) is not True:
        return False
    if getattr(agent, "compression_enabled", True) is not True:
        return False
    if getattr(agent, "compression_in_place", True) is not True:
        return False
    if getattr(agent, "compression_checkpoint_required", False) is True:
        return False
    if getattr(agent, "_persist_disabled", False):
        return False
    if not getattr(agent, "_session_db", None) or not getattr(agent, "session_id", None):
        return False
    if not isinstance(messages, list) or len(messages) < 2:
        return False

    existing = getattr(agent, "_background_compression_job", None)
    if isinstance(existing, BackgroundCompressionJob):
        return False

    compressor = getattr(agent, "context_compressor", None)
    # External context engines own their state and threading model. Background
    # splice-merge is deliberately a built-in-compressor capability until the
    # ContextEngine contract grows an explicit snapshot/commit interface.
    from agent.context_compressor import ContextCompressor

    if not isinstance(compressor, ContextCompressor):
        return False
    threshold = getattr(compressor, "threshold_tokens", 0)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
        return False
    pressure = _background_pressure_tokens(compressor)
    try:
        start_ratio = float(getattr(agent, "_background_compression_start_ratio", 0.8))
    except (TypeError, ValueError):
        start_ratio = 0.8
    start_ratio = min(0.99, max(0.1, start_ratio))
    if pressure < int(threshold * start_ratio):
        return False

    has_content = getattr(compressor, "has_content_to_compress", None)
    if callable(has_content):
        try:
            if has_content(messages) is False:
                return False
        except Exception:
            logger.debug("Background compression eligibility probe failed", exc_info=True)
            return False

    snapshot = copy.deepcopy(messages)
    job = BackgroundCompressionJob(session_id=str(agent.session_id))
    agent._background_compression_job = job
    thread = threading.Thread(
        target=_run_background_compression,
        args=(agent, job, snapshot, system_message or "", pressure),
        name=f"hermes-bg-compress-{str(agent.session_id)[:16]}",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Background compression started: session=%s tokens=%s trigger=%s",
        agent.session_id,
        pressure,
        int(threshold * start_ratio),
    )
    return True


def adopt_completed_background_compression(
    agent: Any,
    conversation_history: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    """Adopt a committed background result, or preserve the caller's history."""
    job = getattr(agent, "_background_compression_job", None)
    if not isinstance(job, BackgroundCompressionJob) or not job.done.is_set():
        return conversation_history

    agent._background_compression_job = None
    if (
        job.error is not None
        or not job.committed
        or job.session_id != str(getattr(agent, "session_id", ""))
    ):
        return conversation_history

    session_db = getattr(agent, "_session_db", None)
    if session_db is None:
        return conversation_history
    try:
        adopted = session_db.get_messages_as_conversation(job.session_id)
    except Exception:
        logger.warning(
            "Background compression committed but adoption reload failed for session %s",
            job.session_id,
            exc_info=True,
        )
        return conversation_history
    if not isinstance(adopted, list) or not adopted:
        return conversation_history

    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None:
        for name, value in job.compressor_state.items():
            try:
                setattr(compressor, name, copy.deepcopy(value))
            except Exception:
                pass
    agent._db_flush_scan_prefix = None
    agent._flushed_db_message_ids = set()
    agent._session_messages = adopted
    # Gateway transcript writers must re-baseline exactly as they do for a
    # foreground in-place boundary. Without this flag they may concatenate the
    # stale pre-compaction SessionEntry history in front of the adopted result.
    agent._last_compaction_in_place = True
    agent._last_compression_attempt_recorded = True
    agent._last_compression_attempt_in_place = True
    logger.info(
        "Background compression adopted: session=%s messages=%d",
        job.session_id,
        len(adopted),
    )
    return adopted
