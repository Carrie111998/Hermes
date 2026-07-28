"""Regression: concurrent execute_code tool dispatch must not poison sys.stdout.

``code_execution_tool``'s sandbox RPC handler runs on a per-connection socket
thread, so several ``execute_code`` calls can be in flight simultaneously (e.g.
a parallel ``delegate_task`` batch, where every subagent uses execute_code).

The old implementation silenced internal tool handlers with a raw
process-global assignment::

    _real_stdout, _real_stderr = sys.stdout, sys.stderr
    devnull = open(os.devnull, "w")
    try:
        sys.stdout = devnull
        sys.stderr = devnull
        result = handle_function_call(...)
    finally:
        sys.stdout, sys.stderr = _real_stdout, _real_stderr
        devnull.close()

``sys.stdout`` is process-global. With two threads interleaving, thread B
captures thread A's devnull as its ``_real_stdout``, A closes that handle, and
B then "restores" the CLOSED handle. Every subsequent bare ``print`` anywhere
in the process raises::

    ValueError: I/O operation on closed file.

Observed 2026-07-28 on a live gateway: 328 occurrences beginning the instant a
3-way parallel delegation launched; all three subagents died. The tracebacks
landed on an innocent ``print`` in agent/conversation_loop.py, nowhere near the
actual culprit.
"""

from __future__ import annotations

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


def test_concurrent_raw_assignment_poisons_stdout():
    """Characterize the exact race the old code had.

    Two threads each save/replace/restore sys.stdout by direct assignment. The
    interleaving leaves sys.stdout bound to a closed handle.
    """

    def body():
        barrier = threading.Barrier(2)

        def worker(hold: float):
            real_out, real_err = sys.stdout, sys.stderr
            devnull = open(os.devnull, "w", encoding="utf-8")
            try:
                sys.stdout = devnull
                sys.stderr = devnull
                barrier.wait(timeout=5)
                time.sleep(hold)
            finally:
                sys.stdout, sys.stderr = real_out, real_err
                devnull.close()

        a = threading.Thread(target=worker, args=(0.05,))
        b = threading.Thread(target=worker, args=(0.30,))
        a.start()
        b.start()
        a.join()
        b.join()

        with pytest.raises(ValueError, match="closed file"):
            sys.stdout.write("this must fail")

    _drain(body)


def test_thread_scoped_silence_survives_concurrency():
    """The replacement primitive is safe under the same interleaving."""
    from agent.thread_scoped_output import thread_scoped_silence

    def body():
        barrier = threading.Barrier(2)

        def worker(hold: float):
            with thread_scoped_silence():
                print("internal tool chatter")
                barrier.wait(timeout=5)
                time.sleep(hold)

        a = threading.Thread(target=worker, args=(0.05,))
        b = threading.Thread(target=worker, args=(0.30,))
        a.start()
        b.start()
        a.join()
        b.join()

        print("survivor")

    captured = _drain(body)
    assert "survivor" in captured
    assert "internal tool chatter" not in captured


def test_code_execution_tool_dispatch_sites_are_thread_scoped():
    """Guard both sandbox dispatch sites against a regression.

    Fails on the pre-fix source, which used ``sys.stdout = devnull``.
    """
    import inspect
    import re

    import tools.code_execution_tool as cet

    src = inspect.getsource(cet)
    # Strip comment lines so the prose explaining the old bug doesn't trip the
    # assertions below.
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )

    assert not re.search(r"^\s*sys\.stdout\s*=\s*devnull", code_only, re.M), (
        "process-global stdout assignment found in code_execution_tool; "
        "use thread_scoped_silence() — this handler runs on concurrent "
        "socket threads and a global rebind poisons sys.stdout process-wide"
    )
    assert not re.search(r"^\s*sys\.stderr\s*=\s*devnull", code_only, re.M)

    # Both dispatch sites (local sandbox + remote sandbox) must be wrapped.
    assert code_only.count("with thread_scoped_silence():") >= 2, (
        "expected both handle_function_call dispatch sites to be wrapped in "
        "thread_scoped_silence()"
    )
