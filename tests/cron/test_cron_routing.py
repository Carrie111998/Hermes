"""Contract tests for deterministic cron capability routing."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch



def test_classify_multi_step_context_job_as_synthesis():
    from cron.routing import classify_cron_job

    result = classify_cron_job(
        prompt=(
            "Review the completed week and next two weeks, compare the inputs, "
            "then produce a concise plan with recommendations."
        ),
        skills=["weekly-review-planning", "google-workspace"],
        enabled_toolsets=["calendar", "file", "web"],
        schedule={"kind": "interval", "minutes": 10080},
        context_from=["upstream-job"],
        output_type="plan",
    )

    assert result.slot == "synthesis"
    assert result.signals["context_from"] is True
    assert result.signals["multiple_skills"] is True
    assert result.signals["output_type"] == "plan"


def test_classify_script_backed_job_as_deterministic():
    from cron.routing import classify_cron_job

    result = classify_cron_job(
        prompt="",
        script="collect_metrics.py",
        schedule={"kind": "interval", "minutes": 60},
    )

    assert result.slot == "deterministic"
    assert result.signals["script"] is True



def test_negated_risk_language_does_not_make_a_job_critical():
    from cron.routing import classify_cron_job

    result = classify_cron_job(
        prompt=(
            "Somente leitura. NUNCA autoaplique nada: apenas sugira. "
            "Sem merge/deploy/gasto."
        ),
        schedule={"kind": "interval", "minutes": 1440},
    )

    assert result.signals["risk"] is False
    assert result.slot != "critical"


def test_cost_tokens_are_not_treated_as_secret_credentials():
    from cron.routing import classify_cron_job

    result = classify_cron_job(
        prompt="Analise eficiência de tokens e custo do relatório.",
        schedule={"kind": "interval", "minutes": 1440},
    )

    assert result.signals["risk"] is False
    assert result.slot != "critical"


def test_positive_token_credential_instruction_remains_critical():
    from cron.routing import classify_cron_job

    result = classify_cron_job(
        prompt="Revogue o access token de produção após a falha de autenticação.",
        schedule={"kind": "once"},
    )

    assert result.signals["risk"] is True
    assert result.slot == "critical"



def test_classify_high_risk_job_as_critical():
    from cron.routing import classify_cron_job

    result = classify_cron_job(
        prompt="Deploy the production release and rotate the payment credentials.",
        skills=["deployment"],
        enabled_toolsets=["terminal", "file"],
        schedule={"kind": "once"},
        output_type="action",
    )

    assert result.slot == "critical"
    assert result.signals["risk"] is True


def test_risk_override_beats_many_synthesis_signals():
    from cron.routing import classify_cron_job

    result = classify_cron_job(
        prompt="Compare the reports, then draft a production release plan.",
        skills=["research", "planning", "release"],
        enabled_toolsets=["web", "file", "terminal"],
        context_from=["upstream-a", "upstream-b"],
        output_type="plan",
    )

    assert result.slot == "critical"
    assert result.scores["synthesis"] > result.scores["critical"]



def test_no_agent_route_does_not_resolve_a_provider():
    from cron.routing import build_cron_routing_record, resolve_cron_route

    job = {
        "id": "script-only",
        "prompt": "",
        "script": "watchdog.py",
        "no_agent": True,
        "skills": [],
        "enabled_toolsets": None,
        "context_from": None,
        "routing_slot": None,
    }
    record = build_cron_routing_record(job, config={})
    assert record["mode"] == "no_agent"
    assert record["classification"]["skipped"] is True

    with pytest.raises(AssertionError):
        # The no-agent record is intentionally not an inference route.
        resolve_cron_route({**job, "routing": record}, {})



def test_resolve_route_uses_configured_slot_and_audits_runtime(monkeypatch):
    from cron.routing import resolve_cron_route

    calls = []

    def fake_resolve(**kwargs):
        calls.append(kwargs)
        return {
            "provider": "nous",
            "requested_provider": "nous",
            "model": "openai/gpt-5.6-luna",
            "api_key": "test-key",
            "base_url": "https://inference.example/v1",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve,
    )
    cfg = {
        "cron": {
            "routing": {
                "enabled": True,
                "slots": {
                    "synthesis": {
                        "provider": "nous",
                        "model": "openai/gpt-5.6-luna",
                        "reasoning_effort": "medium",
                    }
                },
            }
        }
    }
    job = {
        "id": "synthesis-job",
        "prompt": "Summarize the source material and draft a report.",
        "skills": ["research"],
        "enabled_toolsets": ["web"],
        "context_from": None,
        "routing": {
            "version": 1,
            "mode": "agent",
            "slot": "synthesis",
            "reasoning_effort": "medium",
            "requested_model": "openai/gpt-5.6-luna",
            "requested_provider": "nous",
        },
    }

    route = resolve_cron_route(job, cfg)

    assert route.slot == "synthesis"
    assert route.model == "openai/gpt-5.6-luna"
    assert route.provider == "nous"
    assert route.reasoning_effort == "medium"
    assert route.audit["status"] == "resolved"
    assert route.audit["family_switch"] is False
    assert calls == [
        {
            "requested": "nous",
            "target_model": "openai/gpt-5.6-luna",
        }
    ]



def test_resolve_route_fails_closed_without_eligible_runtime(monkeypatch):
    from cron.routing import CronRoutingError, resolve_cron_route

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no credential")),
    )
    job = {
        "id": "blocked-job",
        "routing": {
            "version": 1,
            "mode": "agent",
            "slot": "critical",
            "reasoning_effort": "high",
            "requested_model": "m",
            "requested_provider": "p",
        },
    }

    with pytest.raises(CronRoutingError, match="fail-closed"):
        resolve_cron_route(job, {"cron": {"routing": {"enabled": True}}})


def test_resolve_route_rejects_provider_family_switch(monkeypatch):
    from cron.routing import CronRoutingError, resolve_cron_route

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "anthropic",
            "api_key": "test-key",
            "base_url": "https://api.anthropic.com",
        },
    )
    job = {
        "routing": {
            "version": 1,
            "mode": "agent",
            "slot": "critical",
            "requested_model": "m",
            "requested_provider": "nous",
            "reasoning_effort": "high",
        }
    }

    with pytest.raises(CronRoutingError, match="family") as exc_info:
        resolve_cron_route(job, {})

    assert exc_info.value.audit["reason"] == "family_switch"
    assert exc_info.value.audit["effective_provider"] == "anthropic"


def test_resolve_route_rejects_runtime_model_switch(monkeypatch):
    from cron.routing import CronRoutingError, resolve_cron_route

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "nous",
            "model": "different-model",
            "api_key": "test-key",
            "base_url": "https://inference.example/v1",
        },
    )
    job = {
        "routing": {
            "version": 1,
            "mode": "agent",
            "slot": "synthesis",
            "requested_model": "selected-model",
            "requested_provider": "nous",
            "reasoning_effort": "medium",
        }
    }

    with pytest.raises(CronRoutingError, match="selected model") as exc_info:
        resolve_cron_route(job, {})

    assert exc_info.value.audit["reason"] == "model_switch"


def test_resolve_route_allows_profile_default_provider_resolution(monkeypatch):
    from cron.routing import resolve_cron_route

    calls = []

    def fake_resolve(**kwargs):
        calls.append(kwargs)
        return {
            "provider": "test",
            "api_key": "test-key",
            "base_url": "http://test.local",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve,
    )
    route = resolve_cron_route(
        {
            "routing": {
                "version": 1,
                "mode": "agent",
                "slot": "interpretation",
                "requested_model": "profile-model",
                "requested_provider": "auto",
            }
        },
        {},
    )

    assert route.provider == "test"
    assert calls == [{"requested": None, "target_model": "profile-model"}]


def test_routed_preflight_does_not_resolve_provider_again(monkeypatch):
    from cron.scheduler import _preflight_check_provider_key

    calls = []
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: calls.append(kwargs),
    )

    assert _preflight_check_provider_key(
        {
            "routing": {"mode": "agent", "slot": "synthesis"},
            "provider": None,
            "model": None,
        },
        {},
    ) is None
    assert calls == []


def test_routed_scheduler_run_reuses_atomic_runtime_and_audits_route(tmp_path):
    from cron.scheduler import run_job

    runtime_calls = []
    runtime = {
        "provider": "nous",
        "requested_provider": "nous",
        "api_key": "test-key",
        "base_url": "https://inference.example/v1",
        "api_mode": "chat_completions",
    }

    def fake_resolve(**kwargs):
        runtime_calls.append(kwargs)
        return dict(runtime)

    fake_db = MagicMock()
    job = {
        "id": "routed-run",
        "name": "routed run",
        "prompt": "Produce a synthesis.",
        "model": None,
        "provider": None,
        "base_url": None,
        "skills": [],
        "routing": {
            "version": 1,
            "mode": "agent",
            "slot": "synthesis",
            "requested_model": "selected-model",
            "requested_provider": "nous",
            "reasoning_effort": "high",
        },
    }

    with patch("cron.scheduler.load_config", return_value={}), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("cron.scheduler._cron_preflight_enabled", return_value=False), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=fake_resolve), \
         patch("cron.scheduler._usage_audit_path", return_value=tmp_path / "usage_audit.jsonl"), \
         patch("run_agent.AIAgent") as agent_cls:
        agent = MagicMock()
        agent.run_conversation.return_value = {"final_response": "ok"}
        agent_cls.return_value = agent

        success, _output, final_response, error = run_job(job)

    assert success is True
    assert final_response == "ok"
    assert error is None
    assert runtime_calls == [{"requested": "nous", "target_model": "selected-model"}]
    kwargs = agent_cls.call_args.kwargs
    assert kwargs["model"] == "selected-model"
    assert kwargs["provider"] == "nous"
    assert kwargs["fallback_model"] is None
    assert kwargs["reasoning_config"] == {"enabled": True, "effort": "high"}

    audit = __import__("json").loads((tmp_path / "usage_audit.jsonl").read_text().splitlines()[0])
    assert audit["routing_slot"] == "synthesis"
    assert audit["routing_effective_model"] == "selected-model"
    assert audit["routing_effective_provider"] == "nous"
    assert audit["routing_reasoning_effort"] == "high"
    assert audit["routing_status"] == "completed"


def test_routed_scheduler_blocks_without_agent_or_fallback_and_audits_failure(tmp_path):
    from cron.scheduler import run_job

    job = {
        "id": "blocked-routed-run",
        "name": "blocked routed run",
        "prompt": "Produce a report.",
        "deliver": "local",
        "routing": {
            "version": 1,
            "mode": "agent",
            "slot": "synthesis",
            "requested_model": "selected-model",
            "requested_provider": "nous",
            "reasoning_effort": "medium",
        },
    }

    with patch("cron.scheduler.load_config", return_value={}), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=RuntimeError("credential unavailable")), \
         patch("cron.scheduler._usage_audit_path", return_value=tmp_path / "usage_audit.jsonl"), \
         patch("run_agent.AIAgent") as agent_cls, \
         patch("cron.scheduler.get_fallback_chain") as fallback_chain:
        success, doc, _final_response, error = run_job(job)

    assert success is False
    assert agent_cls.called is False
    assert fallback_chain.called is False
    assert error is not None and "fail-closed" in error
    assert "no model call was made" in doc

    audit = __import__("json").loads((tmp_path / "usage_audit.jsonl").read_text().splitlines()[0])
    assert audit["routing_slot"] == "synthesis"
    assert audit["routing_effective_model"] is None
    assert audit["routing_effective_provider"] is None
    assert audit["routing_reasoning_effort"] == "medium"
    assert audit["routing_status"] == "blocked"
    assert "credential unavailable" in audit["routing_failure_reason"]


def test_routed_safety_guard_runs_before_runtime_resolution(tmp_path):
    from cron.scheduler import run_job

    resolver_calls = []
    job = {
        "id": "unsafe-routed-run",
        "name": "unsafe routed run",
        "prompt": "Deploy the release.",
        "deliver": "local",
        "routing": {
            "version": 1,
            "mode": "agent",
            "slot": "critical",
            "requested_model": "selected-model",
            "requested_provider": "anthropic",
            "requested_base_url": "https://evil.example/v1",
            "reasoning_effort": "high",
        },
    }

    with patch("cron.scheduler.load_config", return_value={}), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             side_effect=lambda **kwargs: resolver_calls.append(kwargs),
         ), \
         patch("cron.scheduler._usage_audit_path", return_value=tmp_path / "usage_audit.jsonl"), \
         patch("run_agent.AIAgent") as agent_cls:
        success, doc, _final_response, error = run_job(job)

    assert success is False
    assert agent_cls.called is False
    assert resolver_calls == []
    assert error is not None and "safety gate" in error
    assert "critical" in doc
    audit = __import__("json").loads((tmp_path / "usage_audit.jsonl").read_text().splitlines()[0])
    assert audit["routing_slot"] == "critical"
    assert audit["routing_effective_model"] is None
    assert audit["routing_failure_reason"]



def test_invalid_slot_override_is_rejected_at_shared_creation_contract():
    from cron.routing import build_cron_routing_record

    with pytest.raises(ValueError, match="Unknown cron routing slot"):
        build_cron_routing_record(
            {
                "prompt": "Do something semantic",
                "skills": [],
                "routing_slot": "cheap-but-secret",
                "no_agent": False,
            },
            config={},
        )


def test_non_string_slot_override_is_rejected_at_storage_boundary(tmp_path, monkeypatch):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")

    with pytest.raises(ValueError, match="Invalid routing_slot"):
        jobs.create_job(
            prompt="hello",
            schedule="every 1h",
            routing_slot=3,
        )

    assert jobs.load_jobs() == []



def test_slot_classification_is_deterministic_for_same_inputs():
    from cron.routing import classify_cron_job

    kwargs = {
        "prompt": "Check the status and report only if it changed.",
        "skills": ["status-check"],
        "enabled_toolsets": ["web"],
        "schedule": {"kind": "interval", "minutes": 15},
        "monitor_script": "status.py",
        "output_type": "alert",
    }
    first = classify_cron_job(**kwargs)
    second = classify_cron_job(**kwargs)

    assert first == second
    assert first.slot == "interpretation"
    assert first.score == second.score



def test_no_agent_does_not_call_classification(monkeypatch):
    from cron import routing

    monkeypatch.setattr(
        routing,
        "classify_cron_job",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("classified")),
    )
    record = routing.build_cron_routing_record(
        {
            "prompt": "",
            "script": "watchdog.py",
            "no_agent": True,
            "skills": [],
        },
        config={},
    )
    assert record["mode"] == "no_agent"
    assert record["classification"]["skipped"] is True


def test_create_job_persists_shared_route_and_audit_fields(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {"provider": "nous"},
    )
    monkeypatch.setattr(jobs, "_resolve_default_model_snapshot", lambda: "model-a")

    created = jobs.create_job(
        prompt="Summarize the collected reports and draft a plan.",
        schedule="every 1h",
        skills=["research", "weekly-review-planning"],
    )

    assert created["routing"]["slot"] == "synthesis"
    assert created["routing"]["requested_model"] == "model-a"
    assert created["routing"]["requested_provider"] == "nous"
    assert created["routing"]["fallback_policy"] == "fail_closed"


def test_update_rebuilds_route_when_existing_routed_job_changes(tmp_path, monkeypatch):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(jobs, "_resolve_default_model_snapshot", lambda: "model-a")
    created = jobs.create_job(
        prompt="Check status",
        schedule="every 1h",
        routing_slot="interpretation",
    )

    updated = jobs.update_job(created["id"], {"routing_slot": "critical"})

    assert updated["routing_slot"] == "critical"
    assert updated["routing"]["slot"] == "critical"
    assert "explicit slot override" in updated["routing"]["classification"]["reason"]


def test_legacy_update_without_explicit_slot_keeps_legacy_execution_path(tmp_path, monkeypatch):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    jobs.save_jobs(
        [
            {
                "id": "legacy-route-job",
                "name": "legacy route job",
                "prompt": "hello",
                "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
                "schedule_display": "every 60m",
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
            }
        ]
    )

    updated = jobs.update_job("legacy-route-job", {"name": "renamed", "routing_slot": None})

    assert updated["name"] == "renamed"
    assert "routing" not in updated


def test_routed_update_reclassifies_when_schedule_changes(tmp_path, monkeypatch):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    created = jobs.create_job(
        prompt="Check status",
        schedule="every 1h",
        routing_slot="interpretation",
    )
    assert created["routing"]["classification"]["signals"]["high_frequency"] is False

    updated = jobs.update_job(created["id"], {"schedule": "every 5m"})

    assert updated["routing"]["classification"]["signals"]["high_frequency"] is True
