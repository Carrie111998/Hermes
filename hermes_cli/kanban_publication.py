"""Bounded GitHub publication proof for Kanban completion."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

TIMEOUT_SECONDS = 15


def command_runner(args: list[str], repo: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            args, cwd=str(repo), capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "command unavailable or timed out"
    if completed.returncode != 0:
        return False, "command failed"
    return True, completed.stdout.strip()


def verify(*, repo_path: str, branch: str, expected_base: str, pr_number: int, command_runner=command_runner) -> Optional[str]:
    """Return a refusal reason, or None when the publication proof is valid."""
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return "publication proof rejected: pr_number must be an integer"
    if not all(isinstance(value, str) and value.strip() for value in (repo_path, branch, expected_base)):
        return "publication proof rejected: publication proof fields must be non-empty strings"
    repo = Path(repo_path).expanduser()
    if not repo.is_dir():
        return "publication proof rejected: repo_path is not an existing directory"
    ok, root = command_runner(["git", "rev-parse", "--show-toplevel"], repo)
    if not ok or not root or Path(root).resolve() != repo.resolve():
        return "publication proof rejected: repo_path is not the Git repository root"
    ok, dirty = command_runner(["git", "status", "--porcelain"], repo)
    if not ok:
        return "publication proof rejected: could not inspect Git status"
    if dirty:
        return "publication proof rejected: working tree is not clean"
    ok, head = command_runner(["git", "rev-parse", "HEAD"], repo)
    if not ok or not head:
        return "publication proof rejected: could not resolve local HEAD"
    ok, checked_out = command_runner(["git", "symbolic-ref", "--short", "HEAD"], repo)
    if not ok or not checked_out:
        return "publication proof rejected: repository is detached"
    if checked_out != branch:
        return "publication proof rejected: checked-out branch does not match explicit branch"
    ok, remote = command_runner(["git", "ls-remote", "origin", f"refs/heads/{branch}"], repo)
    remote_head = remote.split()[0] if ok and remote.split() else ""
    if not ok or remote_head != head:
        return "publication proof rejected: origin branch does not match local HEAD"
    ok, pr_json = command_runner(
        ["gh", "pr", "view", str(pr_number), "--json", "state,headRefOid,baseRefName"], repo
    )
    if not ok:
        return "publication proof rejected: GitHub PR was not found or could not be read"
    try:
        pr = json.loads(pr_json)
    except (TypeError, json.JSONDecodeError):
        return "publication proof rejected: GitHub returned invalid PR data"
    if pr.get("state") != "OPEN":
        return "publication proof rejected: GitHub PR is not open"
    if pr.get("headRefOid") != head:
        return "publication proof rejected: GitHub PR head does not match local HEAD"
    if pr.get("baseRefName") != expected_base:
        return "publication proof rejected: GitHub PR base does not match expected_base"
    return None


class PublicationProofError(RuntimeError):
    """Raised when a required completion lacks valid publication proof."""
