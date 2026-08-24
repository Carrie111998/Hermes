"""trigger_job() must survive the stale-schedule guard (#94010).

The guard added for #93049 re-anchors any cron next_run_at that is not an
occurrence of the job's current expression. trigger_job() deliberately writes
next_run_at = "now" — an arbitrary second that is (almost surely) not an
occurrence — so every manual trigger of a recurring cron job was re-anchored
without firing, while trigger_job still returned a truthy job dict: a silent
no-op reported as success on every "run now" path (CLI `hermes cron run`,
REST POST /api/jobs/{id}/run). The fix stamps a one-shot ``fire_now`` marker
that the guard consumes to fall through to the fire path exactly once.
"""

import json
from datetime import datetime, timedelta

import pytest

import cron.jobs as jobs_mod
from cron import jobs as cron_jobs


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _reload_store():
    """Read the current store's job records via the module's path resolution."""
    doc = json.loads(cron_jobs._current_cron_store().jobs_file.read_text())
    return doc["jobs"] if isinstance(doc, dict) else doc


def _create_recurring_job(monkeypatch, expr="0 7 * * *"):
    """Create a cron job through the public create_job surface."""
    return cron_jobs.create_job(
        prompt="say hi",
        schedule=expr,
        name="recurring job",
    )


def test_trigger_job_stamps_fire_now_marker(cron_home, monkeypatch):
    job = _create_recurring_job(monkeypatch)
    result = cron_jobs.trigger_job(job["id"])
    assert result is not None
    stored = _reload_store()
    assert stored[0].get("fire_now") is True


def test_fire_now_bypasses_reanchor_and_fires(cron_home, monkeypatch):
    """The exact #94010 shape: a manual-trigger next_run_at that is not an
    expression occurrence must still surface as due (fire), not re-anchor."""
    job = _create_recurring_job(monkeypatch, expr="0 7 1 * *")
    cron_jobs.trigger_job(job["id"])

    due = cron_jobs.get_due_jobs()
    due_ids = [d.get("id") or d.get("job", {}).get("id") for d in due]
    assert job["id"] in due_ids

    stored = _reload_store()[0]
    # The marker is consumed by the fire decision — it must not linger to
    # neutralize the guard for a later genuine schedule edit.
    assert stored.get("fire_now") is None


def test_stale_schedule_without_marker_still_reanchors(cron_home, monkeypatch):
    """The #93049 protection is intact: a next_run_at from a jobs.json edit
    (no marker) still re-anchors without firing."""
    job = _create_recurring_job(monkeypatch, expr="0 7 1 * * *")
    stored = _reload_store()
    # Simulate the direct edit: a wrong-instant next_run_at in the past, no
    # fire_now marker.
    stored[0]["next_run_at"] = (
        (datetime.now() - timedelta(minutes=5)).isoformat()
    )
    cron_jobs._current_cron_store().jobs_file.write_text(json.dumps(stored))

    due = cron_jobs.get_due_jobs()
    due_ids = [d.get("id") or d.get("job", {}).get("id") for d in due]
    assert job["id"] not in due_ids
    after = _reload_store()[0]
    assert after["next_run_at"] != stored[0]["next_run_at"]
    assert not after.get("fire_now")
