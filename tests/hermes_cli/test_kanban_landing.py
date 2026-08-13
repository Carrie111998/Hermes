"""Idempotent Git/GitHub landing reconciliation for guarded factory cards."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli.kanban_landing import LandingError, reconcile_github_landing


CANDIDATE = "a" * 40


class ScriptedRunner:
    def __init__(
        self,
        *,
        remote_head: str = "",
        prs: list[dict] | None = None,
        checks: list[dict] | None = None,
    ):
        self.remote_head = remote_head
        self.prs = list(prs or [])
        self.checks = list(
            checks
            or [{"name": "unit", "state": "SUCCESS", "bucket": "pass", "link": "u"}]
        )
        self.calls: list[tuple[str, ...]] = []
        self.created = 0

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[:3] == ["git", "cat-file", "-e"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(args, 0, CANDIDATE + "\n", "")
        if args[:3] == ["git", "ls-remote", "--heads"]:
            out = f"{self.remote_head}\trefs/heads/factory/task\n" if self.remote_head else ""
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[:3] == ["git", "push", "origin"]:
            self.remote_head = CANDIDATE
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, "main\n", "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.prs), "")
        if args[:3] == ["gh", "pr", "create"]:
            self.created += 1
            self.prs = [{
                "number": 42,
                "url": "https://github.com/acme/repo/pull/42",
                "state": "OPEN",
                "headRefOid": CANDIDATE,
            }]
            return subprocess.CompletedProcess(args, 0, self.prs[0]["url"] + "\n", "")
        if args[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.checks), "")
        raise AssertionError(f"unexpected command: {args}")


def test_landing_reconciles_before_mutation_and_resumes_without_duplicates(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    receipts: list[str] = []
    receipt_keys: set[str] = set()

    def record_once(key: str, evidence: str) -> None:
        if key in receipt_keys:
            return
        receipt_keys.add(key)
        receipts.append(evidence)

    first = reconcile_github_landing(
        tmp_path,
        branch="factory/task",
        candidate_sha=CANDIDATE,
        title="Ship exact candidate",
        body="Factory card t_example",
        progress=record_once,
        run=runner,
    )

    assert first == {
        "base_branch": "main",
        "branch": "factory/task",
        "candidate_sha": CANDIDATE,
        "pr_number": 42,
        "pr_state": "OPEN",
        "pr_url": "https://github.com/acme/repo/pull/42",
        "remote_head_sha": CANDIDATE,
        "ci_state": "pass",
        "checks": runner.checks,
    }
    assert receipts == [
        f"GitHub branch factory/task points at exact candidate {CANDIDATE}",
        f"GitHub PR #42 exists for factory/task at exact candidate {CANDIDATE}",
    ]
    assert runner.created == 1
    assert runner.calls.index(("git", "ls-remote", "--heads", "origin", "refs/heads/factory/task")) < runner.calls.index(
        ("git", "push", "origin", f"{CANDIDATE}:refs/heads/factory/task")
    )
    assert runner.calls.index(("gh", "pr", "list", "--state", "all", "--head", "factory/task", "--json", "number,url,state,headRefOid", "--limit", "100")) < runner.calls.index(
        ("git", "push", "origin", f"{CANDIDATE}:refs/heads/factory/task")
    )
    assert runner.calls.index(("gh", "pr", "list", "--state", "all", "--head", "factory/task", "--json", "number,url,state,headRefOid", "--limit", "100")) < runner.calls.index(
        ("gh", "pr", "create", "--base", "main", "--head", "factory/task", "--title", "Ship exact candidate", "--body", "Factory card t_example")
    )

    receipts.clear()
    second = reconcile_github_landing(
        tmp_path,
        branch="factory/task",
        candidate_sha=CANDIDATE,
        title="Ship exact candidate",
        body="Factory card t_example",
        progress=record_once,
        run=runner,
    )
    assert second == first
    assert receipts == []
    assert runner.created == 1
    assert sum(1 for call in runner.calls if call[:3] == ("git", "push", "origin")) == 1


def test_landing_backfills_receipts_for_preexisting_provider_effects(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        remote_head=CANDIDATE,
        prs=[{
            "number": 42,
            "url": "https://github.com/acme/repo/pull/42",
            "state": "OPEN",
            "headRefOid": CANDIDATE,
        }],
    )
    receipts: list[tuple[str, str]] = []

    reconcile_github_landing(
        tmp_path,
        branch="factory/task",
        candidate_sha=CANDIDATE,
        title="Ship exact candidate",
        progress=lambda key, evidence: receipts.append((key, evidence)),
        run=runner,
    )

    assert receipts == [
        (
            "github-branch:factory/task:" + CANDIDATE,
            f"GitHub branch factory/task points at exact candidate {CANDIDATE}",
        ),
        (
            "github-pr:42:" + CANDIDATE,
            f"GitHub PR #42 exists for factory/task at exact candidate {CANDIDATE}",
        ),
    ]
    assert not any(call[:3] == ("git", "push", "origin") for call in runner.calls)
    assert not any(call[:3] == ("gh", "pr", "create") for call in runner.calls)


def test_landing_refuses_to_overwrite_a_different_remote_head(tmp_path: Path) -> None:
    runner = ScriptedRunner(remote_head="b" * 40)

    with pytest.raises(LandingError, match="different candidate"):
        reconcile_github_landing(
            tmp_path,
            branch="factory/task",
            candidate_sha=CANDIDATE,
            title="Ship exact candidate",
            run=runner,
        )

    assert not any(call[:3] == ("git", "push", "origin") for call in runner.calls)
    assert not any(call[:3] == ("gh", "pr", "create") for call in runner.calls)


def test_landing_reconciles_existing_pr_before_any_push(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        prs=[{
            "number": 7,
            "url": "https://github.com/acme/repo/pull/7",
            "state": "OPEN",
            "headRefOid": "b" * 40,
        }]
    )

    with pytest.raises(LandingError, match="does not match"):
        reconcile_github_landing(
            tmp_path,
            branch="factory/task",
            candidate_sha=CANDIDATE,
            title="Ship exact candidate",
            run=runner,
        )

    assert not any(call[:3] == ("git", "push", "origin") for call in runner.calls)


def test_landing_refuses_closed_or_ambiguous_pull_requests(tmp_path: Path) -> None:
    for prs, message in (
        ([{"number": 1, "url": "u", "state": "CLOSED", "headRefOid": CANDIDATE}], "closed"),
        ([
            {"number": 1, "url": "u1", "state": "OPEN", "headRefOid": CANDIDATE},
            {"number": 2, "url": "u2", "state": "OPEN", "headRefOid": CANDIDATE},
        ], "multiple"),
    ):
        runner = ScriptedRunner(remote_head=CANDIDATE, prs=prs)
        with pytest.raises(LandingError, match=message):
            reconcile_github_landing(
                tmp_path,
                branch="factory/task",
                candidate_sha=CANDIDATE,
                title="Ship exact candidate",
                run=runner,
            )


def test_landing_requires_full_exact_candidate_sha(tmp_path: Path) -> None:
    with pytest.raises(LandingError, match="full 40-character"):
        reconcile_github_landing(
            tmp_path,
            branch="factory/task",
            candidate_sha="abc123",
            title="Ship exact candidate",
            run=ScriptedRunner(),
        )
