"""Security contract for strict unattended cron jobs."""

import pytest


def test_unknown_policy_id_fails_closed():
    from cron.policy import CronPolicyError, validate_job_policy

    with pytest.raises(CronPolicyError, match="unknown cron policy"):
        validate_job_policy({"policy_id": "not-registered"})


def _strict_policy_job():
    from cron.policy import STRICT_UNATTENDED_POLICY_ID

    return {
        "policy_id": STRICT_UNATTENDED_POLICY_ID,
        "strict_toolsets": True,
        "no_mcp": True,
        "no_fallback": True,
        "created_paused": True,
        "enabled_toolsets": ["safe"],
        "provider": "openrouter",
        "model": "example/model",
        "no_agent": False,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("strict_toolsets", False, "strict_toolsets=true"),
        ("no_mcp", False, "no_mcp=true"),
        ("no_fallback", False, "no_fallback=true"),
        ("created_paused", False, "created_paused=true"),
        ("enabled_toolsets", None, "explicit enabled_toolsets list"),
        ("enabled_toolsets", ["memory"], "persistent memory toolset"),
        ("enabled_toolsets", ["coding"], "persistent memory toolset"),
        ("enabled_toolsets", ["not-registered"], "unknown strict toolset"),
        ("provider", "", "non-empty provider pin"),
        ("model", None, "non-empty model pin"),
        ("no_agent", True, "agent-backed job"),
    ],
)
def test_strict_policy_requires_every_isolation_control_and_pin(field, value, message):
    from cron.policy import CronPolicyError, validate_job_policy

    job = _strict_policy_job()
    job[field] = value
    with pytest.raises(CronPolicyError, match=message):
        validate_job_policy(job)


def test_valid_strict_policy_is_accepted():
    from cron.policy import validate_job_policy

    validate_job_policy(_strict_policy_job())


def _operator_capability():
    from cron.policy import cron_operator_capability

    return cron_operator_capability()


def _resume_policy_job(jobs_mod, job_id):
    return jobs_mod.resume_job(job_id, _operator_capability=_operator_capability())


def _update_policy_job(jobs_mod, job_id, updates):
    return jobs_mod.update_job(
        job_id, updates, _operator_capability=_operator_capability()
    )


def _point_store(jobs_mod, tmp_path, monkeypatch):
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", cron_dir / "output")


def _create_strict_policy_job(jobs_mod, *, operator_capability=None):
    from cron.policy import (
        STRICT_UNATTENDED_POLICY_ID,
        cron_operator_capability,
    )

    if operator_capability is None:
        operator_capability = cron_operator_capability()
    return jobs_mod.create_job(
        prompt="review bounded evidence",
        schedule="every hour",
        model="example/model",
        provider="openrouter",
        enabled_toolsets=["safe"],
        policy_id=STRICT_UNATTENDED_POLICY_ID,
        strict_toolsets=True,
        no_mcp=True,
        no_fallback=True,
        start_paused=True,
        _operator_capability=operator_capability,
    )


def test_core_rejects_policy_creation_without_operator_capability(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="trusted operator"):
        jobs_mod.create_job(
            prompt="review bounded evidence",
            schedule="every hour",
            model="example/model",
            provider="openrouter",
            enabled_toolsets=[],
            policy_id="strict-unattended-v1",
            strict_toolsets=True,
            no_mcp=True,
            no_fallback=True,
            start_paused=True,
        )
    assert jobs_mod.load_jobs() == []


