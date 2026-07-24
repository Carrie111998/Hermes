from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.fleet.adapters.base import safe_child_environment
from hermes_cli.fleet.adapters.external_cli import ExternalCliAdapter
from hermes_cli.fleet.adapters.native_provider import NativeProviderAdapter
from hermes_cli.fleet.types import (
    AdapterKind,
    AdapterRequest,
    LaneProfile,
    Qualification,
    ReasonCode,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _profile(kind: AdapterKind = AdapterKind.NATIVE_PROVIDER) -> LaneProfile:
    return LaneProfile(
        lane_id="test_lane",
        order=0,
        adapter_kind=kind,
        provider_id="subscription-provider",
        ordered_models=("m1", "m2"),
        supported_efforts=("low", "high"),
        capabilities=frozenset({"workspace_write", "shell"}),
        allowed_auth_kinds=frozenset(
            {"oauth_subscription", "cli_subscription"}
        ),
        executable=sys.executable if kind is AdapterKind.EXTERNAL_CLI else None,
    )


def _qualification(
    profile: LaneProfile, auth_kind: str = "oauth_subscription"
) -> Qualification:
    return Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        auth_kind=auth_kind,
        auth_source="subscription-store",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        executable=profile.executable,
        version="1.0.0",
        evidence_id="qualification:test",
    )


def _request(profile: LaneProfile) -> AdapterRequest:
    return AdapterRequest(
        task_id="task-1",
        cwd=Path.cwd(),
        prompt="perform the bounded task",
        profile=profile,
        model="m1",
        effort="low",
        timeout_seconds=2,
    )


def test_safe_child_environment_allowlists_runtime_and_scrubs_credentials():
    env = safe_child_environment(
        {
            "PATH": "safe-path",
            "USERPROFILE": "C:/Users/test",
            "OPENAI_API_KEY": "canary-openai",
            "ANTHROPIC_API_KEY": "canary-anthropic",
            "XAI_API_KEY": "canary-xai",
            "RANDOM_SECRET": "canary-secret",
        }
    )

    assert env["PATH"] == "safe-path"
    assert env["USERPROFILE"] == "C:/Users/test"
    assert not any("canary" in value for value in env.values())
    assert not any("API_KEY" in key or "SECRET" in key for key in env)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda q: replace(q, auth_kind="api_key"),
            ReasonCode.AUTH_KIND_FORBIDDEN,
        ),
        (
            lambda q: replace(q, auth_source=None),
            ReasonCode.AUTH_SOURCE_UNKNOWN,
        ),
        (
            lambda q: replace(q, overage_disabled=True),
            None,
        ),
        (
            lambda q: replace(q, overage_disabled=False),
            ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON,
        ),
        (
            lambda q: replace(q, overage_disabled=None),
            ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON,
        ),
        (
            lambda q: replace(q, fast_off_supported=False),
            ReasonCode.QUALIFICATION_FAILED,
        ),
    ],
)
def test_native_adapter_enforces_subscription_billing_and_fast_off(
    mutate, reason
):
    calls = []
    profile = _profile()
    qualification = mutate(_qualification(profile))
    adapter = NativeProviderAdapter(lambda **kwargs: calls.append(kwargs) or {
        "ok": True,
        "provider_id": profile.provider_id,
        "model_id": "m1",
        "auth_kind": "oauth_subscription",
        "output": "done",
    })

    result = adapter.execute(_request(profile), qualification)

    if reason is None:
        assert result.ok
        assert calls[0]["fallback_enabled"] is False
        assert calls[0]["fast_mode"] is False
    else:
        assert not result.ok
        assert result.reason is reason
        assert calls == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("provider_id", "fallback-provider", ReasonCode.PROVIDER_MISMATCH),
        ("model_id", "fallback-model", ReasonCode.MODEL_MISMATCH),
        ("auth_kind", "api_key", ReasonCode.CREDENTIAL_MISMATCH),
    ],
)
def test_native_adapter_rejects_runner_provenance_mismatch(field, value, reason):
    profile = _profile()
    payload = {
        "ok": True,
        "provider_id": profile.provider_id,
        "model_id": "m1",
        "auth_kind": "oauth_subscription",
        "output": "done",
    }
    payload[field] = value
    adapter = NativeProviderAdapter(lambda **_: payload)

    result = adapter.execute(_request(profile), _qualification(profile))

    assert not result.ok
    assert result.reason is reason


