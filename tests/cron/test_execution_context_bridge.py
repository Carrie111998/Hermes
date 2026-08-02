"""Cron execution lineage must reach job-owned subprocesses without global-env leaks."""

import pytest

import cron.scheduler as scheduler
from tools.environments.local import _sanitize_subprocess_env


EXECUTION_ENV = {
    "HERMES_CRON_EXECUTION_ID": "execution-123",
    "HERMES_CRON_JOB_ID": "job-456",
    "HERMES_CRON_EXECUTION_SOURCE": "builtin",
    "HERMES_CRON_SCHEDULED_FOR": "2026-08-01T08:40:00+00:00",
    "HERMES_CRON_DELIVERY_TARGETS_JSON": (
        '[{"chat_id":"-100123","platform":"telegram","thread_id":"55"}]'
    ),
}


def _patch_completed_pipeline(monkeypatch, captured):
    def fake_run_job(job, *, defer_agent_teardown=None):
        captured.update(job)
        return True, "out", "final response", None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_: "/tmp/output.md")
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        scheduler, "finish_execution", lambda execution_id, **kwargs: {"id": execution_id},
    )
    monkeypatch.setattr(
        scheduler, "mark_execution_running", lambda execution_id: {"id": execution_id},
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *args, **kwargs: True)


def test_direct_run_carries_execution_identity_and_nominal_schedule(monkeypatch):
    captured = {}
    _patch_completed_pipeline(monkeypatch, captured)
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda job_id, *, source, scheduled_for=None: {
            "id": "execution-direct",
            "job_id": job_id,
            "source": source,
            "scheduled_for": scheduled_for,
        },
    )

    assert scheduler.run_one_job({
        "id": "job-direct",
        "name": "direct",
        "next_run_at": "2026-08-01T08:40:00+00:00",
    }) is True

    assert captured["execution_id"] == "execution-direct"
    assert captured["execution_source"] == "builtin"
    assert captured["scheduled_for"] == "2026-08-01T08:40:00+00:00"


def test_builtin_tick_preserves_claimed_execution_and_due_time(monkeypatch):
    captured = {}
    _patch_completed_pipeline(monkeypatch, captured)
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [{
        "id": "job-builtin",
        "name": "builtin",
        "next_run_at": "2026-08-01T08:40:00+00:00",
    }])
    monkeypatch.setattr(scheduler, "advance_next_runs", lambda *_: 1)
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda job_id, *, source, scheduled_for=None: {
            "id": "execution-builtin",
            "job_id": job_id,
            "source": source,
            "scheduled_for": scheduled_for,
        },
    )
    monkeypatch.setattr(
        scheduler,
        "get_execution",
        lambda execution_id: {
            "id": execution_id,
            "job_id": "job-builtin",
            "source": "builtin",
            "scheduled_for": "2026-08-01T08:40:00+00:00",
        },
    )

    assert scheduler.tick(verbose=False, sync=True) == 1
    assert captured["execution_id"] == "execution-builtin"
    assert captured["execution_source"] == "builtin"
    assert captured["scheduled_for"] == "2026-08-01T08:40:00+00:00"


def test_existing_execution_uses_durable_source_not_spoofed_job_field(monkeypatch):
    captured = {}
    _patch_completed_pipeline(monkeypatch, captured)
    monkeypatch.setattr(
        scheduler,
        "get_execution",
        lambda execution_id: {
            "id": execution_id,
            "job_id": "job-auth",
            "source": "chronos",
            "scheduled_for": "2026-08-01T09:10:00+00:00",
        },
    )

    assert scheduler.run_one_job({
        "id": "job-auth",
        "execution_id": "execution-auth",
        "execution_source": "builtin",
        "scheduled_for": "2026-08-01T09:10:00+00:00",
    }) is True
    assert captured["execution_source"] == "chronos"
    assert captured["scheduled_for"] == "2026-08-01T09:10:00+00:00"


def test_context_rejects_non_allowlisted_scheduler_source_before_installation():
    with pytest.raises(ValueError, match="scheduler source"):
        scheduler._install_cron_execution_context({
            "execution_id": "execution-auth",
            "id": "job-auth",
            "execution_source": "external",
            "scheduled_for": "2026-08-01T09:10:00+00:00",
            "_resolved_delivery_targets": [],
        })


def test_execution_context_is_bridged_to_subprocess_env_and_cleared():
    scheduler._install_cron_execution_context({
        "execution_id": EXECUTION_ENV["HERMES_CRON_EXECUTION_ID"],
        "id": EXECUTION_ENV["HERMES_CRON_JOB_ID"],
        "execution_source": EXECUTION_ENV["HERMES_CRON_EXECUTION_SOURCE"],
        "scheduled_for": EXECUTION_ENV["HERMES_CRON_SCHEDULED_FOR"],
        "deliver": "origin",
        "origin": {
            "platform": "telegram", "chat_id": "-100123", "thread_id": "55",
        },
    })
    try:
        child_env = _sanitize_subprocess_env({})
        assert {key: child_env[key] for key in EXECUTION_ENV} == EXECUTION_ENV
    finally:
        scheduler._clear_cron_execution_context()

    cleared_env = _sanitize_subprocess_env({})
    assert all(cleared_env.get(key, "") == "" for key in EXECUTION_ENV)
