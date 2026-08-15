"""Regression: ``install.sh --commit`` must fail closed (#87268).

An abbreviated (or otherwise invalid) SHA used to fall through the whole
install: the by-SHA fetch was swallowed by ``|| true`` (the remote refuses
abbreviated refs), the following ``git checkout --detach`` misparsed the
unknown name as a pathspec, and the installer still printed success and
exited 0 — leaving the user on the branch tip while believing they were
pinned. These tests pin both halves of the fix:

- argument parsing rejects anything that is not a full 40-char hex SHA;
- the pin block aborts the install when the target cannot be fetched or
  checked out, instead of continuing unpinned.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)

_VALID_SHA = "0123456789abcdef0123456789abcdef01234567"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _extract_case_branch() -> str:
    """Pull the ``--commit)`` case branch out of install.sh's arg parsing."""
    text = INSTALL_SH.read_text()
    match = re.search(
        r"--commit\|-Commit\)(?:(?!^\s+--)[^\n]*\n)+?^\s+;;",
        text,
        re.MULTILINE,
    )
    assert match is not None, "--commit case branch not found in install.sh"
    return match.group(0)


def _extract_pin_block() -> str:
    """Pull the commit-pin block out of install.sh's update_repo()."""
    text = INSTALL_SH.read_text()
    match = re.search(
        r'if \[ -n "\$INSTALL_COMMIT" \]; then.*?\n    fi\n',
        text,
        re.DOTALL,
    )
    assert match is not None, "commit-pin block not found in install.sh"
    return match.group(0)


def _run_parse_branch(*args: str) -> subprocess.CompletedProcess:
    """Execute install.sh's --commit branch inside a minimal case loop."""
    script = "\n".join(
        [
            "INSTALL_COMMIT=''",
            "while [[ $# -gt 0 ]]; do",
            "    case $1 in",
            _extract_case_branch(),
            "        *) shift ;;",
            "    esac",
            "done",
            'echo "INSTALL_COMMIT=$INSTALL_COMMIT"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", script, "--", *args],
        capture_output=True,
        text=True,
    )


def _run_pin_block(repo_dir: Path, commit: str) -> subprocess.CompletedProcess:
    """Execute install.sh's pin block standalone, like the real script (no set -e)."""
    script = "\n".join(
        [
            "log_info() { echo \"INFO $*\"; }",
            "log_warn() { echo \"WARN $*\"; }",
            "log_error() { echo \"ERROR $*\"; }",
            f'INSTALL_COMMIT="{commit}"',
            "FORCE_COMMIT=false",
            f'cd "{repo_dir}"',
            _extract_pin_block(),
            'echo "PIN_BLOCK_COMPLETED"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )


class TestCommitArgumentValidation:
    def test_abbreviated_sha_rejected(self):
        result = _run_parse_branch("--commit", "56e2ba5")
        assert result.returncode == 1
        assert "40-character" in result.stdout

    def test_non_hex_sha_rejected(self):
        result = _run_parse_branch("--commit", "z" * 40)
        assert result.returncode == 1
        assert "40-character" in result.stdout

    def test_missing_value_rejected(self):
        result = _run_parse_branch("--commit")
        assert result.returncode == 1
        assert "40-character" in result.stdout

    def test_valid_sha_accepted_and_lowercased(self):
        result = _run_parse_branch("--commit", _VALID_SHA.upper())
        assert result.returncode == 0
        assert f"INSTALL_COMMIT={_VALID_SHA}" in result.stdout


class TestPinFailsClosed:
    @pytest.fixture
    def repo(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _git(repo_dir, "init", "-q", "-b", "main")
        (repo_dir / "f.txt").write_text("rev0\n")
        _git(repo_dir, "add", "f.txt")
        _git(repo_dir, "commit", "-qm", "rev0")
        return repo_dir

    def test_unfetchable_pin_aborts_instead_of_continuing(self, repo):
        """The reported bug: fetch fails, install used to continue unpinned."""
        head_before = _git(repo, "rev-parse", "HEAD")

        result = _run_pin_block(repo, _VALID_SHA)

        assert result.returncode == 1, "an unfetchable pin must abort the install"
        assert "PIN_BLOCK_COMPLETED" not in result.stdout
        assert "Failed to fetch pinned commit" in result.stdout
        assert _git(repo, "rev-parse", "HEAD") == head_before

    def test_fetchable_pin_still_checks_out(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")

        result = _run_pin_block(repo, sha)

        assert result.returncode == 0
        assert "PIN_BLOCK_COMPLETED" in result.stdout
        assert _git(repo, "rev-parse", "HEAD") == sha
