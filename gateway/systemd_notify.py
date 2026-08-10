"""systemd ``sd_notify(3)`` event-loop watchdog (opt-in, #gateway-hang).

The gateway runs under systemd. The in-process Discord liveness probe and the
reconnect watcher recover *detectable* failures, but they all run ON the asyncio
event loop. If the loop itself wedges (a blocking call, a deadlock, a stuck
task), nothing on the loop can recover it — the process stays alive, Discord
goes silent, and only a manual ``systemctl restart`` recovers it. That is the
"unresponsive until I restart the gateway" failure mode.

This module closes that gap by feeding systemd's watchdog (``WatchdogSec=``) from
an asyncio task. The ping originates ON the event loop, so a wedged loop *stops
pinging* and systemd kills + restarts the service. As a secondary signal, each
tick measures its own scheduling lag: if a heartbeat cycle runs late (the loop
was blocked but not fully dead), the watchdog latches ``unhealthy``, reports a
``STATUS=`` diagnostic, and stops sending ``WATCHDOG=1`` so systemd still acts.

CRITICAL design rule: the ping MUST come from the event loop, never from a
background thread. A thread would keep pinging happily while the loop is frozen,
which is exactly the failure this exists to catch.

Everything here is stdlib-only (no ``sdnotify`` / ``systemd`` PyPI dependency)
and best-effort: a liveness mechanism must never be able to crash the gateway.
The feature is gated by ``gateway.systemd_watchdog_seconds`` in config; when
disabled (0), :class:`SystemdWatchdog` is a no-op and ``Type=simple`` is
preserved.

Lifecycle state transitions and ``STOPPING=1`` are owned exclusively by
:meth:`SystemdWatchdog.stop`. A heartbeat that self-exits on unhealthy latch
or error retains a completed task handle until ``stop()`` runs. When
``stop()`` is externally cancelled while the heartbeat is still winding down,
a done-callback clears the orphaned task reference once the coroutine exits
and lifecycle state is already idle.

:class:`SystemdWatchdog` additionally requires a valid runtime environment:
``NOTIFY_SOCKET``, positive finite ``WATCHDOG_USEC``, ``AF_UNIX`` support, and
(when set) a matching ``WATCHDOG_PID``. Missing or invalid watchdog env yields
``enabled=False`` and ``start()`` returns False — there is no fallback interval.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_STATUS_LEN = 256

# Lifecycle states for start/stop serialization.
_STATE_IDLE = "idle"
_STATE_RUNNING = "running"
_STATE_STOPPING = "stopping"


def _sanitize_status(status: str) -> str:
    """Return a single-line STATUS payload safe for sd_notify datagrams."""
    if not isinstance(status, str):
        status = str(status)
    sanitized = status.replace("\r", " ").replace("\n", " ").replace("\0", " ")
    if len(sanitized) > _MAX_STATUS_LEN:
        sanitized = sanitized[:_MAX_STATUS_LEN]
    return sanitized


# ── NOTIFY_SOCKET handling ──────────────────────────────────────────────────
#
# systemd provides ``$NOTIFY_SOCKET`` to the main process when ``WatchdogSec=`` /
# ``NotifyAccess=`` are configured. Per sd_notify(3) it is an AF_UNIX path or,
# with a leading "@", an abstract-namespace socket.


def _resolve_notify_socket() -> Optional[str]:
    """Return ``$NOTIFY_SOCKET`` (or None). Read fresh on every call.

    systemd sets this once at process start and we read it on each ping. We
    deliberately do NOT cache across calls: the value can differ between
    invocations in tests, and a stale cache would silently route pings to a
    dead socket. The cost is one ``os.environ`` lookup per ping (~every 15-45s),
    which is negligible.
    """
    raw = os.environ.get("NOTIFY_SOCKET", "").strip()
    return raw or None


def notify(message: str) -> bool:
    """Send a single ``sd_notify`` datagram. Best-effort; never raises.

    Returns True if a datagram was sent, False if there is no notify socket or
    the send failed. Uses a non-blocking datagram send so a wedged systemd
    notify socket can never block the event loop. ``message`` is the raw payload,
    e.g. ``"WATCHDOG=1"`` or ``"READY=1\\nSTATUS=running"``.
    """
    addr = _resolve_notify_socket()
    if not addr:
        return False
    try:
        if addr.startswith("@"):
            # Abstract namespace socket: leading "@" → NUL byte.
            sock_addr = "\0" + addr[1:]
        else:
            sock_addr = addr
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            try:
                sock.connect(sock_addr)
            except BlockingIOError:
                # A non-blocking connect can report EINPROGRESS even though an
                # AF_UNIX datagram "connection" (just a default destination) is
                # effectively established. Proceed to send; a genuinely
                # unreachable socket still fails at send() and is caught below.
                pass
            sock.send(message.encode("utf-8"))
        return True
    except Exception:
        logger.debug("sd_notify(%r) failed", message, exc_info=True)
        return False


def watchdog_interval_seconds() -> Optional[float]:
    """Return systemd's watchdog timeout (``$WATCHDOG_USEC``) in seconds.

    Returns None when the variable is missing, invalid, or non-positive (i.e.
    systemd is not watching this process). Reading it at runtime keeps the ping
    cadence in sync with the unit file's ``WatchdogSec``.
    """
    raw = os.environ.get("WATCHDOG_USEC", "").strip()
    if not raw:
        return None
    try:
        usec = int(raw)
    except (TypeError, ValueError):
        return None
    if usec <= 0:
        return None
    try:
        seconds = usec / 1_000_000.0
    except OverflowError:
        return None
    if not math.isfinite(seconds):
        return None
    return seconds


def _watchdog_pid_matches() -> bool:
    """Return True when ``WATCHDOG_PID`` is absent or matches this process."""
    raw = os.environ.get("WATCHDOG_PID", "").strip()
    if not raw:
        return True
    try:
        return int(raw) == os.getpid()
    except (TypeError, ValueError):
        return False


def _runtime_watchdog_available() -> bool:
    """Return True when the process env supports an in-loop systemd watchdog."""
    if not hasattr(socket, "AF_UNIX"):
        return False
    if not _resolve_notify_socket():
        return False
    wd = watchdog_interval_seconds()
    if wd is None or wd <= 0:
        return False
    if not _watchdog_pid_matches():
        return False
    return True


class SystemdWatchdog:
    """Feeds systemd's watchdog from the asyncio event loop.

    A background task pings ``WATCHDOG=1`` at roughly half the systemd timeout.
    Because the task runs on the loop, a wedged loop stops pinging and systemd
    restarts the service. Each tick also measures its own scheduling lag; if a
    cycle runs late (loop blocked but not fully dead) the watchdog latches
    ``unhealthy``, emits a ``STATUS=`` diagnostic, and stops pinging.

    Gated by ``config_enabled`` (from ``gateway.systemd_watchdog_seconds > 0``)
    *and* a valid systemd watchdog runtime environment. When disabled or the env
    is incomplete, every method is a no-op and ``Type=simple`` is preserved.

    Lifecycle: ``stop()`` owns every transition to idle, every ``STOPPING=1``
    send, and every ``_task`` clear. When the heartbeat self-exits (unhealthy
    latch or unexpected error) it sets ``_stopped`` but leaves ``_state``
    ``running`` and retains the completed task handle until ``stop()`` runs.
    ``start()`` after a self-exit clears a done task and schedules a fresh
    heartbeat; ``STOPPING=1`` is sent exactly once on the next ``stop()``. If
    ``stop()`` itself is externally cancelled while the heartbeat is still
    winding down, ``STOPPING=1`` is still sent; the orphaned task reference
    clears autonomously when the coroutine finishes (a subsequent ``start()``
    refuses only while that coroutine is still live).
    """

    def __init__(
        self,
        *,
        config_enabled: bool = True,
        lag_tolerance_seconds: Optional[float] = None,
        ping_interval_seconds: Optional[float] = None,
    ) -> None:
        self.config_enabled = bool(config_enabled)
        self._lag_tolerance = lag_tolerance_seconds
        self._ping_interval_override = ping_interval_seconds
        self._resolved_ping_interval: Optional[float] = None
        self.unhealthy = False
        self._last_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._state = _STATE_IDLE
        self._lock: Optional[asyncio.Lock] = None
        self._stopping_sent = False

    @property
    def enabled(self) -> bool:
        return self.config_enabled and _runtime_watchdog_available()

    def start(self) -> bool:
        """Start the heartbeat task. Returns True if enabled and started."""
        if not self.enabled:
            return False
        if self._state == _STATE_STOPPING:
            return False
        if self._task is not None and not self._task.done():
            if self._state == _STATE_RUNNING:
                return True
            # Orphaned heartbeat still finishing after a cancelled ``stop()``.
            return False
        if self._task is not None and self._task.done():
            self._task = None

        interval = self._resolve_ping_interval()
        if interval is None:
            return False

        # Deliberate restart after unhealthy: reset latch and error state.
        self.unhealthy = False
        self._last_error = None
        self._stopped = False
        self._stopping_sent = False
        self._resolved_ping_interval = interval

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            if self._lock is None:
                self._lock = asyncio.Lock()
            self._state = _STATE_RUNNING
            task = loop.create_task(self._heartbeat())
            task.add_done_callback(self._on_heartbeat_done)
            self._task = task
        except Exception:
            logger.debug("Failed to start systemd watchdog heartbeat", exc_info=True)
            self._state = _STATE_IDLE
            self._task = None
            return False
        return True

    def ready(self, status: str = "Hermes Gateway running") -> bool:
        """Signal ``READY=1`` (harmless under Type=simple). Returns True if enabled."""
        if not self.enabled:
            return False
        safe_status = _sanitize_status(status)
        return notify(f"READY=1\nSTATUS={safe_status}")

    def record_tick(self, *, deadline: float, now: float) -> bool:
        """Record one heartbeat cycle. Pings ``WATCHDOG=1`` when the loop kept up.

        ``deadline`` is when the cycle was due (before ``asyncio.sleep``) and
        ``now`` is when the sleep resumed; ``max(0, now - deadline)`` is the
        loop's scheduling lag for that cycle. If the lag exceeds the tolerance
        the watchdog latches ``unhealthy``, reports a ``STATUS=`` diagnostic, and
        returns False (and stops pinging, so systemd acts). Returns True when a
        ``WATCHDOG=1`` ping was sent.

        Invalid inputs (non-numeric, NaN, inf) return False without raising and
        without latching unhealthy. No-ops (returns False, no notify) once the
        watchdog has stopped or the heartbeat is not in the running lifecycle
        state — including late direct calls after ``stop()`` has sent
        ``STOPPING=1``.
        """
        if not self.enabled:
            return False
        if self._stopped or self._state != _STATE_RUNNING:
            return False
        if self.unhealthy:
            return False

        try:
            deadline_f = float(deadline)
            now_f = float(now)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(deadline_f) or not math.isfinite(now_f):
            return False

        tolerance = self._resolve_lag_tolerance()
        lag = max(0.0, now_f - deadline_f)
        if lag > tolerance:
            self.unhealthy = True
            msg = _sanitize_status(
                "watchdog unhealthy: event loop lag "
                f"{lag:.3f}s exceeds tolerance {tolerance:.3f}s"
            )
            notify(f"STATUS={msg}")
            return False
        return notify("WATCHDOG=1")

    async def stop(self) -> None:
        """Cancel the heartbeat and signal ``STOPPING=1`` (last notification).

        Cleanup (clear task, send ``STOPPING=1`` once, return to idle) runs in
        a ``finally`` block so an external cancellation of ``stop()`` itself
        cannot wedge lifecycle state in ``STOPPING``.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            if self._state == _STATE_IDLE and (
                self._task is None or self._task.done()
            ):
                if self._task is not None and self._task.done():
                    self._task = None
                return

            self._state = _STATE_STOPPING
            self._stopped = True

            task = self._task
            try:
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling() > 0:
                            raise
                    except Exception:
                        logger.debug(
                            "systemd watchdog heartbeat task ended with error",
                            exc_info=True,
                        )
            finally:
                # Cleanup runs even when stop() itself is externally cancelled
                # so lifecycle state cannot wedge in STOPPING. Only drop the
                # task handle once the coroutine has actually finished — a
                # cancelled stop() may exit before the heartbeat completes.
                if task is None or task.done():
                    self._task = None
                if (
                    not self._stopping_sent
                    and self.config_enabled
                    and _resolve_notify_socket()
                ):
                    notify("STOPPING=1")
                    self._stopping_sent = True
                self._state = _STATE_IDLE

    # ── internals ───────────────────────────────────────────────────────────

    def _reconcile_heartbeat_exit(self) -> None:
        """Record heartbeat self-exit; ``stop()`` owns idle transition.

        The heartbeat loop exits on unhealthy latch or unexpected error. State
        stays ``running`` with a completed task handle so a later ``stop()``
        still sends ``STOPPING=1`` exactly once.
        """
        self._stopped = True

    def _on_heartbeat_done(self, task: asyncio.Task) -> None:
        """Clear an orphaned task handle after ``stop()`` already returned idle."""
        try:
            if self._state != _STATE_IDLE:
                return
            if self._task is not task:
                return
            self._task = None
        except Exception:
            logger.debug(
                "systemd watchdog orphan heartbeat reconciliation failed",
                exc_info=True,
            )

    def _watchdog_budget(self) -> Optional[float]:
        wd = watchdog_interval_seconds()
        if wd is None or not math.isfinite(wd) or wd <= 0:
            return None
        return wd

    def _safe_default_interval(self, wd: float) -> float:
        return max(wd / 2.0, 0.001)

    def _safe_default_tolerance(self, wd: float, interval: float) -> float:
        """Tolerance strictly inside the remaining watchdog budget after a ping."""
        remaining = max(wd - interval, 0.001)
        # Use 40% of the post-ping slack, capped so interval + tolerance < wd.
        tolerance = remaining * 0.4
        tolerance = min(tolerance, max(wd - interval - 0.001, 0.001))
        return max(tolerance, 0.001)

    def _validate_positive_finite(self, value: object) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(parsed) or parsed <= 0:
            return None
        return parsed

    def _resolve_ping_interval(self) -> Optional[float]:
        wd = self._watchdog_budget()
        if wd is None:
            return None

        default_interval = self._safe_default_interval(wd)
        interval = default_interval

        override = self._validate_positive_finite(self._ping_interval_override)
        if self._ping_interval_override is not None:
            if override is None:
                interval = default_interval
            else:
                tolerance = self._compute_lag_tolerance(wd, override, self._lag_tolerance)
                if override + tolerance >= wd:
                    interval = default_interval
                else:
                    interval = override

        tolerance = self._compute_lag_tolerance(wd, interval, self._lag_tolerance)
        if interval + tolerance >= wd:
            return None
        return interval

    def _compute_lag_tolerance(
        self,
        wd: float,
        interval: float,
        override: Optional[float],
    ) -> float:
        safe_default = self._safe_default_tolerance(wd, interval)
        if override is None:
            return safe_default
        parsed = self._validate_positive_finite(override)
        if parsed is None:
            return safe_default
        if interval + parsed >= wd:
            return safe_default
        return parsed

    def _resolve_lag_tolerance(self) -> float:
        wd = self._watchdog_budget() or 0.0
        interval = self._resolved_ping_interval
        if interval is None:
            interval = self._safe_default_interval(wd) if wd > 0 else 0.001
        return self._compute_lag_tolerance(wd, interval, self._lag_tolerance)

    async def _heartbeat(self) -> None:
        interval = self._resolved_ping_interval
        if interval is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        while not self._stopped:
            try:
                deadline = loop.time() + interval
                await asyncio.sleep(interval)
                if self._stopped:
                    return
                now = loop.time()
                self.record_tick(deadline=deadline, now=now)
                if self.unhealthy:
                    # Latched unhealthy: stop pinging so systemd acts on timeout.
                    self._reconcile_heartbeat_exit()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never let an unexpected per-cycle failure kill the sole task
                # without recording why; latch unhealthy and exit the loop.
                logger.exception("systemd watchdog heartbeat cycle failed")
                self.unhealthy = True
                self._last_error = str(exc)
                msg = _sanitize_status(f"watchdog unhealthy: heartbeat error: {exc}")
                notify(f"STATUS={msg}")
                self._reconcile_heartbeat_exit()
                return
