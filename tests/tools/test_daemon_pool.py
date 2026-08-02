"""Tests for tools.daemon_pool.DaemonThreadPoolExecutor.

The daemon pool exists so abandoned workers (interrupted/timed-out tool
batches, wedged memory-provider syncs) can never block interpreter exit:
stdlib ThreadPoolExecutor workers are non-daemon AND registered in
concurrent.futures.thread._threads_queues, whose atexit hook joins every
worker unconditionally — even after shutdown(wait=False).
"""

import subprocess
import sys
import threading
import time
from unittest import mock

from concurrent.futures.thread import _threads_queues

from tools.daemon_pool import DaemonThreadPoolExecutor


def test_workers_are_daemon_threads():
    pool = DaemonThreadPoolExecutor(max_workers=2)
    try:
        info = pool.submit(
            lambda: (threading.current_thread().daemon, threading.current_thread())
        ).result(timeout=10)
        is_daemon, worker = info
        assert is_daemon is True
        # Not registered with concurrent.futures' atexit join hook.
        assert worker not in _threads_queues
    finally:
        pool.shutdown(wait=True)


def test_idle_worker_reuse():
    pool = DaemonThreadPoolExecutor(max_workers=4)
    try:
        tid1 = pool.submit(threading.get_ident).result(timeout=10)
        time.sleep(0.05)  # let the worker park on the idle semaphore
        tid2 = pool.submit(threading.get_ident).result(timeout=10)
        assert tid1 == tid2
    finally:
        pool.shutdown(wait=True)


def test_wedged_worker_does_not_block_interpreter_exit():
    """A worker stuck in a long sleep must not hold the process open.

    With stdlib ThreadPoolExecutor this subprocess hangs until the sleep
    finishes (the atexit hook joins the worker); with the daemon pool it
    exits as soon as the main thread returns.
    """
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools.daemon_pool import DaemonThreadPoolExecutor\n"
        "import time\n"
        "pool = DaemonThreadPoolExecutor(max_workers=1)\n"
        "pool.submit(time.sleep, 120)\n"
        "time.sleep(0.3)\n"
        "pool.shutdown(wait=False)\n"
        "print('main-done', flush=True)\n"
    ) % (str(_repo_root()),)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "main-done" in proc.stdout


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]


def test_py314_worker_context_branch_spawns_worker_with_ctx():
    """#76621: on Python 3.14 the executor exposes _create_worker_context
    instead of _initializer/_initargs; _adjust_thread_count must pass the
    context object to _worker rather than the removed attributes.

    Simulate the 3.14 shape by attaching _create_worker_context to a real
    pool instance and capturing the Thread constructor args (the thread is
    never actually started, so the interpreter-local _worker signature is
    irrelevant).
    """
    pool = DaemonThreadPoolExecutor(max_workers=1)
    ctx = object()
    pool._create_worker_context = lambda: ctx  # simulate 3.14 layout
    try:
        with mock.patch("tools.daemon_pool.threading.Thread") as thread_cls:
            thread_cls.return_value.start = mock.Mock()
            pool._adjust_thread_count()
        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        args = kwargs["args"]
        # (executor_ref, ctx, work_queue) — 3.14 worker signature
        assert len(args) == 3
        assert args[1] is ctx
        assert args[2] is pool._work_queue
        assert kwargs["daemon"] is True
    finally:
        pool.shutdown(wait=True)
