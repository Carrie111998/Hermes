"""The listener watchdog must cross a real socket, and must fire only on a real deaf listener.

Every probe test here binds an actual TCP port. That is deliberate: the failure
this module exists for -- an alive-but-deaf :7484 -- was invisible to every
in-process check the service had, and a test that stubs the socket would be
invisible to it too.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable

import pytest

from session_bridge.listener_watchdog import (
    DEAF_LISTENER_REASON,
    ListenerWatchdog,
    probe_listener,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _MinimalHttpListener:
    """A listener that accepts and answers -- the healthy control."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b'Content-Type: application/json\r\n'
                        b"Content-Length: 15\r\n"
                        b"Connection: close\r\n"
                        b'\r\n{"status":"ok"}'
                    )
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class _DeafListener:
    """Bound and listening, but nothing ever calls accept().

    This is the trap the watchdog has to survive. The Windows kernel completes
    the TCP handshake out of the listen backlog with no help from the process,
    so a bare ``connect()`` SUCCEEDS against this socket. Only a probe that
    waits for an application-level answer can tell it apart from a healthy one.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# probe_listener -- the part that crosses the socket
# --------------------------------------------------------------------------


def test_probe_succeeds_against_a_listener_that_answers() -> None:
    listener = _MinimalHttpListener()
    try:
        assert probe_listener("127.0.0.1", listener.port, timeout=5.0) is True
    finally:
        listener.close()


def test_probe_fails_against_a_closed_port() -> None:
    port = _free_port()
    assert probe_listener("127.0.0.1", port, timeout=5.0) is False


def test_bare_connect_cannot_detect_a_deaf_listener() -> None:
    """Pins WHY the probe speaks HTTP instead of just connecting.

    If this test ever fails, a bare connect() became a valid liveness check on
    this platform and the probe could be simplified. Until then, do not.
    """
    listener = _DeafListener()
    try:
        with socket.create_connection(
            ("127.0.0.1", listener.port), timeout=5.0
        ) as sock:
            assert sock is not None  # the handshake completed with no accept()
    finally:
        listener.close()


def test_probe_fails_against_a_bound_but_deaf_listener() -> None:
    listener = _DeafListener()
    try:
        assert probe_listener("127.0.0.1", listener.port, timeout=1.0) is False
    finally:
        listener.close()


def test_probe_swallows_a_hostile_host_instead_of_raising() -> None:
    assert probe_listener("", -1, timeout=0.5) is False


@pytest.mark.parametrize(
    ("bind_host", "expected"),
    [
        ("0.0.0.0", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("*", "127.0.0.1"),
        ("::", "::1"),
        ("[::]", "::1"),
        ("127.0.0.1", "127.0.0.1"),
        ("192.168.1.5", "192.168.1.5"),
    ],
)
def test_a_wildcard_bind_is_dialled_on_the_loopback_address(
    bind_host: str, expected: str
) -> None:
    """A wildcard says what to listen on; it is not an address to connect to.

    Without this the watchdog would fail its own probe for reasons that have
    nothing to do with the listener, and restart a healthy service every ~90s.
    """
    from session_bridge.listener_watchdog import probe_host_for

    assert probe_host_for(bind_host) == expected


def test_the_watchdog_reports_the_address_it_actually_dials() -> None:
    watchdog = ListenerWatchdog(
        host="0.0.0.0", port=7484, on_deaf=lambda _n: None
    )
    assert watchdog.host == "127.0.0.1"


# --------------------------------------------------------------------------
# ListenerWatchdog -- the escalation policy, driven by a scripted probe
# --------------------------------------------------------------------------


def _watchdog(
    results: list[bool],
    *,
    threshold: int = 3,
    arm_after_probes: int = 10_000,
    on_deaf: Callable[[int], None] | None = None,
) -> tuple[ListenerWatchdog, list[int], threading.Event]:
    fired: list[int] = []
    stop = threading.Event()
    remaining = list(results)

    def _probe(host: str, port: int, *, timeout: float) -> bool:
        return remaining.pop(0)

    def _wait(event: threading.Event, seconds: float) -> bool:
        # Ending the script must not itself look like a probe result -- an
        # exhausted script stops the loop BEFORE the next probe, so it cannot
        # arm the watchdog or extend a failure run.
        return event.is_set() or not remaining

    watchdog = ListenerWatchdog(
        host="127.0.0.1",
        port=7484,
        on_deaf=on_deaf or fired.append,
        probe=_probe,
        wait=_wait,
        interval=0.0,
        timeout=0.0,
        threshold=threshold,
        arm_after_probes=arm_after_probes,
    )
    return watchdog, fired, stop


def test_watchdog_does_not_fire_before_the_listener_ever_answered() -> None:
    """A service that is still binding must not be shot by its own watchdog."""
    watchdog, fired, stop = _watchdog([False] * 8, arm_after_probes=20)
    watchdog.run(stop)
    assert fired == []
    assert watchdog.fired is False
    assert watchdog.armed is False


def test_a_listener_that_never_answers_is_not_ignored_forever() -> None:
    """The backstop. Found by a live test, not by reasoning.

    Arming on first success alone left a hole: an accept chain that dies before
    the watchdog's first probe never arms it, so the process sits deaf for its
    whole life -- the exact outage this module exists to end. A listener that
    has never answered across the full backstop window is broken, not starting.
    """
    watchdog, fired, stop = _watchdog(
        [False] * 20, threshold=3, arm_after_probes=5
    )
    watchdog.run(stop)
    assert watchdog.armed is True
    assert fired == [3], "the backstop must arm and then still honour the threshold"


def test_the_backstop_is_a_backstop_and_not_a_startup_gate() -> None:
    """uvicorn runs the app lifespan BEFORE it binds; a slow start must survive.

    If someone shrinks this default, a legitimately slow coordinator startup
    starts getting the service killed and restarted in a loop.
    """
    from session_bridge.listener_watchdog import (
        DEFAULT_ARM_AFTER_PROBES,
        DEFAULT_INTERVAL_SECONDS,
    )

    window = DEFAULT_ARM_AFTER_PROBES * DEFAULT_INTERVAL_SECONDS
    assert window >= 600, f"backstop window is only {window}s; too tight for startup"


def test_a_late_first_answer_still_arms_normally() -> None:
    """Reaching the backstop must not poison a listener that then recovers."""
    watchdog, fired, stop = _watchdog(
        [False, False, True, False, False], threshold=3, arm_after_probes=2
    )
    watchdog.run(stop)
    assert watchdog.armed is True
    assert fired == [], "a success must clear the failure run the backstop started"


def test_watchdog_fires_after_threshold_consecutive_failures_once_armed() -> None:
    watchdog, fired, stop = _watchdog([True, False, False, False, False])
    watchdog.run(stop)
    assert fired == [3]
    assert watchdog.fired is True


def test_watchdog_does_not_fire_below_the_threshold() -> None:
    watchdog, fired, stop = _watchdog([True, False, False])
    watchdog.run(stop)
    assert fired == []
    assert watchdog.fired is False


def test_a_single_good_answer_resets_the_failure_run() -> None:
    """A transient host stall must not accumulate across minutes."""
    watchdog, fired, stop = _watchdog([True, False, False, True, False, False])
    watchdog.run(stop)
    assert fired == []
    assert watchdog.fired is False


def test_watchdog_fires_exactly_once() -> None:
    watchdog, fired, stop = _watchdog([True] + [False] * 30)
    watchdog.run(stop)
    assert fired == [3]


def test_watchdog_stops_when_the_stop_event_is_set() -> None:
    watchdog, fired, stop = _watchdog([True, False, False, False])
    stop.set()
    watchdog.run(stop)
    assert fired == []
    assert watchdog.armed is False


def test_watchdog_survives_a_probe_that_raises() -> None:
    """The watchdog must never take the process down by its own exception."""
    calls: list[int] = []
    stop = threading.Event()
    state = {"n": 0}

    def _probe(host: str, port: int, *, timeout: float) -> bool:
        state["n"] += 1
        if state["n"] == 1:
            return True
        if state["n"] > 6:
            stop.set()
            return True
        raise RuntimeError("probe blew up")

    watchdog = ListenerWatchdog(
        host="127.0.0.1",
        port=7484,
        on_deaf=calls.append,
        probe=_probe,
        wait=lambda event, seconds: event.is_set(),
        interval=0.0,
        timeout=0.0,
        threshold=3,
    )
    watchdog.run(stop)
    assert calls == [3]


def test_on_deaf_raising_does_not_escape_the_watchdog_thread() -> None:
    def _explode(_consecutive: int) -> None:
        raise RuntimeError("handler blew up")

    watchdog, _fired, stop = _watchdog([True] + [False] * 5, on_deaf=_explode)
    watchdog.run(stop)
    assert watchdog.fired is True


# --------------------------------------------------------------------------
# Live wiring -- a real thread against a real port that really dies
# --------------------------------------------------------------------------


def test_watchdog_thread_fires_when_a_real_listener_stops_answering() -> None:
    listener = _MinimalHttpListener()
    fired = threading.Event()
    watchdog = ListenerWatchdog(
        host="127.0.0.1",
        port=listener.port,
        on_deaf=lambda _n: fired.set(),
        interval=0.05,
        timeout=1.0,
        threshold=2,
    )
    watchdog.start()
    try:
        # Arm against the healthy listener before killing it, otherwise the
        # watchdog is entitled to stay quiet forever.
        for _ in range(200):
            if watchdog.armed:
                break
            threading.Event().wait(0.05)
        assert watchdog.armed, "watchdog never saw the listener answer"
        listener.close()
        assert fired.wait(20.0), "watchdog missed a listener that stopped answering"
    finally:
        watchdog.stop()
        listener.close()


def test_reason_is_not_exit_code_one() -> None:
    """Exit code 1 is reserved for an uncaught BaseException traceback."""
    from session_bridge.listener_watchdog import DEAF_LISTENER_EXIT_CODE

    assert DEAF_LISTENER_EXIT_CODE == 3
    assert DEAF_LISTENER_REASON == "service_listener_deaf"


# --------------------------------------------------------------------------
# The upstream defect this module compensates for
# --------------------------------------------------------------------------


def test_cpython_proactor_still_abandons_the_accept_chain() -> None:
    """Pins the claim in the module docstring to CPython's actual source.

    ``BaseProactorEventLoop._start_serving`` re-arms via ``add_done_callback``
    only in its ``else:`` branch; the ``except OSError`` branch reports and
    closes the socket. If CPython ever starts re-arming there, this watchdog's
    reason for existing is gone and it should be re-evaluated -- so this test is
    expected to FAIL on that upgrade, loudly, rather than leave a stale comment.
    """
    import inspect

    from asyncio import proactor_events

    source = inspect.getsource(proactor_events.BaseProactorEventLoop._start_serving)
    assert "Accept failed on a socket" in source
    except_branch = source.split("except OSError as exc:", 1)[1].split(
        "except exceptions.CancelledError", 1
    )[0]
    assert "sock.close()" in except_branch
    assert "add_done_callback" not in except_branch


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
