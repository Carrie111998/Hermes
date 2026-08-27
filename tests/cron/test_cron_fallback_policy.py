"""Strict per-job cron fallback-boundary contracts."""

from __future__ import annotations

import contextlib
import json
import re
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import run_job


def _isolated_jobs(monkeypatch):
    """Return cron.jobs with all persistence confined to memory."""
    import cron.jobs as jobs

    stored = []

    @contextlib.contextmanager
    def _noop_lock():
        yield

    monkeypatch.setattr(jobs, "_jobs_lock", _noop_lock, raising=True)
    monkeypatch.setattr(jobs, "load_jobs", lambda: list(stored), raising=True)

    def _save(records, **_kwargs):
        stored[:] = records

    monkeypatch.setattr(jobs, "save_jobs", _save, raising=True)
    return jobs, stored


def test_create_job_persists_explicit_none_policy(monkeypatch):
    jobs, stored = _isolated_jobs(monkeypatch)

    job = jobs.create_job(
        prompt="private review",
        schedule="every 1 hour",
        provider="openai-codex",
        model="gpt-5.6-sol",
        fallback_policy="none",
    )

    assert job["fallback_policy"] == "none"
    assert job["fallback_snapshot"] is None
    assert stored[0]["fallback_policy"] == "none"


def test_pinned_snapshot_persists_only_sanitised_route_metadata(monkeypatch):
    jobs, _stored = _isolated_jobs(monkeypatch)
    monkeypatch.setattr(
        jobs,
        "_resolve_current_fallback_chain",
        lambda: [
            {
                "provider": "custom",
                "model": "private-model",
                "base_url": (
                    "https://alice:supersecret@example.internal:8443/"
                    "private-token/v1?api_key=query-secret#fragment-secret"
                ),
                "api_key": "field-secret",
            }
        ],
        raising=False,
    )

    job = jobs.create_job(
        prompt="approved public task",
        schedule="every 1 hour",
        fallback_policy="pinned",
    )

    snapshot = job["fallback_snapshot"]
    assert snapshot["routes"] == [
        {
            "provider": "custom",
            "model": "private-model",
            "base_url": "https://example.internal:8443",
        }
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", snapshot["fingerprint"])
    rendered = json.dumps(snapshot, sort_keys=True)
    for secret in (
        "alice",
        "supersecret",
        "private-token",
        "query-secret",
        "fragment-secret",
        "field-secret",
    ):
        assert secret not in rendered


def test_update_to_pinned_atomically_captures_current_route(monkeypatch):
    jobs, _stored = _isolated_jobs(monkeypatch)
    job = jobs.create_job(
        prompt="review",
        schedule="every 1 hour",
        fallback_policy="inherit",
    )
    monkeypatch.setattr(
        jobs,
        "_resolve_current_fallback_chain",
        lambda: [
            {
                "provider": "openrouter",
                "model": "fallback-model",
                "base_url": "https://openrouter.ai/api/v1",
            }
        ],
    )

    updated = jobs.update_job(job["id"], {"fallback_policy": "pinned"})

    assert updated["fallback_policy"] == "pinned"
    assert updated["fallback_snapshot"]["routes"] == [
        {
            "provider": "openrouter",
            "model": "fallback-model",
            "base_url": "https://openrouter.ai",
        }
    ]


def test_legacy_job_without_policy_reads_as_inherit():
    import cron.jobs as jobs

    normalized = jobs._normalize_job_record({
        "id": "legacy-job",
        "name": "legacy",
        "prompt": "run",
        "schedule": {"kind": "interval", "seconds": 3600},
    })

    assert normalized["fallback_policy"] == "inherit"
    assert normalized["fallback_snapshot"] is None


def test_legacy_job_no_agent_update_defaults_policy_to_inherit(monkeypatch):
    jobs, stored = _isolated_jobs(monkeypatch)
    created = jobs.create_job(
        prompt="legacy job",
        schedule="every 1 hour",
        script="print('ok')",
    )
    stored[0].pop("fallback_policy", None)
    stored[0].pop("fallback_snapshot", None)

    updated = jobs.update_job(created["id"], {"no_agent": True})

    assert updated["no_agent"] is True
    assert updated["fallback_policy"] == "inherit"
    assert updated["fallback_snapshot"] is None


def test_pinned_fingerprint_is_independent_of_url_secret_components():
    import cron.jobs as jobs

    first = jobs.snapshot_fallback_chain([
        {
            "provider": "custom",
            "model": "model-a",
            "base_url": (
                "https://alice:first-secret@example.internal:8443/"
                "tenant-route/v1?api_key=first-query#first-fragment"
            ),
        }
    ])
    second = jobs.snapshot_fallback_chain([
        {
            "provider": "custom",
            "model": "model-a",
            "base_url": (
                "https://bob:second-secret@example.internal:8443/"
                "tenant-route/v1?api_key=second-query#second-fragment"
            ),
        }
    ])

    assert first["routes"] == second["routes"]
    assert first["fingerprint"] == second["fingerprint"]


def test_pinned_fingerprint_changes_when_endpoint_path_changes():
    import cron.jobs as jobs

    first = jobs.snapshot_fallback_chain([
        {
            "provider": "custom",
            "model": "model-a",
            "base_url": "https://example.internal:8443/tenant-a/v1?token=secret",
        }
    ])
    second = jobs.snapshot_fallback_chain([
        {
            "provider": "custom",
            "model": "model-a",
            "base_url": "https://example.internal:8443/tenant-b/v1?token=secret",
        }
    ])

    assert first["routes"] == second["routes"]
    assert "tenant-a" not in repr(first["routes"])
    assert "tenant-b" not in repr(second["routes"])
    assert first["fingerprint"] != second["fingerprint"]


def _run_job_with_resolver(job, tmp_path, resolver):
    fake_db = MagicMock()
    patches = (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=resolver,
        ),
    )
    with patch("run_agent.AIAgent") as agent_cls, ExitStack() as stack:
        agent = MagicMock()
        agent.run_conversation.return_value = {"final_response": "ok"}
        agent_cls.return_value = agent
        for item in patches:
            stack.enter_context(item)
        result = run_job(job)
    return result, agent_cls


