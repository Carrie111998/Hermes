"""Loud-failure boundary for the agent loop (SR-471 / ADR-0024 §3).

R57 ran silently for hours because the agent loop classified a *library*
``TypeError`` (the openai parse_response crash on ``output=None``) as a
non-retryable "programming bug", returned an empty-response result, and emitted
NO alert. The traceback half was closed by ``exc_info=True`` at
``run_agent.py:10783``; this module is the **alert half**: it emits an
``AGENT_LOOP_FAULT`` bus event for ANY unhandled stream-accumulation exception,
**ignoring the upstream non-retryable classification** — silence is the bug.

Design constraints:
* Best-effort: it must NEVER raise and NEVER block the agent loop (it is called
  from ``run_agent.py``'s abort path). Every failure mode degrades to a debug log.
* Lazy bus access: ``run_agent`` has no events imports; the bus is constructed
  here (``EventBus()`` defaults to the canonical ``~/.hermes/events/event_bus.db``).
* Rate-capped per ADR-0016 ("cap at the emitter"): a small per-process token
  budget keyed on ``(source, exception_type)`` so one wedged run cannot flood the
  bus. Cross-process storms are additionally bounded downstream by the
  ``watchdog_alerts`` verbosity ladder + the FailureClusterDetector coalescing
  this event dovetails with.
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Per-process emit budget. Keyed on (source, exception_type). Resets only on
# process restart — appropriate for a cron subprocess (one run) and benign for
# the long-lived gateway (a handful of distinct fault signatures per restart).
_RATE_CAP_MAX = 3
# Not thread-safe by design: under gateway multi-threading, racing get->check->set
# may over-emit by at most (N_threads - 1) per signature — acceptable for a small
# cap, and a lock on the abort path is not worth the added latency.
_emit_counts: dict[tuple[str, str], int] = {}


def reset_rate_cap() -> None:
    """Test hook: clear the per-process emit budget."""
    _emit_counts.clear()


def _resolve_source(source_hint: Optional[str]) -> str:
    """Best-effort canonical agent identity for the fault.

    Prefers explicit env hints, then the caller's hint (e.g. log_prefix), then
    'agent-loop'. Normalised through canonical_agent_source so it lines up with
    the FailureClusterDetector / watchdog_alerts taxonomy.
    """
    raw = (
        os.environ.get("HERMES_CRON_JOB_NAME")
        or os.environ.get("HERMES_AGENT_SOURCE")
        or (source_hint or "")
    )
    raw = raw.strip().strip("[]").strip()
    if not raw:
        raw = "agent-loop"
    try:
        from events.producers.agent_source_mapping import canonical_agent_source
        return canonical_agent_source(raw)
    except Exception:
        return raw


def emit_agent_loop_fault(
    exc: BaseException,
    *,
    source_hint: Optional[str] = None,
    phase: str = "stream_accumulation",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    status_code: Optional[int] = None,
    bus: Any = None,
) -> bool:
    """Emit an AGENT_LOOP_FAULT event for ``exc``. Returns True if emitted.

    Never raises. Honours the per-process rate cap. ``bus`` is injectable for
    tests; production passes None and an EventBus() is built lazily.
    """
    try:
        from events.schema import EventType, Priority

        source = _resolve_source(source_hint)
        exc_type = type(exc).__name__
        key = (source, exc_type)
        count = _emit_counts.get(key, 0)
        if count >= _RATE_CAP_MAX:
            logger.debug("AGENT_LOOP_FAULT rate-capped for %s/%s", source, exc_type)
            return False

        tb_tail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-2000:]
        payload = {
            "exception_type": exc_type,
            "message": str(exc)[:500],
            "phase": phase,
            "provider": provider or "",
            "model": model or "",
            "status_code": status_code,
            "traceback_tail": tb_tail,
            "rate_capped_after": _RATE_CAP_MAX,
        }

        if bus is None:
            from events.bus import EventBus
            bus = EventBus()

        bus.emit(
            event_type=EventType.AGENT_LOOP_FAULT,
            source=source,
            payload=payload,
            priority=Priority.HIGH,
        )
        # Charge the budget only on a CONFIRMED emit: if bus.emit() above raised
        # (swallowed by the outer except), we must not burn a slot and drop a
        # later legitimate alert early. The cap bounds delivered notifications,
        # not failed attempts.
        _emit_counts[key] = count + 1
        logger.info("Emitted AGENT_LOOP_FAULT (%s) from %s", exc_type, source)
        return True
    except Exception:  # pragma: no cover - alerting must never break the loop
        logger.debug("emit_agent_loop_fault failed (swallowed)", exc_info=True)
        return False
