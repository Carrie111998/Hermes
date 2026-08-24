"""Native Windows integration tests for the SSH runtime trust boundary."""

from __future__ import annotations

import subprocess

import pytest

import hermes_cli.windows_ssh_runtime as runtime


pytestmark = pytest.mark.windows_only


def _junction(link, target):
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create junction: {result.stdout} {result.stderr}")


def _payload(hermes_path):
    return {
        "ownershipId": "0123456789abcdef0123456789abcdef",
        "spawnNonce": "0123456789abcdef",
        "hermesPath": str(hermes_path),
    }


def test_spawn_rejects_reparse_hermes_before_interpreter_selection(tmp_path, monkeypatch):
    target = tmp_path / "external-hermes"
    target.mkdir()
    hermes = tmp_path / "hermes.exe"
    _junction(hermes, target)

    monkeypatch.setattr(
        runtime,
        "_resolve_direct_interpreter",
        lambda *_args: pytest.fail("reparse Hermes path reached interpreter selection"),
    )

    try:
        with pytest.raises(ValueError, match="link or reparse point"):
            runtime.spawn_backend(_payload(hermes))
    finally:
        subprocess.run(
            ["cmd.exe", "/d", "/c", "rmdir", str(hermes)],
            capture_output=True,
            check=False,
        )


def test_spawn_rejects_reparse_python_entry_before_interpreter_selection(
    tmp_path, monkeypatch
):
    hermes = tmp_path / "hermes.exe"
    hermes.write_bytes(b"placeholder")
    target = tmp_path / "external-python"
    target.mkdir()
    python_entry = tmp_path / "python.exe"
    _junction(python_entry, target)

    monkeypatch.setattr(
        runtime,
        "_resolve_direct_interpreter",
        lambda *_args: pytest.fail("reparse interpreter reached interpreter selection"),
    )

    try:
        with pytest.raises(ValueError, match="link or reparse point"):
            runtime.spawn_backend(_payload(hermes))
    finally:
        subprocess.run(
            ["cmd.exe", "/d", "/c", "rmdir", str(python_entry)],
            capture_output=True,
            check=False,
        )
