"""Make a per-test timeout a test FAILURE instead of a whole-process kill.

``pyproject.toml`` pins ``--timeout-method=thread``. That is not a preference:
``signal`` needs ``signal.SIGALRM``, which is Unix-only and INTERNALERRORs every
test on Windows. But the thread method answers a timeout with ``os._exit(1)``
(``pytest_timeout.timeout_timer``), which takes the whole pytest process down --
no summary line, no junit XML, and the reported failure set is whatever had
accumulated when the run died.

On a ~9,400-test single-process ``tests/hermes_cli`` run that made the count vary
between invocations (245 / 244 / 245 on 2026-08-18) and the suite unusable as a
regression gate: two runs of identical code cannot be compared when neither one
finishes and each stops somewhere different.

This restores, on every platform, the behaviour the POSIX ``signal`` method
already has -- raise the timeout *into the main thread*, let pytest record one
failed test, and let the run continue to its summary line.

``pytest_timeout_set_timer`` is a ``firstresult`` hook and pytest-timeout's own
implementation is ``trylast``, so a ``tryfirst`` implementation here wins
cleanly without patching the installed package.

**The os._exit escalation is kept as a second stage.**
``PyThreadState_SetAsyncExc`` is only delivered at a bytecode boundary, so a test
blocked inside a C call (``socket.recv``, ``subprocess.wait``, ``os.fsync``,
``LockFile``) will not notice it. After ``ESCALATION_GRACE`` seconds without the
test unwinding we fall back to pytest-timeout's own killer. So this is never
*worse* than stock behaviour -- a genuinely wedged test still ends the process --
it just gives a merely-slow test the chance to die politely first, which is the
case that was destroying these runs.
"""

from __future__ import annotations

import ctypes
import os
import threading

import pytest
import pytest_timeout

#: Seconds to wait for the async exception to actually land before falling back
#: to pytest-timeout's ``os._exit(1)``.
#:
#: Deliberately long. A pending async exception does not expire -- it fires the
#: moment the main thread next executes a bytecode -- so escalation is only ever
#: needed for a call that NEVER returns. The cases that actually destroyed runs
#: here were calls that return *late* (a socket connect to api.github.com under
#: a stubbed-but-leaked seam, ``subprocess.run`` on a wedged child), and killing
#: those at cap+30s would recreate the very failure this module exists to
#: prevent. A too-long grace only costs wall clock on a genuinely hung test; a
#: too-short one costs the whole run's results.
ESCALATION_GRACE = float(os.environ.get("HERMES_TIMEOUT_ESCALATION_GRACE", "120"))


def _failure_class(timeout):
    """Build the exception to inject, with pytest-timeout's own message.

    ``PyThreadState_SetAsyncExc`` takes a *class*, not an instance, so the
    message has to be baked into ``__init__``. Deriving from
    ``pytest.fail.Exception`` (a ``BaseException``) is deliberate: a plain
    ``Exception`` would be swallowed by the ``except Exception`` handlers that
    are common in this suite's subprocess and network helpers.
    """
    message = pytest_timeout.PYTEST_FAILURE_MESSAGE % timeout

    class TimeoutFailure(pytest.fail.Exception):
        def __init__(self):
            super().__init__(message, pytrace=False)

    return TimeoutFailure


def _async_raise(thread_id, exc_type):
    """Deliver ``exc_type`` into ``thread_id``. Returns True if it was armed."""
    affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id), ctypes.py_object(exc_type)
    )
    if affected > 1:
        # Should be impossible for a real thread id; undo rather than leave
        # several threads armed with a pending exception.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), None)
        return False
    return affected == 1


@pytest.hookimpl(tryfirst=True)
def pytest_timeout_set_timer(item, settings):
    """Arm a non-fatal timeout; return None to defer to pytest-timeout."""
    if settings.method != "thread":
        # 'signal' already raises into the main thread on POSIX -- leave it be.
        return None
    if settings.timeout is None or settings.timeout <= 0:
        return None
    main_thread = threading.main_thread()
    if threading.current_thread() is not main_thread:
        # The test is not running on the thread we know how to interrupt.
        return None
    main_ident = main_thread.ident
    if main_ident is None:
        return None

    # ``state``/``lock`` close the race the stock implementation closes with
    # ``timer.join()``: a timer that fires just as the test ends must not inject
    # its exception into the *next* test. ``cancel`` claims the lock, marks the
    # item finished, then joins, so ``fire`` either runs to completion first or
    # sees ``finished`` and does nothing.
    state = {"finished": False, "escalation": None}
    lock = threading.Lock()

    def escalate():
        # The async exception never landed: the main thread is blocked in a C
        # call. Fall back to pytest-timeout's own killer so a truly wedged test
        # still terminates the run rather than hanging it forever.
        with lock:
            if state["finished"]:
                return
        pytest_timeout.timeout_timer(item, settings)

    def fire():
        with lock:
            if state["finished"]:
                return
            if not settings.disable_debugger_detection and pytest_timeout.is_debugging():
                return
            terminal = item.config.get_terminal_writer()
            terminal.sep("+", title="Timeout")
            pytest_timeout.dump_stacks(terminal)
            terminal.sep("+", title="Timeout")
            if not _async_raise(main_ident, _failure_class(settings.timeout)):
                pytest_timeout.timeout_timer(item, settings)
                return
            escalation = threading.Timer(ESCALATION_GRACE, escalate)
            escalation.name = "hermes-timeout-escalate %s" % item.nodeid
            escalation.daemon = True
            state["escalation"] = escalation
            escalation.start()

    timer = threading.Timer(settings.timeout, fire)
    timer.name = "hermes-timeout %s" % item.nodeid
    timer.daemon = True

    def cancel():
        with lock:
            state["finished"] = True
            pending = state["escalation"]
        timer.cancel()
        timer.join()
        if pending is not None:
            pending.cancel()

    item.cancel_timeout = cancel
    timer.start()
    return True
