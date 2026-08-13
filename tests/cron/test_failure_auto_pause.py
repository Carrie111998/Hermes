"""Tests for the consecutive-failure auto-pause on recurring cron jobs.

A recurring job that fails ``failure_limit`` times in a row (per-job field >
``HERMES_CRON_FAILURE_LIMIT`` env > default 3) is paused by ``mark_job_run``
instead of firing and alerting on every subsequent tick forever. Resume and
manual trigger reset the streak; success resets the streak; one-shots are
exempt (they reach a terminal state on their own).
"""

import pytest

from cron.jobs import (
    create_job,
    get_job,
    get_due_jobs,
    mark_job_run,
    resume_job,
    trigger_job,
    update_job,
    failure_would_pause,
)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _recurring_job(**kwargs):
    return create_job(prompt="Watch the thing", schedule="every 1h", **kwargs)


class TestFailureStreak:
    def test_failure_increments_and_success_resets(self, tmp_cron_dir):
        job = _recurring_job()
        mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["consecutive_failures"] == 1

        mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["consecutive_failures"] == 2

        mark_job_run(job["id"], success=True)
        assert get_job(job["id"])["consecutive_failures"] == 0

    def test_missing_key_reads_as_zero(self, tmp_cron_dir):
        """Records written before the field existed must not crash or pause early."""
        job = _recurring_job()
        update_job(job["id"], {"consecutive_failures": None})
        mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["consecutive_failures"] == 1


class TestAutoPause:
    def test_recurring_job_pauses_at_default_limit(self, tmp_cron_dir):
        job = _recurring_job()
        for _ in range(2):
            mark_job_run(job["id"], success=False, error="boom")
        stored = get_job(job["id"])
        assert stored["state"] == "scheduled"
        assert stored["enabled"] is True

        mark_job_run(job["id"], success=False, error="boom")
        stored = get_job(job["id"])
        assert stored["state"] == "paused"
        assert stored["enabled"] is False
        assert "auto-paused after 3 consecutive failures" in stored["paused_reason"]
        assert stored["paused_at"] is not None

    def test_paused_job_is_not_due(self, tmp_cron_dir):
        job = _recurring_job()
        for _ in range(3):
            mark_job_run(job["id"], success=False, error="boom")
        due_ids = [j["id"] for j in get_due_jobs()]
        assert job["id"] not in due_ids

    def test_success_between_failures_prevents_pause(self, tmp_cron_dir):
        job = _recurring_job()
        for _ in range(2):
            mark_job_run(job["id"], success=False, error="boom")
        mark_job_run(job["id"], success=True)
        for _ in range(2):
            mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["state"] == "scheduled"

    def test_per_job_limit_overrides_default(self, tmp_cron_dir):
        job = _recurring_job()
        update_job(job["id"], {"failure_limit": 1})
        mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["state"] == "paused"

    def test_env_limit_overrides_default(self, tmp_cron_dir, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_LIMIT", "5")
        job = _recurring_job()
        for _ in range(4):
            mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["state"] == "scheduled"
        mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["state"] == "paused"

    def test_zero_limit_disables_auto_pause(self, tmp_cron_dir, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_LIMIT", "0")
        job = _recurring_job()
        for _ in range(6):
            mark_job_run(job["id"], success=False, error="boom")
        stored = get_job(job["id"])
        assert stored["state"] == "scheduled"
        assert stored["consecutive_failures"] == 6

    def test_manual_pause_reason_is_not_overwritten(self, tmp_cron_dir):
        """A failure recorded against an already-paused job keeps the manual pause."""
        from cron.jobs import pause_job

        job = _recurring_job()
        pause_job(job["id"], reason="operator hold")
        mark_job_run(job["id"], success=False, error="boom")
        stored = get_job(job["id"])
        assert stored["state"] == "paused"
        assert stored["paused_reason"] == "operator hold"


class TestOneShotExemption:
    def test_one_shot_never_reports_pending_pause(self, tmp_cron_dir):
        job = create_job(prompt="Once", schedule="30m")
        update_job(job["id"], {"consecutive_failures": 99})
        assert failure_would_pause(get_job(job["id"])) is False

    def test_failed_one_shot_ends_completed_not_paused(self, tmp_cron_dir):
        job = create_job(prompt="Once", schedule="30m")
        mark_job_run(job["id"], success=False, error="boom")
        stored = get_job(job["id"])
        assert stored["state"] == "completed"
        assert stored["enabled"] is False


class TestStreakReset:
    def test_resume_resets_streak(self, tmp_cron_dir):
        job = _recurring_job()
        for _ in range(3):
            mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["state"] == "paused"

        resumed = resume_job(job["id"])
        assert resumed["state"] == "scheduled"
        assert resumed["consecutive_failures"] == 0
        # Two more failures must not immediately re-pause (limit is 3).
        for _ in range(2):
            mark_job_run(job["id"], success=False, error="boom")
        assert get_job(job["id"])["state"] == "scheduled"

    def test_trigger_resets_streak(self, tmp_cron_dir):
        job = _recurring_job()
        for _ in range(2):
            mark_job_run(job["id"], success=False, error="boom")
        triggered = trigger_job(job["id"])
        assert triggered["consecutive_failures"] == 0


class TestFailureWouldPause:
    def test_true_only_on_the_tripping_failure(self, tmp_cron_dir):
        job = _recurring_job()
        assert failure_would_pause(get_job(job["id"])) is False
        for _ in range(2):
            mark_job_run(job["id"], success=False, error="boom")
        assert failure_would_pause(get_job(job["id"])) is True

    def test_false_when_disabled(self, tmp_cron_dir, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_FAILURE_LIMIT", "0")
        job = _recurring_job()
        update_job(job["id"], {"consecutive_failures": 10})
        assert failure_would_pause(get_job(job["id"])) is False
