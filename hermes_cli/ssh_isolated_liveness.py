"""Child-side liveness for SSH-isolated ``hermes serve`` (#101626).

``serve --isolated`` is ``setsid``/``nohup`` detached so it survives SSH
close (#91668). ``PPID=1`` is therefore normal, not an orphan signal, and
the local Electron ``HERMES_PARENT_PID`` watchdog cannot see a laptop on
the other side of the tunnel.

This module is the child-side contract that *is* compatible with that
detach:

- tunneled loopback is a half-open path, so WS protocol ping stays on;
- after a grace window with no authenticated client, the process may exit;
- two SSH-isolated serves must not both hold the same HERMES_HOME.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

# 15 minutes: longer than a brief reconnect (#91668), shorter than a night
# of Power Nap accumulation.
DEFAULT_SSH_ISOLATED_IDLE_GRACE_S = 900.0
# Ping interval/timeout for the SSH-isolated loopback path. Timeout sits
# above the documented GIL-stall class (~226s, #53773) so a live tunnel is
# not dropped by a long agent turn; a sleeping laptop still fails to pong.
SSH_ISOLATED_WS_PING_INTERVAL_S = 60.0
SSH_ISOLATED_WS_PING_TIMEOUT_S = 600.0
SSH_ISOLATED_LOCK_NAME = ".ssh-isolated-serve.lock"
SSH_ISOLATED_HOME_LOCKED_SENTINEL = "BACKEND_SSH_ISOLATED_HOME_LOCKED"


def ssh_isolated_ws_ping_window(
    *,
    is_loopback: bool,
    ssh_session_token: Optional[str],
    default_interval: float,
    default_timeout: float,
) -> tuple[Optional[float], Optional[float]]:
    """Return uvicorn ``ws_ping_interval`` / ``ws_ping_timeout``.

    Plain loopback Desktop has no network hop, so ping stays disabled.
    SSH-isolated loopback *is* a tunneled hop: enable ping even on 127.0.0.1.
    """
    token = (ssh_session_token or "").strip()
    if is_loopback and not token:
        return None, None
    if is_loopback and token:
        interval = max(float(default_interval), SSH_ISOLATED_WS_PING_INTERVAL_S)
        timeout = max(float(default_timeout), interval, SSH_ISOLATED_WS_PING_TIMEOUT_S)
        return interval, timeout
    return float(default_interval), float(default_timeout)


def ssh_isolated_should_exit(
    *,
    has_ssh_token: bool,
    now: float,
    last_client_at: float,
    grace_s: float,
    ppid: Optional[int] = None,
    turn_in_flight: bool = False,
) -> bool:
    """True when an SSH-isolated backend has been client-idle past grace.

    ``ppid`` is accepted and ignored: isolated remotes legitimately live at
    pid 1 after ``setsid``. An in-flight agent turn holds the process even
    with no client so a lid-close does not kill a running job.
    """
    del ppid
    if turn_in_flight:
        return False
    if not has_ssh_token:
        return False
    try:
        grace = float(grace_s)
    except (TypeError, ValueError):
        return False
    if grace <= 0:
        grace = DEFAULT_SSH_ISOLATED_IDLE_GRACE_S
    try:
        idle_for = float(now) - float(last_client_at)
    except (TypeError, ValueError):
        return False
    return idle_for >= grace


def acquire_ssh_isolated_home_lock(hermes_home) -> Optional[int]:
    """Non-blocking exclusive lock on ``{hermes_home}/.ssh-isolated-serve.lock``.

    Returns a held fd (keep it open for the process lifetime) or ``None``
    if another SSH-isolated serve already owns this home.
    """
    root = Path(hermes_home)
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(root / SSH_ISOLATED_LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _idle_grace_s() -> float:
    return DEFAULT_SSH_ISOLATED_IDLE_GRACE_S


class SshIsolatedIdleTracker:
    """Authenticated-client clock for the SSH-isolated idle watchdog."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._live = 0
        self._zero_since = clock()

    def on_open(self) -> None:
        with self._lock:
            self._live += 1

    def on_close(self) -> None:
        with self._lock:
            self._live = max(0, self._live - 1)
            if self._live == 0:
                self._zero_since = self._clock()

    def last_client_at(self, now: Optional[float] = None) -> float:
        now = self._clock() if now is None else now
        with self._lock:
            if self._live > 0:
                return now
            return self._zero_since

    def live_count(self) -> int:
        with self._lock:
            return self._live

    def touch(self) -> None:
        """Treat now as client activity (in-flight turn, live socket)."""
        with self._lock:
            self._zero_since = self._clock()

    def now(self) -> float:
        return self._clock()


_idle_tracker: Optional[SshIsolatedIdleTracker] = None


def note_ssh_isolated_client_open() -> None:
    if _idle_tracker is not None:
        _idle_tracker.on_open()


def note_ssh_isolated_client_close() -> None:
    if _idle_tracker is not None:
        _idle_tracker.on_close()


@contextmanager
def track_ssh_isolated_ws() -> Iterator[None]:
    """Count an authenticated WebSocket for the SSH-isolated idle clock."""
    note_ssh_isolated_client_open()
    try:
        yield
    finally:
        note_ssh_isolated_client_close()


def ssh_isolated_idle_step(
    *,
    has_ssh_token: bool,
    tracker: SshIsolatedIdleTracker,
    grace_s: float,
    turn_in_flight: bool,
) -> bool:
    """One watchdog tick. True → request graceful shutdown.

    An in-flight turn refreshes the idle clock so the client gets a full
    grace window after the job finishes.
    """
    if turn_in_flight:
        tracker.touch()
        return False
    return ssh_isolated_should_exit(
        has_ssh_token=has_ssh_token,
        now=tracker.now(),
        last_client_at=tracker.last_client_at(),
        grace_s=grace_s,
        turn_in_flight=False,
    )


def start_ssh_isolated_idle_watchdog(
    *,
    has_ssh_token: bool,
    poll_s: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    request_shutdown: Optional[Callable[[], None]] = None,
    turn_probe: Optional[Callable[[], bool]] = None,
    tracker: Optional[SshIsolatedIdleTracker] = None,
) -> Optional[SshIsolatedIdleTracker]:
    """Daemon thread: ask uvicorn to exit after idle grace. No-op without ssh token.

    ``request_shutdown`` should set ``server.should_exit = True`` so WAL and
    lifespan flush. ``os._exit`` is not used here.
    """
    global _idle_tracker
    if not has_ssh_token:
        return None
    owned = tracker or SshIsolatedIdleTracker(clock=clock)
    _idle_tracker = owned
    grace = _idle_grace_s()
    poll = max(0.5, float(poll_s))

    def _probe() -> bool:
        if turn_probe is None:
            return False
        try:
            return bool(turn_probe())
        except Exception:
            return False

    def _loop() -> None:
        while not ssh_isolated_idle_step(
            has_ssh_token=True,
            tracker=owned,
            grace_s=grace,
            turn_in_flight=_probe(),
        ):
            sleep_fn(poll)
        if request_shutdown is not None:
            request_shutdown()

    threading.Thread(target=_loop, daemon=True, name="ssh-isolated-idle").start()
    return owned
