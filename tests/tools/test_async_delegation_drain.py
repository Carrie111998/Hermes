"""The one-shot ``-q`` exit path must not kill in-flight background subagents.

``delegate_task(background=true)`` children run on a DAEMON executor
(tools/async_delegation._get_executor), so they are destroyed the instant the
owning process exits. That is correct for an interactive session but wrong for
a one-shot ``hermes chat -q`` run — notably a kanban worker, which reaches
``sys.exit`` as soon as its final turn returns and silently loses whatever its
subagents were doing.

The gateway already refuses to scale to zero while ``active_count() > 0``
(gateway/run.py). These tests cover the equivalent guard for the short-lived
process: ``drain_active``.
"""

from __future__ import annotations

import threading
import time

import pytest

from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def _clean_records():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _fake_running(delegation_id: str, status: str = "running") -> None:
    with ad._records_lock:
        ad._records[delegation_id] = {"status": status}


def _finish(delegation_id: str) -> None:
    with ad._records_lock:
        ad._records[delegation_id]["status"] = "completed"


def test_drain_returns_immediately_when_nothing_running():
    started = time.monotonic()
    assert ad.drain_active(30) == 0
    assert time.monotonic() - started < 1.0


def test_drain_waits_for_an_in_flight_delegation():
    _fake_running("deleg_aaa")

    def _finish_soon():
        time.sleep(0.3)
        _finish("deleg_aaa")

    threading.Thread(target=_finish_soon, daemon=True).start()

    started = time.monotonic()
    stranded = ad.drain_active(10, poll_interval=0.05)
    elapsed = time.monotonic() - started

    assert stranded == 0, "drain must wait until the child finishes"
    assert elapsed >= 0.25, "drain returned before the child was done"


def test_drain_reports_stranded_delegations_on_timeout():
    """Timeout must report survivors, not pretend a clean drain."""
    _fake_running("deleg_bbb")
    _fake_running("deleg_ccc")

    stranded = ad.drain_active(0.2, poll_interval=0.05)

    assert stranded == 2, (
        "a timed-out drain must return the count still running so the caller "
        "can warn; these are then classified by recover_abandoned_delegations"
    )


def test_zero_timeout_does_not_block():
    _fake_running("deleg_ddd")
    started = time.monotonic()
    assert ad.drain_active(0) == 1
    assert time.monotonic() - started < 0.5


def test_finalizing_counts_as_in_flight():
    """A child mid-finalize still has a result to record - do not exit on it."""
    _fake_running("deleg_eee", status="finalizing")
    assert ad.drain_active(0.1, poll_interval=0.05) == 1


def test_on_wait_receives_progress_and_failures_are_swallowed():
    _fake_running("deleg_fff")
    seen: list[int] = []

    def _cb(remaining, elapsed):
        seen.append(remaining)
        raise RuntimeError("logging blew up")

    stranded = ad.drain_active(0.2, poll_interval=0.05, on_wait=_cb)

    assert stranded == 1
    assert seen, "on_wait should have been called at least once"
    assert all(n == 1 for n in seen)


def test_active_ids_lists_in_flight_only():
    _fake_running("deleg_ggg")
    _fake_running("deleg_hhh", status="finalizing")
    with ad._records_lock:
        ad._records["deleg_done"] = {"status": "completed"}

    ids = set(ad.active_ids())
    assert ids == {"deleg_ggg", "deleg_hhh"}
