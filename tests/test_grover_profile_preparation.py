from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from grover_runtime.profile_preparation import (
    build_profile_commands,
    run_profile_commands,
)


def test_prepare_commands_create_both_profiles_without_cloning_credentials():
    commands = build_profile_commands("prepare")

    rendered = [" ".join(command) for command in commands]
    assert rendered[:2] == [
        "hermes profile create grover-prod --no-alias --no-skills",
        "hermes profile create grover-shadow --no-alias --no-skills",
    ]
    assert all("--clone" not in command for command in rendered)
    assert all(".env" not in command for command in rendered)
    assert (
        "hermes -p grover-prod config set gateway.platforms.telegram.enabled false"
        in rendered
    )
    assert (
        "hermes -p grover-shadow config set gateway.platforms.telegram.enabled false"
        in rendered
    )
    assert (
        "hermes -p grover-shadow config set "
        "gateway.platforms.telegram.extra.external_effects false" in rendered
    )
    assert (
        "hermes -p grover-shadow plugins enable grover-shadow-guard "
        "--no-allow-tool-override" in rendered
    )


def test_profile_preparation_cannot_bypass_cutover_health_and_receipt_gates():
    with pytest.raises(ValueError, match="cutover requires grover_runtime.operations"):
        build_profile_commands("cutover-prod")


def test_unknown_profile_operation_fails_closed():
    with pytest.raises(ValueError, match="unknown profile operation"):
        build_profile_commands("deploy-everywhere")


def test_existing_profile_aborts_instead_of_reusing_credential_state(monkeypatch):
    monkeypatch.setattr(
        "grover_runtime.profile_preparation.platform.system", lambda: "Darwin"
    )
    run = Mock(
        return_value=SimpleNamespace(
            returncode=1,
            stdout="profile already exists",
            stderr="",
        )
    )
    monkeypatch.setattr("grover_runtime.profile_preparation.subprocess.run", run)

    with pytest.raises(RuntimeError, match="profile already exists"):
        run_profile_commands(build_profile_commands("prepare"))

    assert run.call_count == 1
