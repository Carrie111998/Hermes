"""Tests for tools.daemon_pool.DaemonThreadPoolExecutor.

The daemon pool exists so abandoned workers (interrupted/timed-out tool
batches, wedged memory-provider syncs) can never block interpreter exit:
stdlib ThreadPoolExecutor workers are non-daemon AND registered in
concurrent.futures.thread._threads_queues, whose atexit hook joins every
worker unconditionally — even after shutdown(wait=False).
"""

import inspect
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from concurrent.futures.thread import _threads_queues, _worker

from tools.daemon_pool import DaemonThreadPoolExecutor

# Python 3.14 refactored concurrent.futures.thread._worker: the
# initializer/initargs params were replaced by a single ``ctx`` argument
# produced by ThreadPoolExecutor._create_worker_context().  This flag drives
# version-contract assertions so the test suite fails loudly if the daemon
# pool ever ships the wrong args shape for the running interpreter.
_WORKER_USES_CTX = "ctx" in inspect.signature(_worker).parameters


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


def test_worker_args_match_running_interpreter_contract():
    """Lock the _worker args shape to the active Python interpreter.

    CPython 3.14 collapsed ``_worker(executor_ref, work_queue, initializer,
    initargs)`` into ``_worker(executor_ref, ctx, work_queue)`` where ``ctx``
    comes from ``_create_worker_context()``.  The daemon pool must build the
    args tuple that the *running* interpreter's ``_worker`` expects — passing
    the legacy 4-tuple to a 3-param ``_worker`` (or vice-versa) kills the worker
    and silently drops every submitted future.

    We intercept ``threading.Thread`` so we can inspect the args the pool hands
    to ``_worker`` without spawning a real worker thread.  Only on 3.14+ do
    we stub ``_create_worker_context`` (the legacy path never calls it).
    """
    captured: dict = {}

    class CaptureThread(threading.Thread):
        def start(self):
            captured["args"] = self._args
            captured["daemon"] = self.daemon
            captured["target"] = self._target

    thread_patcher = patch("tools.daemon_pool.threading.Thread", CaptureThread)

    # Only stub _create_worker_context on interpreters that actually have it
    # (Python >= 3.14).  On older runtimes the legacy branch never calls it, so
    # injecting it with create=True would trick the pool into taking the 3.14
    # path and produce a false-positive failure.  On 3.14+ the method is an
    # instance attribute (set by ThreadPoolExecutor.__init__, not a class
    # method), so we set it directly on the pool instance after construction.
    thread_patcher.start()
    try:
        pool = DaemonThreadPoolExecutor(max_workers=1)
        if _WORKER_USES_CTX:
            pool._create_worker_context = lambda *_a: "FAKE_CTX"  # type: ignore[attr-defined]
        pool._adjust_thread_count()
        pool.shutdown(wait=False)
    finally:
        thread_patcher.stop()

    assert "args" in captured, "Thread target wasn't created (patch missed?)"
    assert captured["target"] is _worker, "pool must still use stdlib _worker"
    assert captured["daemon"] is True, "worker must be daemon=True"

    worker_args = captured["args"]
    if _WORKER_USES_CTX:
        # Python >= 3.14: (executor_ref, ctx, work_queue) — 3 positional args.
        assert len(worker_args) == 3
        assert worker_args[1] == "FAKE_CTX"
    else:
        # Python 3.8–3.13: (executor_ref, work_queue, initializer, initargs).
        assert len(worker_args) == 4


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]
