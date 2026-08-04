"""Cron pre-agent policy contract for triage-only fallbacks."""
from unittest.mock import MagicMock, patch


def test_cron_auth_resolution_does_not_promote_triage_fallback_to_full_job(tmp_path):
    from cron.scheduler import run_job
    from hermes_cli.auth import AuthError

    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: gpt-5.6-terra\n"
        "  provider: openai-codex\n"
        "fallback_providers:\n"
        "  - provider: custom\n"
        "    model: qwen3:8b\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    failure_policy: triage_and_notify\n",
        encoding="utf-8",
    )
    job = {
        "id": "triage-only-auth-fallback",
        "name": "triage-only auth fallback",
        "prompt": "perform consequential work",
        "provider_snapshot": "openai-codex",
        "model_snapshot": "gpt-5.6-terra",
    }
    requested: list[str | None] = []

    def resolver(**kwargs):
        requested.append(kwargs.get("requested"))
        raise AuthError("No Codex credentials stored")

    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=MagicMock()),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolver),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch("run_agent.AIAgent") as agent_cls,
    ):
        success, _output, _final_response, error = run_job(job)

    assert success is False
    assert error is not None
    assert requested == [None]
    agent_cls.assert_not_called()


def test_cron_auth_resolution_malformed_policy_fails_closed_before_later_fallback(tmp_path):
    from cron.scheduler import run_job
    from hermes_cli.auth import AuthError

    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: gpt-5.6-terra\n"
        "  provider: openai-codex\n"
        "fallback_providers:\n"
        "  - provider: custom\n"
        "    model: malformed-boundary\n"
        "    failure_policy: triage_and_notfiy\n"
        "  - provider: openrouter\n"
        "    model: must-not-run\n"
        "    failure_policy: continue\n",
        encoding="utf-8",
    )
    job = {
        "id": "malformed-policy-auth-fallback",
        "name": "malformed policy auth fallback",
        "prompt": "perform consequential work",
        "provider_snapshot": "openai-codex",
        "model_snapshot": "gpt-5.6-terra",
    }
    requested: list[str | None] = []

    def resolver(**kwargs):
        requested.append(kwargs.get("requested"))
        if len(requested) == 1:
            raise AuthError("No Codex credentials stored")
        return {
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": "must-not-run",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "must-not-be-used",
            "api_mode": "chat_completions",
        }

    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=MagicMock()),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolver),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch("run_agent.AIAgent") as agent_cls,
    ):
        success, _output, _final_response, error = run_job(job)

    assert success is False
    assert error is not None
    assert "invalid failure_policy" in error
    assert requested == [None]
    agent_cls.assert_not_called()


def _run_held_job_through_persistence(
    monkeypatch,
    tmp_path,
    *,
    agent_result,
    schedule,
):
    from cron import executions, jobs, scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "_hermes_home", tmp_path)
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")

    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: primary-model\n"
        "  provider: openrouter\n",
        encoding="utf-8",
    )
    runtime = {
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "fake-test-key",
        "api_mode": "chat_completions",
    }
    session_db = MagicMock()
    session_db.get_compression_tip.return_value = None
    agent = MagicMock()
    agent.run_conversation.return_value = dict(agent_result)
    agent.get_activity_summary.return_value = {"seconds_since_activity": 0.0}
    deliveries: list[str] = []
    captured_outcomes = []
    real_run_job = scheduler.run_job

    def capture_run_job(job, *, defer_agent_teardown=None):
        outcome = real_run_job(job, defer_agent_teardown=defer_agent_teardown)
        captured_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(scheduler, "run_job", capture_run_job)
    monkeypatch.setattr("hermes_cli.env_loader.load_hermes_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr("hermes_cli.env_loader.reset_secret_source_cache", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr("hermes_state.SessionDB", lambda: session_db)
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: [])
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda _provider: MagicMock(has_credentials=lambda: False),
    )
    monkeypatch.setattr("run_agent.AIAgent", lambda **_kwargs: agent)
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda _job, content, **_kwargs: deliveries.append(content) or None,
    )

    job = jobs.create_job(
        prompt="perform consequential work",
        schedule=schedule,
        name="held persistence",
        model="primary-model",
        provider="openrouter",
        deliver="local",
    )
    assert scheduler.run_one_job(job) is True
    return {
        "job": jobs.get_job(job["id"]),
        "execution": executions.latest_execution(job["id"]),
        "deliveries": deliveries,
        "outcome": captured_outcomes[0],
        "agent": agent,
    }


def test_successful_notifier_persists_recurring_work_as_held(monkeypatch, tmp_path):
    final_response = "held original work; bounded notifier succeeded"
    observed = _run_held_job_through_persistence(
        monkeypatch,
        tmp_path,
        schedule="every 1h",
        agent_result={
            "final_response": final_response,
            "completed": True,
            "failed": False,
            "held": True,
            "turn_exit_reason": "fallback_triage_notified",
        },
    )

    assert observed["outcome"].work_status == "held"
    assert observed["outcome"].watcher_succeeded is True
    legacy_success, _legacy_output, legacy_response, legacy_error = observed["outcome"]
    assert legacy_success is False
    assert legacy_response == final_response
    assert legacy_error == observed["outcome"].hold_detail
    assert observed["job"]["last_status"] == "held"
    assert observed["job"]["last_notification_status"] == "ok"
    assert observed["job"]["state"] == "scheduled"
    assert observed["job"]["enabled"] is True
    assert observed["job"]["repeat"]["completed"] == 0
    assert observed["job"]["recovery_disposition"] == "next_scheduled_occurrence_no_replay"
    assert observed["execution"]["status"] == "held"
    assert "bounded notifier succeeded" in observed["execution"]["error"]
    assert observed["deliveries"] == [final_response]
    observed["agent"].run_conversation.assert_called_once()


def test_failed_notifier_persists_deterministic_held_detail_without_continuation(
    monkeypatch,
    tmp_path,
):
    final_response = "held original work; bounded notifier failed"
    observed = _run_held_job_through_persistence(
        monkeypatch,
        tmp_path,
        schedule="every 1h",
        agent_result={
            "final_response": final_response,
            "completed": False,
            "failed": True,
            "held": True,
            "turn_exit_reason": "fallback_triage_local_failed",
        },
    )

    assert observed["outcome"].work_status == "held"
    assert observed["outcome"].watcher_succeeded is False
    assert observed["job"]["last_status"] == "held"
    assert observed["job"]["last_notification_status"] == "error"
    assert "bounded notifier failed" in observed["job"]["last_error"]
    assert "no automatic replay" in observed["job"]["last_error"]
    assert observed["job"]["state"] == "scheduled"
    assert observed["execution"]["status"] == "held"
    assert observed["execution"]["error"] == observed["job"]["last_error"]
    assert observed["deliveries"] == [final_response]
    observed["agent"].run_conversation.assert_called_once()


def test_oneshot_held_work_is_not_persisted_as_successfully_completed(monkeypatch, tmp_path):
    observed = _run_held_job_through_persistence(
        monkeypatch,
        tmp_path,
        schedule="30m",
        agent_result={
            "final_response": "held original one-shot work",
            "completed": True,
            "failed": False,
            "held": True,
            "turn_exit_reason": "fallback_triage_notified",
        },
    )

    assert observed["job"]["last_status"] == "held"
    assert observed["job"]["state"] == "held"
    assert observed["job"]["state"] != "completed"
    assert observed["job"]["enabled"] is False
    assert observed["job"]["next_run_at"] is None
    assert observed["job"]["recovery_disposition"] == "manual_recreate_required"
    assert observed["execution"]["status"] == "held"