def test_core_rejects_policy_lifecycle_without_operator_capability(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs_mod
    from cron.policy import cron_operator_capability

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(
        jobs_mod, operator_capability=cron_operator_capability()
    )

    with pytest.raises(ValueError, match="trusted operator"):
        jobs_mod.update_job(job["id"], {"prompt": "untrusted change"})
    with pytest.raises(ValueError, match="trusted operator"):
        jobs_mod.resume_job(job["id"])
    with pytest.raises(ValueError, match="trusted operator"):
        jobs_mod.trigger_job(job["id"])
    with pytest.raises(ValueError, match="trusted operator"):
        jobs_mod.remove_job(job["id"])

    paused = jobs_mod.pause_job(job["id"], reason="containment")
    assert paused["state"] == "paused"
    assert jobs_mod.get_job(job["id"])["prompt"] != "untrusted change"


def test_strict_policy_is_first_persisted_as_non_runnable(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)

    writes = []
    real_save = jobs_mod.save_jobs

    def capture_save(jobs):
        writes.append([dict(job) for job in jobs])
        real_save(jobs)

    monkeypatch.setattr(jobs_mod, "save_jobs", capture_save)

    job = _create_strict_policy_job(jobs_mod)

    assert len(writes) == 1
    first = writes[0][0]
    assert first == job
    assert first["enabled"] is False
    assert first["state"] == "paused"
    assert first["paused_at"]
    assert first["next_run_at"] is None
    assert first["created_paused"] is True


def test_policy_fields_are_visible_in_operator_readback(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod
    from tools.cronjob_tools import _format_job

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)

    formatted = _format_job(job)

    assert formatted["policy_id"] == "strict-unattended-v1"
    assert formatted["strict_toolsets"] is True
    assert formatted["no_mcp"] is True
    assert formatted["no_fallback"] is True
    assert formatted["created_paused"] is True
    assert formatted["enabled_toolsets"] == ["safe"]


@pytest.mark.parametrize(
    "field",
    [
        "policy_id",
        "strict_toolsets",
        "no_mcp",
        "no_fallback",
        "created_paused",
    ],
)
def test_policy_fields_are_immutable(field, tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)

    with pytest.raises(ValueError, match="cannot be updated"):
        jobs_mod.update_job(
            job["id"], {field: None}, _operator_capability=_operator_capability()
        )


def test_resume_revalidates_tampered_policy_before_scheduling(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod
    from cron.policy import CronPolicyError

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    records = jobs_mod.load_jobs()
    records[0]["no_mcp"] = False
    jobs_mod.save_jobs(records)

    with pytest.raises(CronPolicyError, match="no_mcp=true"):
        jobs_mod.resume_job(job["id"], _operator_capability=_operator_capability())

    stored = jobs_mod.get_job(job["id"])
    assert stored["enabled"] is False
    assert stored["state"] == "paused"
    assert stored["next_run_at"] is None


def test_valid_policy_can_be_resumed_after_paused_first_creation(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)

    resumed = _resume_policy_job(jobs_mod, job["id"])

    assert resumed["enabled"] is True
    assert resumed["state"] == "scheduled"
    assert resumed["next_run_at"]
    assert resumed["created_paused"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled_toolsets", ["safe", "terminal"]),
        ("provider", "anthropic"),
        ("model", "other/model"),
        ("base_url", "https://other.invalid/v1"),
    ],
)
def test_policy_capability_and_runtime_pins_are_immutable(
    field, value, tmp_path, monkeypatch
):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)

    with pytest.raises(ValueError, match=f"policy field {field!r} cannot be updated"):
        _update_policy_job(jobs_mod, job["id"], {field: value})


