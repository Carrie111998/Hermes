import json
from unittest.mock import patch

import pytest

from cron.jobs import (
    create_job,
    get_job,
    load_jobs,
    mark_job_delivery_pending,
    mark_job_run,
)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def test_delivery_state_is_pending_until_success_is_recorded(tmp_cron_dir) -> None:
    job = create_job(prompt="Report", schedule="every 1h")

    mark_job_delivery_pending(job["id"])
    assert get_job(job["id"])["last_delivery_status"] == "pending"

    mark_job_run(job["id"], success=True, delivery_attempted=True)
    assert get_job(job["id"])["last_delivery_status"] == "sent"


def test_delivery_failure_is_not_recorded_as_sent(tmp_cron_dir) -> None:
    job = create_job(prompt="Report", schedule="every 1h")

    mark_job_delivery_pending(job["id"])
    mark_job_run(
        job["id"],
        success=True,
        delivery_error="Telegram send failed: timed out",
        delivery_attempted=True,
    )
    updated = get_job(job["id"])

    assert updated["last_delivery_status"] == "failed"
    assert updated["last_delivery_error"] == "Telegram send failed: timed out"


def test_silent_run_records_not_requested(tmp_cron_dir) -> None:
    job = create_job(prompt="Report", schedule="every 1h")

    mark_job_run(job["id"], success=True, delivery_attempted=False)

    assert get_job(job["id"])["last_delivery_status"] == "not_requested"


def test_interruption_does_not_erase_pending_delivery_state(tmp_cron_dir) -> None:
    job = create_job(prompt="Report", schedule="every 1h")

    mark_job_delivery_pending(job["id"])
    mark_job_run(job["id"], success=False, error="gateway shutdown")

    assert get_job(job["id"])["last_delivery_status"] == "pending"


def test_interruption_before_delivery_clears_previous_terminal_state(
    tmp_cron_dir,
) -> None:
    job = create_job(prompt="Report", schedule="every 1h")
    mark_job_run(job["id"], success=True, delivery_attempted=True)
    completed = get_job(job["id"])
    assert completed is not None
    assert completed["last_delivery_status"] == "sent"

    mark_job_run(job["id"], success=False, error="gateway shutdown")

    interrupted = get_job(job["id"])
    assert interrupted is not None
    assert interrupted["last_delivery_status"] == "not_requested"


@pytest.mark.parametrize("wrapped", [True, False])
def test_legacy_delivery_error_backfills_failed_status(
    tmp_cron_dir, wrapped: bool
) -> None:
    jobs = [
        {"id": "failed", "last_delivery_error": "Telegram send failed"},
        {"id": "not-attempted", "last_delivery_error": None},
    ]
    payload = {"jobs": jobs} if wrapped else jobs
    jobs_file = tmp_cron_dir / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    jobs_file.write_text(json.dumps(payload), encoding="utf-8")

    loaded = {job["id"]: job for job in load_jobs()}

    assert loaded["failed"]["last_delivery_status"] == "failed"
    assert loaded["not-attempted"]["last_delivery_status"] == "not_requested"


def test_unconfigured_target_is_not_recorded_as_sent(tmp_cron_dir) -> None:
    job = create_job(prompt="Report", schedule="every 1h")

    mark_job_delivery_pending(job["id"])
    mark_job_run(
        job["id"],
        success=True,
        delivery_attempted=True,
        delivery_status="not_configured",
    )

    assert get_job(job["id"])["last_delivery_status"] == "not_configured"


def test_local_only_run_does_not_claim_external_delivery() -> None:
    from cron.scheduler import run_one_job

    job = {
        "id": "local-job",
        "name": "local job",
        "prompt": "report",
        "deliver": "local",
    }
    with (
        patch("cron.scheduler.claim_dispatch", return_value=True),
        patch("agent.secret_scope.set_secret_scope", return_value=None),
        patch("agent.secret_scope.build_profile_secret_scope", return_value=None),
        patch("agent.secret_scope.reset_secret_scope"),
        patch(
            "cron.scheduler.run_job",
            return_value=(True, "full output", "final response", None),
        ),
        patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"),
        patch("cron.scheduler._is_cron_silence_response", return_value=False),
        patch("cron.scheduler._deliver_result", return_value=None),
        patch("cron.scheduler.mark_job_delivery_pending") as pending,
        patch("cron.scheduler.mark_job_run") as completed,
    ):
        assert run_one_job(job) is True

    pending.assert_not_called()
    assert completed.call_args.kwargs["delivery_attempted"] is False


def test_unresolved_origin_never_enters_pending_state() -> None:
    from cron.scheduler import run_one_job

    job = {
        "id": "origin-job",
        "name": "origin job",
        "prompt": "report",
        "deliver": "origin",
    }
    with (
        patch("cron.scheduler.claim_dispatch", return_value=True),
        patch("agent.secret_scope.set_secret_scope", return_value=None),
        patch("agent.secret_scope.build_profile_secret_scope", return_value=None),
        patch("agent.secret_scope.reset_secret_scope"),
        patch(
            "cron.scheduler.run_job",
            return_value=(True, "full output", "final response", None),
        ),
        patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"),
        patch("cron.scheduler._is_cron_silence_response", return_value=False),
        patch("cron.scheduler._resolve_delivery_targets", return_value=[]),
        patch("cron.scheduler._deliver_result", return_value=None),
        patch("cron.scheduler.mark_job_delivery_pending") as pending,
        patch("cron.scheduler.mark_job_run") as completed,
    ):
        assert run_one_job(job) is True

    pending.assert_not_called()
    assert completed.call_args.kwargs["delivery_attempted"] is False
    assert completed.call_args.kwargs["delivery_status"] == "not_configured"


@pytest.mark.parametrize(
    "delivery_failure",
    ["Telegram send failed", TimeoutError("adapter timed out")],
)
def test_adapter_failure_updates_job_and_execution_outcomes(delivery_failure) -> None:
    from cron.scheduler import run_one_job

    job = {
        "id": "delivery-failure-job",
        "name": "delivery failure",
        "prompt": "report",
        "deliver": "telegram:123",
    }

    def fail_delivery(*_args, **_kwargs):
        if isinstance(delivery_failure, Exception):
            raise delivery_failure
        return delivery_failure

    with (
        patch("cron.scheduler.claim_dispatch", return_value=True),
        patch("agent.secret_scope.set_secret_scope", return_value=None),
        patch("agent.secret_scope.build_profile_secret_scope", return_value=None),
        patch("agent.secret_scope.reset_secret_scope"),
        patch(
            "cron.scheduler.run_job",
            return_value=(True, "full output", "final response", None),
        ),
        patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"),
        patch("cron.scheduler._is_cron_silence_response", return_value=False),
        patch("cron.scheduler._deliver_result", side_effect=fail_delivery),
        patch("cron.scheduler.mark_job_delivery_pending") as pending,
        patch("cron.scheduler.mark_job_run") as completed,
        patch("cron.scheduler.finish_execution") as finish_execution,
    ):
        assert run_one_job(job) is True

    pending.assert_called_once_with(job["id"])
    assert completed.call_args.kwargs["delivery_attempted"] is True
    assert completed.call_args.kwargs["delivery_error"] == str(delivery_failure)
    assert finish_execution.call_args.kwargs["delivery_outcome"] == "failed"
