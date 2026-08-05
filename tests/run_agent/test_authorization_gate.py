"""Tests for the concurrent authorization gate (issue #79719).

The gate serializes approval/pre-tool-block prompts and excludes their queue
from the batch deadline. Before the fix, a worker wedged *inside* the gate
(a hanging ``pre_tool_block`` plugin, or an approval round-trip to a client
that went away) had two coupled failure modes:

1. The serialization lock was an unbounded blocking acquire: every other
   worker needing authorization blocked behind the wedged holder forever.
2. ``excluded_seconds()`` grew 1:1 with wall clock while the window was open,
   and the batch-deadline loop adds it to the deadline on every poll — so
   ``remaining`` was constant and the deadline never fired.

These tests drive the real ``_ConcurrentToolAuthorizationGate`` with the same
arithmetic as the deadline loop (mirroring the issue's reproduction).
"""

import sys
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)


def _spawn_wedged_worker(gate, lock_held, release):
    """Run a callback that wedges inside ``gate.run`` until released."""

    def wedged_callback():
        lock_held.set()
        release.wait(30.0)
        return "wedged"

    t = threading.Thread(target=gate.run, args=(wedged_callback,), daemon=True)
    t.start()
    assert lock_held.wait(1.0), "wedged worker never entered the gate"
    return t


def test_wedged_lock_holder_does_not_starve_later_worker():
    """A worker holding the serialization lock must not block later workers.

    With an unbounded acquire, the contender below would block forever. The
    bounded acquire makes it run its prompt unserialized after the lock
    timeout — the same tradeoff the start-order gate (#79705) accepts.
    """
    from agent.tool_executor import _ConcurrentToolAuthorizationGate

    gate = _ConcurrentToolAuthorizationGate(
        lock_timeout=0.05, max_window_seconds=60.0
    )
    lock_held = threading.Event()
    release = threading.Event()
    holder = _spawn_wedged_worker(gate, lock_held, release)

    results: list = []

    def _run_fast():
        results.append(gate.run(lambda: "ran"))

    contender = threading.Thread(target=_run_fast, daemon=True)
    started = time.monotonic()
    contender.start()
    contender.join(timeout=2.0)
    elapsed = time.monotonic() - started

    release.set()
    holder.join(timeout=1.0)

    assert not contender.is_alive(), (
        "later worker starved behind the wedged lock holder"
    )
    assert results == ["ran"], f"unexpected result: {results}"
    assert elapsed < 1.0, (
        f"later worker took {elapsed:.2f}s to give up on the lock"
    )


def test_open_window_exclusion_is_capped():
    """An open window must not grow the deadline exclusion 1:1 with wall clock."""
    from agent.tool_executor import _ConcurrentToolAuthorizationGate

    gate = _ConcurrentToolAuthorizationGate(
        lock_timeout=30.0, max_window_seconds=0.1
    )
    lock_held = threading.Event()
    release = threading.Event()
    worker = _spawn_wedged_worker(gate, lock_held, release)

    time.sleep(0.2)  # window open well past the 0.1s cap
    first = gate.excluded_seconds()
    time.sleep(0.3)
    second = gate.excluded_seconds()

    release.set()
    worker.join(timeout=1.0)

    assert second <= 0.1 + 0.05, (
        f"exclusion grew past the cap: {second:.3f}s"
    )
    assert second - first < 0.05, (
        "exclusion still grows 1:1 with wall clock: "
        f"{first:.3f}s -> {second:.3f}s"
    )


def test_wedged_window_no_longer_defeats_the_batch_deadline():
    """Mirror of the issue's repro table: the batch deadline must fire.

    The deadline loop computes ``remaining = (deadline + excluded_seconds()) -
    now`` on every poll. While the window was open, excluded_seconds() grew
    with wall clock, so ``now`` cancelled out and ``remaining`` never reached
    zero — the turn hung forever. With the cap, the exclusion stops growing
    and the deadline converges.
    """
    from agent.tool_executor import _ConcurrentToolAuthorizationGate

    gate = _ConcurrentToolAuthorizationGate(
        lock_timeout=30.0, max_window_seconds=0.1
    )
    lock_held = threading.Event()
    release = threading.Event()
    worker = _spawn_wedged_worker(gate, lock_held, release)

    deadline = time.monotonic() + 0.3
    started = time.monotonic()
    fired = False
    while True:
        remaining = (deadline + gate.excluded_seconds()) - time.monotonic()
        if remaining <= 0:
            fired = True
            break
        if time.monotonic() - started > 5.0:
            break  # would never fire on the unbounded code — assert below
        time.sleep(0.02)

    release.set()
    worker.join(timeout=1.0)

    assert fired, (
        "batch deadline never fired while the authorization window was open "
        "(remaining stayed constant)"
    )


def test_serialization_preserved_when_lock_is_available():
    """The bound must not break normal serialization of approval prompts."""
    from agent.tool_executor import _ConcurrentToolAuthorizationGate

    gate = _ConcurrentToolAuthorizationGate(
        lock_timeout=5.0, max_window_seconds=60.0
    )
    order: list = []
    lock_held = threading.Event()

    def first_callback():
        order.append("first:start")
        lock_held.set()
        time.sleep(0.1)
        order.append("first:end")
        return "first"

    def second_callback():
        order.append("second")
        return "second"

    t1 = threading.Thread(target=gate.run, args=(first_callback,), daemon=True)
    t1.start()
    assert lock_held.wait(1.0), "first worker never entered the gate"

    t2 = threading.Thread(target=gate.run, args=(second_callback,), daemon=True)
    t2.start()
    t2.join(timeout=2.0)
    t1.join(timeout=2.0)

    assert order == ["first:start", "first:end", "second"], (
        f"approval prompts were not serialized: {order}"
    )


def test_exclusion_ceiling_tracks_approval_timeout():
    """The ceiling must cover the approval gate's own bounded human wait.

    A legitimate approval round-trip is bounded by ``approvals.timeout``
    inside the approval gate, so the exclusion ceiling (timeout + margin)
    must sit above it: legitimate human waits stay excluded from the batch
    deadline; only unbounded wedges (which the approval gate cannot produce)
    hit the ceiling.
    """
    import agent.tool_executor as te

    ceiling = te._authorization_gate_max_window_seconds()
    try:
        from tools.approval import _get_approval_timeout

        assert ceiling > float(_get_approval_timeout()), (
            f"ceiling {ceiling:.1f}s does not cover the approval timeout "
            f"{_get_approval_timeout()}s"
        )
    except Exception:
        assert ceiling == te._AUTHORIZATION_GATE_MAX_WINDOW_S


def test_exclusion_ceiling_falls_back_when_config_unreadable(monkeypatch):
    """When the approval config cannot be read, use the fixed fallback."""
    import agent.tool_executor as te

    monkeypatch.setitem(sys.modules, "tools.approval", None)
    assert (
        te._authorization_gate_max_window_seconds()
        == te._AUTHORIZATION_GATE_MAX_WINDOW_S
    )