def test_update_revalidates_existing_policy_record(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod
    from cron.policy import CronPolicyError

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    records = jobs_mod.load_jobs()
    records[0]["no_fallback"] = False
    jobs_mod.save_jobs(records)

    with pytest.raises(CronPolicyError, match="no_fallback=true"):
        _update_policy_job(jobs_mod, job["id"], {"name": "must not persist"})

    assert jobs_mod.get_job(job["id"])["name"] != "must not persist"


def test_invalid_policy_fails_before_config_mcp_or_agent(monkeypatch):
    from unittest.mock import patch

    from cron.scheduler import run_job

    job = {"id": "invalid-policy", "name": "invalid", "policy_id": "unknown"}
    with (
        patch(
            "hermes_cli.config.require_parseable_user_config",
            side_effect=AssertionError("config must not be read"),
        ),
        patch(
            "tools.mcp_tool.discover_mcp_tools",
            side_effect=AssertionError("MCP must not be discovered"),
        ),
        patch(
            "run_agent.AIAgent",
            side_effect=AssertionError("agent must not be constructed"),
        ),
    ):
        success, _doc, _final, error = run_job(job)

    assert success is False
    assert "unknown cron policy" in error


@pytest.mark.parametrize("enabled_toolsets", [["memory"], ["coding"]])
def test_strict_memory_toolset_fails_before_agent_construction(
    monkeypatch, enabled_toolsets
):
    from unittest.mock import patch

    from cron.scheduler import run_job

    job = _runnable_policy_job()
    job["enabled_toolsets"] = enabled_toolsets
    with patch(
        "run_agent.AIAgent",
        side_effect=AssertionError("agent must not initialize persistent memory"),
    ):
        success, _doc, _final, error = run_job(job)

    assert success is False
    assert "persistent memory toolset" in error


def test_invalid_persisted_policy_is_auto_paused_with_diagnostic(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod
    from cron.scheduler import run_job

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    resumed = _resume_policy_job(jobs_mod, job["id"])
    records = jobs_mod.load_jobs()
    records[0]["policy_id"] = "tampered-policy"
    jobs_mod.save_jobs(records)
    notified = []
    monkeypatch.setattr(
        "cron.scheduler._notify_provider_jobs_changed",
        lambda: notified.append(True),
    )

    success, _doc, _final, error = run_job(dict(records[0]))

    assert success is False
    assert "unknown cron policy" in error
    stored = jobs_mod.get_job(resumed["id"])
    assert stored["enabled"] is False
    assert stored["state"] == "paused"
    assert stored["next_run_at"] is None
    assert "unknown cron policy" in stored["paused_reason"]
    assert notified == [True]


def test_due_scan_notifies_external_scheduler_after_policy_quarantine(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    _resume_policy_job(jobs_mod, job["id"])
    records = jobs_mod.load_jobs()
    records[0]["policy_id"] = "tampered-policy"
    records[0]["next_run_at"] = "2000-01-01T00:00:00+00:00"
    jobs_mod.save_jobs(records)

    notified = []
    monkeypatch.setattr(
        "cron.scheduler._notify_provider_jobs_changed",
        lambda: notified.append(True),
    )

    assert jobs_mod.get_due_jobs() == []
    assert notified == [True]


def test_fire_claim_notifies_external_scheduler_after_policy_quarantine(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    _resume_policy_job(jobs_mod, job["id"])
    records = jobs_mod.load_jobs()
    records[0]["no_mcp"] = False
    jobs_mod.save_jobs(records)

    notified = []
    monkeypatch.setattr(
        "cron.scheduler._notify_provider_jobs_changed",
        lambda: notified.append(True),
    )

    assert jobs_mod.claim_job_for_fire(job["id"], force=False) is False
    assert notified == [True]


def test_due_scan_quarantines_invalid_policy_record(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    _resume_policy_job(jobs_mod, job["id"])
    records = jobs_mod.load_jobs()
    records[0]["policy_id"] = "tampered-policy"
    records[0]["next_run_at"] = "2000-01-01T00:00:00+00:00"
    jobs_mod.save_jobs(records)

    assert jobs_mod.get_due_jobs() == []
    stored = jobs_mod.get_job(job["id"])
    assert stored["enabled"] is False
    assert stored["state"] == "paused"
    assert stored["next_run_at"] is None
    assert "unknown cron policy" in stored["paused_reason"]


def test_strict_no_mcp_toolsets_are_exact_even_when_global_mcp_exists(monkeypatch):
    from cron.scheduler import _resolve_cron_enabled_toolsets

    monkeypatch.setattr(
        "hermes_cli.tools_config.enabled_mcp_server_names",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("MCP config must not be read")
        ),
    )
    job = {
        "enabled_toolsets": ["safe"],
        "strict_toolsets": True,
        "no_mcp": True,
    }

    assert _resolve_cron_enabled_toolsets(job, {}) == ["safe"]


def test_strict_empty_allowlist_does_not_expand_to_platform_defaults(monkeypatch):
    from cron.scheduler import _resolve_cron_enabled_toolsets

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("platform defaults must not be read")
        ),
    )

    assert (
        _resolve_cron_enabled_toolsets(
            {"enabled_toolsets": [], "strict_toolsets": True, "no_mcp": True}, {}
        )
        == []
    )


def _runnable_policy_job():
    job = _strict_policy_job()
    job.update({
        "id": "strict-run",
        "name": "strict run",
        "prompt": "review bounded evidence",
        "schedule": {"kind": "interval", "seconds": 3600},
        "enabled": True,
        "state": "scheduled",
    })
    return job


def test_strict_run_skips_mcp_and_passes_exact_agent_controls(tmp_path):
    from unittest.mock import MagicMock, patch

    from cron.scheduler import run_job

    discover = MagicMock(side_effect=AssertionError("MCP discovery forbidden"))
    fake_db = MagicMock()
    with (
        patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
        patch("hermes_cli.config.require_parseable_user_config"),
        patch("cron.scheduler._cron_preflight_enabled", return_value=False),
        patch("cron.scheduler._guard_job_credential_exfil"),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("cron.scheduler.get_fallback_chain", return_value=[{"provider": "x"}]),
        patch("tools.mcp_tool.discover_mcp_tools", discover),
        patch("hermes_state.get_shared_session_db", return_value=fake_db),
        patch("run_agent.AIAgent") as agent_cls,
    ):
        agent_cls.return_value.run_conversation.return_value = {"final_response": "ok"}
        success, _doc, final, error = run_job(_runnable_policy_job())

    assert success is True
    assert final == "ok"
    assert error is None
    discover.assert_not_called()
    kwargs = agent_cls.call_args.kwargs
    assert kwargs["enabled_toolsets"] == ["safe"]
    assert kwargs["fallback_model"] is None
    assert kwargs["skip_tool_search_assembly"] is True
    assert kwargs["exclude_mcp_tools"] is True
    assert kwargs["skip_memory"] is True
    assert agent_cls.return_value._skip_mcp_refresh is True
    assert agent_cls.return_value._exclude_mcp_tools is True
    assert agent_cls.return_value._skip_tool_search_assembly is True


def test_strict_refresh_reuses_initial_tool_isolation(monkeypatch):
    import types

    import model_tools
    from tools.mcp_tool import refresh_agent_mcp_tools

    def tool(name):
        return {
            "type": "function",
            "function": {"name": name, "description": "", "parameters": {}},
        }

    calls = []

    def definitions(**kwargs):
        calls.append(kwargs)
        if kwargs.get("exclude_mcp_tools") and kwargs.get("skip_tool_search_assembly"):
            return [tool("strict_reader")]
        return [tool("strict_reader"), tool("mcp__escape__write"), tool("tool_call")]

    monkeypatch.setattr(model_tools, "get_tool_definitions", definitions)
    agent = types.SimpleNamespace(
        tools=[tool("strict_reader")],
        valid_tool_names={"strict_reader"},
        enabled_toolsets=["strict-reader"],
        disabled_toolsets=[],
        _exclude_mcp_tools=True,
        _skip_tool_search_assembly=True,
        _tool_snapshot_generation=-1,
    )

    assert refresh_agent_mcp_tools(agent) == set()
    assert calls[-1]["exclude_mcp_tools"] is True
    assert calls[-1]["skip_tool_search_assembly"] is True
    assert agent.valid_tool_names == {"strict_reader"}


def test_strict_tool_search_scope_excludes_mcp_rebuild(monkeypatch):
    import types

    import model_tools
    from agent.tool_executor import _tool_search_scoped_names

    captured = {}

    def definitions(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(model_tools, "get_tool_definitions", definitions)
    agent = types.SimpleNamespace(
        enabled_toolsets=["strict-reader"],
        disabled_toolsets=[],
        _exclude_mcp_tools=True,
        _skip_tool_search_assembly=True,
    )

    assert _tool_search_scoped_names(agent) == frozenset()
    assert captured["exclude_mcp_tools"] is True
    assert captured["skip_tool_search_assembly"] is True


def test_aiagent_forwards_raw_tool_schema_mode():
    from unittest.mock import patch

    from run_agent import AIAgent

    with patch("agent.agent_init.init_agent") as init:
        AIAgent(
            model="example/model",
            skip_tool_search_assembly=True,
            exclude_mcp_tools=True,
        )

    assert init.call_args.kwargs["skip_tool_search_assembly"] is True
    assert init.call_args.kwargs["exclude_mcp_tools"] is True


def test_aiagent_preserves_historical_positional_save_trajectories_slot():
    from unittest.mock import patch

    from run_agent import AIAgent

    with patch("agent.agent_init.init_agent") as init:
        AIAgent(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "example/model",
            5,
            None,
            ["safe"],
            [],
            True,
        )

    kwargs = init.call_args.kwargs
    assert kwargs["save_trajectories"] is True
    assert kwargs["skip_tool_search_assembly"] is False
    assert kwargs["exclude_mcp_tools"] is False


def test_cronjob_preserves_historical_positional_task_and_session_tail(monkeypatch):
    import json

    from tools.cronjob_tools import cronjob

    monkeypatch.setattr("tools.cronjob_tools.list_jobs", lambda **_kwargs: [])
    historical_args = (
        ["list"]
        + [None] * 23
        + [
            "legacy-task-id",
            "legacy-session-id",
        ]
    )

    result = json.loads(cronjob(*historical_args))

    assert result["success"] is True
    assert result["jobs"] == []


def test_no_fallback_stops_after_primary_resolution_failure(tmp_path):
    from unittest.mock import MagicMock, patch

    from cron.scheduler import run_job
    from hermes_cli.auth import AuthError

    fallback = MagicMock(return_value=[{"provider": "anthropic", "model": "other"}])
    with (
        patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
        patch("hermes_cli.config.require_parseable_user_config"),
        patch("cron.scheduler._cron_preflight_enabled", return_value=False),
        patch("cron.scheduler._guard_job_credential_exfil"),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=AuthError("primary unavailable"),
        ),
        patch("cron.scheduler.get_fallback_chain", fallback),
        patch(
            "tools.mcp_tool.discover_mcp_tools",
            side_effect=AssertionError("MCP discovery forbidden"),
        ),
        patch(
            "run_agent.AIAgent",
            side_effect=AssertionError("agent must not be constructed"),
        ),
    ):
        success, _doc, _final, error = run_job(_runnable_policy_job())

    assert success is False
    assert "primary unavailable" in error
    fallback.assert_not_called()


def _build_cron_parser():
    import argparse

    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=lambda _args: 0)
    return parser


def test_strict_policy_cli_flags_are_creation_only():
    parser = _build_cron_parser()
    args = parser.parse_args([
        "cron",
        "create",
        "every hour",
        "review",
        "--policy-id",
        "strict-unattended-v1",
        "--enabled-toolset",
        "safe",
        "--enabled-toolset",
        "web",
        "--strict-toolsets",
        "--no-mcp",
        "--no-fallback",
        "--start-paused",
    ])

    assert args.policy_id == "strict-unattended-v1"
    assert args.enabled_toolsets == ["safe", "web"]
    assert args.strict_toolsets is True
    assert args.no_mcp is True
    assert args.no_fallback is True
    assert args.start_paused is True

    with pytest.raises(SystemExit):
        parser.parse_args(["cron", "edit", "job", "--policy-id", "x"])


def test_cron_create_forwards_strict_policy_flags(monkeypatch):
    from hermes_cli import cron as cron_cli

    args = _build_cron_parser().parse_args([
        "cron",
        "create",
        "every hour",
        "review",
        "--model",
        "example/model",
        "--provider",
        "openrouter",
        "--policy-id",
        "strict-unattended-v1",
        "--enabled-toolset",
        "safe",
        "--strict-toolsets",
        "--no-mcp",
        "--no-fallback",
        "--start-paused",
    ])
    captured = {}

    def fake_api(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "job_id": "job",
            "name": "strict",
            "schedule": "every hour",
            "next_run_at": None,
            "job": {},
        }

    monkeypatch.setattr(cron_cli, "_cron_api", fake_api)
    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)

    assert cron_cli.cron_create(args) == 0
    assert captured["policy_id"] == "strict-unattended-v1"
    assert captured["enabled_toolsets"] == ["safe"]
    assert captured["strict_toolsets"] is True
    assert captured["no_mcp"] is True
    assert captured["no_fallback"] is True
    assert captured["start_paused"] is True


