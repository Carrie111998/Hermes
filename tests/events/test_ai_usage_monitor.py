"""Scheduling contract for the resident (in-gateway) AI-usage collector.

The value of this monitor is entirely in what it refuses to do: it must not
block the shared subscriber poll loop, must not let two collections overlap,
and must not raise into the loop no matter how the collector itself fails.
Those are the properties pinned here. The collection itself is not exercised --
the runner is injected.
"""

from __future__ import annotations

import threading

import pytest

from events.producers.ai_usage_monitor import (
    PRODUCTION_FILENAME,
    SHADOW_FILENAME,
    AIUsageCollectorMonitor,
    resolve_mode,
)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _drain(monitor: AIUsageCollectorMonitor, timeout: float = 5.0) -> None:
    """Wait for any in-flight worker to finish, then reap it via check()."""
    worker = monitor._worker
    if worker is not None:
        worker.join(timeout)
        assert not worker.is_alive(), "worker did not finish"


def _monitor(clock, runner, **kwargs) -> AIUsageCollectorMonitor:
    kwargs.setdefault("mode", "shadow")
    kwargs.setdefault("interval_seconds", 900.0)
    kwargs.setdefault("start_immediately", True)
    return AIUsageCollectorMonitor(clock=clock, runner=runner, **kwargs)


# -- mode resolution ---------------------------------------------------------

def test_resolve_mode_defaults_to_shadow_when_unset():
    assert resolve_mode({}) == "shadow"


@pytest.mark.parametrize("value", ["off", "shadow", "on", "ON", " Shadow "])
def test_resolve_mode_accepts_valid_values_case_insensitively(value):
    assert resolve_mode({"HERMES_AI_USAGE_RESIDENT": value}) == value.strip().lower()


def test_unrecognised_mode_degrades_to_shadow_not_on():
    """A typo must never promote the resident collector to owning the snapshot.

    Two writers to ai-tokens.json would fight; failing safe means failing to
    shadow.
    """
    assert resolve_mode({"HERMES_AI_USAGE_RESIDENT": "yes"}) == "shadow"


# -- output routing ----------------------------------------------------------

def test_shadow_mode_writes_beside_production_not_over_it(tmp_path):
    m = AIUsageCollectorMonitor(mode="shadow", home=str(tmp_path))
    assert m.out_path.name == SHADOW_FILENAME
    assert m.out_path.parent.name == "architecture-map"


def test_on_mode_owns_the_production_snapshot(tmp_path):
    m = AIUsageCollectorMonitor(mode="on", home=str(tmp_path))
    assert m.out_path.name == PRODUCTION_FILENAME


def test_off_mode_is_disabled_and_never_runs(tmp_path):
    calls = []
    m = AIUsageCollectorMonitor(
        mode="off", home=str(tmp_path), runner=lambda p: calls.append(p),
        start_immediately=True,
    )
    assert not m.enabled
    m.check()
    assert calls == []


# -- scheduling --------------------------------------------------------------

def test_runs_once_when_due_and_writes_to_the_shadow_path(tmp_path):
    clock = FakeClock()
    calls = []
    m = _monitor(clock, lambda p: (calls.append(p), {"providers": [1, 2, 3]})[1],
                 home=str(tmp_path))

    m.check()
    _drain(m)

    assert len(calls) == 1
    assert calls[0].name == SHADOW_FILENAME
    assert m.runs_completed == 1
    assert m.last_provider_count == 3
    assert m.last_error is None


def test_does_not_run_again_before_the_interval_elapses(tmp_path):
    clock = FakeClock()
    calls = []
    m = _monitor(clock, lambda p: calls.append(p), home=str(tmp_path))

    m.check()
    _drain(m)
    clock.advance(899.0)
    m.check()
    _drain(m)

    assert len(calls) == 1


def test_runs_again_once_the_interval_has_elapsed(tmp_path):
    clock = FakeClock()
    calls = []
    m = _monitor(clock, lambda p: calls.append(p), home=str(tmp_path))

    m.check()
    _drain(m)
    clock.advance(900.0)
    m.check()
    _drain(m)

    assert len(calls) == 2


