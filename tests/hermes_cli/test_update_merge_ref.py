"""Tests for ``_merge_upstream_ref`` — the ``hermes update --merge-ref`` core.

``--merge-ref TAG_OR_REF`` fetches one ref/tag from the fixed ``upstream``
remote and merges it into the current fork branch, independent of the
``origin/<branch>`` pull. Its contract is fail-closed: a conflict must abort
the in-progress merge and restore HEAD to EXACTLY the pre-update SHA so no
install/dependency step ever runs on a half-merged tree.

These tests drive the real helper against real temporary git repositories
(no fake-git monkeypatching), so the fetch → resolve → merge → abort → restore
chain is exercised end to end. The annotated-tag case also covers the
``FETCH_HEAD^{commit}`` peel leg that a lightweight-ref test would miss.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli.update_cmd import _merge_upstream_ref

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary required for merge-ref tests"
)

GIT = ["git"]


def _git(args, cwd, check=True):
    return subprocess.run(
        GIT + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    # Local identity + no signing so this runs on a bare CI box with no global
    # git config and no GPG key.
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "commit.gpgsign", "false"], path)


def _commit(path: Path, relpath: str, content: str, message: str) -> str:
    (path / relpath).write_text(content)
    _git(["add", relpath], path)
    _git(["commit", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


def _setup(root: Path):
    """Build a project repo with an ``upstream`` remote offering two refs.

    Returns ``(project, branch, pre_pull_sha)`` where the project has diverged
    from a shared base on ``shared.txt``. The ``upstream`` remote carries:
      * its default branch — diverged on the SAME line (merges → conflict);
      * annotated tag ``v-clean`` — a base-rooted commit that only ADDS a file
        (merges → clean, and forces the annotated-tag peel).
    """
    project = root / "project"
    _init_repo(project)
    base_sha = _commit(project, "shared.txt", "base\n", "base")
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], project).stdout.strip()

    # upstream = clone of project@base (guarantees a common ancestor)
    upstream = root / "upstream"
    _git(["clone", str(project), str(upstream)], root)
    _git(["config", "user.email", "up@example.com"], upstream)
    _git(["config", "user.name", "Up"], upstream)
    _git(["config", "commit.gpgsign", "false"], upstream)

    # A base-rooted, non-conflicting commit → annotated tag v-clean.
    _git(["checkout", "-b", "cleanwork", base_sha], upstream)
    _commit(upstream, "newfile.txt", "clean\n", "clean add")
    _git(["tag", "-a", "v-clean", "-m", "clean ref"], upstream)
    # Upstream's default branch diverges on shared.txt (conflict material).
    _git(["checkout", branch], upstream)
    _commit(upstream, "shared.txt", "upstream change\n", "upstream conflict")

    # Project diverges on the same line, then wires the upstream remote.
    _git(["remote", "add", "upstream", str(upstream)], project)
    pre_pull_sha = _commit(project, "shared.txt", "project change\n", "project change")
    return project, branch, pre_pull_sha


def test_conflict_aborts_and_restores_pre_pull_sha(tmp_path):
    project, branch, pre_pull_sha = _setup(tmp_path)

    result = _merge_upstream_ref(GIT, str(project), branch, branch, pre_pull_sha)

    assert result is False
    # HEAD restored to EXACTLY the pre-update SHA.
    assert _git(["rev-parse", "HEAD"], project).stdout.strip() == pre_pull_sha
    # Working tree clean — no lingering conflict markers / staged state.
    assert _git(["status", "--porcelain"], project).stdout.strip() == ""
    # No merge left in progress.
    assert not (project / ".git" / "MERGE_HEAD").exists()
    # The project's own version survived; upstream's change was not applied.
    assert (project / "shared.txt").read_text() == "project change\n"


def test_clean_merge_of_annotated_tag_succeeds(tmp_path):
    project, branch, pre_pull_sha = _setup(tmp_path)

    result = _merge_upstream_ref(GIT, str(project), "v-clean", branch, pre_pull_sha)

    assert result is True
    # A real 3-way merge happened (HEAD advanced past the pre-pull SHA).
    assert _git(["rev-parse", "HEAD"], project).stdout.strip() != pre_pull_sha
    assert _git(["status", "--porcelain"], project).stdout.strip() == ""
    # Upstream's added file landed; the project's own line is preserved.
    assert (project / "newfile.txt").read_text() == "clean\n"
    assert (project / "shared.txt").read_text() == "project change\n"
