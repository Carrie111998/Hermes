"""Queued cron dispatch recovery during gateway shutdown (#99120)."""

from __future__ import annotations

import concurrent.futures
import threading
from unittest.mock import patch

import pytest

import cron.jobs as jobs_mod
import cron.scheduler as sched


@pytest.fixture
def cron_store(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(sched, "_get_hermes_home", lambda: hermes_home)
    yield hermes_home
    sched._running_job_ids.clear()
    sched._running_since.clear()
    sched._running_futures.clear()
    sched._queued_dispatches.clear()


class DeferredPool:
    def __init__(self):
        self.futures: list[concurrent.futures.Future] = []

    def submit(self, _callback):
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.futures.append(future)
        return future


class BlockingSubmitPool:
    def __init__(self):
        self.future: concurrent.futures.Future = concurrent.futures.Future()
        self.submit_entered = threading.Event()
        self.allow_submit_return = threading.Event()

    def submit(self, _callback):
        self.submit_entered.set()
        assert self.allow_submit_return.wait(timeout=5)
        return self.future


def _queue_without_starting(job: dict, pool: DeferredPool, monkeypatch) -> concurrent.futures.Future:
    monkeypatch.setattr(sched, "get_due_jobs", lambda: [dict(job)])
    monkeypatch.setattr(sched, "_get_parallel_pool", lambda _workers: pool)
    monkeypatch.setattr(sched, "_get_sequential_pool", lambda: pool)

    assert sched.tick(verbose=False, sync=False) == 1
    assert len(pool.futures) == 1
    assert job["id"] in sched.get_running_job_ids()
    return pool.futures[0]


def test_shutdown_cancels_queued_recurring_tick_without_marking_job_failed(
    cron_store, monkeypatch
):
    job = jobs_mod.create_job(prompt="poll", schedule="every 10m", deliver="local")
    pool = DeferredPool()
    future = _queue_without_starting(job, pool, monkeypatch)
    next_scheduled_run = jobs_mod.get_job(job["id"])["next_run_at"]

    cancelled = sched.cancel_queued_jobs_for_shutdown("gateway restart")

    assert cancelled == [job["id"]]
    assert future.cancelled()
    assert job["id"] not in sched.get_running_job_ids()
    reloaded = jobs_mod.get_job(job["id"])
    assert reloaded["enabled"] is True
    assert reloaded.get("last_status") is None
    assert reloaded["next_run_at"] == next_scheduled_run


def test_shutdown_clears_queued_oneshot_claim_so_next_gateway_can_fire(
    cron_store, monkeypatch
):
    job = jobs_mod.create_job(prompt="remind me", schedule="in 30m", deliver="local")
    stored = jobs_mod.load_jobs()
    for row in stored:
        if row["id"] == job["id"]:
            row["run_claim"] = {"at": "2026-08-31T00:00:00+00:00", "by": "test:1"}
            job = dict(row)
    jobs_mod.save_jobs(stored)

    pool = DeferredPool()
    future = _queue_without_starting(job, pool, monkeypatch)

    cancelled = sched.cancel_queued_jobs_for_shutdown("gateway restart")

    assert cancelled == [job["id"]]
    assert future.cancelled()
    reloaded = jobs_mod.get_job(job["id"])
    assert reloaded["enabled"] is True
    assert reloaded.get("run_claim") is None
    assert reloaded["next_run_at"] == job["next_run_at"]


def test_shutdown_does_not_cancel_a_worker_that_already_started(cron_store):
    job_id = "running-job"
    future: concurrent.futures.Future = concurrent.futures.Future()
    assert future.set_running_or_notify_cancel() is True
    sched._running_job_ids.add(job_id)
    sched._running_futures[job_id] = future
    sched._queued_dispatches[job_id] = {
        "future": future,
        "execution_id": "execution-running",
        "job": {"id": job_id, "schedule": {"kind": "interval", "minutes": 10}},
        "context": None,
    }

    assert sched.cancel_queued_jobs_for_shutdown("gateway restart") == []
    assert not future.cancelled()
    assert job_id in sched.get_running_job_ids()
    future.set_result(True)


def test_shutdown_cancels_during_submit_to_future_publication(cron_store, monkeypatch):
    job = jobs_mod.create_job(prompt="poll", schedule="every 10m", deliver="local")
    pool = BlockingSubmitPool()
    monkeypatch.setattr(sched, "get_due_jobs", lambda: [dict(job)])
    monkeypatch.setattr(sched, "_get_parallel_pool", lambda _workers: pool)
    monkeypatch.setattr(sched, "_get_sequential_pool", lambda: pool)
    tick_result: list[int] = []

    tick_thread = threading.Thread(
        target=lambda: tick_result.append(sched.tick(verbose=False, sync=False))
    )
    tick_thread.start()
    assert pool.submit_entered.wait(timeout=5)
    assert sched._running_lock.locked()
    pool.allow_submit_return.set()
    tick_thread.join(timeout=5)

    assert not tick_thread.is_alive()
    assert tick_result == [1]
    assert sched.cancel_queued_jobs_for_shutdown("gateway restart") == [job["id"]]
    assert pool.future.cancelled()
    assert job["id"] not in sched.get_running_job_ids()
