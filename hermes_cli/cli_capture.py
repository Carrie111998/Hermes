"""One serialization boundary for gateway CLI-style output capture.

`contextlib.redirect_stdout` swaps **process-global** `sys.stdout`. It is not
thread-local, so two gateway requests that each capture a CLI handler's output
in a worker thread will interleave: one reply can absorb another request's
text, and a reply can be lost entirely. Reproduced with two overlapping
`/project` calls — both replies contained the second request's output and the
first request's output vanished.

Every gateway path that runs a CLI handler for its printed output therefore
takes the same lock here. Two rules make it safe:

* **One lock, not one per command.** `/project` and `/kanban` capture the same
  global streams, so a per-command lock would leave the cross-command leak
  open.
* **The lock spans the whole capture region**, not each `redirect_*` block. A
  function that captures twice (``kanban.run_slash`` inspects the first
  buffer before running the second) must hold it across both, or another
  thread redirects in the gap.

It is an ``RLock``: a captured handler that re-enters this module nests its
buffers correctly instead of deadlocking.

**Never acquire this on the event loop.** Callers run their handler in
``asyncio.to_thread``; the lock is contended in worker threads only, so a slow
command delays other *commands*, never the gateway's message loop.
"""

from __future__ import annotations

import contextlib
import io
import threading
from typing import Any, Callable, Iterator, Tuple

# Module-global on purpose: it guards a process-global resource.
_CAPTURE_LOCK = threading.RLock()


@contextlib.contextmanager
def cli_output_lock() -> Iterator[None]:
    """Hold the capture boundary without redirecting anything.

    For a callee that does its own ``redirect_stdout`` — possibly more than
    once — and only needs the exclusion.
    """
    with _CAPTURE_LOCK:
        yield


@contextlib.contextmanager
def captured_streams() -> Iterator[Tuple[io.StringIO, io.StringIO]]:
    """Exclusive capture of stdout and stderr into two fresh buffers.

    The streams are restored on the way out, exception or not, and — because
    the lock is held for the whole region — restored to what this thread
    replaced rather than to another request's buffer.
    """
    with _CAPTURE_LOCK:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err


def capture_cli_output(fn: Callable[..., Any], /, *args: Any,
                       **kwargs: Any) -> Tuple[str, str, Any]:
    """Run *fn* under the boundary; return ``(stdout, stderr, result)``."""
    with captured_streams() as (out, err):
        result = fn(*args, **kwargs)
    return out.getvalue(), err.getvalue(), result
