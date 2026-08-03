"""Turn-process supervision helpers for the gateway.

Extracted from gateway/run.py: the dequeue / reap / abandon / watch cluster
that supervises individual gateway turns and the background processes they
spawn, plus the interrupt-reason constants and the internal control-interrupt
message set they share. Pure move — no behavior change.
"""

import logging
import threading
from typing import Callable, Optional

from agent.interrupt_compat import request_hard_interrupt
from gateway.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


def _dequeue_pending_event(adapter, session_key: str) -> MessageEvent | None:
    """Consume and return the full pending event for a session.

    Queued follow-ups must preserve their media metadata so they can re-enter
    the normal image/STT/document preprocessing path instead of being reduced
    to a placeholder string.
    """
    return adapter.get_pending_message(session_key)


_INTERRUPT_REASON_STOP = "Stop requested"
_INTERRUPT_REASON_RESET = "Session reset requested"
_INTERRUPT_REASON_TIMEOUT = "Execution timed out (inactivity)"
_INTERRUPT_REASON_SSE_DISCONNECT = "SSE client disconnected"
_INTERRUPT_REASON_GATEWAY_SHUTDOWN = "Gateway shutting down"
_INTERRUPT_REASON_GATEWAY_RESTART = "Gateway restarting"


def _reap_gateway_turn_processes(
    task_id: str,
    process_baseline,
    *,
    source: str,
    is_still_current: Optional[Callable[[], bool]] = None,
) -> int:
    """Reap only background processes created by one abandoned turn.

    ``task_id`` is session-scoped (task_id == session_id), not turn-scoped,
    so a *replacement* turn on the same session can start and spawn its own
    legitimate process while this reap is still in flight. ``is_still_current``
    — a closure over the run_generation captured when the reaping turn began
    or was interrupted — lets the caller detect that a newer turn has since
    claimed the session and bail out instead of killing that newer turn's
    process. The newer turn snapshots its own baseline independently, so
    skipping here does not leave anything permanently unreaped.
    """
    if not task_id:
        # ProcessSession.task_id defaults to "" for sessionless callers, so a
        # blank id would match (and kill) every unrelated empty-task process
        # instead of this turn's own. Nothing session-scoped to reap.
        return 0
    if is_still_current is not None:
        try:
            if not is_still_current():
                logger.debug(
                    "Skipping reap for turn %s (%s): a newer turn already "
                    "claimed this session; it owns its own baseline.",
                    task_id,
                    source,
                )
                return 0
        except Exception:
            logger.debug(
                "is_still_current check failed for turn %s (%s); reaping anyway",
                task_id,
                source,
                exc_info=True,
            )

    from tools.process_registry import process_registry

    try:
        killed = process_registry.kill_started_since(
            task_id,
            process_baseline,
            source=source,
        )
    except Exception:
        # Runs on a detached daemon thread (interrupt and timeout call
        # sites both fire-and-forget it) — an uncaught exception here
        # would only surface via threading.excepthook, bypassing the
        # app's logger. Swallow and log through the normal channel instead.
        logger.warning(
            "Failed to reap background processes for turn %s (%s)",
            task_id,
            source,
            exc_info=True,
        )
        return 0
    if killed:
        logger.warning(
            "Reaped %d background process(es) created by abandoned turn %s (%s)",
            killed,
            task_id,
            source,
        )
    return killed


def _abandon_timed_out_gateway_turn(
    *,
    agent_holder,
    task_id: str,
    process_baseline,
    worker_done: threading.Event,
    timeout_fired: threading.Event,
    cleanup_lock: threading.Lock,
    is_still_current: Optional[Callable[[], bool]] = None,
) -> bool:
    """Interrupt one timed-out turn and reap only processes it created."""
    with cleanup_lock:
        if worker_done.is_set() or timeout_fired.is_set():
            return False
        timeout_fired.set()

    agent = agent_holder[0] if agent_holder else None
    if agent is not None:
        try:
            request_hard_interrupt(agent, _INTERRUPT_REASON_TIMEOUT)
        except Exception:
            logger.debug("Timed-out agent interrupt failed", exc_info=True)

    try:
        _reap_gateway_turn_processes(
            task_id,
            process_baseline,
            source="gateway_turn_timeout",
            is_still_current=is_still_current,
        )
    except Exception:
        logger.warning(
            "Failed to reap background processes for timed-out turn %s",
            task_id,
            exc_info=True,
        )
    return True


def _watch_gateway_turn_inactivity(
    *,
    agent_holder,
    task_id: str,
    process_baseline,
    timeout: float,
    worker_done: threading.Event,
    timeout_fired: threading.Event,
    cleanup_lock: threading.Lock,
    poll_interval: float = 5.0,
    is_still_current: Optional[Callable[[], bool]] = None,
) -> None:
    """Thread watchdog that remains runnable when gateway asyncio is starved."""
    while not worker_done.wait(max(0.01, poll_interval)):
        agent = agent_holder[0] if agent_holder else None
        if agent is None or not hasattr(agent, "get_activity_summary"):
            continue
        try:
            idle_seconds = float(
                agent.get_activity_summary().get("seconds_since_activity", 0.0)
            )
        except Exception:
            continue
        if idle_seconds < timeout:
            continue
        _abandon_timed_out_gateway_turn(
            agent_holder=agent_holder,
            task_id=task_id,
            process_baseline=process_baseline,
            worker_done=worker_done,
            timeout_fired=timeout_fired,
            cleanup_lock=cleanup_lock,
            is_still_current=is_still_current,
        )
        return


_CONTROL_INTERRUPT_MESSAGES = frozenset(
    {
        _INTERRUPT_REASON_STOP.lower(),
        _INTERRUPT_REASON_RESET.lower(),
        _INTERRUPT_REASON_TIMEOUT.lower(),
        _INTERRUPT_REASON_SSE_DISCONNECT.lower(),
        _INTERRUPT_REASON_GATEWAY_SHUTDOWN.lower(),
        _INTERRUPT_REASON_GATEWAY_RESTART.lower(),
    }
)


def _is_control_interrupt_message(message: Optional[str]) -> bool:
    """Return True when an interrupt message is internal control flow."""
    if not message:
        return False
    normalized = " ".join(str(message).strip().split()).lower()
    return normalized in _CONTROL_INTERRUPT_MESSAGES
