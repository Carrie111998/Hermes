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
    return max(resolved, 0.0)


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
    kill_margin = _nonnegative_finite(
        systemd_kill_margin_s,
        fallback=DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S,
    )
    controlled_exit_deadline = drain + cleanup_budget
    timeout_stop_sec = math.ceil(controlled_exit_deadline + kill_margin)
    return GatewayShutdownTiming(
        drain_timeout_s=drain,
        post_drain_cleanup_budget_s=cleanup_budget,
        controlled_exit_deadline_s=controlled_exit_deadline,
        systemd_kill_margin_s=kill_margin,
        systemd_timeout_stop_sec=timeout_stop_sec,
    )