def test_none_policy_propagates_auxiliary_boundary_into_agent_worker(tmp_path):
    from agent import auxiliary_client as auxiliary

    (tmp_path / "config.yaml").write_text(
        "cron:\n  preflight: false\nmodel:\n  default: primary-model\n",
        encoding="utf-8",
    )
    observed = []

    def resolver(**_kwargs):
        return {
            "api_key": "test-key",
            "base_url": "https://primary.invalid/v1",
            "provider": "openai-codex",
            "api_mode": "codex_responses",
        }

    def build_agent(*_args, **_kwargs):
        observed.append(("constructor", auxiliary.get_auxiliary_fallback_boundary()))
        agent = MagicMock()

        def run_conversation(_prompt):
            observed.append(("worker", auxiliary.get_auxiliary_fallback_boundary()))
            return {"final_response": "ok"}

        agent.run_conversation.side_effect = run_conversation
        return agent

    job = {
        "id": "none-auxiliary-boundary",
        "name": "none auxiliary boundary",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": "none",
    }
    fake_db = MagicMock()
    patches = (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=resolver,
        ),
        patch("run_agent.AIAgent", side_effect=build_agent),
    )

    assert auxiliary.get_auxiliary_fallback_boundary() is None
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        success, _output, _final, error = run_job(job)

    assert success is True, error
    assert observed == [
        ("constructor", {"policy": "none", "chain": []}),
        ("worker", {"policy": "none", "chain": []}),
    ]
    assert auxiliary.get_auxiliary_fallback_boundary() is None


def test_none_policy_stops_after_primary_auth_failure(tmp_path):
    from hermes_cli.auth import AuthError

    (tmp_path / "config.yaml").write_text(
        "cron:\n"
        "  preflight: false\n"
        "model:\n"
        "  default: primary-model\n"
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: fallback-model\n",
        encoding="utf-8",
    )
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs.get("requested"))
        if len(calls) == 1:
            raise AuthError("primary credential unavailable")
        return {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
        }

    job = {
        "id": "none-runtime",
        "name": "none runtime",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": "none",
    }

    (success, _output, _final, error), agent_cls = _run_job_with_resolver(
        job, tmp_path, resolver
    )

    assert success is False
    assert calls == ["openai-codex"]
    assert agent_cls.called is False
    assert "fallback_policy=none" in (error or "")


def test_none_policy_preflight_does_not_use_global_chain_as_rescue(tmp_path):
    from hermes_cli.auth import AuthError

    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: primary-model\n"
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: fallback-model\n",
        encoding="utf-8",
    )
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs.get("requested"))
        raise AuthError("primary credential unavailable")

    job = {
        "id": "none-preflight",
        "name": "none preflight",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": "none",
    }

    (success, _output, _final, error), agent_cls = _run_job_with_resolver(
        job, tmp_path, resolver
    )

    assert success is False
    assert calls == ["openai-codex"]
    assert agent_cls.called is False
    assert "[blocked_config]" in (error or "")


def test_none_policy_failure_summary_does_not_consult_global_chain(monkeypatch):
    import cron.scheduler as scheduler

    def unexpected_config_read():
        raise AssertionError("none-policy summary must not read global fallback config")

    monkeypatch.setattr(scheduler, "load_config", unexpected_config_read)
    message = scheduler._summarize_cron_failure_for_delivery(
        {
            "id": "none-summary",
            "name": "none summary",
            "fallback_policy": "none",
        },
        "HTTP 429: rate limit exceeded",
    )

    assert "fallback_policy=none" in message
    assert "no fallback provider was attempted" in message
    assert "exhausted" not in message.lower()


