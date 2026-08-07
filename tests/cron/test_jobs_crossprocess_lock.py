"""Regression test for the jobs.json cross-process lock.

Background: ``hermes cron pause`` runs in its own process (CLI → cronjob tool →
``pause_job`` → ``update_job`` → ``save_jobs``), entirely separate from the
gateway process that also writes ``jobs.json`` (``mark_job_run`` /
``advance_next_run`` / due-fast-forward). The module's ``threading.Lock`` only
serializes writers *inside one process*, so a CLI pause issued while the gateway
was live could be silently lost to a concurrent gateway write — the job kept
firing even though the CLI reported "Paused".

``_jobs_lock()`` closes that gap with a short-held cross-process advisory file
lock. This test proves the lock actually excludes a *separate process*, which an
in-process ``threading.Lock`` cannot do.
"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from cron import jobs


# Repo root (parent of the ``cron`` package) so the child process can import it.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(jobs.__file__)))


@pytest.mark.skipif(jobs.fcntl is None, reason="POSIX fcntl/flock required")
def test_jobs_lock_excludes_another_process(tmp_path, monkeypatch):
    cron_dir = tmp_path / "cron"
    output_dir = cron_dir / "output"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", output_dir)

    ready = tmp_path / "child_holds_lock"
    release = tmp_path / "child_may_release"
    blocker_started = tmp_path / "blocker_started"
    blocker_acquired = tmp_path / "blocker_acquired"
    holder = tmp_path / "holder.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import sys, time, pathlib
            sys.path.insert(0, {_REPO_ROOT!r})
            from cron import jobs

            jobs.CRON_DIR = pathlib.Path({str(cron_dir)!r})
            jobs.JOBS_FILE = jobs.CRON_DIR / "jobs.json"
            jobs.OUTPUT_DIR = jobs.CRON_DIR / "output"

            with jobs._jobs_lock():
                pathlib.Path({str(ready)!r}).write_text("1")
                # Hold the lock until the parent signals (bounded so a wedged
                # test can never hang CI).
                for _ in range(1000):
                    if pathlib.Path({str(release)!r}).exists():
                        break
                    time.sleep(0.01)
            """
        )
    )

    blocker = tmp_path / "blocker.py"
    blocker.write_text(
        textwrap.dedent(
            f"""
            import sys, pathlib
            sys.path.insert(0, {_REPO_ROOT!r})
            from cron import jobs

            jobs.CRON_DIR = pathlib.Path({str(cron_dir)!r})
            jobs.JOBS_FILE = jobs.CRON_DIR / "jobs.json"
            jobs.OUTPUT_DIR = jobs.CRON_DIR / "output"

            pathlib.Path({str(blocker_started)!r}).write_text("1")
            with jobs._jobs_lock():
                pathlib.Path({str(blocker_acquired)!r}).write_text("1")
            """
        )
    )

    child = subprocess.Popen([sys.executable, str(holder)])
    blocker_child = None
    try:
        # Wait until the child is inside the critical section.
        for _ in range(1000):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "child never acquired _jobs_lock()"

        # While the child holds it, a non-blocking acquire of the SAME lock file
        # from this process must fail. A threading.Lock could never block here.
        lock_file = jobs._jobs_lock_file()
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            with pytest.raises(OSError):
                jobs.fcntl.flock(fd, jobs.fcntl.LOCK_EX | jobs.fcntl.LOCK_NB)
        finally:
            os.close(fd)

        # A second _jobs_lock() caller in another process should block until the
        # holder releases, rather than falling through with only a process-local
        # threading lock.
        blocker_child = subprocess.Popen([sys.executable, str(blocker)])
        for _ in range(1000):
            if blocker_started.exists():
                break
            time.sleep(0.01)
        assert blocker_started.exists(), "blocker process never started"
        time.sleep(0.05)
        assert not blocker_acquired.exists(), "second process entered _jobs_lock() while held"
    finally:
        release.write_text("1")
        child.wait(timeout=15)
        if blocker_child is not None:
            blocker_child.wait(timeout=15)

    assert blocker_acquired.exists(), "second process did not acquire _jobs_lock() after release"

    # Once the child has released, the lock is freely acquirable again.
    with jobs._jobs_lock():
        pass