def test_interval_is_measured_from_completion_not_from_start(tmp_path):
    """A slow run must not be instantly re-due the moment it lands.

    The scheduled task this replaces stamps last_run_at at COMPLETION; keeping
    that semantic means a run that takes most of the interval still gets a full
    interval of quiet afterwards.
    """
    clock = FakeClock()
    calls = []

    def slow(path):
        clock.advance(800.0)  # run occupies most of the interval
        calls.append(path)

    m = _monitor(clock, slow, home=str(tmp_path))
    m.check()
    _drain(m)

    # 800s of the 900s interval was consumed by the run itself. If the interval
    # were measured from START, 100s more would make it due.
    clock.advance(100.0)
    m.check()
    _drain(m)
    assert len(calls) == 1

    clock.advance(800.0)
    m.check()
    _drain(m)
    assert len(calls) == 2


# -- overlap protection ------------------------------------------------------

def test_a_slow_run_causes_a_skip_never_a_second_concurrent_run(tmp_path):
    """Same guarantee MultipleInstancesPolicy=IgnoreNew gave at the task edge."""
    clock = FakeClock()
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def blocking(path):
        calls.append(path)
        entered.set()
        release.wait(5.0)

    m = _monitor(clock, blocking, home=str(tmp_path))
    m.check()
    assert entered.wait(5.0), "worker never started"

    # Interval elapses while the first run is still in flight.
    clock.advance(1800.0)
    m.check()
    m.check()

    assert len(calls) == 1, "a second run started while one was in flight"
    assert m.runs_skipped_in_flight == 2

    release.set()
    _drain(m)


def test_check_returns_promptly_while_a_run_is_in_flight(tmp_path):
    """check() runs on the shared poll loop; it must not wait on the worker."""
    clock = FakeClock()
    release = threading.Event()
    entered = threading.Event()

    def blocking(path):
        entered.set()
        release.wait(5.0)

    m = _monitor(clock, blocking, home=str(tmp_path))
    m.check()
    assert entered.wait(5.0)

    done = threading.Event()

    def call_check():
        m.check()
        done.set()

    threading.Thread(target=call_check, daemon=True).start()
    assert done.wait(1.0), "check() blocked while a run was in flight"

    release.set()
    _drain(m)


def test_a_finished_worker_is_reaped_so_the_next_run_can_start(tmp_path):
    clock = FakeClock()
    calls = []
    m = _monitor(clock, lambda p: calls.append(p), home=str(tmp_path))

    m.check()
    _drain(m)
    assert m._worker is not None  # not yet reaped; reaping happens on next tick

    clock.advance(900.0)
    m.check()
    _drain(m)
    assert len(calls) == 2


# -- failure containment -----------------------------------------------------

def test_a_failing_run_is_recorded_and_never_raises(tmp_path):
    clock = FakeClock()

    def boom(path):
        raise RuntimeError("provider exploded")

    m = _monitor(clock, boom, home=str(tmp_path))
    m.check()  # must not raise
    _drain(m)

    assert m.runs_failed == 1
    assert m.runs_completed == 0
    assert "provider exploded" in (m.last_error or "")


def test_a_failing_run_does_not_wedge_the_schedule(tmp_path):
    clock = FakeClock()
    calls = []

    def boom(path):
        calls.append(path)
        raise RuntimeError("nope")

    m = _monitor(clock, boom, home=str(tmp_path))
    m.check()
    _drain(m)
    clock.advance(900.0)
    m.check()
    _drain(m)

    assert len(calls) == 2, "a failed run blocked all later runs"


def test_check_swallows_a_clock_that_raises(tmp_path):
    """check() is called from the poll loop inside a try/except, but the loop
    logs and continues -- so anything raising here is still a visible error.
    Belt and braces: it must not propagate."""
    def angry_clock():
        raise OSError("clock unavailable")

    m = AIUsageCollectorMonitor(
        mode="shadow", home=str(tmp_path), clock=angry_clock,
        runner=lambda p: None,
    )
    m.check()  # must not raise


# -- diagnostics -------------------------------------------------------------

def test_status_reports_mode_target_and_counters(tmp_path):
    clock = FakeClock()
    m = _monitor(clock, lambda p: {"providers": [1]}, home=str(tmp_path))
    m.check()
    _drain(m)

    status = m.get_status()
    assert status["mode"] == "shadow"
    assert status["enabled"] is True
    assert status["out_path"].endswith(SHADOW_FILENAME)
    assert status["runs_completed"] == 1
    assert status["in_flight"] is False