def test_pinned_route_drift_fails_before_provider_resolution(tmp_path):
    import cron.jobs as cron_jobs

    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: primary-model\n"
        "fallback_providers:\n"
        "  - provider: current-provider\n"
        "    model: current-model\n"
        "    base_url: https://current.example/v1?token=current-secret\n",
        encoding="utf-8",
    )
    expected = cron_jobs.snapshot_fallback_chain([
        {
            "provider": "expected-provider",
            "model": "expected-model",
            "base_url": "https://expected.example/v1?token=expected-secret",
        }
    ])
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs)
        return {
            "api_key": "test-key",
            "base_url": "https://primary.invalid/v1",
            "provider": "openai-codex",
            "api_mode": "codex_responses",
        }

    job = {
        "id": "pinned-drift",
        "name": "pinned drift",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": "pinned",
        "fallback_snapshot": expected,
    }

    (success, _output, _final, error), agent_cls = _run_job_with_resolver(
        job, tmp_path, resolver
    )

    assert success is False
    assert calls == []
    assert agent_cls.called is False
    assert "[drift_skip]" in (error or "")
    for private_value in (
        "expected-provider",
        "expected-model",
        "expected-secret",
        "current-provider",
        "current-model",
        "current-secret",
    ):
        assert private_value not in (error or "")


def test_invalid_stored_policy_is_blocked_without_echoing_value(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: primary-model\n", encoding="utf-8"
    )
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs)
        raise AssertionError("provider resolution must not run")

    job = {
        "id": "invalid-policy",
        "name": "invalid policy",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": "unknown-policy-with-private-value",
    }

    (success, _output, _final, error), agent_cls = _run_job_with_resolver(
        job, tmp_path, resolver
    )

    assert success is False
    assert calls == []
    assert agent_cls.called is False
    assert "[blocked_config]" in (error or "")
    assert "unknown-policy-with-private-value" not in (error or "")


@pytest.mark.parametrize(
    "stored_policy",
    [None, False, 0, [], {}],
    ids=["null", "false", "zero", "list", "mapping"],
)
def test_falsy_malformed_stored_policy_is_blocked(tmp_path, stored_policy):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: primary-model\n", encoding="utf-8"
    )
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs)
        raise AssertionError("provider resolution must not run")

    job = {
        "id": "invalid-falsy-policy",
        "name": "invalid falsy policy",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": stored_policy,
    }

    (success, _output, _final, error), agent_cls = _run_job_with_resolver(
        job, tmp_path, resolver
    )

    assert success is False
    assert calls == []
    assert agent_cls.called is False
    assert "[blocked_config]" in (error or "")


def test_pinned_create_rejects_chain_without_usable_route(monkeypatch):
    jobs, stored = _isolated_jobs(monkeypatch)
    monkeypatch.setattr(
        jobs,
        "_resolve_current_fallback_chain",
        lambda: [{"provider": "", "model": "", "api_key": "private-value"}],
    )

    with pytest.raises(ValueError, match="configured global fallback route"):
        jobs.create_job(
            prompt="run",
            schedule="every 1 hour",
            fallback_policy="pinned",
        )

    assert stored == []


def test_no_agent_create_ignores_policy_without_resolving_chain(monkeypatch):
    jobs, _stored = _isolated_jobs(monkeypatch)

    def unexpected_resolution():
        raise AssertionError("no_agent create must not read fallback config")

    monkeypatch.setattr(jobs, "_resolve_current_fallback_chain", unexpected_resolution)
    job = jobs.create_job(
        prompt="",
        schedule="every 1 hour",
        script="watchdog.py",
        no_agent=True,
        fallback_policy="pinned",
    )

    assert job["fallback_policy"] == "inherit"
    assert job["fallback_snapshot"] is None


def test_no_agent_update_clears_existing_pinned_policy(monkeypatch):
    jobs, _stored = _isolated_jobs(monkeypatch)
    monkeypatch.setattr(
        jobs,
        "_resolve_current_fallback_chain",
        lambda: [{"provider": "openrouter", "model": "fallback-model"}],
    )
    job = jobs.create_job(
        prompt="run",
        schedule="every 1 hour",
        script="watchdog.py",
        fallback_policy="pinned",
    )

    updated = jobs.update_job(job["id"], {"no_agent": True})

    assert updated["fallback_policy"] == "inherit"
    assert updated["fallback_snapshot"] is None


