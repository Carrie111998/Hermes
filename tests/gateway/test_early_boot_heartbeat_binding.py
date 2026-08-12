"""The early-boot heartbeat thread must stay bound to the home it started with.

``start_gateway`` starts a daemon thread that rewrites the gateway heartbeat
every 30s until ``runner.start()`` brings up the real writer. That thread can
outlive the scope that started it — ``_early_hb_stop.set()`` is skipped
entirely when ``runner.start()`` raises, and under pytest the thread outlives
the test's ``monkeypatch`` teardown either way.

If it resolves ``gateway_heartbeat_path()`` on every tick, the tick that lands
after teardown writes into whatever ``HERMES_HOME`` was *restored* to — the
real ``~/.hermes``. Five tests in ``tests/gateway/test_runner_startup_failures``
call ``start_gateway()`` directly, and they are startup-*failure* tests, so the
raising path is the one they exercise.

The rule (GBrain ``concepts/import-time-hermes-home-snapshot-bug``): resolve at
the moment the value's meaning is fixed, then CARRY it. For this thread that
moment is thread start — it is inside ``start_gateway``, well after any profile
override has set ``HERMES_HOME``, so capturing there is correct.
"""

import threading
import time

import pytest
from tests.gateway.hang_guards import HANG_GUARD_S


def _wait_for(predicate, timeout=HANG_GUARD_S, interval=0.02):
    """Bounded wait-until — never a fixed sleep (events invariant #4)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_heartbeat_thread_writes_only_to_the_home_captured_at_start(
    tmp_path, monkeypatch
):
    """The tick that lands after HERMES_HOME changes must not follow it."""
    from gateway.run import start_early_boot_heartbeat

    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home_a))

    stop = threading.Event()
    thread = start_early_boot_heartbeat(stop, interval_seconds=0.05)
    try:
        assert _wait_for(lambda: (home_a / "gateway.heartbeat").exists()), (
            "thread never wrote a heartbeat under the home it started with"
        )

        # The moment monkeypatch teardown restores the env under the thread.
        monkeypatch.setenv("HERMES_HOME", str(home_b))

        # Let several more ticks elapse under the NEW home.
        time.sleep(0.05 * 8)

        assert not (home_b / "gateway.heartbeat").exists(), (
            "heartbeat thread followed HERMES_HOME after teardown — it would "
            "write into the real ~/.hermes once a test restores the env"
        )
    finally:
        stop.set()
        thread.join(timeout=5)


def test_heartbeat_skips_the_write_when_its_captured_home_is_gone(tmp_path):
    """A thread bound to a deleted pytest tmp_path must not recreate it."""
    from events.gateway_integration import _write_heartbeat

    gone = tmp_path / "deleted_home"
    target = gone / "gateway.heartbeat"

    # `gone` was never created — stands in for a torn-down tmp_path.
    _write_heartbeat(0, path=target)

    assert not gone.exists(), (
        "writer recreated a home that no longer exists — a hook bound to a "
        "deleted tmp_path must leave no litter"
    )


def test_direct_callers_still_resolve_live(tmp_path, monkeypatch):
    """Passing no path keeps the current, correct behaviour for live callers."""
    from events.gateway_integration import _write_heartbeat

    home = tmp_path / "live_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    _write_heartbeat(0)

    assert (home / "gateway.heartbeat").exists()


def test_start_gateway_stops_the_heartbeat_even_when_start_raises():
    """The stop event must be set on the raising path, not just the happy one.

    ``_early_hb_stop.set()`` sits after ``await runner.start()`` with no
    ``finally``, so an exception there leaks the thread for the life of the
    process. Assert the source shape rather than booting a real gateway.
    """
    import inspect

    from gateway import run as gateway_run

    src = inspect.getsource(gateway_run.start_gateway)
    assert "_early_hb_stop" in src, "early-boot heartbeat wiring disappeared"

    start_idx = src.index("_early_hb_stop = threading.Event()")
    tail = src[start_idx:]
    set_idx = tail.index("_early_hb_stop.set()")
    between = tail[:set_idx]

    assert "try:" in between and "finally:" in tail[:set_idx + 200], (
        "_early_hb_stop.set() is not protected by a finally — if "
        "runner.start() raises, the heartbeat thread runs for the life of "
        "the process, resolving HERMES_HOME on every tick"
    )


def test_early_returns_retire_the_nous_auth_keepalive():
    """The keepalive must not outlive a start_gateway() that returns early.

    ``start_nous_auth_keepalive()`` runs during boot but the only
    ``stop_nous_auth_keepalive()`` used to sit after ``wait_for_shutdown()``,
    so a failed start left a daemon thread that waits 60s and then re-resolves
    HERMES_HOME to touch the auth store — i.e. the real ``~/.hermes/auth.json``
    once a test restores the env.
    """
    import inspect

    from gateway import run as gateway_run

    src = inspect.getsource(gateway_run.start_gateway)
    start_idx = src.index("start_nous_auth_keepalive()")
    tail = src[start_idx:]

    # Every early `return` between the keepalive start and wait_for_shutdown()
    # must retire it first.
    shutdown_idx = tail.index("await runner.wait_for_shutdown()")
    pre_shutdown = tail[:shutdown_idx]

    assert pre_shutdown.count("_stop_nous_keepalive_quietly()") >= 2, (
        "an early return path leaves the nous auth keepalive thread running; "
        "it will re-resolve HERMES_HOME 60s later and write to the auth store"
    )


@pytest.mark.parametrize("attr", ["_stop_nous_keepalive_quietly", "start_early_boot_heartbeat"])
def test_helpers_are_module_level_and_testable(attr):
    """Both seams must stay importable — a closure cannot be regression-tested."""
    from gateway import run as gateway_run

    assert callable(getattr(gateway_run, attr))
