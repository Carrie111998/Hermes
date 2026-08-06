from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

from hermes_cli import web_git


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_push_approval_survives_restart_without_plaintext_local_authority(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    remote.mkdir()
    repo.mkdir()
    _git(remote, "init", "--bare", "-q")
    _git(repo, "init", "-b", "feature", "-q")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "push", "-qu", "origin", "feature")
    (repo / "file.txt").write_text("base\nnext\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", "next")

    request = web_git.review_create_push_request(str(repo), now=100)
    decision = {**request, "approved": True, "approvedBy": "human-1"}

    database = home / "workspace-push-requests.db"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    raw = database.read_bytes()
    assert str(repo).encode() not in raw
    assert str(remote).encode() not in raw

    # Simulate a web process restart: the process-memory registry is gone.
    web_git._push_approvals.clear()
    result = web_git.review_push_approved_by_request_id(decision, now=101)
    assert result == {"commitSha": request["commitSha"], "ok": True}
    assert _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}") == "origin/feature"
    assert _git(remote, "rev-parse", "refs/heads/feature") == request["commitSha"]

    web_git._push_approvals.clear()
    with pytest.raises(RuntimeError, match="consumed"):
        web_git.review_push_approved_by_request_id(decision, now=102)
