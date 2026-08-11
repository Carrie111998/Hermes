"""In-process wake channel between event producers and the cron scheduler.

Event-driven activation deliberately does NOT go through ``jobs.trigger_job``.
That function writes ``jobs.json`` on every call — contending with the
scheduler's own roughly-per-minute rewrite of the same file — and it sets
``enabled: True``, which would silently revive a worker an operator had
deliberately disabled. Neither is acceptable on a hot path that fires whenever
a mailbox message lands.

This channel is a bounded, thread-safe set of job IDs that ``tick()`` drains.
Requesting a wake touches no file and takes no lock the scheduler holds.

**It is intentionally volatile.** Pending wakes are lost if the gateway
restarts, and that is safe *only* because the deterministic reconciler
re-derives outstanding work from the mailbox and the activation ledger.
Durability belongs there, not here — a wake is a hint that work exists, never
the record that it does.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

#: Upper bound on queued wakes. The scheduler drains every tick, so a healthy
#: system never approaches this; reaching it means a producer is looping, and
#: dropping loudly beats growing without bound inside the gateway process.
MAX_PENDING = 512

_pending: set[str] = set()
_lock = threading.Lock()


def request_wake(job_id: Any, *, caller: Any, reason: Any = None) -> bool:
    """Ask the scheduler to run ``job_id`` on its next tick.

    Returns True when this call newly queued the job. False means it was
    already queued (duplicate events collapse) or the channel is full.
    """
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")
    if not isinstance(caller, str) or not caller.strip():
        # Matches trigger_job's attribution requirement: an anonymous wake is
        # impossible to trace in a postmortem.
        raise ValueError("caller must be a non-empty string")

    key = job_id.strip()
    with _lock:
        if key in _pending:
            return False
        if len(_pending) >= MAX_PENDING:
            logger.warning(
                "cron wake channel full (%d); dropping wake for %s from %s",
                MAX_PENDING, key, caller,
            )
            return False
        _pending.add(key)

    logger.info("cron wake requested: job=%s caller=%s reason=%s", key, caller, reason)
    return True


def drain_wakes() -> set[str]:
    """Atomically take and clear every pending wake."""
    with _lock:
        drained = set(_pending)
        _pending.clear()
    return drained


def pending_wakes() -> frozenset[str]:
    """Observe without consuming (diagnostics and tests)."""
    with _lock:
        return frozenset(_pending)


def clear_wakes() -> None:
    with _lock:
        _pending.clear()
