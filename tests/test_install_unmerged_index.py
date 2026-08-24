"""Regression: installers must fail closed on a pre-existing unmerged index.

A previously interrupted update can leave ``$INSTALL_DIR`` with unmerged index
entries (files in a conflicted, "needs merge" state). In that state the update
path's ``git stash`` aborts with "could not write index" and the following
``git checkout <branch>`` aborts with "you need to resolve your current index
first" -- surfacing to GUI/bootstrap users as ``git checkout main failed
(exit 1)`` and failing the whole install at the repository stage.

An unmerged index cannot be represented losslessly by ``git stash``. Both
installers therefore refuse before stash/checkout rather than resetting away
the user's in-progress conflict resolution.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _make_unmerged_repo(repo: Path) -> None:
    """Leave ``repo`` with a conflicted (unmerged) index, as an interrupted
    update would."""
    _git(repo, "init")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    remote = repo.parent / "origin.git"
    _git(repo.parent, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    # Capture the default branch name only after the first commit exists
    # (rev-parse on an unborn HEAD errors).
    start = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    _git(repo, "checkout", "-b", "feature")
    (repo / "f.txt").write_text("feature side\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "feature")

    _git(repo, "checkout", start)
    (repo / "f.txt").write_text("main side\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "mainside")

    # Conflicting merge — exits non-zero and leaves the index unmerged.
    _git(repo, "merge", "feature", check=False)


def _unmerged_snapshot(repo: Path) -> tuple[str, str, str, bytes, str]:
    return (
        _git(repo, "rev-parse", "HEAD").stdout.strip(),
        _git(repo, "status", "--porcelain=v1", "-z").stdout,
        _git(repo, "ls-files", "--stage").stdout,
        (repo / "f.txt").read_bytes(),
        _git(repo, "stash", "list", "--format=%H%x09%gs").stdout,
    )


@pytest.mark.live_system_guard_bypass
def test_install_sh_refuses_unmerged_index_without_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    _make_unmerged_repo(repo)

    # Sanity: this is exactly the state that breaks `git stash` / `git checkout`.
    assert _git(repo, "ls-files", "--unmerged").stdout.strip(), (
        "test setup failed to produce an unmerged index"
    )

    before = _unmerged_snapshot(repo)
    res = subprocess.run(
        [
            "bash",
            "-c",
            'installer=$1; repo=$2; set --; source "$installer"; '
            'update_managed_checkout "$repo" main',
            "unmerged-test",
            str(INSTALL_SH),
            str(repo),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert res.returncode != 0
    assert "unresolved conflicts" in res.stdout
    assert _unmerged_snapshot(repo) == before


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="needs PowerShell",
)
def test_install_ps1_refuses_unmerged_index_without_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    _make_unmerged_repo(repo)
    before = _unmerged_snapshot(repo)
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". '{INSTALL_PS1}'; "
        f"Update-ManagedCheckout -Repo '{repo}' -Branch main"
    )

    res = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert res.returncode != 0
    assert _unmerged_snapshot(repo) == before


def test_install_ps1_stops_venv_resident_processes_before_parking_venv() -> None:
    """The Windows venv-recreate path must stop every process running out of the
    old venv before moving it aside.

    A gateway autostarted by a scheduled task runs as
    ``venv\\Scripts\\pythonw.exe -m hermes_cli.main gateway run`` — image name
    ``pythonw``, not ``hermes.exe`` — so the ``taskkill /IM hermes.exe`` guard
    misses it and the loaded ``.pyd`` stays locked (issues #47036/#47557/#47910).
    The recreate branch must sweep by venv path prefix before Rename-Item, and
    must never fall back to an in-place ``Remove-Item`` of the live ``venv``
    (#83149 — that path can gut site-packages with no rollback).
    """
    text = INSTALL_PS1.read_text()

    # The hermes.exe tree-kill is preserved (kills spawned child processes too).
    assert 'taskkill /F /T /IM hermes.exe' in text

    # The venv path-prefix sweep exists. It must match by case-insensitive
    # StartsWith, NOT PowerShell -like: a venv path containing wildcard
    # metacharacters ('[', ']') — legal in a Windows user name — silently fails
    # to match under -like, reintroducing the exact miss this fix closes.
    idx_recreate = text.index("Virtual environment already exists, recreating")
    idx_sweep = text.index("StartsWith($venvPrefix", idx_recreate)
    assert "[System.StringComparison]::OrdinalIgnoreCase" in text[idx_sweep:idx_sweep + 200]
    assert 'ExecutablePath -like "$venvRoot' not in text, (
        "the -like wildcard match must not be used for venv path scoping"
    )

    # The process sweep must run before the venv is parked, or it is a no-op.
    idx_park = text.index('Rename-Item -LiteralPath "venv"', idx_recreate)
    assert idx_sweep < idx_park, (
        "venv-resident processes must be stopped before Rename-Item parks the venv"
    )
    assert 'Remove-Item -Recurse -Force "venv"' not in text[idx_recreate:], (
        "must not fall back to in-place delete of the live venv (#83149)"
    )
    assert "Could not move the existing venv aside" in text[idx_recreate:]
