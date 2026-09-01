"""Regression tests for #99879: routine catch-up dispatch must surface lateness.

An overdue recurring job that gateway downtime caused to miss its window
must not appear as an ordinary on-time run. The fix tags the in-memory
job with _dispatch_meta (scheduled vs actual time, lateness, kind) so
logs, execution records, and the Routines UI can display
"catch-up — scheduled 09:00, ran 09:31 (31m late)" instead of silently
"ok".
"""

import copy
from datetime import datetime, timezone, timedelta
from unittest.mock import patch


def test_overdue_beyond_grace_gets_catch_up_meta(monkeypatch):
    """A job whose next_run_at is 31m in the past gets dispatch_meta."""
    from cron.jobs import _get_due_jobs_locked
    from hermes_time import now as hermes_now

    # Freeze now at 09:31 UTC on 2026-09-01
    fixed_now = datetime(2026, 9, 1, 9, 31, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: fixed_now)

    # Due job scheduled for 09:00 (31m overdue)
    overdue_run = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc).isoformat()
    job = {
        "id": "catch-up-meta-test",
        "name": "daily 9am",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "next_run_at": overdue_run,
        "last_run_at": "2026-08-31T09:00:00+00:00",
    }

    with patch("cron.jobs.load_jobs", return_value=[copy.deepcopy(job)]), \
         patch("cron.jobs.save_jobs"), \
         patch("cron.jobs._ensure_croniter", return_value=False), \
         patch("cron.jobs.compute_next_run", return_value="2026-09-02T09:00:00+00:00"), \
         patch("cron.jobs._compute_grace_seconds", return_value=60), \
         patch("cron.jobs.record_catch_up_occurrence"), \
         patch("cron.jobs._sweep_completed_oneshots", return_value=False), \
         patch("cron.scheduler._cron_interval_minutes", return_value=1440.0):

        due = _get_due_jobs_locked()

    dispatched = next((j for j in due if j["id"] == "catch-up-meta-test"), None)
    assert dispatched is not None, "overdue job should still dispatch once (catch-up)"
    meta = dispatched.get("_dispatch_meta")
    assert meta is not None, "_dispatch_meta must be set on catch-up dispatches"
    assert meta["scheduled_at"] == overdue_run
    assert meta["dispatch_kind"] == "catch_up"
    # 09:31 - 09:00 = 31m = 1860s
    assert 1859 <= meta["lateness_seconds"] <= 1861
    assert "dispatched_at" in meta


def test_on_time_job_has_no_catch_up_meta(monkeypatch):
    """A job due exactly now must NOT get catch-up tagging."""
    from cron.jobs import _get_due_jobs_locked

    fixed_now = datetime(2026, 9, 1, 9, 0, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: fixed_now)

    next_run = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc).isoformat()
    job = {
        "id": "on-time-test",
        "name": "daily 9am",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "next_run_at": next_run,
        "last_run_at": "2026-08-31T09:00:00+00:00",
    }

    with patch("cron.jobs.load_jobs", return_value=[copy.deepcopy(job)]), \
         patch("cron.jobs.save_jobs"), \
         patch("cron.jobs._ensure_croniter", return_value=False), \
         patch("cron.jobs.compute_next_run", return_value="2026-09-02T09:00:00+00:00"), \
         patch("cron.jobs._compute_grace_seconds", return_value=300), \
         patch("cron.jobs._sweep_completed_oneshots", return_value=False):

        due = _get_due_jobs_locked()

    dispatched = next((j for j in due if j["id"] == "on-time-test"), None)
    assert dispatched is not None
    assert dispatched.get("_dispatch_meta") is None
