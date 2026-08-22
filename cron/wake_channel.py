"""Durable wake queue between event producers and the cron scheduler.

Event-driven activation deliberately does NOT go through ``jobs.trigger_job``.
That function writes ``jobs.json`` on every call — contending with the
scheduler's own roughly-per-minute rewrite of the same file — and it sets
``enabled: True``, which would silently revive a worker an operator had
deliberately disabled. Neither is acceptable on a hot path that fires whenever
a mailbox message lands.

The canonical cross-profile quarantine-control database stores a bounded set of
job IDs that ``tick()`` drains transactionally. Requests survive gateway restart,
and both enqueue and drain participate in the same cross-process dispatch
admission used by the incident barrier.
"""

from __future__ import annotations

import logging
from typing import Any

from jobflow_dispatch.quarantine_control import (
    ack_wake as _durable_ack_wake,
    clear_wakes as _durable_clear_wakes,
    drain_wakes as _durable_drain_wakes,
    peek_wakes as _durable_peek_wakes,
    pending_wakes as _durable_pending_wakes,
    request_wake as _durable_request_wake,
)

logger = logging.getLogger(__name__)

#: Upper bound on queued wakes. The durable store enforces this in the same
#: transaction that inserts a new distinct job ID.
MAX_PENDING = 512


def request_wake(job_id: Any, *, caller: Any, reason: Any = None) -> bool:
    """Durably ask the scheduler to run ``job_id`` on its next tick."""
    queued = _durable_request_wake(job_id, caller=caller, reason=reason)
    if queued:
        logger.info("cron wake requested: job=%s caller=%s reason=%s", job_id, caller, reason)
    return queued


def peek_wakes() -> tuple[dict[str, Any], ...]:
    """Observe exact durable wake capabilities without consuming them."""
    return _durable_peek_wakes()


def ack_wake(wake: dict[str, Any]) -> bool:
    """Consume only the exact wake generation handed off by the scheduler."""
    return _durable_ack_wake(wake)


def drain_wakes() -> set[str]:
    """Legacy destructive drain for explicit maintenance callers only."""
    return _durable_drain_wakes()


def pending_wakes() -> frozenset[str]:
    """Observe durable wakes without consuming them."""
    return _durable_pending_wakes()


def clear_wakes() -> None:
    _durable_clear_wakes()
