"""Shared daemon-thread ThreadPoolExecutor.

Stdlib ``ThreadPoolExecutor`` workers are non-daemon AND are registered in
``concurrent.futures.thread._threads_queues``, whose atexit hook
(``_python_exit``) joins every worker unconditionally — even after
``shutdown(wait=False)``.  A single wedged worker (tool blocked on network
I/O, hung provider daemon, stuck subagent) therefore blocks interpreter
exit forever.

``DaemonThreadPoolExecutor`` spawns daemon workers and skips the
``_threads_queues`` registration, so ``_python_exit`` never joins them and
interpreter shutdown skips them.

The original implementation overrode the private ``_worker`` /
``_adjust_thread_count`` API (version-fragile across CPython releases).
This version monkey-patches ``threading.Thread`` to force ``daemon=True``
during worker spawn (robust on CPython 3.8–3.14) and then de-registers each
spawned worker from ``_threads_queues`` to preserve the non-blocking-exit
guarantee.  ``_initializer`` / ``_initargs`` are re-bound explicitly so the
attributes are always present, even if a CPython version renames the slot
(thanks to the explicit re-bind, ``self._initializer`` never raises
``AttributeError`` at the call site).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor variant whose workers do not block process exit."""

    def __init__(self, max_workers=None, thread_name_prefix='', initializer=None, initargs=()):
        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=initializer,
            initargs=initargs,
        )
        # Re-bind explicitly: guarantees presence even if the base class
        # renames/removes the slot in a future CPython (fixes the
        # "'DaemonThreadPoolExecutor' object has no attribute '_initializer'"
        # call-site crash).
        self._initializer = initializer
        self._initargs = initargs

    def _adjust_thread_count(self):
        orig_thread = threading.Thread

        def daemon_thread(*args, **kwargs):
            kwargs['daemon'] = True
            return orig_thread(*args, **kwargs)

        threading.Thread = daemon_thread
        try:
            super()._adjust_thread_count()
        finally:
            threading.Thread = orig_thread

        # Remove spawned workers from the atexit-join queue so a wedged
        # worker never blocks interpreter exit.
        import concurrent.futures.thread as _ft
        for t in list(self._threads):
            _ft._threads_queues.pop(t, None)
