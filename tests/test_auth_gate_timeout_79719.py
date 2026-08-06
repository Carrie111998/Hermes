"""Test: authorization gate timeout prevents infinite batch deadline extension (#79719).

When a tool is wedged inside the authorization gate (hung pre_tool_block
plugin, dead approval client), the gate must not extend the batch deadline
indefinitely.
"""
import threading
import time

from agent.tool_executor import _ConcurrentToolAuthorizationGate


def test_excluded_seconds_capped():
    """excluded_seconds() must not exceed _MAX_EXCLUDED_SECONDS."""
    gate = _ConcurrentToolAuthorizationGate()

    # Simulate a wedged callback that holds the lock for a long time
    wedge_event = threading.Event()
    done_event = threading.Event()

    def wedged_callback():
        wedge_event.wait(timeout=5)  # Block until we signal
        return "wedged-result"

    # Start the wedged call in a thread
    t = threading.Thread(target=lambda: gate.run(wedged_callback))
    t.start()

    # Wait a bit for the gate to register the pending window
    time.sleep(0.1)

    # excluded_seconds should be growing but capped
    excl = gate.excluded_seconds()
    assert excl <= _ConcurrentToolAuthorizationGate._MAX_EXCLUDED_SECONDS, (
        f"excluded_seconds={excl} exceeds cap {_ConcurrentToolAuthorizationGate._MAX_EXCLUDED_SECONDS}"
    )

    # Release the wedge
    wedge_event.set()
    t.join(timeout=5)


def test_lock_timeout_proceeds():
    """When the serialization lock is held too long, new callers proceed in degraded mode."""
    gate = _ConcurrentToolAuthorizationGate()
    results = []

    # Hold the lock with a long-running callback
    hold_event = threading.Event()
    release_event = threading.Event()

    def holding_callback():
        hold_event.set()
        release_event.wait(timeout=5)
        return "held-result"

    # Start the holding call
    t1 = threading.Thread(target=lambda: results.append(gate.run(holding_callback)))
    t1.start()
    hold_event.wait(timeout=2)

    # Now start another call — it should time out on the lock and proceed
    # in degraded mode rather than hanging forever
    def quick_callback():
        return "quick-result"

    t2 = threading.Thread(target=lambda: results.append(gate.run(quick_callback)))
    t2.start()
    t2.join(timeout=_ConcurrentToolAuthorizationGate._MAX_EXCLUDED_SECONDS + 5)

    # The quick callback should have completed (in degraded mode)
    assert not t2.is_alive(), "Second callback hung — lock timeout did not fire"

    release_event.set()
    t1.join(timeout=5)


def test_normal_operation_unaffected():
    """Normal non-wedged calls work as before."""
    gate = _ConcurrentToolAuthorizationGate()
    result = gate.run(lambda: "ok")
    assert result == "ok"
    assert gate.excluded_seconds() > 0  # Window was tracked
