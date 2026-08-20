"""Turn an alive-but-deaf listener into an exit the supervisor already knows how to fix.

THE FAILURE
-----------
``asyncio``'s Windows proactor loop can abandon a listening socket permanently
while the process it belongs to stays perfectly healthy.
``BaseProactorEventLoop._start_serving`` defines an inner ``loop()`` that re-arms
itself with ``f.add_done_callback(loop)`` after every accept -- but only on the
``else:`` branch. On ``except OSError`` it reports ``'Accept failed on a socket'``
to the loop exception handler, calls ``sock.close()``, and does NOT re-arm. The
accept chain is then dead for the lifetime of the process.

``test_cpython_proactor_still_abandons_the_accept_chain`` pins that claim against
CPython's real source, so it fails loudly if a future CPython fixes it rather
than leaving this docstring to rot.

WHAT TRIGGERS IT HERE
---------------------
Always ``OSError(22, 'The specified network name is no longer available', ..., 64)``
-- WinError 64, raised out of ``finish_accept`` when a client connection
disappears between the IOCP accept completing and the socket being finished. The
*listening* socket is fine; CPython treats the error as fatal to it anyway. On
Unix the equivalent surfaces as ``ConnectionAbortedError`` and IS retried, so
this is a Windows-only sharp edge.

Measured on this box: three occurrences across three different Python services
-- ``agent-dashboard`` and ``errors-dashboard`` both at 2026-06-10 21:08:00, and
``hermes-session-bridge`` at 2026-08-20 07:45:16, which then stayed alive and
deaf for 78 minutes. Rare, host-wide, and total while it lasts.

WHY THIS SHAPE OF FIX
---------------------
Re-arming the accept chain in-process would avoid the outage entirely, but it
means shipping a copy of CPython's ``_start_serving`` internals and re-verifying
it on every interpreter bump -- a standing cost against an event seen roughly
once a quarter per service. Exiting instead reuses a recovery path that is
already proven live in ``launcher.log``: the supervisor replaces an exited child
and reaches healthy in ~20s. That converts a 78-minute outage into ~90s of
detection plus ~20s of restart, with no interpreter-version coupling.

WHY THE PROBE SPEAKS HTTP
-------------------------
A bare ``connect()`` is NOT a liveness check. The kernel completes the TCP
handshake out of the listen backlog with no participation from the process, so
connect succeeds against a listener whose accept loop is dead until the backlog
fills. ``test_bare_connect_cannot_detect_a_deaf_listener`` pins that. The probe
therefore waits for an application-level answer on ``/health``, which is a static
JSON route with no database work -- it costs nothing to ask and it proves the
whole accept -> ASGI -> respond path.

The watchdog runs on a plain thread, not an asyncio task, so that a wedged event
loop cannot also wedge the thing watching it.
"""

from __future__ import annotations

import http.client
import socket
import sys
import threading
from collections.abc import Callable
from typing import Final, Protocol, TextIO

# Reuses EXIT_DEGRADED from the CLI's contract. Never 1: exit code 1 is reserved
# in practice for an uncaught BaseException traceback and a separate
# investigation is keyed on it.
DEAF_LISTENER_EXIT_CODE: Final[int] = 3
DEAF_LISTENER_REASON: Final[str] = "service_listener_deaf"

DEFAULT_PROBE_PATH: Final[str] = "/health"
DEFAULT_INTERVAL_SECONDS: Final[float] = 30.0
DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0

# Three consecutive misses at 30s spacing => ~90s before the service gives up on
# itself. Wide enough to ride out the host stalls this machine actually shows
# (sub-second FS-filter and CDP spikes) without ever approaching the 78-minute
# outage the single-probe alternative would have to tolerate.
DEFAULT_THRESHOLD: Final[int] = 3

# A backstop for a listener that never answers at all. Arming normally happens
# on the first good answer, but a listener whose accept chain dies before the
# watchdog's very first probe would otherwise be ignored for the life of the
# process -- the exact permanent-outage shape this module exists to end. So
# after this many probes the watchdog arms anyway.
#
# 20 probes at 30s is ~10 minutes, which is a backstop and NOT a startup gate.
# It has to clear the slowest legitimate start: uvicorn runs the app lifespan
# (coordinator.start(), which scans provider catalogs) BEFORE it binds, so a
# slow-but-healthy start must never be shot. The launcher's own 10s health gate
# is what handles a service that fails to come up promptly; this only catches
# the case where nothing else ever will.
DEFAULT_ARM_AFTER_PROBES: Final[int] = 20

# How long a shutdown asked for politely gets before the process is ended the
# hard way. The observed failure leaves the event loop healthy, so the graceful
# path should win; this only covers the case where it does not.
DEFAULT_SHUTDOWN_GRACE_SECONDS: Final[float] = 20.0


class _Probe(Protocol):
    def __call__(self, host: str, port: int, *, timeout: float) -> bool: ...


