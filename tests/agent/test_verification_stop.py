from __future__ import annotations

import pytest

from agent.verification_stop import (
    build_verify_on_stop_nudge,
    verify_on_stop_enabled,
)


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"agent": {"verify_on_stop": False}},
        {"agent": {"verify_on_stop": True}},
        {"agent": {"verify_on_stop": "auto"}},
    ],
)
def test_configuration_cannot_reactivate_host_completion_override(config) -> None:
    assert verify_on_stop_enabled(config) is False


def test_environment_cannot_reactivate_host_completion_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

    assert verify_on_stop_enabled() is False


@pytest.mark.parametrize(
    "changed_paths",
    [
        ["README.md"],
        ["src/test_password_rotation.py"],
        ["config.yaml", "scripts/deploy.sh"],
        ["LICENSE", "package.json", "tests/test_feature.py"],
    ],
)
def test_filenames_never_trigger_host_authored_verification(
    changed_paths: list[str],
) -> None:
    assert build_verify_on_stop_nudge(
        session_id="session",
        changed_paths=changed_paths,
    ) is None


def test_retired_nudge_does_not_even_iterate_opaque_paths() -> None:
    class _OpaquePaths:
        def __iter__(self):
            raise AssertionError("host attempted to classify changed paths")

    assert build_verify_on_stop_nudge(
        session_id="session",
        changed_paths=_OpaquePaths(),
        attempts=0,
        max_attempts=99,
    ) is None
