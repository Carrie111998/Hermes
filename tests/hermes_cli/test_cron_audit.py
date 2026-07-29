"""Tests for ``hermes cron audit-models``."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.cron_audit import audit_cron_models


def _job(
    job_id="job-1",
    *,
    model=None,
    provider=None,
    model_snapshot=None,
    provider_snapshot=None,
    no_agent=False,
    enabled=True,
    state=None,
):
    result = {"id": job_id, "name": f"Job {job_id}", "enabled": enabled}
    if state is not None:
        result["state"] = state
    for key, value in (
        ("model", model),
        ("provider", provider),
        ("model_snapshot", model_snapshot),
        ("provider_snapshot", provider_snapshot),
    ):
        if value is not None:
            result[key] = value
    if no_agent:
        result["no_agent"] = True
    return result


def _audit(
    jobs,
    config,
    *,
    resolved_model: str | None = "global-model",
    resolved_provider: str | None = "global-provider",
):
    with (
        patch("cron.jobs.load_jobs", return_value=jobs),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("cron.jobs._resolve_default_model_snapshot", return_value=resolved_model),
        patch(
            "cron.jobs._compute_provider_model_snapshots",
            return_value=(resolved_provider, None),
        ),
    ):
        return json.loads(audit_cron_models(json_output=True))


@pytest.mark.parametrize(
    ("job", "status"),
    [
        (_job(model="m", provider="p"), "pinned"),
        (_job(model="m"), "partial"),
        (_job(provider="p"), "partial"),
        (_job(), "inherited"),
        (_job(model="m", provider="p", no_agent=True), "script-only"),
    ],
)
def test_pinning_status_is_separate_from_drift(job, status):
    data = _audit([job], {"model": {"default": "global-model", "provider": "global-provider"}})
    assert data["jobs"][0]["status"] == status


def test_inherited_job_with_matching_snapshots_is_guarded_not_at_risk():
    data = _audit(
        [_job(model_snapshot="global-model", provider_snapshot="global-provider")],
        {"model": {"default": "global-model", "provider": "global-provider"}},
    )
    result = data["jobs"][0]
    assert result["guarded_axes"] == ["model", "provider"]
    assert result["drifted_axes"] == []
    assert result["at_risk"] is False


def test_drifted_snapshot_is_reported_as_scheduler_skip_risk():
    data = _audit(
        [_job(model_snapshot="old-model", provider_snapshot="old-provider")],
        {"model": {"default": "new-model", "provider": "new-provider"}},
        resolved_model="new-model",
        resolved_provider="new-provider",
    )
    result = data["jobs"][0]
    assert result["drifted_axes"] == ["model", "provider"]
    assert result["at_risk"] is True


@pytest.mark.parametrize(
    "job",
    [
        _job(
            "disabled",
            enabled=False,
            model_snapshot="old-model",
            provider_snapshot="old-provider",
        ),
        _job(
            "paused",
            state="paused",
            model_snapshot="old-model",
            provider_snapshot="old-provider",
        ),
    ],
)
def test_inactive_drift_is_not_counted_as_would_skip_now(job):
    data = _audit(
        [job],
        {"model": {"default": "new-model", "provider": "new-provider"}},
        resolved_model="new-model",
        resolved_provider="new-provider",
    )
    result = data["jobs"][0]
    assert result["active"] is False
    assert result["drifted_axes"] == ["model", "provider"]
    assert result["at_risk"] is False


def test_hermes_model_is_used_when_config_has_no_model_default(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL", "env-model")
    data = _audit(
        [_job(model_snapshot="old-model")],
        {"model": {"provider": "global-provider"}},
        resolved_model=None,
        resolved_provider="global-provider",
    )
    result = data["jobs"][0]
    assert data["effective_default_model"] == "env-model"
    assert result["effective_model"] == "env-model"
    assert result["drifted_axes"] == ["model"]
    assert result["at_risk"] is True


def test_disabled_guard_does_not_claim_job_will_fail():
    data = _audit(
        [_job(model_snapshot="old-model", provider_snapshot="old-provider")],
        {
            "model": {"default": "new-model", "provider": "new-provider"},
            "cron": {"model_drift_guard": False},
        },
        resolved_model="new-model",
        resolved_provider="new-provider",
    )
    result = data["jobs"][0]
    assert result["at_risk"] is False
    assert result["unprotected_axes"] == ["model", "provider"]


def test_pre_snapshot_job_is_unprotected_not_claimed_to_fail():
    data = _audit(
        [_job()],
        {"model": {"default": "new-model", "provider": "new-provider"}},
        resolved_model="new-model",
        resolved_provider="new-provider",
    )
    result = data["jobs"][0]
    assert result["at_risk"] is False
    assert result["unprotected_axes"] == ["model", "provider"]


def test_cron_fleet_defaults_cover_unpinned_axes_and_provider_precedence():
    data = _audit(
        [_job(model_snapshot="old-model", provider_snapshot="old-provider")],
        {
            "model": {"default": "chat-model", "provider": "chat-provider"},
            "cron": {"model": "cron-model", "model_provider": "cron-provider"},
        },
    )
    result = data["jobs"][0]
    assert data["effective_default_model"] == "cron-model"
    assert data["effective_default_provider"] == "cron-provider"
    assert result["effective_model"] == "cron-model"
    assert result["effective_provider"] == "cron-provider"
    assert result["guarded_axes"] == []
    assert result["at_risk"] is False


def test_pinned_axis_is_not_evaluated_for_drift():
    data = _audit(
        [_job(model="pinned", model_snapshot="old", provider_snapshot="global-provider")],
        {"model": {"default": "new", "provider": "global-provider"}},
        resolved_model="new",
    )
    result = data["jobs"][0]
    assert "model" not in result["guarded_axes"]
    assert result["effective_model"] == "pinned"


def test_json_has_machine_readable_safety_fields_without_icons():
    data = _audit([_job()], {"model": {"default": "m", "provider": "p"}}, resolved_model="m", resolved_provider="p")
    result = data["jobs"][0]
    assert "status_icon" not in result
    assert set(
        [
            "effective_model",
            "effective_provider",
            "model_snapshot",
            "provider_snapshot",
            "guarded_axes",
            "drifted_axes",
            "unprotected_axes",
            "at_risk",
        ]
    ).issubset(result)


def test_table_uses_real_edit_command_and_precise_warning():
    with (
        patch("cron.jobs.load_jobs", return_value=[_job(model_snapshot="old")]),
        patch(
            "hermes_cli.config.load_config",
            return_value={"model": {"default": "new", "provider": "p"}},
        ),
        patch("cron.jobs._resolve_default_model_snapshot", return_value="new"),
        patch(
            "cron.jobs._compute_provider_model_snapshots",
            return_value=("p", None),
        ),
    ):
        text = audit_cron_models()
    assert "would skip" in text.lower()
    assert "hermes cron edit <id>" in text
    assert "silently fail" not in text
    assert "hermes cron update" not in text


def test_real_profile_store_and_config_resolution(tmp_path, monkeypatch):
    """Exercise actual config + jobs resolution from a temporary HERMES_HOME."""
    home = tmp_path / "profile"
    (home / "cron").mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"default": "chat-model", "provider": "chat-provider"},
                "cron": {
                    "model": "cron-model",
                    "model_provider": "cron-provider",
                    "model_drift_guard": True,
                },
            }
        ),
        encoding="utf-8",
    )
    (home / "cron" / "jobs.json").write_text(
        json.dumps(
            [
                _job(
                    "real-job",
                    model_snapshot="old-model",
                    provider_snapshot="old-provider",
                )
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    data = json.loads(audit_cron_models(json_output=True))
    assert data["effective_default_model"] == "cron-model"
    assert data["effective_default_provider"] == "cron-provider"
    assert data["jobs"][0]["id"] == "real-job"
    assert data["jobs"][0]["at_risk"] is False


def test_parser_and_handler_dispatch(capsys):
    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=lambda _args: None)
    args = parser.parse_args(["cron", "audit-models", "--json"])
    assert args.cron_command == "audit-models"
    assert args.json_output is True

    with patch(
        "hermes_cli.cron_audit.audit_cron_models", return_value='{"jobs": []}'
    ) as audit:
        assert cron_command(args) == 0
    assert json.loads(capsys.readouterr().out) == {"jobs": []}
    audit.assert_called_once_with(json_output=True)
