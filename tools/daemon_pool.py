"""Shared daemon-thread Executor.

Stdlib ``ThreadPoolExecutor`` workers are non-daemon AND are registered in
``concurrent.futures.thread._threads_queues``, whose atexit hook
(``_python_exit``) joins every worker unconditionally — even after
``shutdown(wait=False)``.  A single wedged worker (tool blocked on network
I/O, hung provider daemon, stuck subagent) therefore blocks interpreter
exit forever.  This is the root cause of multi-minute CLI exits on long
sessions: every abandoned concurrent-tool batch leaves workers that the
exit hook insists on joining.

``DaemonThreadPoolExecutor`` spawns daemon workers instead, so:

  - ``_python_exit`` never joins them (they're never registered with it), and
  - the interpreter's non-daemon thread join at shutdown skips them.

Use it for any pool whose work is best-effort or independently
interruptible and must never hold the process open: concurrent tool
execution, background memory sync, catalog fan-out, subagent timeout
wrappers. Do NOT use it for work that must complete before exit (durable
writes) — those belong on foreground threads with explicit bounded joins.

Built directly on the public ``concurrent.futures.Executor`` ABC and only
stable primitives (``threading``, ``queue.SimpleQueue``, ``weakref``,
``concurrent.futures.Future``) — no CPython-private ``ThreadPoolExecutor``
internals. An earlier version subclassed ``ThreadPoolExecutor`` and
mirrored its private ``_adjust_thread_count`` to get daemon workers;
CPython 3.14 restructured those internals (``__init__`` now stores a
``_create_worker_context`` instead of ``_initializer``/``_initargs``),
which broke that approach with ``AttributeError: ... no attribute
'_initializer'`` on every submit. Building on the public ABC means a
future CPython internals change can't break this again.
"""

from __future__ import annotations

import os
import queue
import threading
import weakref
from concurrent.futures import Executor, Future

__all__ = ["DaemonThreadPoolExecutor"]

_WorkItem = tuple[Future, object, tuple, dict]


class DaemonThreadPoolExecutor(Executor):
    """Executor variant whose workers do not block process exit."""

    def __init__(
        self,
        max_workers: int | None = None,
        thread_name_prefix: str = "",
        initializer=None,
        initargs: tuple = (),
    ) -> None:
        if max_workers is None:
            max_workers = min(32, (os.cpu_count() or 1) + 4)
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix or str(self)
        self._initializer = initializer
        self._initargs = initargs
        self._work_queue: queue.SimpleQueue[_WorkItem | None] = queue.SimpleQueue()
        self._idle_semaphore = threading.Semaphore(0)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._shutdown_flag = False

    def submit(self, fn, /, *args, **kwargs) -> Future:
        with self._lock:
            if self._shutdown_flag:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: Future = Future()
            self._work_queue.put((future, fn, args, kwargs))
            self._spawn_worker_if_needed()
            return future

    def _spawn_worker_if_needed(self) -> None:
        # Mirrors the pool's lazy-reuse heuristic: only spawn a new
        # worker when no idle one is already waiting on the queue.
        if self._idle_semaphore.acquire(timeout=0):
            return
        if len(self._threads) >= self._max_workers:
            return
        work_queue = self._work_queue

        def _on_executor_collected(_ref, q=work_queue) -> None:
            q.put(None)

        thread = threading.Thread(
            name=f"{self._thread_name_prefix}_{len(self._threads)}",
            target=_worker_loop,
            args=(weakref.ref(self, _on_executor_collected), work_queue),
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown_flag = True
            if cancel_futures:
                # Only drains what's still queued — anything a worker
                # already claimed via set_running_or_notify_cancel() runs
                # to completion, matching stdlib's documented semantics.
                while True:
                    try:
                        queued = self._work_queue.get_nowait()
                    except queue.Empty:
                        break
                    if queued is not None:
                        queued[0].cancel()
            for _ in self._threads:
                self._work_queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


def _worker_loop(executor_ref, work_queue) -> None:
    """Run submitted work items until shut down or the pool is collected.

    Takes a *weak* reference to the executor (not a bound method) so a
    pool that's dropped without an explicit ``shutdown()`` doesn't keep
    itself alive forever via its own worker threads — collection fires
    the weakref callback registered in ``_spawn_worker_if_needed``, which
    queues a sentinel so this loop notices and exits.
    """
    executor = executor_ref()
    initializer = executor._initializer if executor is not None else None
    initargs = executor._initargs if executor is not None else ()
    del executor
    if initializer is not None:
        try:
            initializer(*initargs)
        except BaseException:
            return
    while True:
        item = work_queue.get()
        if item is None:
            return
        future, fn, args, kwargs = item
        executor = executor_ref()
        if executor is not None:
            executor._idle_semaphore.release()
        del executor
        if not future.set_running_or_notify_cancel():
            continue
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
