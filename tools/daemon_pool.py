"""Shared daemon-thread ThreadPoolExecutor.

Stdlib ``ThreadPoolExecutor`` workers are non-daemon AND are registered in
``concurrent.futures.thread._threads_queues``, whose atexit hook
(``_python_exit``) joins every worker unconditionally — even after
``shutdown(wait=False)``.  A single wedged worker (tool blocked on network
I/O, hung provider daemon, stuck subagent) therefore blocks interpreter
exit forever.  This is the root cause of multi-minute CLI exits on long
sessions: every abandoned concurrent-tool batch leaves workers that the
exit hook insists on joining.

``DaemonThreadPoolExecutor`` spawns daemon workers and skips the
``_threads_queues`` registration, so:

  - ``_python_exit`` never joins them, and
  - the interpreter's non-daemon thread join at shutdown skips them.

Semantics are otherwise identical (initializer/initargs, work queue,
idle-thread reuse).  Use it for any pool whose work is best-effort or
independently interruptible and must never hold the process open:
concurrent tool execution, background memory sync, catalog fan-out,
subagent timeout wrappers.  Do NOT use it for work that must complete
before exit (durable writes) — those belong on foreground threads with
explicit bounded joins.
"""

from __future__ import annotations

import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker

__all__ = ["DaemonThreadPoolExecutor"]


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block process exit."""

    def _worker_args(self, executor_ref):
        """Build ``_worker``'s positional args for the running CPython.

        This class mirrors a private stdlib function, so its call signature is
        version-coupled and HAS already changed:

          * 3.8–3.13 — ``_worker(executor_reference, work_queue, initializer,
            initargs)``
          * 3.14+    — ``_worker(executor_reference, ctx, work_queue)`` where
            ``ctx`` comes from ``self._create_worker_context()``; the
            ``_initializer``/``_initargs`` attributes are gone.

        Passing the 3.13 shape on 3.14 raised ``AttributeError: no attribute
        '_initializer'`` on the FIRST submit, which broke every delegate_task
        on a 3.14 interpreter (the CLI runs 3.14 even where the gateway venv
        is still 3.11). Branch on the attribute that actually exists rather
        than on a version tuple.
        """
        create_ctx = getattr(self, "_create_worker_context", None)
        if create_ctx is not None:                      # 3.14+
            return (executor_ref, create_ctx(), self._work_queue)
        return (                                        # 3.8–3.13
            executor_ref,
            self._work_queue,
            self._initializer,
            self._initargs,
        )

    def _adjust_thread_count(self) -> None:
        # Mirrors CPython's implementation with two changes: daemon=True and
        # no _threads_queues registration.  Both are required — the daemon flag
        # keeps the interpreter's own non-daemon join from waiting, and staying
        # out of _threads_queues keeps _python_exit from joining.
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=self._worker_args(weakref.ref(self, weakref_cb)),
                daemon=True,
            )
            t.start()
            self._threads.add(t)
