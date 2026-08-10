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


def test_worker_context_signature_compat():
    """Workers spawn and run under both the pre-3.14 and 3.14+ worker APIs.

    CPython 3.14 changed ``concurrent.futures.thread._worker`` from
    ``(executor_reference, work_queue, initializer, initargs)`` to
    ``(executor_reference, ctx, work_queue)`` with a ``WorkerContext``
    carrying the initializer/initargs. This executor must keep spawning
    daemon workers on both signatures (the regression fixed in the 3.14
    compatibility change). Runs in a subprocess so a failure cannot wedge
    the test runner's own thread pool.
    """
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools.daemon_pool import DaemonThreadPoolExecutor\n"
        "import threading\n"
        "thread_local = threading.local()\n"
        "def _init(x):\n"
        "    thread_local.tag = x\n"
        "def _work():\n"
        "    return getattr(thread_local, 'tag', None)\n"
        "pool = DaemonThreadPoolExecutor(max_workers=2, initializer=_init, initargs=('tagged',))\n"
        "try:\n"
        "    results = [pool.submit(_work).result(timeout=10) for _ in range(4)]\n"
        "    print('results:', results, flush=True)\n"
        "    assert results == ['tagged'] * 4, results\n"
        "    print('WORKER-OK', flush=True)\n"
        "finally:\n"
        "    pool.shutdown(wait=True)\n"
    ) % (str(_repo_root()),)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "WORKER-OK" in proc.stdout
    assert "results: ['tagged', 'tagged', 'tagged', 'tagged']" in proc.stdout


def test_no_initializer_spawns_and_reuses():
    """Pool without initializer still spawns and reuses workers (3.14 path)."""
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools.daemon_pool import DaemonThreadPoolExecutor\n"
        "import threading, time\n"
        "pool = DaemonThreadPoolExecutor(max_workers=2)\n"
        "try:\n"
        "    tid1 = pool.submit(threading.get_ident).result(timeout=10)\n"
        "    time.sleep(0.05)\n"
        "    tid2 = pool.submit(threading.get_ident).result(timeout=10)\n"
        "    print('reused:', tid1 == tid2, flush=True)\n"
        "    assert tid1 == tid2\n"
        "    print('REUSE-OK', flush=True)\n"
        "finally:\n"
        "    pool.shutdown(wait=True)\n"
    ) % (str(_repo_root()),)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "REUSE-OK" in proc.stdout
