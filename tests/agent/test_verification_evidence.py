from __future__ import annotations

from pathlib import Path

import pytest

from agent.verification_evidence import (
    VerificationEvidence,
    classify_verification_command,
    mark_workspace_edited,
    record_terminal_result,
    verification_ledger_enabled,
    verification_status,
)


def test_legacy_evidence_shape_remains_importable() -> None:
    evidence = VerificationEvidence(
        command="opaque",
        canonical_command="opaque",
        kind="legacy",
        scope="legacy",
        status="legacy",
        exit_code=0,
        cwd="/workspace",
        root="/workspace",
        session_id="session",
    )

    assert evidence.command == "opaque"


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"agent": {"verification_ledger_enabled": False}},
        {"agent": {"verification_ledger_enabled": True}},
    ],
)
def test_configuration_cannot_reactivate_semantic_ledger(config) -> None:
    assert verification_ledger_enabled(config) is False


@pytest.mark.parametrize(
    ("command", "exit_code", "output"),
    [
        ("pytest tests/test_passwords.py", 0, "99 passed"),
        ("python -m pytest", 1, "failed"),
        ("pnpm run lint && pnpm test", 0, "green"),
        ("echo pytest tests/test_passwords.py", 0, "pytest tests/test_passwords.py"),
        (r"python C:\\Temp\\hermes-ad-hoc-check.py", 0, "ok"),
        ("rm -rf /", 0, "not actually executed"),
    ],
)
def test_terminal_text_is_opaque_model_tool_data(
    tmp_path: Path,
    command: str,
    exit_code: int,
    output: str,
) -> None:
    assert classify_verification_command(
        command,
        cwd=tmp_path,
        session_id="session",
        exit_code=exit_code,
        output=output,
    ) is None


def test_retired_ledger_never_persists_or_exposes_inferred_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert record_terminal_result(
        command="pytest tests/test_passwords.py",
        cwd=tmp_path,
        session_id="session",
        exit_code=0,
        output="all passed",
    ) is None
    assert mark_workspace_edited(
        session_id="session",
        cwd=tmp_path,
        paths=["README.md", "src/test_passwords.py", "config.yaml"],
    ) is None
    assert verification_status(session_id="session", cwd=tmp_path) == {
        "status": "not_applicable",
        "evidence": None,
    }
    assert not (home / "verification_evidence.db").exists()