def test_fallback_snapshot_cannot_be_updated_directly(monkeypatch):
    import pytest

    jobs, _stored = _isolated_jobs(monkeypatch)
    job = jobs.create_job(prompt="run", schedule="every 1 hour", fallback_policy="none")

    with pytest.raises(ValueError, match="fallback_snapshot"):
        jobs.update_job(
            job["id"],
            {"fallback_snapshot": {"fingerprint": "forged", "routes": []}},
        )


def test_update_away_from_pinned_clears_snapshot(monkeypatch):
    jobs, _stored = _isolated_jobs(monkeypatch)
    monkeypatch.setattr(
        jobs,
        "_resolve_current_fallback_chain",
        lambda: [{"provider": "openrouter", "model": "fallback-model"}],
    )
    job = jobs.create_job(
        prompt="run",
        schedule="every 1 hour",
        fallback_policy="pinned",
    )

    updated = jobs.update_job(job["id"], {"fallback_policy": "none"})

    assert updated["fallback_policy"] == "none"
    assert updated["fallback_snapshot"] is None


def test_none_policy_primary_success_gives_agent_no_cross_provider_chain(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "cron:\n"
        "  preflight: false\n"
        "model:\n"
        "  default: primary-model\n"
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: fallback-model\n",
        encoding="utf-8",
    )

    def resolver(**_kwargs):
        return {
            "api_key": "test-key",
            "base_url": "https://primary.invalid/v1",
            "provider": "openai-codex",
            "api_mode": "codex_responses",
        }

    job = {
        "id": "none-success",
        "name": "none success",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": "none",
    }

    (success, _output, final, error), agent_cls = _run_job_with_resolver(
        job, tmp_path, resolver
    )

    assert success is True
    assert error is None
    assert final == "ok"
    assert agent_cls.call_args.kwargs["fallback_model"] is None


def test_pinned_auth_fallback_only_offers_later_routes_to_agent(tmp_path):
    import cron.jobs as cron_jobs
    from hermes_cli.auth import AuthError

    chain = [
        {"provider": "first-fallback", "model": "first-model"},
        {"provider": "second-fallback", "model": "second-model"},
    ]
    (tmp_path / "config.yaml").write_text(
        "cron:\n"
        "  preflight: false\n"
        "model:\n"
        "  default: primary-model\n"
        "fallback_providers:\n"
        "  - provider: first-fallback\n"
        "    model: first-model\n"
        "  - provider: second-fallback\n"
        "    model: second-model\n",
        encoding="utf-8",
    )
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs.get("requested"))
        if kwargs.get("requested") == "openai-codex":
            raise AuthError("primary credential unavailable")
        return {
            "api_key": "test-key",
            "base_url": "https://fallback.invalid/v1",
            "provider": kwargs.get("requested"),
            "api_mode": "chat_completions",
        }

    job = {
        "id": "pinned-auth",
        "name": "pinned auth",
        "prompt": "run",
        "model": "primary-model",
        "provider": "openai-codex",
        "base_url": None,
        "deliver": "local",
        "fallback_policy": "pinned",
        "fallback_snapshot": cron_jobs.snapshot_fallback_chain(chain),
    }

    (success, _output, final, error), agent_cls = _run_job_with_resolver(
        job, tmp_path, resolver
    )

    assert success is True
    assert error is None
    assert final == "ok"
    assert calls == ["openai-codex", "first-fallback"]
    kwargs = agent_cls.call_args.kwargs
    assert kwargs["model"] == "first-model"
    assert kwargs["fallback_model"] == [chain[1]]


def test_pinned_drift_marker_becomes_silent_after_first_alert(tmp_path, monkeypatch):
    import cron.jobs as cron_jobs

    expected_chain = [{"provider": "approved", "model": "approved-model"}]
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: primary-model\n"
        "fallback_providers:\n"
        "  - provider: changed\n"
        "    model: changed-model\n",
        encoding="utf-8",
    )

    def resolver(**_kwargs):
        raise AssertionError("pinned drift must stop before provider resolution")

    with cron_jobs.use_cron_store(tmp_path / "cron-store"):
        monkeypatch.setattr(
            cron_jobs,
            "_resolve_current_fallback_chain",
            lambda: expected_chain,
        )
        job = cron_jobs.create_job(
            prompt="run",
            schedule="every 1 hour",
            provider="openai-codex",
            model="primary-model",
            fallback_policy="pinned",
        )

        first, _agent_cls = _run_job_with_resolver(job, tmp_path, resolver)
        refreshed = cron_jobs.get_job(job["id"])
        assert refreshed is not None
        assert refreshed.get("fallback_drift_alerted") is True
        assert not refreshed.get("drift_alerted")
        second, _agent_cls = _run_job_with_resolver(refreshed, tmp_path, resolver)

    assert "[drift_skip]" in (first[3] or "")
    assert "[drift_skip:silent]" not in (first[3] or "")
    assert "[drift_skip:silent]" in (second[3] or "")