def test_operator_only_cron_api_forwards_policy_fields(monkeypatch):
    import json
    from unittest.mock import patch

    from cron.policy import cron_operator_capability
    from tools.cronjob_tools import cronjob

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {
            "id": "job",
            "name": "strict",
            "prompt": "review",
            "skills": [],
            "schedule_display": "every hour",
            "repeat": {"times": None, "completed": 0},
            "deliver": "local",
            "next_run_at": None,
            "enabled": False,
            "state": "paused",
        }

    with patch(
        "cron.scheduler.create_job_with_scheduler_registration",
        side_effect=fake_create,
    ):
        result = json.loads(
            cronjob(
                action="create",
                schedule="every hour",
                prompt="review",
                model="example/model",
                provider="openrouter",
                policy_id="strict-unattended-v1",
                enabled_toolsets=["safe"],
                strict_toolsets=True,
                no_mcp=True,
                no_fallback=True,
                start_paused=True,
                _operator_capability=cron_operator_capability(),
            )
        )

    assert result["success"] is True
    assert captured["policy_id"] == "strict-unattended-v1"
    assert captured["enabled_toolsets"] == ["safe"]
    assert captured["strict_toolsets"] is True
    assert captured["no_mcp"] is True
    assert captured["no_fallback"] is True
    assert captured["start_paused"] is True


