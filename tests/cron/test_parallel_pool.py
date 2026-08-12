"""Tests for the persistent parallel pool and running-job guard in cron/scheduler.py.

These verify the fix for the tick-blocking issue where as_completed(timeout=600)
prevented the ticker thread from firing, causing all other jobs to be fast-forwarded.
"""

import concurrent.futures
import threading
import time
from unittest.mock import patch

import pytest


class TestPersistentPool:
    """_get_parallel_pool returns a persistent ThreadPoolExecutor."""

    def test_pool_is_reused(self, monkeypatch):
        """Same pool instance returned when max_workers doesn't change."""
        import cron.scheduler as sched

        # Reset module state.
        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None

        pool1 = sched._get_parallel_pool(4)
        pool2 = sched._get_parallel_pool(4)
        assert pool1 is pool2

        # Cleanup.
        sched._shutdown_parallel_pool()


    def test_shutdown_clears_pool(self, monkeypatch):
        """_shutdown_parallel_pool resets state."""
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._get_parallel_pool(2)

        sched._shutdown_parallel_pool()
        assert sched._parallel_pool is None
        assert sched._parallel_pool_max_workers is None


class TestRunningJobGuard:
    """_running_job_ids prevents double-dispatch of active jobs."""

    def test_running_set_prevents_double_dispatch(self, tmp_path, monkeypatch):
        """A job already in _running_job_ids is skipped on the next tick."""
        import cron.scheduler as sched

        # Reset state.
        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        job = {
            "id": "guard-job",
            "name": "guard-test",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
        }

        # Simulate the job already running.
        sched._running_job_ids.add("guard-job")

        dispatched = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: [job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: dispatched.append(j["id"]) or (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False)
        assert n == 0  # skipped, not dispatched
        assert dispatched == []

        sched._running_job_ids.discard("guard-job")
        sched._shutdown_parallel_pool()

    def test_queued_job_heartbeats_claim_before_worker_starts(
        self, monkeypatch
    ):
        """A pool backlog cannot let a durable attempt claim expire."""
        import cron.scheduler as sched

        sched._running_job_ids.clear()
        heartbeat_seen = threading.Event()
        submitted = {}
        advanced_attempts = {}
        job = {
            "id": "queued-claim",
            "name": "queued-claim",
            "prompt": "test",
            "schedule": {"kind": "interval", "minutes": 1},
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00+00:00",
            "deliver": "local",
            "run_claim": {
                "at": "2020-01-01T00:00:00+00:00",
                "by": "attempt-token",
            },
        }

        class QueuedPool:
            def submit(self, fn):
                submitted["worker"] = fn
                return concurrent.futures.Future()

        monkeypatch.setattr(sched, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: [job])
        monkeypatch.setattr(
            sched,
            "advance_next_runs",
            lambda attempts: advanced_attempts.update(attempts) or 0,
        )
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda _limit: QueuedPool())
        monkeypatch.setattr(
            sched,
            "create_execution",
            lambda *_a, **_kw: {"id": "execution-1"},
        )
        monkeypatch.setattr(sched, "run_one_job", lambda *_a, **_kw: True)
        monkeypatch.setattr(
            sched,
            "heartbeat_run_claim",
            lambda *_a, **_kw: heartbeat_seen.set() or True,
        )

        assert sched.tick(verbose=False, sync=False) == 1
        assert heartbeat_seen.wait(timeout=1), (
            "claim heartbeat did not start while work remained queued"
        )
        assert advanced_attempts == {"queued-claim": "attempt-token"}
        submitted["worker"]()
        assert "queued-claim" not in sched.get_running_job_ids()

    def test_queued_worker_does_not_run_after_attempt_token_is_lost(
        self, monkeypatch
    ):
        """The worker rechecks its exact fence immediately before side effects."""
        import cron.scheduler as sched

        sched._running_job_ids.clear()
        submitted = {}
        owned = [True]
        ran = []
        finished = []
        job = {
            "id": "replaced-while-queued",
            "name": "queued",
            "prompt": "test",
            "schedule": {"kind": "interval", "minutes": 1},
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00+00:00",
            "run_claim": {
                "at": "2020-01-01T00:00:00+00:00",
                "by": "original-attempt",
            },
        }

        class QueuedPool:
            def submit(self, fn):
                submitted["worker"] = fn
                return concurrent.futures.Future()

        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: [job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda _limit: QueuedPool())
        monkeypatch.setattr(
            sched, "create_execution", lambda *_a, **_kw: {"id": "execution-2"}
        )
        monkeypatch.setattr(
            sched,
            "heartbeat_run_claim",
            lambda *_a, **_kw: owned[0],
        )
        monkeypatch.setattr(
            sched, "release_run_claim", lambda *_a, **_kw: True
        )
        monkeypatch.setattr(
            sched, "run_one_job", lambda *_a, **_kw: ran.append(True) or True
        )
        monkeypatch.setattr(
            sched,
            "finish_execution",
            lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
        )

        assert sched.tick(verbose=False, sync=False) == 1
        owned[0] = False
        assert submitted["worker"]() is False
        assert ran == []
        assert finished[-1][0] == "execution-2"
        assert "not started" in finished[-1][1]["error"]

    def test_queued_heartbeat_error_and_release_error_still_finish_ledger(
        self, monkeypatch
    ):
        import cron.scheduler as sched

        sched._running_job_ids.clear()
        submitted = {}
        finished = []
        heartbeat_calls = [0]
        job = {
            "id": "queued-store-error",
            "name": "queued",
            "schedule": {"kind": "interval", "minutes": 1},
            "run_claim": {"at": "2020-01-01T00:00:00+00:00", "by": "owner"},
        }

        class QueuedPool:
            def submit(self, fn):
                submitted["worker"] = fn
                return concurrent.futures.Future()

        def heartbeat(*_a, **_kw):
            heartbeat_calls[0] += 1
            if heartbeat_calls[0] == 1:
                return True  # heartbeat startup
            raise OSError("jobs store unavailable")

        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kw: [job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda _limit: QueuedPool())
        monkeypatch.setattr(
            sched, "create_execution", lambda *_a, **_kw: {"id": "exec-error"}
        )
        monkeypatch.setattr(sched, "heartbeat_run_claim", heartbeat)
        monkeypatch.setattr(
            sched,
            "release_run_claim",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("release failed")),
        )
        monkeypatch.setattr(
            sched,
            "finish_execution",
            lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
        )
        monkeypatch.setattr(
            sched,
            "run_one_job",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                AssertionError("unverifiable queued attempt must not run")
            ),
        )

        assert sched.tick(verbose=False, sync=False) == 1
        assert submitted["worker"]() is False
        assert finished[-1][0] == "exec-error"
        assert finished[-1][1]["success"] is False
        assert "unavailable" in finished[-1][1]["error"]

    def test_same_job_id_in_two_profiles_dispatches_independently(
        self, tmp_path, monkeypatch
    ):
        """Multiplex profile-local job IDs must not collide in the running guard."""
        import cron.scheduler as sched
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        sched._running_job_ids.clear()
        workers = []
        job = {
            "id": "shared-id",
            "name": "profile-local",
            "prompt": "test",
            "schedule": {"kind": "interval", "minutes": 1},
            "next_run_at": "2020-01-01T00:00:00+00:00",
        }

        class QueuedPool:
            def submit(self, fn):
                workers.append(fn)
                return concurrent.futures.Future()

        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: [job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "_get_parallel_pool", lambda _limit: QueuedPool())
        execution_count = iter(range(2))
        monkeypatch.setattr(
            sched,
            "create_execution",
            lambda *_a, **_kw: {"id": f"execution-{next(execution_count)}"},
        )
        monkeypatch.setattr(sched, "run_one_job", lambda *_a, **_kw: True)

        for profile in (tmp_path / "a", tmp_path / "b"):
            token = set_hermes_home_override(str(profile))
            try:
                assert sched.tick(verbose=False, sync=False) == 1
            finally:
                reset_hermes_home_override(token)

        assert len(workers) == 2
        for worker in workers:
            assert worker() is True
        assert sched.get_running_job_ids() == frozenset()

    def test_tick_abort_releases_every_unsubmitted_attempt_claim(
        self, monkeypatch
    ):
        import cron.scheduler as sched

        job = {
            "id": "abort-before-submit",
            "run_claim": {
                "at": "2026-01-01T00:00:00+00:00",
                "by": "abort-token",
            },
        }
        released = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: [job])
        monkeypatch.setattr(
            sched,
            "advance_next_runs",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("advance failed")),
        )
        monkeypatch.setattr(
            sched,
            "release_run_claim",
            lambda job_id, *, expected_owner: released.append(
                (job_id, expected_owner)
            ) or True,
        )

        with pytest.raises(RuntimeError, match="advance failed"):
            sched.tick(verbose=False)

        assert released == [("abort-before-submit", "abort-token")]

    def test_unsubmitted_claim_release_retries_after_transient_error(
        self, monkeypatch
    ):
        """A failed early release remains pending for tick's final cleanup."""
        import cron.scheduler as sched

        job = {
            "id": "shutdown-before-submit",
            "run_claim": {
                "at": "2026-01-01T00:00:00+00:00",
                "by": "shutdown-token",
            },
        }
        release_calls = []

        def flaky_release(job_id, *, expected_owner):
            release_calls.append((job_id, expected_owner))
            if len(release_calls) == 1:
                raise OSError("temporary jobs-store failure")
            return True

        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: [job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "_interpreter_shutting_down", lambda *_a: True)
        monkeypatch.setattr(sched, "release_run_claim", flaky_release)

        assert sched.tick(verbose=False) == 0
        assert release_calls == [
            ("shutdown-before-submit", "shutdown-token"),
            ("shutdown-before-submit", "shutdown-token"),
        ]

