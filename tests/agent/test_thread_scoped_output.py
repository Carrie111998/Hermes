"""Tests for agent.thread_scoped_output.thread_scoped_silence.

Behaviour contract: a thread inside ``thread_scoped_silence()`` has its
stdout/stderr routed to devnull, while every OTHER thread keeps writing to the
real stream — even concurrently, while the first thread is still inside the
context.  This is the property the old process-global
``contextlib.redirect_stdout(devnull)`` violated (issue #55769 / #55925).
"""

import io
import sys
import threading
import time

from agent.thread_scoped_output import thread_scoped_silence


def _run_with_real_stream(fn):
    """Bind a StringIO as the real stdout, run fn, return what reached it."""
    real_out = io.StringIO()
    orig = sys.stdout
    sys.stdout = real_out
    try:
        fn()
    finally:
        sys.stdout = orig
    return real_out.getvalue()






def test_stderr_is_also_routed_per_thread():
    real_err = io.StringIO()
    orig = sys.stderr
    sys.stderr = real_err
    try:
        with thread_scoped_silence():
            sys.stderr.write("err-dropped\n")
        sys.stderr.write("err-kept\n")
    finally:
        sys.stderr = orig
    out = real_err.getvalue()
    assert "err-dropped" not in out
    assert "err-kept" in out






def test_many_concurrent_silenced_and_loud_threads():
    """Stress: interleaved silenced/loud threads keep their respective fates."""
    start = threading.Event()
    results_lock = threading.Lock()

    def silenced(i):
        start.wait(timeout=2.0)
        with thread_scoped_silence():
            print(f"S{i}")
            time.sleep(0.05)

    def loud(i):
        start.wait(timeout=2.0)
        time.sleep(0.02)
        print(f"L{i}")

    def body():
        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=silenced, args=(i,)))
            threads.append(threading.Thread(target=loud, args=(i,)))
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=15.0)
        assert not any(t.is_alive() for t in threads), "straggler thread would truncate captured output"

    captured = _run_with_real_stream(body)
    for i in range(5):
        assert f"S{i}" not in captured, f"silenced S{i} leaked"
        assert f"L{i}" in captured, f"loud L{i} swallowed"


def test_repeated_contexts_never_write_to_a_closed_sink():
    """The installed proxy must survive later silenced workers."""
    original = sys.stdout
    try:
        for _ in range(3):
            with thread_scoped_silence():
                sys.stdout.write("hidden\n")
            sys.stdout.fileno()
    finally:
        sys.stdout = original


def _displace_and_silence(cycles: int) -> None:
    """Churn cycle: a process-global redirect displaces the proxy, a worker
    then reinstalls it via thread_scoped_silence, and the redirect's exit
    restores the stale binding — the pattern that used to leak one
    /dev/null handle per cycle."""
    for _ in range(cycles):
        buf = io.StringIO()
        saved = sys.stdout
        sys.stdout = buf
        try:
            with thread_scoped_silence():
                pass
        finally:
            sys.stdout = saved


def test_proxy_reinstall_reuses_sink_and_never_chains():
    """Reinstalls after displacement must reuse the first proxy's sink and
    must not chain proxy -> proxy (chains keep every old sink referenced
    and open for the process lifetime)."""
    import agent.thread_scoped_output as tso

    original = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with thread_scoped_silence():
            pass
        first_sink = tso._installed["stdout"]._sink
        _displace_and_silence(25)
        # sys.stdout now points at a stale proxy (redirect-exit restore);
        # the next reinstall must unwrap it, not chain onto it.
        with thread_scoped_silence():
            pass
        current = tso._installed["stdout"]
        assert current._sink is first_sink
        assert not isinstance(current._passthrough, tso._ThreadRoutingStream)
    finally:
        sys.stdout = original


def test_proxy_reinstall_churn_does_not_grow_devnull_fds():
    """End-to-end fd invariant: repeated displacement cycles keep the
    process's /dev/null descriptor count flat (Linux /proc only)."""
    import os

    import pytest

    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        pytest.skip("needs /proc fd introspection")

    def devnull_fds() -> int:
        count = 0
        for fd in os.listdir(fd_dir):
            try:
                if os.readlink(os.path.join(fd_dir, fd)) == os.devnull:
                    count += 1
            except OSError:
                pass
        return count

    original = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with thread_scoped_silence():
            pass
        baseline = devnull_fds()
        _displace_and_silence(40)
        assert devnull_fds() == baseline
    finally:
        sys.stdout = original