def _fake_cli(tmp_path: Path) -> Path:
    directory = tmp_path / "worker & safe"
    directory.mkdir()
    script = directory / "fake worker.py"
    script.write_text(
        """
import json
import os
import sys
import time

mode = sys.argv[1]
if mode == "sleep":
    time.sleep(5)
if mode == "malformed":
    print("not-json")
    raise SystemExit(0)
if mode == "run":
    data = {
        "schema_version": "1",
        "ok": True,
        "provider_id": "subscription-provider",
        "model_id": sys.argv[sys.argv.index("--model") + 1],
        "auth_kind": "cli_subscription",
        "output": sys.stdin.read(),
        "credential_keys": sorted(
            key for key in os.environ
            if "API_KEY" in key or "SECRET" in key or "TOKEN" in key
        ),
    }
    print(json.dumps(data))
""".strip(),
        encoding="utf-8",
    )
    return script


def test_external_adapter_uses_argv_stdin_bounded_env_and_machine_json(tmp_path):
    script = _fake_cli(tmp_path)
    profile = replace(
        _profile(AdapterKind.EXTERNAL_CLI), executable=sys.executable
    )
    adapter = ExternalCliAdapter(
        sys.executable,
        fixed_args=(str(script), "run"),
        base_environment={
            "PATH": "safe",
            "OPENAI_API_KEY": "canary",
            "RANDOM_SECRET": "canary",
        },
    )
    qualification = replace(
        _qualification(profile, "cli_subscription"),
        executable=str(Path(sys.executable).resolve()),
    )

    result = adapter.execute(_request(profile), qualification)

    assert result.ok
    assert result.output == "perform the bounded task"
    assert result.metadata["credential_keys"] == []


@pytest.mark.parametrize(
    ("mode", "timeout", "cancelled", "reason"),
    [
        ("malformed", 2, False, ReasonCode.MALFORMED_OUTPUT),
        ("sleep", 1, False, ReasonCode.EXECUTION_TIMEOUT),
        ("run", 2, True, ReasonCode.EXECUTION_CANCELLED),
    ],
)
def test_external_adapter_fails_closed_for_bad_output_timeout_or_cancellation(
    tmp_path, mode, timeout, cancelled, reason
):
    script = _fake_cli(tmp_path)
    profile = replace(
        _profile(AdapterKind.EXTERNAL_CLI), executable=sys.executable
    )
    adapter = ExternalCliAdapter(
        sys.executable,
        fixed_args=(str(script), mode),
        cancelled=lambda: cancelled,
    )
    request = replace(_request(profile), timeout_seconds=timeout)
    qualification = replace(
        _qualification(profile, "cli_subscription"),
        executable=str(Path(sys.executable).resolve()),
    )

    result = adapter.execute(request, qualification)

    assert not result.ok
    assert result.reason is reason


def test_external_adapter_rejects_executable_substitution_before_launch(tmp_path):
    script = _fake_cli(tmp_path)
    profile = replace(
        _profile(AdapterKind.EXTERNAL_CLI), executable=sys.executable
    )
    adapter = ExternalCliAdapter(
        sys.executable, fixed_args=(str(script), "run")
    )
    qualification = replace(
        _qualification(profile, "cli_subscription"),
        executable=str(tmp_path / "different.exe"),
    )

    result = adapter.execute(_request(profile), qualification)

    assert not result.ok
    assert result.reason is ReasonCode.QUALIFICATION_FAILED
