"""Autonomous initiation runtime baseline (PR #90820 Round 3 / Round-3 clean rebuild).

This package provides the in-process concurrency control for the agent's
autonomous objective admission. Two contracts:

1. **Concurrency reservation** — only one objective is active per
   process at a time. :func:`reserve_active` is called when an
   objective is admitted; :func:`clear_active` releases the
   reservation when the objective finishes. A second
   :func:`reserve_active` must succeed after the first finishes
   (no stale reservation).

2. **Lifecycle integration** — the kanban task-completion path calls
   :func:`clear_active` at the real production boundary (after a
   task row is updated to ``done`` and the worker has no remaining
   work) so the next objective can be admitted without a stuck
   reservation. The clearance is "real production boundary" — the
   call sits inside the same code path that persists ``done`` /
   ``complete`` / ``kanban_complete`` state, not a free-floating
   hook that can race.

This is a deliberately small surface: ``reserve_active`` /
``clear_active`` plus the kanban wiring. The wider autonomy policy
(priorities, scheduling, goal-mode) lives elsewhere and is out of
scope for the Round 3 contract.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

_logger = logging.getLogger(__name__)


@dataclass
class ActiveReservation:
    """The in-process reservation that gates :func:`reserve_active`.

    Holds the objective id, the admission timestamp, and the reason
    (so a future admission can record why the previous one was
    released — useful for the audit trail).
    """

    objective_id: str
    admitted_at: float
    reason: str = ""


# In-process active reservation. ``None`` means no objective is
# currently active. Protected by ``_lock`` so concurrent callers
# (e.g. the kanban dispatcher and the autonomy initiator racing on
# the same task) see a coherent view.
_lock = threading.RLock()
_active: Optional[ActiveReservation] = None


def reserve_active(objective_id: str, *, reason: str = "") -> ActiveReservation:
    """Reserve the in-process active slot for ``objective_id``.

    Returns the :class:`ActiveReservation` on success. Raises
    :class:`RuntimeError` if a different objective is already active.

    The caller is responsible for invoking :func:`clear_active` when
    the objective finishes (typically wired into the kanban completion
    path so a ``done`` / ``complete`` transition always releases the
    slot).
    """
    global _active
    import time as _time

    with _lock:
        if _active is not None and _active.objective_id != objective_id:
            raise RuntimeError(
                f"autonomy: another objective is already active "
                f"({_active.objective_id!r}); refusing to admit "
                f"{objective_id!r}. Clear the previous reservation first."
            )
        if _active is not None and _active.objective_id == objective_id:
            # Re-admitting the SAME objective (idempotent); refresh ts.
            _active.admitted_at = _time.time()
            return _active
        _active = ActiveReservation(
            objective_id=objective_id,
            admitted_at=_time.time(),
            reason=reason,
        )
        _logger.debug("autonomy: reserved active slot for %r", objective_id)
        return _active


def clear_active(objective_id: Optional[str] = None) -> bool:
    """Release the in-process active reservation.

    If ``objective_id`` is provided, the reservation is only released
    when it matches — prevents a slow finishing task from clearing the
    slot owned by a NEWLY admitted objective. Returns True when a
    reservation was released, False otherwise.
    """
    global _active
    with _lock:
        if _active is None:
            return False
        if objective_id is not None and _active.objective_id != objective_id:
            # Different objective owns the slot — refuse to clobber.
            _logger.warning(
                "autonomy: clear_active(%r) ignored; slot owned by %r",
                objective_id,
                _active.objective_id,
            )
            return False
        released_id = _active.objective_id
        _active = None
        _logger.debug("autonomy: released active slot for %r", released_id)
        return True


def get_active() -> Optional[ActiveReservation]:
    """Return the current active reservation (read-only snapshot)."""
    with _lock:
        return _active


def is_active(objective_id: Optional[str] = None) -> bool:
    """Return True when an active reservation exists (optionally for
    a specific ``objective_id``)."""
    with _lock:
        if _active is None:
            return False
        if objective_id is None:
            return True
        return _active.objective_id == objective_id


__all__ = [
    "ActiveReservation",
    "reserve_active",
    "clear_active",
    "get_active",
    "is_active",
]