class TestSyncMode:
    """tick() blocks by default (sync=True); tick(sync=False) returns immediately."""

    def test_sync_true_blocks_and_returns_correct_count(self, tmp_path, monkeypatch):
        """sync=True waits for jobs and returns actual results."""
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        jobs = [
            {"id": f"job-{i}", "name": f"Job {i}", "prompt": "test",
             "schedule": "every 5m", "enabled": True,
             "next_run_at": "2020-01-01T00:00:00", "deliver": "local"}
            for i in range(3)
        ]

        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: jobs)
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False)
        assert n == 3

        sched._shutdown_parallel_pool()


class TestSequentialPool:
    """Sequential (workdir) jobs use the persistent cron-seq pool.

    Verifies the follow-up fix: env-mutating jobs no longer run inline
    in the ticker thread, so a long workdir job can't starve the
    schedule the same way the parallel path used to.
    """

    def test_sequential_job_does_not_block_ticker(self, tmp_path, monkeypatch):
        """sync=False returns immediately even when a workdir job is slow."""
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._sequential_pool = None
        sched._running_job_ids.clear()

        job = {
            "id": "slow-workdir",
            "name": "slow-workdir",
            "prompt": "test",
            "schedule": "every 5m",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
            "workdir": str(tmp_path),  # makes it sequential
        }

        barrier = threading.Barrier(2, timeout=5)

        def slow_run(j, *, defer_agent_teardown=None, **_kwargs):
            barrier.wait()
            return True, "out", "resp", None

        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: [job])
        monkeypatch.setattr(sched, "advance_next_runs", lambda *_a, **_kw: 0)
        monkeypatch.setattr(sched, "run_job", slow_run)
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        start = time.monotonic()
        n = sched.tick(verbose=False, sync=False)
        elapsed = time.monotonic() - start

        assert n == 1  # optimistic count
        assert elapsed < 1.0  # did NOT block on the slow workdir job

        barrier.wait()
        time.sleep(0.1)
        sched._shutdown_parallel_pool()


    def test_get_sequential_pool_is_persistent(self):
        """_get_sequential_pool returns the same single-thread pool."""
        import cron.scheduler as sched

        sched._sequential_pool = None
        pool1 = sched._get_sequential_pool()
        pool2 = sched._get_sequential_pool()
        assert pool1 is pool2

        sched._shutdown_parallel_pool()
        assert sched._sequential_pool is None


