"""One-shot lateness must honor ONESHOT_GRACE_SECONDS on every path (#93526).

The 120s one-shot miss-grace was enforced by the create/update/resume
validators and the missing-next_run_at recovery helper — but a stored
past-due one-shot (machine off across its fire time) fired arbitrarily
late from the due scan, and the external-provider misfire backstop had no
one-shot bound at all. A 5-hour-stale reminder ran in full and delivered.

Real store against a temp ``HERMES_HOME``; the provider backstop test
drives ``fire_overdue_jobs`` with a fake provider, mirroring its own
test style.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _past_oneshot(minutes_late: int):
    """Persist a one-shot whose stored next_run_at is minutes in the past."""
    from cron.jobs import create_job, load_jobs, save_jobs

    future = (datetime.now().astimezone() + timedelta(days=3650)).replace(microsecond=0)
    job = create_job(
        prompt="stale reminder",
        schedule=future.isoformat(),
        name="late-reminder",
        deliver="local",
    )
    past = (datetime.now().astimezone() - timedelta(minutes=minutes_late)).replace(
        microsecond=0
    )
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["next_run_at"] = past.isoformat()
    save_jobs(jobs)
    return job["id"], past


class TestDueScanGraceBound:
    def test_one_shot_past_grace_is_not_fired(self, temp_home):
        from cron.jobs import get_due_jobs, get_job

        jid, _past = _past_oneshot(minutes_late=30)

        due = get_due_jobs()

        assert all(j["id"] != jid for j in due), (
            "a one-shot missed beyond ONESHOT_GRACE_SECONDS must not fire"
        )
        stored = get_job(jid)
        assert stored["enabled"] is False
        assert stored["state"] == "completed"
        assert stored["next_run_at"] is None
        assert stored.get("last_status") == "missed"

    def test_one_shot_within_grace_still_fires(self, temp_home):
        from cron.jobs import get_due_jobs

        jid, _past = _past_oneshot(minutes_late=1)  # 60s < 120s grace

        due = get_due_jobs()

        assert any(j["id"] == jid for j in due), (
            "within-grace one-shots keep firing (existing contract)"
        )


class TestProviderMisfireBackstopBound:
    def test_backstop_skips_and_retires_past_grace_one_shot(self, temp_home):
        from cron.jobs import get_job
        from cron.scheduler_provider import fire_overdue_jobs

        jid, _past = _past_oneshot(minutes_late=60)

        class _FakeProvider:
            def __init__(self):
                self.claimed = []

            def claim_fire(self, job_id, **kwargs):
                self.claimed.append(job_id)
                return {"id": job_id}

            def fire_claimed(self, *args, **kwargs):
                pass

        provider = _FakeProvider()

        fired = fire_overdue_jobs(provider)

        assert fired == 0
        assert provider.claimed == [], "one-shot must not be claimed/fired late"
        stored = get_job(jid)
        assert stored["state"] == "completed"
        assert stored.get("last_status") == "missed"

    @pytest.mark.usefixtures("temp_home")
    def test_backstop_still_fires_recurring_overdue(self):
        """Recurring misfire catch-up behavior is unchanged."""
        from cron.jobs import create_job, load_jobs, save_jobs
        from cron.scheduler_provider import fire_overdue_jobs

        create_job(prompt="x", schedule="every 60m", name="rec", deliver="local")

        class _FakeProvider:
            def claim_fire(self, job_id, **kwargs):
                return {"id": job_id}

            def fire_claimed(self, *args, **kwargs):
                pass

        # The recurring job's next_run_at is in the future; force it past.
        jobs = load_jobs()
        past = datetime.now().astimezone() - timedelta(hours=5)
        for j in jobs:
            if j["name"] == "rec":
                j["next_run_at"] = past.isoformat()
        save_jobs(jobs)

        provider = _FakeProvider()
        fired = fire_overdue_jobs(provider)

        assert fired >= 1
