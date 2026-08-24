"""Re-arming a consumed finite one-shot must reset its run budget (#93524).

A finished one-shot is retained as a terminal record (``repeat.completed``
>= ``repeat.times``, ``state="completed"``, ``enabled=False``) so its
outcome stays inspectable. Editing its schedule or explicitly triggering
it used to re-enable the record **without** resetting the budget — and at
the new fire time the due-scan's stale-entry guard saw ``completed >=
times`` and silently deleted the job instead of running it (the wedged-
oneshot diagnostic was suppressed because ``last_run_at`` was already
set). "Move my report to tomorrow 9am" did nothing tomorrow.

Real store against a temp ``HERMES_HOME`` (no mocks), matching the
file-touching-code discipline in this directory.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _consumed_oneshot():
    """Create a finite one-shot and drive it to its terminal state."""
    from cron.jobs import create_job, load_jobs, mark_job_run, save_jobs

    job = create_job(
        prompt="write the report",
        schedule=(datetime.now().astimezone() + timedelta(seconds=5)).isoformat(),
        name="report",
        repeat=1,
        deliver="local",
    )
    # Simulate the fire completing: mark_job_run retires it with
    # completed >= times while retaining the record.
    mark_job_run(job["id"], success=True, status="ok")
    jobs = load_jobs()
    stored = next(j for j in jobs if j["id"] == job["id"])
    assert stored["repeat"]["completed"] >= 1, "test setup: one-shot not consumed"
    assert stored["state"] == "completed"
    return job["id"]


def test_schedule_edit_resets_consumed_budget(temp_home):
    from cron.jobs import get_job, update_job

    jid = _consumed_oneshot()
    future = (datetime.now().astimezone() + timedelta(days=1)).replace(
        microsecond=0
    ).isoformat()

    # Mirror what the edit callers do (hermes cron edit / cronjob update):
    # re-enable alongside the new schedule.
    updated = update_job(
        jid,
        {"schedule": future, "enabled": True, "state": "scheduled"},
    )

    assert updated is not None
    assert updated["enabled"] is True
    assert updated["state"] == "scheduled"
    # The budget must be fresh — otherwise the due-scan's stale-entry
    # guard deletes the record instead of firing it (#93524).
    assert updated["repeat"]["completed"] == 0
    assert updated["next_run_at"] is not None

    persisted = get_job(jid)
    assert persisted["repeat"]["completed"] == 0


def test_trigger_resets_consumed_budget(temp_home):
    from cron.jobs import get_job, trigger_job

    jid = _consumed_oneshot()

    triggered = trigger_job(jid)

    assert triggered is not None
    assert triggered["repeat"]["completed"] == 0
    assert triggered["next_run_at"] is not None
    persisted = get_job(jid)
    assert persisted["repeat"]["completed"] == 0


def test_edit_of_unconsumed_one_shot_keeps_zero_budget(temp_home):
    """A not-yet-consumed one-shot edit must stay at completed=0."""
    from cron.jobs import create_job, update_job

    job = create_job(
        prompt="x",
        schedule=(datetime.now().astimezone() + timedelta(hours=2)).isoformat(),
        name="fresh",
        repeat=1,
        deliver="local",
    )
    future = (datetime.now().astimezone() + timedelta(days=1)).replace(
        microsecond=0
    ).isoformat()

    updated = update_job(job["id"], {"schedule": future})

    assert updated["repeat"]["completed"] == 0
    assert updated["state"] == "scheduled"


def test_recurring_job_edit_is_not_disturbed(temp_home):
    """The budget reset must apply only to consumed finite one-shots."""
    from cron.jobs import create_job, load_jobs, save_jobs, update_job

    job = create_job(prompt="x", schedule="every 60m", name="rec", repeat=10)
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["repeat"]["completed"] = 3
    save_jobs(jobs)

    updated = update_job(job["id"], {"schedule": "every 90m"})

    assert updated["repeat"]["completed"] == 3, (
        "recurring jobs track their own progress; edits must not reset it"
    )


def test_edited_consumed_oneshot_passes_the_stale_entry_guard(temp_home):
    """End-to-end shape: after the fix, an edited consumed one-shot no
    longer satisfies the due-scan's dispatch-limit predicate when due."""
    from cron.jobs import get_job, update_job

    jid = _consumed_oneshot()
    soon = (datetime.now().astimezone() + timedelta(seconds=30)).replace(
        microsecond=0
    ).isoformat()
    update_job(jid, {"schedule": soon})

    stored = get_job(jid)
    times = stored["repeat"]["times"]
    completed = stored["repeat"]["completed"]
    assert not (times is not None and times > 0 and completed >= times), (
        "due-scan would remove this entry instead of firing it"
    )