class TestTickBatchAdvance:
    """The tick's pre-dispatch advance must go through advance_next_runs
    exactly once with the whole due set — a revert to the per-job loop
    (or back to advance_next_run) must fail this test, not slip past the
    helper-level I/O pin."""

    def test_tick_calls_advance_next_runs_once_with_all_due_ids(self, tmp_path, monkeypatch):
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        jobs = [
            {"id": f"job-{i}", "name": f"Job {i}", "prompt": "test",
             "schedule": "every 5m", "enabled": True,
             "next_run_at": "2020-01-01T00:00:00", "deliver": "local"}
            for i in range(4)
        ]

        advance_calls = []
        monkeypatch.setattr(sched, "get_due_jobs", lambda **_kwargs: jobs)
        monkeypatch.setattr(
            sched, "advance_next_runs",
            lambda ids: advance_calls.append(list(ids)) or len(list(ids)))
        monkeypatch.setattr(sched, "run_job", lambda j, **_kw: (True, "out", "resp", None))
        monkeypatch.setattr(sched, "save_job_output", lambda *_a, **_kw: "/tmp/out")
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_kw: None)
        monkeypatch.setattr(sched, "_deliver_result", lambda *_a, **_kw: None)

        n = sched.tick(verbose=False)

        assert n == 4
        assert advance_calls == [["job-0", "job-1", "job-2", "job-3"]], (
            f"tick must batch-advance the due set in ONE call; got {advance_calls}")

        sched._shutdown_parallel_pool()
