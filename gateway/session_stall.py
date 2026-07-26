"""Gateway session stall detection helpers (#72016 item 2).

A session is "stalled with pending inbound" when the user has a queued follow-up
message while the running agent has not touched its activity clock for longer
than ``agent.session_stall_timeout``. This is distinct from ``gateway_timeout``
(which kills the in-flight turn) and ``gateway_notify_interval`` (periodic
"still working" heartbeats during healthy long runs).
"""

from __future__ import annotations

from typing import Optional


def should_emit_session_stall_notification(
    *,
    timeout_seconds: float,
    idle_seconds: Optional[float],
    has_pending_inbound: bool,
    already_notified: bool,
) -> bool:
    """Return True when a stall warning should be sent for this session."""
    if timeout_seconds <= 0:
        return False
    if not has_pending_inbound:
        return False
    if already_notified:
        return False
    if idle_seconds is None:
        return False
    return idle_seconds >= timeout_seconds


def should_clear_session_stall_notification(
    *,
    timeout_seconds: float,
    idle_seconds: Optional[float],
    has_pending_inbound: bool,
) -> bool:
    """Return True when a prior stall notice may be cleared (episode ended)."""
    if not has_pending_inbound:
        return True
    if timeout_seconds <= 0:
        return True
    if idle_seconds is None:
        return True
    return idle_seconds < timeout_seconds


def format_session_stall_notification(idle_seconds: float) -> str:
    """User-facing stall warning (ASCII minutes; matches issue #72016 copy)."""
    mins = max(1, int(idle_seconds // 60))
    return (
        f"⚠️ Agent session appears stalled (last activity {mins} min ago). "
        f"Try /new to reset."
    )


def resolve_session_idle_seconds(
    *,
    now: float,
    last_activity_ts: Optional[float] = None,
    turn_started_ts: Optional[float] = None,
    pending_event_ts: Optional[float] = None,
) -> Optional[float]:
    """Pick the best available activity clock and return idle seconds.

    Preference order mirrors the issue's ``last_activity_at`` intent:
    1. Agent ``_last_activity_ts`` (API/tool/compaction progress)
    2. Turn start timestamp (agent still pending construction)
    3. Pending inbound event timestamp (last resort)
    """
    for ts in (last_activity_ts, turn_started_ts, pending_event_ts):
        if ts is None:
            continue
        try:
            idle = float(now) - float(ts)
        except (TypeError, ValueError):
            continue
        if idle < 0:
            return 0.0
        return idle
    return None