def test_degraded_lock_recovers_concurrently_created_job(tmp_path, monkeypatch):
    """#80624: a job a sibling process wrote during a degraded (unlocked)
    window must not be silently discarded by this process's own save.

    ``_jobs_lock()`` intentionally falls through to in-process-only locking
    when the cross-process flock times out or is unavailable (#60703) — that
    is a deliberate liveness tradeoff, not a bug. But before this fix, a save
    made during that window would blindly overwrite jobs.json with whatever
    stale list this process last loaded, discarding any job a sibling process
    (e.g. the CLI) wrote in between. Reproduces that exact race in-process by
    forcing ``_jobs_lock_state`` into the degraded state _jobs_lock() would
    have left it in, without depending on OS-specific flock timing.
    """
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")

    job_a = {"id": "aaaaaaaaaaaa", "name": "existing"}
    job_b = {"id": "bbbbbbbbbbbb", "name": "cli-created"}

    try:
        jobs.save_jobs([job_a])

        # This process enters a degraded critical section (as _jobs_lock()
        # does after a flock timeout) and loads the current, job_b-less state.
        jobs._jobs_lock_state.depth = 1
        jobs._jobs_lock_state.degraded = True
        assert jobs.load_jobs() == [job_a]
        stale_stamp = jobs._jobs_lock_state.load_stamp
        stale_ids = jobs._jobs_lock_state.load_ids

        # A sibling process (e.g. the CLI) creates job_b concurrently, via its
        # own independent, fully-scoped _jobs_lock() cycle.
        jobs._jobs_lock_state.depth = 0
        jobs.save_jobs([job_a, job_b])

        # This process resumes its degraded section with the stale view it
        # actually observed (no job_b) and saves — pre-fix, this silently
        # wiped job_b out of jobs.json.
        jobs._jobs_lock_state.depth = 1
        jobs._jobs_lock_state.degraded = True
        jobs._jobs_lock_state.load_stamp = stale_stamp
        jobs._jobs_lock_state.load_ids = stale_ids
        jobs._save_jobs_unlocked([job_a])
    finally:
        jobs._jobs_lock_state.depth = 0
        jobs._jobs_lock_state.degraded = False
        jobs._jobs_lock_state.load_stamp = None
        jobs._jobs_lock_state.load_ids = None

    on_disk_ids = {j["id"] for j in jobs.load_jobs()}
    assert on_disk_ids == {job_a["id"], job_b["id"]}, (
        "sibling-created job was clobbered by a degraded-lock write (#80624)"
    )


def test_healthy_lock_write_recovers_sibling_degraded_create(tmp_path, monkeypatch):
    """#80624 reverse direction: a *healthy* lock holder can still clobber a
    sibling's degraded write if that sibling raced in and out while this
    process's own critical section was open. flock only excludes other
    processes that also hold it — it does nothing to stop a process that
    gave up waiting for it. The reconcile check must not be gated on this
    process's own ``degraded`` flag, or this direction of the race reopens
    the exact #80624 symptom.
    """
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")

    job_a = {"id": "aaaaaaaaaaaa", "name": "existing"}
    job_b = {"id": "bbbbbbbbbbbb", "name": "cli-created"}

    try:
        jobs.save_jobs([job_a])

        # This process opens a *healthy* critical section (real flock held)
        # and loads the current, job_b-less state.
        jobs._jobs_lock_state.depth = 1
        jobs._jobs_lock_state.degraded = False
        assert jobs.load_jobs() == [job_a]
        stale_stamp = jobs._jobs_lock_state.load_stamp
        stale_ids = jobs._jobs_lock_state.load_ids

        # A sibling process couldn't get the flock, degraded, and created
        # job_b anyway via its own independent _jobs_lock() cycle. (This
        # nested call shares the same thread-local as the outer section only
        # because the test simulates two processes in one thread — real
        # processes each have their own _jobs_lock_state, so this reset
        # doesn't happen in production; restored below to keep the test
        # faithful to the real per-process state.)
        jobs._jobs_lock_state.depth = 0
        jobs.save_jobs([job_a, job_b])

        # This process resumes its still-healthy section with the stale view
        # it actually observed (no job_b) and saves.
        jobs._jobs_lock_state.depth = 1
        jobs._jobs_lock_state.degraded = False
        jobs._jobs_lock_state.load_stamp = stale_stamp
        jobs._jobs_lock_state.load_ids = stale_ids
        jobs._save_jobs_unlocked([job_a])
    finally:
        jobs._jobs_lock_state.depth = 0
        jobs._jobs_lock_state.degraded = False
        jobs._jobs_lock_state.load_stamp = None
        jobs._jobs_lock_state.load_ids = None

    on_disk_ids = {j["id"] for j in jobs.load_jobs()}
    assert on_disk_ids == {job_a["id"], job_b["id"]}, (
        "sibling's degraded create was clobbered by a healthy-lock write (#80624)"
    )
