"""Idempotent Git/GitHub landing reconciliation for guarded Kanban factory work.

This module deliberately owns no lifecycle state. It reads live Git/GitHub
state before each mutation, performs at most one missing external effect, and
lets the caller persist a native progress receipt after every successful side
effect.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Optional


_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
RunCommand = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
ProgressCallback = Callable[[str, str], object]
LifecycleFenceCallback = Callable[[], object]


class LandingError(RuntimeError):
    """The provider state cannot be reconciled without an unsafe mutation."""


def _default_run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )


def _noop_lifecycle_fence() -> None:
    return None


def _checked(run: RunCommand, cwd: Path, args: list[str]) -> str:
    result = run(args, cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise LandingError(f"{' '.join(args[:3])} failed: {detail[:500]}")
    return result.stdout.strip()


def _pr_rows(run: RunCommand, cwd: Path, branch: str) -> list[dict]:
    import json

    raw = _checked(
        run,
        cwd,
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--head",
            branch,
            "--json",
            "number,url,state,headRefOid",
            "--limit",
            "100",
        ],
    )
    try:
        rows = json.loads(raw or "[]")
    except ValueError as exc:
        raise LandingError("gh pr list returned invalid JSON") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise LandingError("gh pr list returned an unexpected response shape")
    return rows


def _check_rows(run: RunCommand, cwd: Path, number: int) -> tuple[str, list[dict]]:
    import json

    args = [
        "gh", "pr", "checks", str(number),
        "--json", "name,state,bucket,link",
    ]
    result = run(args, cwd)
    if result.returncode not in {0, 1, 8}:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise LandingError(f"gh pr checks failed: {detail[:500]}")
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise LandingError("gh pr checks returned invalid JSON") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise LandingError("gh pr checks returned an unexpected response shape")
    buckets = {str(row.get("bucket") or "").lower() for row in rows}
    if buckets & {"fail", "cancel"}:
        state = "fail"
    elif "pending" in buckets:
        state = "pending"
    elif rows and buckets <= {"pass", "skipping"}:
        state = "pass"
    else:
        state = "none"
    return state, rows


def _validate_existing_pr(pr: dict, candidate_sha: str) -> None:
    state = str(pr.get("state") or "").upper()
    if state != "OPEN":
        raise LandingError(f"pull request #{pr.get('number')} is {state.lower() or 'closed'}")
    head_oid = str(pr.get("headRefOid") or "")
    if head_oid != candidate_sha:
        raise LandingError(
            f"pull request #{pr.get('number')} head {head_oid or 'unknown'} does not match {candidate_sha}"
        )


def reconcile_github_landing(
    repository: str | Path,
    *,
    branch: str,
    candidate_sha: str,
    title: str,
    body: str = "",
    progress: Optional[ProgressCallback] = None,
    before_mutation: LifecycleFenceCallback = _noop_lifecycle_fence,
    run: RunCommand = _default_run,
) -> dict:
    """Reconcile one exact candidate branch and pull request, then return facts.

    The function is safe to resume after either external effect. It never force
    pushes, never overwrites a different remote head, never reopens a closed PR,
    and never creates a second PR for the same branch. ``progress`` is invoked
    after exact provider state has been reconciled so callers can immediately
    write (or idempotently backfill) a durable, run-fenced Kanban progress receipt.
    """
    cwd = Path(repository).resolve()
    branch = str(branch or "").strip()
    title = str(title or "").strip()
    candidate_sha = str(candidate_sha or "").strip()
    if not _FULL_SHA_RE.fullmatch(candidate_sha):
        raise LandingError("candidate_sha must be a full 40-character hexadecimal SHA")
    if not branch or branch.startswith("-") or any(ch.isspace() for ch in branch):
        raise LandingError("branch must be a non-empty Git ref without whitespace")
    if not title:
        raise LandingError("pull request title is required")

    _checked(run, cwd, ["git", "cat-file", "-e", f"{candidate_sha}^{{commit}}"])
    resolved = _checked(
        run, cwd, ["git", "rev-parse", "--verify", f"{candidate_sha}^{{commit}}"]
    )
    if resolved != candidate_sha:
        raise LandingError(
            f"candidate object resolved to {resolved or 'nothing'}, expected {candidate_sha}"
        )

    existing_rows = _pr_rows(run, cwd, branch)
    if len(existing_rows) > 1:
        raise LandingError(f"multiple pull requests exist for branch {branch}; reconcile manually")
    if existing_rows:
        _validate_existing_pr(existing_rows[0], candidate_sha)

    remote_line = _checked(
        run,
        cwd,
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
    )
    remote_head = remote_line.split(None, 1)[0] if remote_line else ""
    if remote_head and remote_head != candidate_sha:
        raise LandingError(
            f"remote branch {branch} already points at a different candidate {remote_head}"
        )
    if not remote_head:
        before_mutation()
        _checked(
            run,
            cwd,
            ["git", "push", "origin", f"{candidate_sha}:refs/heads/{branch}"],
        )
        remote_head = candidate_sha
    if progress is not None:
        progress(
            f"github-branch:{branch}:{candidate_sha}",
            f"GitHub branch {branch} points at exact candidate {candidate_sha}",
        )

    base_branch = _checked(
        run,
        cwd,
        ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
    )
    if not base_branch:
        raise LandingError("GitHub repository has no resolvable default branch")

    rows = _pr_rows(run, cwd, branch)
    if len(rows) > 1:
        raise LandingError(f"multiple pull requests exist for branch {branch}; reconcile manually")
    if not rows:
        before_mutation()
        url = _checked(
            run,
            cwd,
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
        ).splitlines()[-1].strip()
        rows = _pr_rows(run, cwd, branch)
        if len(rows) != 1:
            raise LandingError(
                "pull request creation succeeded but exact branch reconciliation did not return one PR"
            )
        if url and not rows[0].get("url"):
            rows[0]["url"] = url

    pr = rows[0]
    _validate_existing_pr(pr, candidate_sha)
    state = str(pr.get("state") or "").upper()
    try:
        number = int(pr["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LandingError("pull request reconciliation returned no valid number") from exc
    url = str(pr.get("url") or "").strip()
    if not url:
        raise LandingError("pull request reconciliation returned no URL")
    if progress is not None:
        progress(
            f"github-pr:{number}:{candidate_sha}",
            f"GitHub PR #{number} exists for {branch} at exact candidate {candidate_sha}",
        )
    ci_state, checks = _check_rows(run, cwd, number)

    return {
        "base_branch": base_branch,
        "branch": branch,
        "candidate_sha": candidate_sha,
        "pr_number": number,
        "pr_state": state,
        "pr_url": url,
        "remote_head_sha": remote_head,
        "ci_state": ci_state,
        "checks": checks,
    }