def probe_listener(
    host: str,
    port: int,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    path: str = DEFAULT_PROBE_PATH,
) -> bool:
    """Cross the socket and require an answer. Never raises.

    Returns True only if the listener accepted the connection AND the
    application produced a response line. A 5xx still counts as alive -- this
    asks whether the service can be reached, not whether it is happy.
    """
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        return response.status > 0
    except (OSError, http.client.HTTPException, ValueError, socket.timeout):
        return False
    except Exception:
        # A probe must never be the thing that takes the service down.
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# A wildcard bind is an instruction about what to LISTEN on, not an address you
# can usefully connect to. Probing the literal "0.0.0.0" is not portable, and a
# probe that cannot connect for its own reasons would restart a perfectly
# healthy service every ~90s. Config allows a non-loopback bind
# (ServiceConfig.allow_non_loopback), so this is reachable, not hypothetical.
_WILDCARD_HOSTS: Final[dict[str, str]] = {
    "": "127.0.0.1",
    "0.0.0.0": "127.0.0.1",
    "*": "127.0.0.1",
    "::": "::1",
    "[::]": "::1",
}


def probe_host_for(bind_host: str) -> str:
    """The address to dial for a service bound to ``bind_host``."""
    return _WILDCARD_HOSTS.get(bind_host.strip(), bind_host)


def _default_wait(stop: threading.Event, seconds: float) -> bool:
    return stop.wait(seconds)


class ListenerWatchdog:
    """Watch one host:port from inside the process that serves it.

    Arming matters: the watchdog stays silent until the listener has answered at
    least once. Without that, a service that is still binding would shoot itself
    during startup, and the launcher's own 10s health gate already covers a
    listener that never comes up at all.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        on_deaf: Callable[[int], None],
        probe: _Probe = probe_listener,
        wait: Callable[[threading.Event, float], bool] = _default_wait,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        threshold: int = DEFAULT_THRESHOLD,
        arm_after_probes: int = DEFAULT_ARM_AFTER_PROBES,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        if arm_after_probes < 1:
            raise ValueError("arm_after_probes must be at least 1")
        self._host = probe_host_for(host)
        self._port = port
        self._on_deaf = on_deaf
        self._probe = probe
        self._wait = wait
        self._interval = interval
        self._timeout = timeout
        self._threshold = threshold
        self._arm_after_probes = arm_after_probes
        self._probes = 0
        self._armed = False
        self._fired = False
        self._consecutive = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        """The address actually dialled -- a wildcard bind is normalised."""
        return self._host

    @property
    def armed(self) -> bool:
        """True once the listener has answered, or once the backstop expired."""
        return self._armed

    @property
    def fired(self) -> bool:
        """True once ``on_deaf`` has been invoked. Never invoked twice."""
        return self._fired

    def run(self, stop: threading.Event) -> None:
        """Probe until the listener is judged deaf or ``stop`` is set."""
        while True:
            if self._wait(stop, self._interval):
                return
            try:
                answered = bool(
                    self._probe(self._host, self._port, timeout=self._timeout)
                )
            except Exception:
                # A probe implementation that raises is itself evidence of
                # nothing; treat it as an unanswered probe rather than letting
                # it kill the watchdog thread silently.
                answered = False
            self._probes += 1
            if answered:
                self._armed = True
                self._consecutive = 0
                continue
            if not self._armed:
                if self._probes < self._arm_after_probes:
                    continue
                # Never answered once, and it has had long enough. A listener
                # this silent is not starting up any more.
                self._armed = True
            self._consecutive += 1
            if self._consecutive < self._threshold:
                continue
            self._fired = True
            try:
                self._on_deaf(self._consecutive)
            except Exception:
                # The handler's job is to end the process. If it cannot, the
                # watchdog has nothing further to offer and must not spin.
                pass
            return

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run,
            args=(self._stop,),
            name="session-bridge-listener-watchdog",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return thread

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)


def make_deaf_listener_handler(
    request_shutdown: Callable[[], None],
    *,
    grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    hard_exit: Callable[[int], None] | None = None,
    timer_factory: Callable[..., threading.Timer] = threading.Timer,
    stream: TextIO | None = None,
) -> Callable[[int], None]:
    """Build the ``on_deaf`` callback: ask nicely, then insist.

    ``request_shutdown`` is the polite path (for uvicorn, setting
    ``server.should_exit``). The timer is the insistence -- it uses the same
    exit code, so a graceful shutdown and a forced one are indistinguishable to
    the supervisor.
    """
    import os

    exit_fn = hard_exit if hard_exit is not None else os._exit
    out: TextIO = stream if stream is not None else sys.stderr

    def _on_deaf(consecutive: int) -> None:
        try:
            print(
                f"session-bridge listener is deaf: {consecutive} consecutive "
                f"unanswered self-probes; exiting with "
                f"{DEAF_LISTENER_EXIT_CODE} ({DEAF_LISTENER_REASON}) so the "
                f"supervisor can replace this process",
                file=out,
                flush=True,
            )
        except Exception:
            pass
        try:
            request_shutdown()
        except Exception:
            pass

        def _insist() -> None:
            try:
                sys.stderr.flush()
            except Exception:
                pass
            exit_fn(DEAF_LISTENER_EXIT_CODE)

        timer = timer_factory(grace, _insist)
        timer.daemon = True
        timer.start()

    return _on_deaf
