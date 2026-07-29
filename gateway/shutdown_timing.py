"""Shared timing policy for supervised gateway shutdown.

The shutdown watchdog is the global bound for the in-process drain and
post-drain cleanup path.  A service manager must wait longer than that bound
so the watchdog can write diagnostics and perform its controlled hard exit
before systemd escalates to SIGKILL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


# One global budget for everything after the configured agent drain window:
# interrupt grace, adapter teardown, agent cleanup, SessionDB close, and
# final marker/log writes.  The out-of-loop watchdog enforces this even when
# an individual cleanup operation wedges.
DEFAULT_SHUTDOWN_POST_DRAIN_CLEANUP_BUDGET_S = 60.0

# Time reserved after the in-process watchdog deadline for its stack dump,
# PID/runtime-lock release, log drain, and os._exit to complete before systemd
# is allowed to send SIGKILL to the cgroup.
DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S = 15.0

# A year is already far beyond a meaningful interactive/service shutdown.
# Bounding every timing component keeps monotonic deadlines finite, makes the
# generated systemd duration portable, and prevents float precision from
# collapsing the supervisor-only margin for absurd custom values.
MAX_GATEWAY_SHUTDOWN_TIMING_COMPONENT_S = 365.0 * 24.0 * 60.0 * 60.0

# The service manager must always be strictly later than the in-process
# watchdog, even if a caller explicitly supplies a zero custom margin.
MIN_SYSTEMD_SHUTDOWN_KILL_MARGIN_S = 1.0


@dataclass(frozen=True)
class GatewayShutdownTiming:
    """Resolved elapsed-time deadlines from the start of ``stop()``."""

    drain_timeout_s: float
    post_drain_cleanup_budget_s: float
    controlled_exit_deadline_s: float
    systemd_kill_margin_s: float
    systemd_timeout_stop_sec: int


def _nonnegative_finite(value: Any, *, fallback: float) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(resolved):
        return fallback
    return min(
        max(resolved, 0.0),
        MAX_GATEWAY_SHUTDOWN_TIMING_COMPONENT_S,
    )


def resolve_gateway_shutdown_timing(
    drain_timeout: Any,
    *,
    post_drain_cleanup_budget_s: Any = (
        DEFAULT_SHUTDOWN_POST_DRAIN_CLEANUP_BUDGET_S
    ),
    systemd_kill_margin_s: Any = DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S,
) -> GatewayShutdownTiming:
    """Resolve the watchdog and service-manager deadlines from one policy.

    ``controlled_exit_deadline_s`` is the shutdown watchdog delay.  The
    generated ``TimeoutStopSec`` is strictly later by the configured margin
    and is rounded up so fractional drain values are never shortened.
    """

    drain = _nonnegative_finite(drain_timeout, fallback=0.0)
    cleanup_budget = _nonnegative_finite(
        post_drain_cleanup_budget_s,
        fallback=DEFAULT_SHUTDOWN_POST_DRAIN_CLEANUP_BUDGET_S,
    )
    kill_margin = max(
        _nonnegative_finite(
            systemd_kill_margin_s,
            fallback=DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S,
        ),
        MIN_SYSTEMD_SHUTDOWN_KILL_MARGIN_S,
    )
    controlled_exit_deadline = math.fsum((drain, cleanup_budget))
    # Add independently rounded terms. Summing as floats first can erase a
    # small margin next to a huge custom drain value; integer arithmetic keeps
    # TimeoutStopSec provably and strictly later than the watchdog deadline.
    timeout_stop_sec = math.ceil(controlled_exit_deadline) + math.ceil(kill_margin)
    if timeout_stop_sec <= controlled_exit_deadline:
        timeout_stop_sec = math.floor(controlled_exit_deadline) + 1
    return GatewayShutdownTiming(
        drain_timeout_s=drain,
        post_drain_cleanup_budget_s=cleanup_budget,
        controlled_exit_deadline_s=controlled_exit_deadline,
        systemd_kill_margin_s=kill_margin,
        systemd_timeout_stop_sec=timeout_stop_sec,
    )
