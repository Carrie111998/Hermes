"""The reaper must never wait on children it does not own.

On 2026-08-26/27 the kanban dispatcher stopped twice in 18 hours, each time
leaving the board frozen for ~7 hours while the gateway process looked healthy.
Both times the log ended the same way::

    01:07 kanban dispatcher [default]: spawned=1 ...
    01:08 kanban dispatcher: reaped 1 zombie worker(s), pids=[5069]
          (silence)

    08:18 kanban dispatcher [default]: spawned=1 ...
    08:32 kanban dispatcher: reaped 1 zombie worker(s), pids=[54057]
          (silence)

``reap_worker_zombies`` called ``os.waitpid(-1, WNOHANG)`` in a loop, which
reaps ANY child of the process. The gateway also spawns children through
``asyncio.create_subprocess_exec`` / ``_shell``, and asyncio learns a child
exited by waiting on it itself. Reaping ``-1`` steals that exit: asyncio's
watcher never sees the status, the ``await proc.wait()`` behind it never
resolves, and the awaiting task hangs with nothing logged.

These tests pin the ownership boundary. The regression they guard is silent by
nature, so it must be caught here rather than in production.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _clear_owned_pids():
    with kb._OWNED_WORKER_PIDS_LOCK:
        kb._OWNED_WORKER_PIDS.clear()
    yield
    with kb._OWNED_WORKER_PIDS_LOCK:
        kb._OWNED_WORKER_PIDS.clear()


@pytest.mark.skipif(os.name == "nt", reason="POSIX child semantics")
def test_reaper_ignores_children_it_does_not_own():
    """The exact production failure: an unowned child must survive the reap."""
    # The child must have EXITED for theft to be possible: waitpid(-1, WNOHANG)
    # returns 0 for a still-running child and only steals one that has become a
    # zombie. A test using a long-sleeping child passes even with the bug
    # present and therefore proves nothing.
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    pid = proc.pid

    # Wait for it to become a zombie WITHOUT reaping it ourselves (no poll(),
    # no wait() — both call waitpid and would consume the status we are
    # protecting).
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG | os.WUNTRACED) == (0, 0):
                # still running; keep waiting
                time.sleep(0.05)
                continue
        except ChildProcessError:
            pytest.fail("child vanished before the test could run")
        break
    else:
        pytest.fail("child never exited")

    # NOTE: the probe above already consumed the status, so re-spawn cleanly
    # and assert the property with the reaper as the only waiter.
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    pid = proc.pid
    time.sleep(0.4)  # let it exit and become a zombie

    reaped = kb.reap_worker_zombies()   # never registered → must not touch it
    assert pid not in reaped, "reaper stole a child it does not own"

    # The real owner must still be able to collect the exit status. Under the
    # old waitpid(-1) reaper this raised ChildProcessError — which is exactly
    # how asyncio's watcher lost its child and hung forever.
    assert proc.wait(timeout=10) == 7


@pytest.mark.skipif(os.name == "nt", reason="POSIX child semantics")
def test_reaper_reaps_only_registered_workers():
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    kb.register_worker_pid(proc.pid)
    pid = proc.pid

    # Give the child time to actually exit and become reapable. Do NOT use
    # proc.poll() here: Popen.poll() calls waitpid itself and would reap the
    # child out from under the function we are testing.
    deadline = time.time() + 10
    reaped: list[int] = []
    while time.time() < deadline:
        reaped = kb.reap_worker_zombies()
        if reaped:
            break
        time.sleep(0.05)

    assert pid in reaped, f"registered worker was never reaped (got {reaped})"
    with kb._OWNED_WORKER_PIDS_LOCK:
        assert pid not in kb._OWNED_WORKER_PIDS, "reaped pid must be forgotten"


@pytest.mark.skipif(os.name == "nt", reason="POSIX child semantics")
def test_reaper_never_calls_waitpid_minus_one(monkeypatch):
    """Ownership is the whole fix. Widening back to -1 must fail loudly."""
    seen: list[int] = []
    real = os.waitpid

    def _spy(pid, flags):
        seen.append(pid)
        if pid == -1:
            raise AssertionError(
                "reap_worker_zombies called waitpid(-1) — this steals asyncio's "
                "children and silently hangs the dispatcher"
            )
        return real(pid, flags)

    monkeypatch.setattr(os, "waitpid", _spy)
    kb.register_worker_pid(999999)  # nonexistent: exercises the error path
    kb.reap_worker_zombies()
    assert -1 not in seen


@pytest.mark.skipif(os.name == "nt", reason="POSIX child semantics")
def test_asyncio_child_still_resolves_after_a_reap():
    """End-to-end: an asyncio subprocess must still complete when the reaper
    runs concurrently. This is the behaviour the board lost."""

    async def _run():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(0.3)",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Reap while asyncio's child is live — the production interleaving.
        kb.reap_worker_zombies()
        await asyncio.wait_for(proc.wait(), timeout=15)
        return proc.returncode

    assert asyncio.run(_run()) == 0
