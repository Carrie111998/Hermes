"""Re-arming a consumed finite one-shot via a schedule edit (#93524).

A consumed one-shot is retained as a terminal record (repeat.completed >=
repeat.times, enabled=False, next_run_at=None) so outcomes stay
inspectable. Editing its schedule re-enables the job with a future
next_run_at — but without resetting repeat.completed, the due scan's
dispatch-limit guard removes the job at the new fire time without firing:
the rescheduled run silently never happens and the record is gone.

A schedule edit is an explicit re-arm gesture, so the spent quota resets.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json doesn't touch the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _seed_consumed_oneshot(home, name="spent") -> str:
    """Persist a consumed finite one-shot (the terminal retention shape)."""
    from cron.jobs import create_job, save_jobs, load_jobs

    job = create_job(prompt="done", schedule="2026-08-25T09:00", name=name)
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["enabled"] = False
            j["state"] = "completed"
            j["next_run_at"] = None
            j["repeat"] = {"times": 1, "completed": 1}
    save_jobs(jobs)
    return job["id"]


def _update(temp_home, job_id: str, **kwargs):
    from tools.cronjob_tools import cronjob

    defaults = {"action": "update", "job_id": job_id}
    defaults.update(kwargs)
    return json.loads(cronjob(**defaults))


def test_schedule_edit_rearms_consumed_oneshot(temp_home):
    """Editing the schedule of a consumed one-shot resets the spent quota —
    the job is enabled with a future next_run_at and completed back to 0."""
    from cron.jobs import get_job

    jid = _seed_consumed_oneshot(temp_home)

    result = _update(temp_home, jid, schedule="2030-01-01T09:00")

    assert result["success"] is True
    job = get_job(jid)
    assert job["enabled"] is True
    assert job["state"] == "scheduled"
    assert job["next_run_at"] is not None
    assert job["next_run_at"].startswith("2030-01-01")
    assert job["repeat"]["completed"] == 0
    assert job["repeat"]["times"] == 1


def test_schedule_edit_leaves_unconsumed_quota_alone(temp_home):
    """Control: an unconsumed one-shot's completed counter is untouched."""
    from cron.jobs import create_job, get_job

    job = create_job(prompt="fresh", schedule="2030-01-01T09:00", name="fresh")

    result = _update(temp_home, job["id"], schedule="2030-02-01T09:00")

    assert result["success"] is True
    stored = get_job(job["id"])
    assert stored["repeat"]["completed"] == 0
    assert stored["next_run_at"].startswith("2030-02-01")


def test_schedule_edit_with_new_repeat_resets_under_new_quota(temp_home):
    """--repeat alongside the schedule edit composes: the quota resets under
    the operator's new times value."""
    from cron.jobs import get_job

    jid = _seed_consumed_oneshot(temp_home, name="multi")

    result = _update(temp_home, jid, schedule="2030-01-01T09:00", repeat=3)

    assert result["success"] is True
    job = get_job(jid)
    assert job["repeat"] == {"times": 3, "completed": 0}
    assert job["enabled"] is True


def test_paused_consumed_oneshot_not_rearmed(temp_home):
    """A paused record's schedule edit does not re-enable — the reset must
    not fire either (the re-arm block is gated on state != paused)."""
    from cron.jobs import create_job, save_jobs, load_jobs, get_job

    job = create_job(prompt="p", schedule="2030-01-01T09:00", name="paused")
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["enabled"] = False
            j["state"] = "paused"
            j["repeat"] = {"times": 1, "completed": 1}
    save_jobs(jobs)

    result = _update(temp_home, job["id"], schedule="2030-02-01T09:00")

    assert result["success"] is True
    stored = get_job(job["id"])
    assert stored["state"] == "paused"
    assert stored["enabled"] is False