def test_full_cycle_edited_consumed_oneshot_fires_exactly_once(temp_home):
    """End-to-end regression for #93524: consume a one-shot via the real
    dispatch path, edit its schedule to a (near) future time, and assert the
    scheduler fires it exactly once at the new time and then RETAINS the
    record as completed — instead of silently deleting it."""
    import time

    from cron.jobs import (
        claim_dispatch,
        create_job,
        get_due_jobs,
        load_jobs,
        mark_job_run,
        update_job,
    )

    job = create_job(
        prompt="write the report",
        schedule=(datetime.now().astimezone() + timedelta(seconds=2)).isoformat(),
        name="report-e2e",
        repeat=1,
        deliver="local",
    )
    jid = job["id"]
    # Real consumption path: claim the dispatch, then record the run.
    assert claim_dispatch(jid)
    mark_job_run(jid, success=True, status="ok")
    consumed = next(j for j in load_jobs() if j["id"] == jid)
    assert consumed["repeat"]["completed"] >= 1
    assert consumed.get("last_run_at") is not None

    # Edit the schedule to a near-future time (what "move my report to
    # tomorrow" does, compressed for the test) and re-enable.
    new_time = (datetime.now().astimezone() + timedelta(seconds=1)).isoformat()
    updated = update_job(
        jid, {"schedule": new_time, "enabled": True, "state": "scheduled"}
    )
    assert updated is not None
    assert updated["repeat"]["completed"] == 0, "budget must be fresh after edit"

    time.sleep(1.5)
    due = get_due_jobs()
    due_ids = [d["id"] for d in due]
    assert due_ids.count(jid) == 1, (
        f"edited one-shot must be dispatched exactly once, got due={due_ids} "
        f"(pre-fix behavior: the due-scan guard deleted it instead)"
    )
    # Record must survive the scan (get_due_jobs claims it, doesn't delete it).
    assert any(j["id"] == jid for j in load_jobs())

    # Complete the fire; the record is retained as a terminal completed
    # one-shot — not deleted.
    mark_job_run(jid, success=True, status="ok")
    final = next((j for j in load_jobs() if j["id"] == jid), None)
    assert final is not None, "completed one-shot record must be retained"
    assert final["state"] == "completed"
    assert final["repeat"]["completed"] >= 1

    # And it must not fire a second time.
    assert jid not in [d["id"] for d in get_due_jobs()]


def test_due_scan_guard_warns_on_record_with_last_run_at(temp_home, caplog):
    """If a consumed record with last_run_at somehow reaches the due-scan
    guard (pre-fix stores, hand edits), removal must log at WARNING with a
    diagnostic — never a silent INFO delete (#93524)."""
    import logging
    import time

    from cron.jobs import (
        claim_dispatch,
        create_job,
        get_due_jobs,
        load_jobs,
        mark_job_run,
        save_jobs,
    )

    job = create_job(
        prompt="x",
        schedule=(datetime.now().astimezone() + timedelta(seconds=1)).isoformat(),
        name="warn-guard",
        repeat=1,
        deliver="local",
    )
    jid = job["id"]
    assert claim_dispatch(jid)
    mark_job_run(jid, success=True, status="ok")

    # Simulate a pre-fix re-arm: enabled+scheduled+due but budget still spent.
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == jid:
            j["enabled"] = True
            j["state"] = "scheduled"
            j["next_run_at"] = (
                datetime.now().astimezone() - timedelta(seconds=1)
            ).isoformat()
    save_jobs(jobs)
    time.sleep(0.1)

    with caplog.at_level(logging.INFO, logger="cron.jobs"):
        due = get_due_jobs()

    assert jid not in [d["id"] for d in due]
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "dispatch limit" in r.getMessage()
    ]
    assert warnings, "removal of a record with last_run_at must log at WARNING"
    assert "93524" in warnings[0].getMessage()
