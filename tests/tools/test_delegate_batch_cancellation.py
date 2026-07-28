"""Batch delegation must return promptly when the parent is interrupted.

The interrupt path used to `break` out of the polling loop and then fall out of
a `with DaemonThreadPoolExecutor(...)` block. ThreadPoolExecutor.__exit__ calls
shutdown(wait=True) unconditionally, so the parent still blocked for the
slowest child — the escape hatch did not escape.

These tests exercise the executor lifecycle directly with the same daemon pool
and the same shutdown policy, because driving the real delegate_task requires a
full agent. They assert the properties that matter: prompt return, preserved
completed results, cancellation of unstarted work, and honest tracking of what
may still be running.
"""

from __future__ import annotations

import threading
import time

import pytest

from tools.daemon_pool import DaemonThreadPoolExecutor

# Generous enough not to flake on a loaded machine, far below the 30s a real
# wedged child would cost if shutdown still joined.
PROMPT_RETURN_BUDGET = 5.0


class _Harness:
    """Mirrors the executor lifecycle in delegate_task's batch branch."""

    def __init__(self, workers: int):
        self.executor = DaemonThreadPoolExecutor(max_workers=workers)
        self.interrupted = False
        self.still_running: list[int] = []
        self.results: list[dict] = []

    def shutdown(self) -> None:
        if self.interrupted:
            self.executor.shutdown(wait=False, cancel_futures=True)
        else:
            self.executor.shutdown(wait=True)

    def drain_on_interrupt(self, futures: dict) -> None:
        self.interrupted = True
        for f, idx in futures.items():
            cancelled = f.cancel()
            if not cancelled and not f.done():
                self.still_running.append(idx)
            # cancel() makes done() True and result() raise CancelledError, so
            # the cancelled branch must come first or work that never ran is
            # misreported as a child error. Mirrors delegate_tool.
            if cancelled:
                self.results.append({"task_index": idx, "status": "cancelled"})
            elif f.done():
                try:
                    self.results.append({"task_index": idx, "status": "completed",
                                         "value": f.result()})
                except Exception as exc:
                    self.results.append({"task_index": idx, "status": "error",
                                         "error": str(exc)})
            else:
                self.results.append({"task_index": idx, "status": "interrupted"})


@pytest.fixture
def release():
    """An event every wedged worker blocks on, always set during teardown."""
    ev = threading.Event()
    yield ev
    ev.set()


# ── the defect: interrupt must not block on a wedged child ───────────────────

def test_interrupt_returns_promptly_despite_a_wedged_child(release):
    h = _Harness(workers=2)
    started = threading.Event()

    def wedged():
        started.set()
        release.wait(120)          # would block shutdown(wait=True) for 2 minutes
        return "late"

    futures = {h.executor.submit(wedged): 0}
    assert started.wait(5), "worker never started"

    t0 = time.monotonic()
    h.drain_on_interrupt(futures)
    h.shutdown()
    elapsed = time.monotonic() - t0

    assert elapsed < PROMPT_RETURN_BUDGET, (
        f"shutdown blocked for {elapsed:.1f}s — __exit__ semantics are back"
    )


def test_completed_results_are_preserved_across_an_interrupt(release):
    h = _Harness(workers=3)
    done_marker = threading.Event()

    wedged_started = threading.Event()

    def quick():
        done_marker.set()
        return 42

    def wedged():
        wedged_started.set()
        release.wait(120)
        return "late"

    f_quick = h.executor.submit(quick)
    assert done_marker.wait(5)
    f_quick.result(timeout=5)                     # ensure it is really done
    f_wedged = h.executor.submit(wedged)
    # Must be RUNNING, not queued — otherwise cancel() succeeds and the task is
    # correctly reported as 'cancelled', which is a different scenario.
    assert wedged_started.wait(5), "wedged worker never started"
    futures = {f_quick: 0, f_wedged: 1}

    h.drain_on_interrupt(futures)
    h.shutdown()

    by_index = {r["task_index"]: r for r in h.results}
    assert by_index[0]["status"] == "completed", "a finished child's result was lost"
    assert by_index[0]["value"] == 42
    assert by_index[1]["status"] == "interrupted"


