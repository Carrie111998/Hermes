"""Fail-closed, exact-order runtime preparation and cutover tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grover_runtime.operations import (
    CommandResult,
    CutoverCommands,
    CutoverRolledBack,
    CutoverSpec,
    RuntimeLayout,
    SafetyViolation,
    execute_cutover,
    sanitize_runtime_environment,
    validate_runtime_layout,
)


def _commands() -> CutoverCommands:
    return CutoverCommands(
        probe=("probe",),
        disable_new=("disable-new",),
        stop_old=("stop-old",),
        enable_new=("enable-new",),
        start_new=("start-new",),
        stop_new=("stop-new",),
        start_old=("start-old",),
        health_new=("health-new",),
        health_old=("health-old",),
        receipt=("receipt",),
    )


class StatefulRunner:
    def __init__(
        self, *, state: str = "old", fail_health_new: bool = False, receipt=None
    ):
        self.state = state
        self.fail_health_new = fail_health_new
        self.receipt = receipt or {
            "delivered": True,
            "provider": "telegram",
            "provider_message_id": "777",
            "identity_source": "provider",
            "release_id": "release-1",
        }
        self.events: list[str] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, argv, *, env):
        op = argv[0]
        self.events.append(op)
        self.environments.append(dict(env))
        if op == "probe":
            ids = {
                "old": ["legacy"],
                "new": ["grover-prod"],
                "none": [],
                "duplicate": ["legacy", "grover-prod"],
            }[self.state]
            return CommandResult(0, json.dumps({"pollers": ids}), "")
        if op == "disable-new":
            return CommandResult(0, "", "")
        if op == "stop-old":
            self.state = "none"
            return CommandResult(0, "", "")
        if op == "enable-new":
            return CommandResult(0, "", "")
        if op == "start-new":
            self.state = "new"
            return CommandResult(0, "", "")
        if op == "stop-new":
            self.state = "none"
            return CommandResult(0, "", "")
        if op == "start-old":
            self.state = "old"
            return CommandResult(0, "", "")
        if op == "health-new":
            payload = {
                "healthy": not self.fail_health_new,
                "consumer_id": "grover-prod",
                "release_id": "release-1",
            }
            return CommandResult(0, json.dumps(payload), "")
        if op == "health-old":
            return CommandResult(
                0,
                json.dumps({"healthy": True, "consumer_id": "legacy"}),
                "",
            )
        if op == "receipt":
            return CommandResult(0, json.dumps(self.receipt), "")
        raise AssertionError(op)


def _spec() -> CutoverSpec:
    return CutoverSpec(
        release_id="release-1",
        old_consumer_id="legacy",
        new_consumer_id="grover-prod",
        commands=_commands(),
        prod_home=Path.cwd() / ".test-grover-prod",
    )


def test_cutover_has_explicit_zero_then_one_consumer_order_and_receipt_gate():
    runner = StatefulRunner()

    result = execute_cutover(_spec(), runner, base_environment={"PATH": "safe"})

    assert result.already_active is False
    assert result.provider_message_id == "777"
    assert runner.state == "new"
    assert all(
        environment["HERMES_HOME"] == str(_spec().prod_home)
        for environment in runner.environments
    )
    assert runner.events == [
        "probe",
        "disable-new",
        "probe",
        "stop-old",
        "probe",
        "enable-new",
        "start-new",
        "probe",
        "health-new",
        "receipt",
    ]


def test_cutover_is_idempotent_when_exact_new_consumer_is_already_healthy():
    runner = StatefulRunner(state="new")

    result = execute_cutover(_spec(), runner, base_environment={})

    assert result.already_active is True
    assert runner.events == ["probe", "health-new", "receipt"]


def test_health_failure_rolls_back_only_after_new_is_stopped_and_disabled():
    runner = StatefulRunner(fail_health_new=True)

    with pytest.raises(CutoverRolledBack, match="new runtime health gate failed"):
        execute_cutover(_spec(), runner, base_environment={})

    assert runner.state == "old"
    assert runner.events[-6:] == [
        "stop-new",
        "disable-new",
        "probe",
        "start-old",
        "probe",
        "health-old",
    ]


def test_rollback_recovers_when_stop_new_mutates_state_then_reports_failure():
    class MutatingFailureRunner(StatefulRunner):
        def __call__(self, argv, *, env):
            if argv[0] == "stop-new":
                self.events.append("stop-new")
                self.state = "none"
                return CommandResult(1, "", "reported failure after stop")
            return super().__call__(argv, env=env)

    runner = MutatingFailureRunner(fail_health_new=True)

    with pytest.raises(CutoverRolledBack, match="new runtime health gate failed"):
        execute_cutover(_spec(), runner, base_environment={})

    assert runner.state == "old"
    assert runner.events[-6:] == [
        "stop-new",
        "disable-new",
        "probe",
        "start-old",
        "probe",
        "health-old",
    ]


def test_missing_provider_native_identity_fails_and_rolls_back():
    runner = StatefulRunner(
        receipt={
            "delivered": True,
            "provider": "telegram",
            "identity_source": "provider",
            "release_id": "release-1",
        }
    )

    with pytest.raises(CutoverRolledBack, match="provider-native message identity"):
        execute_cutover(_spec(), runner, base_environment={})

    assert runner.state == "old"


def test_duplicate_pollers_fail_before_any_mutating_command():
    runner = StatefulRunner(state="duplicate")

    with pytest.raises(SafetyViolation, match="exactly one known Telegram consumer"):
        execute_cutover(_spec(), runner, base_environment={})

    assert runner.events == ["probe"]


def test_runtime_layout_separates_profiles_credentials_and_release(tmp_path: Path):
    prod = tmp_path / "profiles" / "grover-prod"
    shadow = tmp_path / "profiles" / "grover-shadow"
    release = tmp_path / "releases"
    prod.mkdir(parents=True)
    shadow.mkdir(parents=True)
    release.mkdir()
    (shadow / ".env").write_text("# deliberately empty\n", encoding="utf-8")

    layout = RuntimeLayout(prod_home=prod, shadow_home=shadow, release_root=release)
    validate_runtime_layout(layout)

    (shadow / ".env").write_text("TELEGRAM_BOT_TOKEN=accidental\n", encoding="utf-8")
    with pytest.raises(SafetyViolation, match="shadow credential file is not empty"):
        validate_runtime_layout(layout)


def test_runtime_layout_rejects_shared_or_nested_homes(tmp_path: Path):
    prod = tmp_path / "same"
    prod.mkdir()
    with pytest.raises(SafetyViolation, match="must be disjoint"):
        validate_runtime_layout(
            RuntimeLayout(prod_home=prod, shadow_home=prod, release_root=tmp_path / "r")
        )


def test_runtime_environment_does_not_inherit_credentials(tmp_path: Path):
    env = sanitize_runtime_environment(
        {
            "PATH": "safe-path",
            "HOME": "safe-home",
            "TELEGRAM_BOT_TOKEN": "secret",
            "OPENAI_API_KEY": "secret",
            "AWS_SESSION_TOKEN": "secret",
            "UNRELATED_SECRET": "secret",
        },
        profile="grover-shadow",
        home=tmp_path / "shadow",
        role="shadow",
    )

    assert env["PATH"] == "safe-path"
    assert env["HERMES_PROFILE"] == "grover-shadow"
    assert env["GROVER_RUNTIME_ROLE"] == "shadow"
    assert not any(
        marker in key.upper() for key in env for marker in ("TOKEN", "KEY", "SECRET")
    )
