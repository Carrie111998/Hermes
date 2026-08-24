"""Stale one-shot fire guard in the due scan (#93526).

ONESHOT_GRACE_SECONDS is the one-shot miss-grace — the contract already
enforced at the create/update/resume entry points and by the recovery
helper (a >grace one-shot with a MISSING next_run_at is ineligible).
But a one-shot stored WITH a past next_run_at (machine off across its
fire time) fell through the due scan's grace check (kind excluded) and
fired arbitrarily late, burning a full agent turn to deliver a stale
alert. The guard skips the fire and retires the record as missed, using
the terminal retention shape (enabled=False, state=completed,
next_run_at=None, no run-outcome writes — no run happened).
"""

from __future__ import annotations

from datetime import timedelta

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json doesn't touch the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _seed_past_due_oneshot(home, hours_past=5, name="stale") -> str:
    """Persist a one-shot whose stored next_run_at is already hours past."""
    from cron.jobs import create_job, save_jobs, load_jobs, _hermes_now

    job = create_job(prompt="reminder", schedule="2030-01-01T09:00", name=name)
    now = _hermes_now()
    stale = (now - timedelta(hours=hours_past)).isoformat()
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["next_run_at"] = stale
            j["schedule"]["run_at"] = stale.split(".")[0]
    save_jobs(jobs)
    return job["id"]


def test_past_due_oneshot_beyond_grace_not_fired(temp_home):
    """A one-shot hours past its fire time is retired as missed, not fired."""
    from cron.jobs import get_due_jobs, get_job
    from cron.jobs import ONESHOT_GRACE_SECONDS

    assert ONESHOT_GRACE_SECONDS == 120  # guard uses the one-shot contract
    jid = _seed_past_due_oneshot(temp_home)

    due = get_due_jobs()

    assert [j["id"] for j in due if j["id"] == jid] == []
    stored = get_job(jid)
    assert stored["enabled"] is False
    assert stored["state"] == "completed"
    assert stored["next_run_at"] is None
    # No run happened — the run-outcome fields must not claim one did.
    assert stored.get("last_run_at") is None
    assert stored.get("repeat", {}).get("completed", 0) == 0


def test_recently_due_oneshot_within_grace_still_fires(temp_home):
    """Control: a one-shot a few seconds past its time still fires — the
    grace exists so jobs created just after their requested minute run."""
    from cron.jobs import create_job, save_jobs, load_jobs, get_due_jobs
    from cron.jobs import _hermes_now

    job = create_job(prompt="fresh", schedule="2030-01-01T09:00", name="ok")
    now = _hermes_now()
    recent = (now - timedelta(seconds=30)).isoformat()
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["next_run_at"] = recent
            j["schedule"]["run_at"] = recent.split(".")[0]
    save_jobs(jobs)

    due = get_due_jobs()

    assert job["id"] in [j["id"] for j in due]


def test_retired_missed_oneshot_not_rediscovered_on_next_tick(temp_home):
    """The retired record cannot re-evaluate past-due on a later tick."""
    from cron.jobs import get_due_jobs, get_job

    jid = _seed_past_due_oneshot(temp_home, hours_past=5, name="once-only")
    get_due_jobs()  # first tick retires it

    due_again = get_due_jobs()  # second tick must be a no-op for it

    assert [j["id"] for j in due_again if j["id"] == jid] == []
    stored = get_job(jid)
    assert stored["enabled"] is False
    assert stored["next_run_at"] is None
