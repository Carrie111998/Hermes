"""Observational cron activity telemetry.

Every test drives the real ``run_job`` seam and asserts against the real
SQLite store, so a broken wiring cannot pass by exercising a helper alone.
Telemetry here is observational only: it must never change the existing
``(success, full_output_doc, final_response, error_message)`` tuple.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
from unittest.mock import MagicMock, patch

import pytest


def _policy_declaration(aliases, execution_class="S1", quality_floor="standard"):
    deterministic = execution_class == "D0"
    return {
        "policy_version": 1,
        "owner": "fleet-tests",
        "aliases": list(aliases),
        "execution_class": execution_class,
        "quality_floor": "deterministic" if deterministic else quality_floor,
        "preferred_models": [] if deterministic else ["deepseek/deepseek-v4-pro"],
        "allowed_fallbacks": [] if deterministic else ["openai-codex/gpt-5.6-sol"],
        "reasoning": {"mode": "enabled", "effort": "medium"},
        "budgets": {
            "max_turns": 4,
            "max_model_calls": 0 if deterministic else 8,
            "max_uncached_input_tokens": 100000,
            "max_cache_read_tokens": 400000,
            "max_cache_write_tokens": 40000,
            "max_output_tokens": 8000,
            "max_reasoning_tokens": 8000,
            "max_tool_calls": 20,
            "wall_clock_seconds": 900,
            "retries": 1,
            "max_children": 0,
            "max_child_depth": 0,
            "max_recorded_provider_cost_usd": "0.50",
            "max_api_equivalent_cost_usd": "2.00",
        },
        "tools": {"allow": ["terminal"], "deny": []},
        "outcome_contract": {"required_artifacts": [], "required_validations": []},
        "escalation": {"on": ["quality_gate_failed"]},
    }


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


@pytest.fixture
def registry(monkeypatch):
    """Fixture registry — never the production YAML — for exact isolation."""
    from activity_policy.registry import ActivityRegistry
    from cron import scheduler

    built = ActivityRegistry.from_mapping({
        "enforcement": "observe",
        "activities": {
            "fleet.tests.script": _policy_declaration(["mapped-script-job"], "D0"),
            "fleet.tests.model": _policy_declaration(["mapped-model-job"]),
        },
    })
    monkeypatch.setattr(scheduler, "_get_activity_registry", lambda: built)
    return built


def _runs(home):
    from activity_telemetry.store import ActivityStore

    store = ActivityStore(home / "telemetry" / "activity.db")
    conn = store._get_conn()
    return [dict(row) for row in conn.execute(
        "SELECT * FROM logical_activity_runs ORDER BY rowid"
    ).fetchall()]


def _only_run(home):
    rows = _runs(home)
    assert len(rows) == 1, f"expected exactly one logical run, got {len(rows)}"
    return rows[0]


@contextmanager
def _model_runtime(run_conversation):
    """Drive the model path with provider resolution and the agent stubbed."""
    with (
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=MagicMock()),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent") as agent_cls,
    ):
        agent = MagicMock()
        if isinstance(run_conversation, Exception):
            agent.run_conversation.side_effect = run_conversation
        else:
            agent.run_conversation.return_value = run_conversation
        agent_cls.return_value = agent
        yield agent_cls


def _model_job(name="mapped-model-job"):
    from cron.jobs import create_job

    job = create_job(prompt="do the thing", schedule="every 5m", deliver="local")
    job["name"] = name
    return job


def _script_job(hermes_env, script_name, body, **extra):
    from cron.jobs import create_job

    script = hermes_env / "scripts" / f"{script_name}.sh"
    script.write_text(f"#!/bin/bash\n{body}\n")
    job = create_job(
        prompt=None,
        schedule="every 5m",
        script=f"{script_name}.sh",
        no_agent=True,
        deliver="local",
    )
    job.update(extra)
    return job


# ---------------------------------------------------------------------------
# Mapping, correlation, and compatibility
# ---------------------------------------------------------------------------


def test_unmapped_legacy_job_runs_without_telemetry(hermes_env, registry):
    from cron.scheduler import run_job

    job = _script_job(hermes_env, "legacy", "echo hi", name="not-in-registry")
    success, _doc, final_response, error = run_job(job)

    assert success is True
    assert error is None
    assert "hi" in final_response
    assert not (hermes_env / "telemetry" / "activity.db").exists()


def test_explicit_activity_id_wins_over_job_name_alias(hermes_env, registry):
    from cron.scheduler import run_job

    job = _script_job(
        hermes_env, "explicit", "echo hi",
        name="mapped-script-job", activity_id="fleet.tests.model",
    )
    run_job(job)

    assert _only_run(hermes_env)["activity_id"] == "fleet.tests.model"


def test_external_correlation_is_distinct_from_session(hermes_env, registry):
    from cron.scheduler import run_job

    job = _script_job(
        hermes_env, "correlated", "echo hi",
        name="mapped-script-job", correlation_id="event-123",
    )
    run_job(job)

    row = _only_run(hermes_env)
    assert row["correlation_id"] == "event-123"
    assert row["session_id"] is None
    assert row["run_id"] != "event-123"


def test_absent_correlation_defaults_to_the_run_id(hermes_env, registry):
    from cron.scheduler import run_job

    run_job(_script_job(hermes_env, "plain", "echo hi", name="mapped-script-job"))

    row = _only_run(hermes_env)
    assert row["correlation_id"] == row["run_id"]


def test_run_records_policy_version_profile_and_trigger(hermes_env, registry):
    from cron.scheduler import run_job

    run_job(_script_job(hermes_env, "attrib", "echo hi", name="mapped-script-job"))

    row = _only_run(hermes_env)
    assert row["policy_version"] == 1
    assert row["trigger_source"] == "cron"
    assert str(hermes_env) in row["effective_hermes_home"]


# ---------------------------------------------------------------------------
# Terminal evidence for every early branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "process", "final", "evidence", "expect_success"),
    (
        ("echo oops >&2\nexit 3", "failed", "failed", "script_failed", False),
        # The plan's "wakeAgent:false" label is normalized to a single-colon
        # bounded reference so it satisfies the store's evidence contract.
        ("echo '{\"wakeAgent\": false}'", "no_work", "no_work", "wake_gate_false", True),
        ("# nothing", "no_work", "no_work", "empty_stdout", True),
        ("echo done", "succeeded", "unknown", "script_completed", True),
    ),
)
def test_no_agent_branches_record_bounded_terminal_evidence(
    hermes_env, registry, body, process, final, evidence, expect_success
):
    import json

    from cron.scheduler import run_job

    success, _doc, _final_response, _error = run_job(
        _script_job(hermes_env, "branch", body, name="mapped-script-job")
    )

    assert success is expect_success
    row = _only_run(hermes_env)
    assert row["process_outcome"] == process
    assert row["final_outcome"] == final
    assert row["delivery_outcome"] == "unknown"
    assert json.loads(row["evidence_refs_json"]) == [f"evidence:{evidence}"]
    assert row["finished_at"] is not None


def test_hybrid_wake_gate_records_no_work_without_a_session(hermes_env, registry):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script = hermes_env / "scripts" / "gate.sh"
    script.write_text('#!/bin/bash\necho \'{"wakeAgent": false}\'\n')
    job = create_job(
        prompt="do the thing", schedule="every 5m", script="gate.sh", deliver="local"
    )
    job["name"] = "mapped-model-job"

    with patch("run_agent.AIAgent") as ai_mock:
        success, _doc, _final_response, error = run_job(job)

    ai_mock.assert_not_called()
    assert success is True and error is None
    row = _only_run(hermes_env)
    assert row["process_outcome"] == "no_work"
    assert row["final_outcome"] == "no_work"
    assert row["session_id"] is None


def test_prompt_injection_block_records_blocked(hermes_env, registry, monkeypatch):
    from cron import scheduler
    from cron.jobs import create_job
    from cron.scheduler import CronPromptInjectionBlocked, run_job

    def _blocked(*_a, **_k):
        raise CronPromptInjectionBlocked("threat pattern matched")

    monkeypatch.setattr(scheduler, "_build_job_prompt", _blocked)
    job = create_job(prompt="do the thing", schedule="every 5m", deliver="local")
    job["name"] = "mapped-model-job"

    success, _doc, _final_response, error = run_job(job)

    assert success is False
    assert "threat pattern matched" in error
    row = _only_run(hermes_env)
    assert row["process_outcome"] == "blocked"
    assert row["final_outcome"] == "blocked"
    assert row["delivery_outcome"] == "unknown"


def test_empty_prompt_records_no_work(hermes_env, registry, monkeypatch):
    from cron import scheduler
    from cron.jobs import create_job
    from cron.scheduler import SILENT_MARKER, run_job

    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_a, **_k: None)
    job = create_job(prompt="do the thing", schedule="every 5m", deliver="local")
    job["name"] = "mapped-model-job"

    success, _doc, final_response, error = run_job(job)

    assert success is True and error is None and final_response == SILENT_MARKER
    row = _only_run(hermes_env)
    assert row["process_outcome"] == "no_work"
    assert row["final_outcome"] == "no_work"


# ---------------------------------------------------------------------------
# Model-loop branches
# ---------------------------------------------------------------------------


def test_model_completion_is_process_success_but_semantically_unknown(
    hermes_env, registry, monkeypatch
):
    import json

    from cron import scheduler
    from cron.scheduler import run_job

    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_a, **_k: "do it")

    with _model_runtime({"final_response": "all done", "completed": True}):
        success, _doc, final_response, error = run_job(_model_job())

    assert success is True and error is None and "all done" in final_response
    row = _only_run(hermes_env)
    assert row["process_outcome"] == "succeeded"
    assert row["final_outcome"] == "unknown"
    assert row["delivery_outcome"] == "unknown"
    assert row["session_id"].startswith("cron_")
    assert json.loads(row["evidence_refs_json"]) == [f"session:{row['session_id']}"]


def test_model_loop_failure_records_failed(hermes_env, registry, monkeypatch):
    from cron import scheduler
    from cron.scheduler import run_job

    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_a, **_k: "do it")

    with _model_runtime(RuntimeError("provider exploded")):
        success, _doc, _final_response, error = run_job(_model_job())

    assert success is False
    assert "provider exploded" in error
    row = _only_run(hermes_env)
    assert row["process_outcome"] == "failed"
    assert row["final_outcome"] == "failed"
    assert row["delivery_outcome"] == "unknown"


def test_recorder_is_passed_to_the_agent_constructor(hermes_env, registry, monkeypatch):
    from cron import scheduler
    from cron.scheduler import run_job

    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_a, **_k: "do it")

    with _model_runtime({"final_response": "ok"}) as agent_cls:
        run_job(_model_job())

    recorder = agent_cls.call_args.kwargs["activity_recorder"]
    row = _only_run(hermes_env)
    assert recorder is not None
    assert recorder.run_id == row["run_id"]
    # The recorder carries the same session the agent was given, so runtime
    # usage lands on this logical run and not on a second orphan run.
    assert agent_cls.call_args.kwargs["session_id"] == row["session_id"]


def test_unmapped_model_job_gets_no_recorder(hermes_env, registry, monkeypatch):
    from cron import scheduler
    from cron.scheduler import run_job

    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_a, **_k: "do it")

    with _model_runtime({"final_response": "ok"}) as agent_cls:
        run_job(_model_job(name="not-in-registry"))

    assert agent_cls.call_args.kwargs["activity_recorder"] is None
    assert not (hermes_env / "telemetry" / "activity.db").exists()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_open_failure_leaves_the_job_result_unchanged(hermes_env, registry, monkeypatch, caplog):
    from cron import scheduler
    from cron.scheduler import run_job

    def _explode(**_kwargs):
        raise OSError("telemetry disk with secret=must-not-appear")

    monkeypatch.setattr(scheduler, "_open_cron_activity", _explode)
    job = _script_job(hermes_env, "isolated", "echo hi", name="mapped-script-job")

    success, _doc, final_response, error = run_job(job)

    assert success is True and error is None and "hi" in final_response
    assert "must-not-appear" not in caplog.text


def test_finish_failure_leaves_the_job_result_unchanged(hermes_env, registry, monkeypatch, caplog):
    """A broken terminal write must not change what the job returned."""
    from activity_telemetry.store import ActivityStore
    from cron.scheduler import run_job

    def _explode(*_a, **_k):
        raise RuntimeError("terminal write failed with secret=must-not-appear")

    monkeypatch.setattr(ActivityStore, "finish", _explode)
    job = _script_job(hermes_env, "isolated2", "echo hi", name="mapped-script-job")

    success, _doc, final_response, error = run_job(job)

    assert success is True and error is None and "hi" in final_response
    assert "must-not-appear" not in caplog.text
    # The run stays open rather than claiming an outcome it never observed.
    assert _only_run(hermes_env)["finished_at"] is None


def test_finish_helper_swallows_an_invalid_outcome(hermes_env, registry, caplog):
    """The helper itself is the last guard against a bad terminal call."""
    from cron.scheduler import _finish_cron_activity, _open_cron_activity

    policy = registry.policies["fleet.tests.script"]
    recorder = _open_cron_activity(
        job={"id": "j"}, policy=policy, run_id="r", correlation_id="r",
        profile="default", effective_hermes_home=str(hermes_env),
    )
    assert recorder is not None

    _finish_cron_activity(recorder, process="definitely-not-an-outcome")

    assert _only_run(hermes_env)["finished_at"] is None
    assert "ValueError" in caplog.text


def test_unloadable_registry_behaves_as_unmapped(hermes_env, monkeypatch, caplog):
    from activity_policy.schema import PolicyError
    from cron import scheduler
    from cron.scheduler import run_job

    def _explode():
        raise PolicyError("invalid policy YAML with secret=must-not-appear")

    monkeypatch.setattr(scheduler, "_load_activity_registry", _explode)
    monkeypatch.setattr(scheduler, "_ACTIVITY_REGISTRY", None, raising=False)
    monkeypatch.setattr(scheduler, "_ACTIVITY_REGISTRY_LOADED", False, raising=False)
    job = _script_job(hermes_env, "noreg", "echo hi", name="mapped-script-job")

    success, _doc, final_response, error = run_job(job)

    assert success is True and error is None and "hi" in final_response
    assert not (hermes_env / "telemetry" / "activity.db").exists()
    assert "must-not-appear" not in caplog.text