def test_operator_create_preserves_empty_strict_allowlist():
    import json
    from unittest.mock import patch

    from cron.policy import cron_operator_capability
    from tools.cronjob_tools import cronjob

    job = {
        "id": "job",
        "name": "strict",
        "prompt": "review",
        "skills": [],
        "schedule_display": "every hour",
        "repeat": {"times": None, "completed": 0},
        "deliver": "local",
        "next_run_at": None,
        "enabled": False,
        "state": "paused",
    }
    with patch(
        "cron.scheduler.create_job_with_scheduler_registration", return_value=job
    ) as create:
        result = json.loads(
            cronjob(
                action="create",
                schedule="every hour",
                prompt="review",
                model="example/model",
                provider="openrouter",
                policy_id="strict-unattended-v1",
                enabled_toolsets=[],
                strict_toolsets=True,
                no_mcp=True,
                no_fallback=True,
                start_paused=True,
                _operator_capability=cron_operator_capability(),
            )
        )

    assert result["success"] is True
    assert create.call_args.kwargs["enabled_toolsets"] == []


def test_model_tool_cannot_create_registered_policy_job():
    import json

    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(
            action="create",
            schedule="every hour",
            prompt="review",
            policy_id="strict-unattended-v1",
        )
    )

    assert result["success"] is False
    assert "trusted operator" in result["error"]


