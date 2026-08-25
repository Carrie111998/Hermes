"""Shared fixture for driving a REAL foreign process that holds an open
execution row.

``live_execution_for_job`` answers a strictly cross-process question — it
excludes rows owned by the calling process on purpose (a stale own-process row
would otherwise read as live forever; see its docstring). So a test that opens
a row with ``create_execution`` in the pytest process is testing nothing: that
row is excluded by design.

Every "a live run blocks this" assertion therefore needs a genuinely separate,
genuinely alive process. That is fiddly enough — and easy enough to get subtly
wrong — to be worth writing once.

Not named ``test_*`` so pytest does not collect it as a test module.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def live_foreign_run(ledger: Path, job_id: str, *, timeout: float = 120.0):
    """Hold a non-terminal execution row for ``job_id`` in another live process.

    Yields that process's real pid. On exit the process is killed and the
    context does not return until the OS agrees it is gone, so a following
    "…and now the job is fireable again" assertion tests the guard rather than
    racing teardown.

    Two traps this exists to encapsulate:

    * ``sys.executable`` in this venv is a ~256KB **uv trampoline stub** that
      re-execs the real interpreter, so ``Popen.pid`` is NOT the process that
      runs the code (measured: 29096 vs 41112). The child reports its own
      ``os.getpid()`` through a file, and that is the pid we kill and probe.
    * ``cron.executions.EXECUTIONS_FILE`` is resolved at import time, so the
      child must assign it after import rather than rely on ``HERMES_HOME``.
    """
    ready = ledger.parent / f"ready-{job_id}.pid"
    ready.parent.mkdir(parents=True, exist_ok=True)
    src = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from pathlib import Path
        import cron.executions as ex
        ex.EXECUTIONS_FILE = Path({str(ledger)!r})
        rec = ex.create_execution({job_id!r}, source="direct")
        ex.mark_execution_running(rec["id"])
        Path({str(ready)!r}).write_text(str(os.getpid()))
        # Hard self-bound so a leaked child can never outlive the suite.
        time.sleep({timeout!r})
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", src],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    worker_pid = None
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _, err = proc.communicate()
                raise AssertionError(
                    "foreign run died before opening its row: "
                    f"{err.decode(errors='replace')[-2000:]}"
                )
            try:
                worker_pid = int(ready.read_text().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.05)
        if worker_pid is None:
            raise AssertionError("foreign run never opened its execution row")
        yield worker_pid
    finally:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=60)
        if worker_pid is not None:
            with contextlib.suppress(Exception):
                import psutil

                psutil.Process(worker_pid).kill()
            _wait_until_dead(worker_pid)
        with contextlib.suppress(OSError):
            ready.unlink()


def _wait_until_dead(pid: int, *, timeout: float = 60.0) -> None:
    """Block until ``pid`` is gone, so release assertions aren't racing."""
    from gateway.status import _pid_exists

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} survived teardown; cannot test release")
