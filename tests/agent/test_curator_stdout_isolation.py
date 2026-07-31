"""Regression: the curator's LLM fork must not poison process-global stdout.

``run_curator_review()`` runs its LLM pass on a daemon thread by default.  The
old implementation wrapped ``review_agent.run_conversation()`` in a
process-global ``contextlib.redirect_stdout(open(os.devnull))``.  Because
``redirect_stdout`` rebinds ``sys.stdout`` for the WHOLE process, two overlapping
curator/background-review passes restore in the wrong order and leave
``sys.stdout`` pointing at an already-closed devnull handle.  Every subsequent
bare ``print`` anywhere in the process then raises::

    ValueError: I/O operation on closed file.

Observed 2026-07-27 on a live gateway: nine cron jobs failed inside a single
scheduler tick, all with that error, because one curator pass had poisoned
``sys.stdout`` for the entire process.

The contract asserted here is behavioural, not implementation-shaped: after any
number of concurrent curator review passes, a print from an unrelated thread
must still reach the real stream.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import time

import pytest


def _drain(fn):
    """Bind a StringIO as the real stdout, run fn, return what reached it."""
    real_out = io.StringIO()
    orig = sys.stdout
    sys.stdout = real_out
    try:
        fn()
    finally:
        sys.stdout = orig
    return real_out.getvalue()


def test_global_redirect_on_overlapping_threads_poisons_stdout():
    """Characterize the bug we are protecting against.

    This documents WHY the curator may not use ``contextlib.redirect_stdout``
    from a worker thread. If CPython ever makes redirect_stdout thread-local
    this test will fail loudly and the guard below can be revisited.
    """

    def body():
        def worker(hold: float):
            with open(os.devnull, "w", encoding="utf-8") as devnull, \
                    contextlib.redirect_stdout(devnull):
                time.sleep(hold)

        # Overlapping enter/exit: A exits first and restores sys.stdout to the
        # devnull handle B installed; B then exits and closes that handle.
        a = threading.Thread(target=worker, args=(0.05,))
        b = threading.Thread(target=worker, args=(0.30,))
        a.start()
        time.sleep(0.02)
        b.start()
        a.join()
        b.join()

        with pytest.raises(ValueError, match="closed file"):
            sys.stdout.write("this must fail")

    _drain(body)


def test_thread_scoped_silence_leaves_stdout_usable():
    """The replacement primitive survives the same overlapping pattern."""
    from agent.thread_scoped_output import thread_scoped_silence

    def body():
        def worker(hold: float):
            with thread_scoped_silence():
                print("silenced chatter")
                time.sleep(hold)

        a = threading.Thread(target=worker, args=(0.05,))
        b = threading.Thread(target=worker, args=(0.30,))
        a.start()
        time.sleep(0.02)
        b.start()
        a.join()
        b.join()

        # No poisoning: the main thread can still print.
        print("survivor")

    captured = _drain(body)
    assert "survivor" in captured
    assert "silenced chatter" not in captured


def test_curator_llm_pass_does_not_use_global_redirect():
    """Source-level guard on the exact call site that caused the outage.

    A behavioural test would need a real forked AIAgent + credentials; instead
    we assert the invariant that matters: the curator's review call is wrapped
    in ``thread_scoped_silence()``, not a process-global redirect.
    """
    import inspect

    import agent.curator as curator

    src = inspect.getsource(curator)

    # Find the block that runs the forked agent's conversation.
    assert "run_conversation(user_message=prompt)" in src
    idx = src.index("run_conversation(user_message=prompt)")
    window = src[max(0, idx - 600):idx]
    assert "thread_scoped_silence()" in window, (
        "curator LLM pass must silence output per-thread; a process-global "
        "redirect_stdout here poisons sys.stdout for every other thread"
    )
    assert "redirect_stdout" not in window, (
        "process-global redirect_stdout found at the curator LLM call site"
    )
