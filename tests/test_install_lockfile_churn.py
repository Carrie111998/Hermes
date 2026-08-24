"""Managed installer lockfile normalization against real repositories."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _managed_checkout(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "package.json").write_text('{"dependencies":{"a":"1"}}\n')
    (origin / "package-lock.json").write_text('{"lock":"old"}\n')
    _git(origin, "add", "package.json", "package-lock.json")
    _git(origin, "commit", "-m", "init")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _git(checkout, "config", "user.email", "t@t")
    _git(checkout, "config", "user.name", "t")
    return checkout


def _run_sh(checkout: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'installer=$1; repo=$2; set --; source "$installer"; '
            'update_managed_checkout "$repo" main',
            "lockfile-test",
            str(INSTALL_SH),
            str(checkout),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)
def test_install_sh_preserves_unpaired_lockfile_edit_transactionally(
    tmp_path: Path,
) -> None:
    checkout = _managed_checkout(tmp_path)
    (checkout / "package-lock.json").write_text('{"lock":"runtime-churn"}\n')

    result = _run_sh(checkout)

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Discarded npm lockfile churn" not in result.stdout
    assert "hermes-install-autostash-" in _git(checkout, "stash", "list").stdout
    assert (checkout / "package-lock.json").read_text() == '{"lock":"runtime-churn"}\n'


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)
def test_install_sh_preserves_intentional_package_and_lockfile_edits(
    tmp_path: Path,
) -> None:
    checkout = _managed_checkout(tmp_path)
    (checkout / "package.json").write_text('{"dependencies":{"a":"2"}}\n')
    (checkout / "package-lock.json").write_text('{"lock":"intentional"}\n')

    result = _run_sh(checkout)

    assert result.returncode == 0, result.stderr + result.stdout
    assert (checkout / "package.json").read_text() == '{"dependencies":{"a":"2"}}\n'
    assert (checkout / "package-lock.json").read_text() == '{"lock":"intentional"}\n'
    stashes = _git(checkout, "stash", "list").stdout
    assert "hermes-install-autostash-" in stashes


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    shutil.which("git") is None or POWERSHELL is None,
    reason="needs git and PowerShell",
)
def test_install_ps1_preserves_unpaired_lockfile_edit_transactionally(
    tmp_path: Path,
) -> None:
    checkout = _managed_checkout(tmp_path)
    (checkout / "package-lock.json").write_text('{"lock":"runtime-churn"}\n')
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". '{INSTALL_PS1}'; "
        f"Update-ManagedCheckout -Repo '{checkout}' -Branch main"
    )

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "hermes-install-autostash-" in _git(checkout, "stash", "list").stdout
    assert (checkout / "package-lock.json").read_text() == '{"lock":"runtime-churn"}\n'