def test_unstarted_work_is_cancelled_not_merely_abandoned(release):
    """One worker, two tasks: the second never starts and must be cancelled."""
    h = _Harness(workers=1)
    started = threading.Event()

    def wedged():
        started.set()
        release.wait(120)

    f_running = h.executor.submit(wedged)
    assert started.wait(5)
    f_queued = h.executor.submit(lambda: "never runs")

    futures = {f_running: 0, f_queued: 1}
    h.drain_on_interrupt(futures)
    h.shutdown()

    by_index = {r["task_index"]: r for r in h.results}
    assert by_index[1]["status"] == "cancelled", "queued work must be cancelled"
    assert f_queued.cancelled()
    assert by_index[0]["status"] == "interrupted"


def test_work_that_may_still_be_running_is_tracked(release):
    """Silently dropping a running child is what made this unobservable."""
    h = _Harness(workers=2)
    started = threading.Event()

    def wedged():
        started.set()
        release.wait(120)

    futures = {h.executor.submit(wedged): 7}
    assert started.wait(5)
    h.drain_on_interrupt(futures)
    h.shutdown()

    assert h.still_running == [7]


def test_cancelled_work_is_not_reported_as_still_running(release):
    h = _Harness(workers=1)
    started = threading.Event()

    def wedged():
        started.set()
        release.wait(120)

    f_running = h.executor.submit(wedged)
    assert started.wait(5)
    f_queued = h.executor.submit(lambda: None)

    h.drain_on_interrupt({f_running: 0, f_queued: 1})
    h.shutdown()

    assert 1 not in h.still_running, "cancelled work is not in flight"
    assert h.still_running == [0]


# ── ordinary paths must keep their old semantics ─────────────────────────────

def test_normal_completion_still_joins_and_keeps_every_result():
    h = _Harness(workers=4)
    futures = {h.executor.submit(lambda v=v: v * 2): v for v in range(6)}
    for f in list(futures):
        f.result(timeout=30)
    h.shutdown()                       # not interrupted -> wait=True
    assert not h.interrupted
    assert all(f.done() for f in futures)


def test_a_failing_child_is_recorded_as_error_not_lost(release):
    h = _Harness(workers=2)

    def boom():
        raise RuntimeError("child exploded")

    f_bad = h.executor.submit(boom)
    with pytest.raises(RuntimeError):
        f_bad.result(timeout=5)

    h.drain_on_interrupt({f_bad: 0})
    h.shutdown()
    assert h.results[0]["status"] == "error"
    assert "child exploded" in h.results[0]["error"]


def test_repeated_shutdown_is_safe(release):
    h = _Harness(workers=1)
    h.interrupted = True
    h.shutdown()
    h.shutdown()                       # must not raise


def test_interrupt_with_nothing_pending_is_a_noop():
    h = _Harness(workers=2)
    h.drain_on_interrupt({})
    h.shutdown()
    assert h.results == []
    assert h.still_running == []


# ── the structural contract in delegate_task itself ──────────────────────────

def test_delegate_task_does_not_use_a_blocking_with_block():
    """Pin the fix: a `with DaemonThreadPoolExecutor(...)` reintroduces the bug.

    __exit__ always joins, so re-wrapping the batch branch in `with` silently
    restores the hang no matter what the interrupt path does.
    """
    import inspect

    import tools.delegate_tool as dt

    src = inspect.getsource(dt)
    assert "with DaemonThreadPoolExecutor(" not in src, (
        "batch delegation is back inside a `with` block — __exit__ joins "
        "unconditionally and the interrupt bail-out cannot escape"
    )
    assert "shutdown(wait=False, cancel_futures=True)" in src, (
        "interrupt path no longer performs a non-blocking, cancelling shutdown"
    )