def test_cli_cron_api_marks_direct_operator_calls_trusted(monkeypatch):
    import json

    from cron.policy import is_trusted_cron_operator
    from hermes_cli import cron as cron_cli

    captured = {}

    def fake_cronjob(**kwargs):
        capability = kwargs.pop("_operator_capability", None)
        captured.update(kwargs)
        captured["trusted_scope"] = is_trusted_cron_operator(capability)
        return json.dumps({"success": True})

    monkeypatch.setattr("tools.cronjob_tools.cronjob", fake_cronjob)

    assert cron_cli._cron_api(action="list")["success"] is True
    assert captured["trusted_scope"] is True
    assert "trusted_operator" not in captured


def test_model_cannot_forge_trusted_operator_keyword():
    import json

    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(
            action="create",
            schedule="every hour",
            prompt="review",
            policy_id="strict-unattended-v1",
            _operator_capability=True,
        )
    )

    assert result["success"] is False
    assert "trusted operator" in result["error"]


@pytest.mark.parametrize(
    "action", ["update", "resume", "run", "run_now", "trigger", "remove"]
)
def test_model_tool_cannot_mutate_or_activate_policy_job(action, tmp_path, monkeypatch):
    import json

    import cron.jobs as jobs_mod
    from tools.cronjob_tools import cronjob

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)

    result = json.loads(
        cronjob(
            action=action,
            job_id=job["id"],
            prompt="changed" if action == "update" else None,
        )
    )

    assert result["success"] is False
    assert "trusted operator" in result["error"]
    assert jobs_mod.get_job(job["id"])["state"] == "paused"


def test_cli_rearm_reconciles_external_scheduler(monkeypatch):
    from types import SimpleNamespace

    import cron.jobs as jobs_mod
    import cron.scheduler as scheduler_mod
    from hermes_cli.cron import cron_resume

    monkeypatch.setattr(
        jobs_mod,
        "rearm_oneshot",
        lambda *args, **kwargs: {
            "id": "protected-job",
            "name": "protected",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        },
    )
    notified = []
    monkeypatch.setattr(
        scheduler_mod,
        "_notify_provider_jobs_changed",
        lambda: notified.append(True),
    )

    assert (
        cron_resume(
            SimpleNamespace(
                job_id="protected-job",
                run_at="2030-01-01T00:00:00+00:00",
                run_now=False,
            )
        )
        == 0
    )
    assert notified == [True]


def test_paused_first_job_is_not_registered_with_external_scheduler(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs_mod
    from cron.policy import STRICT_UNATTENDED_POLICY_ID
    from cron.scheduler import create_job_with_scheduler_registration

    _point_store(jobs_mod, tmp_path, monkeypatch)

    class Provider:
        def register_job(self, _job):
            raise AssertionError("paused job must not be externally registered")

    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler", lambda: Provider()
    )

    job = create_job_with_scheduler_registration(
        prompt="review",
        schedule="every hour",
        model="example/model",
        provider="openrouter",
        enabled_toolsets=["safe"],
        policy_id=STRICT_UNATTENDED_POLICY_ID,
        strict_toolsets=True,
        no_mcp=True,
        no_fallback=True,
        start_paused=True,
        _operator_capability=_operator_capability(),
    )

    assert job["enabled"] is False
    assert job["next_run_at"] is None


def test_paused_policy_job_rejects_stale_due_time_and_nonforced_claim(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    records = jobs_mod.load_jobs()
    records[0]["next_run_at"] = "2000-01-01T00:00:00+00:00"
    jobs_mod.save_jobs(records)
    before = jobs_mod.get_job(job["id"])

    assert all(item["id"] != job["id"] for item in jobs_mod.get_due_jobs())
    assert jobs_mod.claim_job_for_fire(job["id"], force=False) is False
    assert jobs_mod.get_job(job["id"]) == before


def test_forced_fire_requires_operator_capability_after_resume(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    resumed = _resume_policy_job(jobs_mod, job["id"])
    assert resumed["enabled"] is True

    with pytest.raises(ValueError, match="trusted operator"):
        jobs_mod.claim_job_for_fire(job["id"], force=True)
    claimed = jobs_mod.claim_job_for_fire(
        job["id"],
        force=True,
        _operator_capability=_operator_capability(),
        return_job=True,
    )
    assert isinstance(claimed, dict)


def test_provider_force_fire_requires_capability_for_resumed_policy_job(
    tmp_path, monkeypatch
):
    import cron.executions as executions
    import cron.jobs as jobs_mod
    from cron.scheduler_provider import InProcessCronScheduler

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    _resume_policy_job(jobs_mod, job["id"])
    monkeypatch.setattr(
        executions,
        "create_execution",
        lambda _job_id, source: {"id": f"execution-{source}"},
    )
    monkeypatch.setattr(executions, "finish_execution", lambda *_args, **_kwargs: None)
    provider = InProcessCronScheduler()
    monkeypatch.setattr(provider, "fire_claimed", lambda *_args, **_kwargs: True)

    with pytest.raises(ValueError, match="trusted operator"):
        provider.fire_due(job["id"], force=True)

    assert (
        provider.fire_due(
            job["id"],
            force=True,
            _operator_capability=_operator_capability(),
        )
        is True
    )


def test_forced_fire_cannot_reactivate_valid_paused_policy(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    claimed = jobs_mod.claim_job_for_fire(
        job["id"], force=True, _operator_capability=_operator_capability()
    )
    assert claimed is False
    stored = jobs_mod.get_job(job["id"])
    assert stored["enabled"] is False
    assert stored["state"] == "paused"


def test_core_rearm_requires_operator_capability(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = jobs_mod.create_job(
        prompt="review bounded evidence",
        schedule="in 1h",
        model="example/model",
        provider="openrouter",
        enabled_toolsets=[],
        policy_id="strict-unattended-v1",
        strict_toolsets=True,
        no_mcp=True,
        no_fallback=True,
        start_paused=True,
        _operator_capability=_operator_capability(),
    )

    with pytest.raises(ValueError, match="trusted operator"):
        jobs_mod.rearm_oneshot(job["id"], "in 2h")


def test_tampered_policy_job_is_not_due_or_claimable(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    records = jobs_mod.load_jobs()
    records[0].update({
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "next_run_at": "2000-01-01T00:00:00+00:00",
        "no_mcp": False,
    })
    jobs_mod.save_jobs(records)

    assert all(item["id"] != job["id"] for item in jobs_mod.get_due_jobs())
    assert jobs_mod.claim_job_for_fire(job["id"], force=False) is False
    stored = jobs_mod.get_job(job["id"])
    assert stored["enabled"] is False
    assert stored["state"] == "paused"
    assert stored["next_run_at"] is None
    assert "no_mcp=true" in stored["paused_reason"]


def test_fire_claim_quarantines_invalid_policy_without_due_scan(tmp_path, monkeypatch):
    import cron.jobs as jobs_mod

    _point_store(jobs_mod, tmp_path, monkeypatch)
    job = _create_strict_policy_job(jobs_mod)
    _resume_policy_job(jobs_mod, job["id"])
    records = jobs_mod.load_jobs()
    records[0]["no_fallback"] = False
    jobs_mod.save_jobs(records)

    assert jobs_mod.claim_job_for_fire(job["id"], force=True) is False
    stored = jobs_mod.get_job(job["id"])
    assert stored["enabled"] is False
    assert stored["state"] == "paused"
    assert stored["next_run_at"] is None
    assert "no_fallback=true" in stored["paused_reason"]


def test_raw_strict_tool_schema_excludes_previously_registered_mcp_tool():
    import model_tools
    from tools.registry import registry

    mcp_name = "mcp__collision_probe__read"
    direct_name = "strict_collision_probe_read"
    mcp_check_calls = []
    registry.register(
        name=mcp_name,
        toolset="safe",
        schema={
            "name": mcp_name,
            "description": "test MCP tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args=None: "ok",
        check_fn=lambda: mcp_check_calls.append(True) or True,
    )
    registry.register(
        name=direct_name,
        toolset="safe",
        schema={
            "name": direct_name,
            "description": "test direct tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args=None: "ok",
    )
    model_tools._clear_tool_defs_cache()
    try:
        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=["safe"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
            exclude_mcp_tools=True,
        )
    finally:
        registry.deregister(mcp_name)
        registry.deregister(direct_name)
        model_tools._clear_tool_defs_cache()

    names = {item["function"]["name"] for item in definitions}
    assert mcp_check_calls == []
    assert direct_name in names
    assert mcp_name not in names
    assert not any(tool_name.startswith("mcp__") for tool_name in names)
    assert {"tool_search", "tool_describe", "tool_call"}.isdisjoint(names)
