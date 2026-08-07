from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
RUNTIME = ROOT / "ops" / "muncho" / "runtime"
RELEASE_ENTRYPOINTS = (
    "hermes",
    "hermes-acp",
    "hermes-agent",
    "muncho-ops",
    "muncho-release",
)


def _load_routine():
    sys.path.insert(0, str(RUNTIME))
    try:
        spec = importlib.util.spec_from_file_location(
            "fork_upstream_auto_sync_pr_routine",
            RUNTIME / "fork_upstream_auto_sync_pr_routine.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(RUNTIME))


def _fork_repository_identity(routine):
    return {
        "isCrossRepository": False,
        "headRepository": {"nameWithOwner": routine.FORK_REPO},
        "headRepositoryOwner": {"login": routine.FORK_OWNER},
    }


def test_runtime_keeps_open_candidate_stable_when_upstream_advances(monkeypatch):
    routine = _load_routine()
    snapshot = "1" * 40
    current = "2" * 40
    fork = "3" * 40
    head = "4" * 40
    pr = {
        **_fork_repository_identity(routine),
        "headRefName": "codex/upstream-sync-auto-20260711-1200",
        "headRefOid": head,
        "baseRefName": "main",
        "number": 91,
        "state": "OPEN",
    }
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="9" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch=pr["headRefName"],
        head_sha=head,
        base_sha="6" * 40,
        upstream_sha=snapshot,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    fresh = {
        "fork_main_ref": fork,
        "merge_base": "5" * 40,
        "upstream_main_ref": current,
    }

    def contains(repo, base, candidate):
        return repo == routine.UPSTREAM_REPO and base == snapshot and candidate == current

    monkeypatch.setattr(routine, "compare_shows_head_contains_base", contains)
    assert routine.stale_candidate_reason(manifest, pr, fresh) is None


def test_pr_title_body_and_prefix_lookalikes_never_gain_candidate_identity():
    routine = _load_routine()
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="8" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-state-branch",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    lookalike = {
        "number": 92,
        "headRefName": "codex/upstream-sync-auto-exact-state-branch",
        "headRefOid": "4" * 40,
        "baseRefName": "main",
        "title": "chore: sync fork with upstream main",
        "body": "Automated fork-only upstream sync PR",
    }

    mismatches = routine.candidate_pr_mismatches(lookalike, manifest)
    assert "candidate_pr_number_mismatch" in mismatches
    assert "candidate_pr_headRefName_mismatch" in mismatches


def test_exact_published_manifest_matches_without_reading_display_text():
    routine = _load_routine()
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="7" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-state-branch",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    observed = {
        **_fork_repository_identity(routine),
        "number": 91,
        "headRefName": "exact-state-branch",
        "headRefOid": "4" * 40,
        "baseRefName": "main",
        "title": "unrelated display title",
        "body": "unrelated display body",
    }

    assert routine.candidate_pr_mismatches(observed, manifest) == []


def test_cross_repository_same_branch_and_head_never_matches_manifest():
    routine = _load_routine()
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="6" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-state-branch",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    lookalike = {
        "number": 91,
        "headRefName": "exact-state-branch",
        "headRefOid": "4" * 40,
        "baseRefName": routine.FORK_BRANCH,
        "isCrossRepository": True,
        "headRepository": {"nameWithOwner": "attacker/hermes-agent"},
        "headRepositoryOwner": {"login": "attacker"},
    }

    mismatches = routine.candidate_pr_mismatches(lookalike, manifest)

    assert "candidate_pr_cross_repository_mismatch" in mismatches
    assert "candidate_pr_head_repository_mismatch" in mismatches
    assert "candidate_pr_head_owner_mismatch" in mismatches


def test_manifest_base_and_upstream_shas_require_exact_head_ancestry(
    monkeypatch,
):
    routine = _load_routine()
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="5" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-state-branch",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    observed: list[tuple[str, str, str]] = []

    def contains(repo, base, head):
        observed.append((repo, base, head))
        return base == "3" * 40

    monkeypatch.setattr(routine, "compare_shows_head_contains_base", contains)

    assert routine.candidate_commit_mismatches(manifest) == [
        "candidate_head_missing_exact_upstream_sha"
    ]
    assert observed == [
        (routine.FORK_REPO, "3" * 40, "4" * 40),
        (routine.FORK_REPO, "2" * 40, "4" * 40),
    ]


def test_later_candidate_lookup_uses_only_exact_stored_pr_number(monkeypatch):
    routine = _load_routine()
    observed: list[list[str]] = []

    def fake_gh_json(args):
        observed.append(args)
        return {"number": 481}

    monkeypatch.setattr(routine, "gh_json", fake_gh_json)
    assert routine.pr_view(481) == {"number": 481}
    assert observed == [
        [
            "pr",
            "view",
            "481",
            "--repo",
            routine.FORK_REPO,
            "--json",
            (
                "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                "mergeable,mergeStateStatus,statusCheckRollup,labels,"
                "isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ]
    ]


def test_prepared_state_is_recovered_only_from_exact_private_ledger(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    state = tmp_path / "candidate.json"
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="6" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-prepared-branch",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    monkeypatch.setattr(routine, "AUTO_STATE", state)
    routine.append_candidate_manifest(state, prepared)

    def forbidden_lookup(*_args, **_kwargs):
        raise AssertionError("prepared state must not infer an orphan PR")

    monkeypatch.setattr(routine, "pr_view", forbidden_lookup)
    manifest, candidate, blockers = routine._candidate_state_for_plan([])

    assert manifest == prepared
    assert candidate is None
    assert blockers == []


def test_exact_candidate_query_uses_plain_branch_and_structured_repo_identity(
    monkeypatch,
):
    routine = _load_routine()
    observed: list[list[str]] = []

    def fake_gh_json(args):
        observed.append(args)
        return []

    monkeypatch.setattr(routine, "gh_json", fake_gh_json)

    assert (
        routine.list_exact_branch_candidate_prs(
            "exact-prepared-branch",
            "4" * 40,
        )
        == []
    )
    command = observed[0]
    assert command[command.index("--head") + 1] == "exact-prepared-branch"
    assert "lomliev:exact-prepared-branch" not in command
    fields = command[command.index("--json") + 1]
    assert "isCrossRepository" in fields
    assert "headRepository" in fields
    assert "headRepositoryOwner" in fields


def test_prepared_state_recovers_existing_exact_pr_without_push(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    state = tmp_path / "candidate.json"
    monkeypatch.setattr(routine, "AUTO_STATE", state)
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="5" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-prepared-branch",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    routine.append_candidate_manifest(state, prepared)
    candidate = {
        **_fork_repository_identity(routine),
        "number": 91,
        "url": "https://github.com/lomliev/hermes-agent/pull/91",
        "state": "OPEN",
        "headRefName": prepared["branch"],
        "headRefOid": prepared["head_sha"],
        "baseRefName": routine.FORK_BRANCH,
    }
    monkeypatch.setattr(
        routine,
        "list_exact_branch_candidate_prs",
        lambda *_args: [candidate],
    )
    monkeypatch.setattr(
        routine,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing exact PR attempted a push")
        ),
    )
    monkeypatch.setattr(routine, "_execute_locked", lambda _args: 0)
    report = {
        "fresh_refs": {"behind_by": 1, "ahead_by": 0},
    }

    assert (
        routine._recover_prepared_candidate(
            argparse.Namespace(execute=True),
            report,
            prepared,
        )
        == 0
    )
    recovered = routine.recover_candidate_manifest(state)
    assert recovered is not None
    assert recovered["phase"] == "published"
    assert recovered["pr_number"] == 91


def test_prepared_state_retries_exact_push_and_pr_once_after_crash(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    state = tmp_path / "candidate.json"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(routine, "AUTO_STATE", state)
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="4" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-prepared-branch",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    routine.append_candidate_manifest(state, prepared)
    candidate = {
        **_fork_repository_identity(routine),
        "number": 91,
        "url": "https://github.com/lomliev/hermes-agent/pull/91",
        "state": "OPEN",
        "headRefName": prepared["branch"],
        "headRefOid": prepared["head_sha"],
        "baseRefName": routine.FORK_BRANCH,
    }
    monkeypatch.setattr(
        routine,
        "list_exact_branch_candidate_prs",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        routine,
        "_validate_prepared_worktree",
        lambda _manifest: worktree,
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(cmd, **_kwargs):
        commands.append(tuple(cmd))
        return routine.CmdResult(list(cmd), 0, "", "")

    monkeypatch.setattr(routine, "run", fake_run)
    monkeypatch.setattr(
        routine,
        "create_candidate_pr",
        lambda **_kwargs: (
            candidate,
            routine.CmdResult([], 0, candidate["url"], ""),
        ),
    )
    monkeypatch.setattr(routine, "_execute_locked", lambda _args: 0)

    assert (
        routine._recover_prepared_candidate(
            argparse.Namespace(execute=True),
            {"fresh_refs": {"behind_by": 1, "ahead_by": 0}},
            prepared,
        )
        == 0
    )
    pushes = [command for command in commands if "push" in command]
    assert len(pushes) == 1
    recovered = routine.recover_candidate_manifest(state)
    assert recovered is not None and recovered["phase"] == "published"


def test_candidate_routine_has_no_merge_or_deploy_execution_surface():
    source = (
        RUNTIME / "fork_upstream_auto_sync_pr_routine.py"
    ).read_text(encoding="utf-8")
    assert '"pr",\n            "merge"' not in source
    assert "queue_auto_deploy_request" not in source
    assert "AUTO_MERGE_DEPLOY" not in source
    assert "known_conflict_auto_resolver" not in source
    prepared = source.index(
        "append_candidate_manifest(AUTO_STATE, prepared_manifest)"
    )
    push = source.index('"push",', prepared)
    create = source.index('"create",', push)
    published = source.index("append_candidate_manifest(AUTO_STATE, manifest)", create)
    assert prepared < push < create < published


def test_open_candidate_flow_performs_no_external_mutation(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="9" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-open-candidate",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    candidate = {
        **_fork_repository_identity(routine),
        "number": 91,
        "url": "https://github.com/lomliev/hermes-agent/pull/91",
        "state": "OPEN",
        "headRefName": "exact-open-candidate",
        "headRefOid": "4" * 40,
        "baseRefName": "main",
    }
    monkeypatch.setattr(
        routine,
        "build_plan",
        lambda _args: {
            "created_at_utc": "2026-07-30T12:00:00Z",
            "status": "candidate_pr_exists_review_required_no_action",
            "blocked": False,
            "blockers": [],
            "fresh_refs": {
                "fork_main_ref": "3" * 40,
                "upstream_main_ref": "5" * 40,
                "merge_base": "6" * 40,
                "ahead_by": 0,
                "behind_by": 1,
            },
            "candidate_manifest": manifest,
            "candidate_pr": candidate,
            "proposed_branch": "unused",
        },
    )
    external_commands: list[tuple[str, ...]] = []

    def forbidden_run(cmd, **_kwargs):
        external_commands.append(tuple(cmd))
        raise AssertionError("open candidate flow attempted an external command")

    reports: list[dict] = []
    monkeypatch.setattr(routine, "run", forbidden_run)
    monkeypatch.setattr(
        routine,
        "clear_blocker_delivery_state",
        lambda _path: None,
    )
    monkeypatch.setattr(
        routine,
        "write_report",
        lambda report: reports.append(dict(report)),
    )
    monkeypatch.setattr(routine, "AUTO_STATE", tmp_path / "candidate.json")

    assert routine._execute_locked(argparse.Namespace(execute=True)) == 0
    assert external_commands == []
    assert reports[-1]["candidate_ref_frozen"] is True
    assert reports[-1]["later_upstream_is_tail_drift"] is True
    assert reports[-1]["tail_drift_rebinds_candidate"] is False


def test_closed_candidate_fails_closed_without_reopen_or_evidence_cleanup(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="8" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-closed-candidate",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    candidate = {
        **_fork_repository_identity(routine),
        "number": 91,
        "url": "https://github.com/lomliev/hermes-agent/pull/91",
        "state": "CLOSED",
        "headRefName": "exact-closed-candidate",
        "headRefOid": "4" * 40,
        "baseRefName": "main",
    }
    monkeypatch.setattr(routine, "load_monitor", lambda: {})
    monkeypatch.setattr(
        routine,
        "compare_refs",
        lambda: {
            "fork_main_ref": "3" * 40,
            "upstream_main_ref": "5" * 40,
            "merge_base": "6" * 40,
            "ahead_by": 0,
            "behind_by": 1,
            "compare_status": "behind",
            "compare_url": None,
        },
    )
    monkeypatch.setattr(routine, "list_open_fork_prs", lambda: [])
    monkeypatch.setattr(
        routine,
        "_candidate_state_for_plan",
        lambda _prs: (manifest, candidate, []),
    )
    external_commands: list[tuple[str, ...]] = []

    def forbidden_run(cmd, **_kwargs):
        external_commands.append(tuple(cmd))
        raise AssertionError("closed candidate attempted an external command")

    def forbidden_cleanup(*_args, **_kwargs):
        raise AssertionError("closed candidate evidence was removed")

    reports: list[dict] = []
    monkeypatch.setattr(routine, "run", forbidden_run)
    monkeypatch.setattr(
        routine,
        "cleanup_exact_candidate_worktree",
        forbidden_cleanup,
    )
    monkeypatch.setattr(
        routine,
        "append_candidate_terminal_receipt",
        forbidden_cleanup,
    )
    monkeypatch.setattr(
        routine,
        "apply_blocker_notification_dedupe",
        lambda _report, _pr: False,
    )
    monkeypatch.setattr(
        routine,
        "write_report",
        lambda report: reports.append(dict(report)),
    )
    monkeypatch.setattr(routine, "AUTO_STATE", tmp_path / "candidate.json")

    assert routine._execute_locked(argparse.Namespace(execute=True)) == 2
    assert external_commands == []
    assert reports[-1]["status"] == (
        "blocked_candidate_closed_requires_operator_reconciliation"
    )
    assert reports[-1]["blockers"] == [
        "candidate_closed_requires_operator_reconciliation"
    ]


def test_merged_candidate_reconciles_exact_state_then_continues(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id="7" * 64,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.FORK_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch="exact-merged-candidate",
        head_sha="4" * 40,
        base_sha="3" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    manifest = routine.publish_candidate_manifest(prepared, pr_number=91)
    candidate = {
        **_fork_repository_identity(routine),
        "number": 91,
        "state": "MERGED",
        "headRefName": manifest["branch"],
        "headRefOid": manifest["head_sha"],
        "baseRefName": routine.FORK_BRANCH,
    }
    plans = iter(
        [
            {
                "created_at_utc": "2026-07-30T12:00:00Z",
                "status": "candidate_merged_requires_reconciliation",
                "blocked": False,
                "blockers": [],
                "fresh_refs": {
                    "behind_by": 1,
                    "fork_main_ref": "5" * 40,
                },
                "candidate_manifest": manifest,
                "candidate_pr": candidate,
                "proposed_branch": "unused",
            },
            {
                "created_at_utc": "2026-07-30T12:01:00Z",
                "status": "no_drift_no_action",
                "blocked": False,
                "blockers": [],
                "fresh_refs": {"behind_by": 0},
                "candidate_manifest": None,
                "candidate_pr": None,
                "proposed_branch": "unused",
            },
        ]
    )
    monkeypatch.setattr(routine, "build_plan", lambda _args: next(plans))
    reconciled: list[str] = []
    monkeypatch.setattr(
        routine,
        "cleanup_exact_candidate_worktree",
        lambda exact: reconciled.append(f"worktree:{exact['pr_number']}") or True,
    )
    monkeypatch.setattr(
        routine,
        "compare_shows_head_contains_base",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        routine,
        "append_candidate_terminal_receipt",
        lambda *_args, **_kwargs: (
            reconciled.append("terminal")
            or {"receipt_sha256": "a" * 64}
        ),
    )
    monkeypatch.setattr(
        routine,
        "clear_blocker_delivery_state",
        lambda _path: None,
    )
    monkeypatch.setattr(routine, "write_report", lambda _report: None)
    monkeypatch.setattr(routine, "AUTO_STATE", tmp_path / "candidate.json")
    monkeypatch.setattr(
        routine,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("merged reconciliation attempted an external command")
        ),
    )

    assert routine._execute_locked(argparse.Namespace(execute=True)) == 0
    assert reconciled == ["terminal", "worktree:91"]


def test_candidate_create_flow_mutates_only_fork_draft_candidate(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    fork_sha = "1" * 40
    upstream_sha = "2" * 40
    head_sha = "3" * 40
    created_at = "2026-07-30T12:00:00Z"
    branch = routine.branch_name(created_at)
    worktree_root = tmp_path / "worktrees"
    monkeypatch.setattr(routine, "WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(routine, "AUTO_STATE", tmp_path / "state" / "candidate.json")
    monkeypatch.setattr(
        routine,
        "BLOCKER_DEDUPE_STATE",
        tmp_path / "state" / "dedupe.json",
    )
    monkeypatch.setattr(routine, "disk_free_bytes", lambda _path: 6 * 1024**3)
    monkeypatch.setattr(routine, "changed_python_files", lambda *_args: [])
    monkeypatch.setattr(
        routine,
        "clear_blocker_delivery_state",
        lambda _path: None,
    )
    monkeypatch.setattr(routine, "write_report", lambda _report: None)
    monkeypatch.setattr(routine, "now_utc", lambda: created_at)
    monkeypatch.setattr(routine, "load_monitor", lambda: {})
    monkeypatch.setattr(
        routine,
        "compare_refs",
        lambda: {
            "fork_main_ref": fork_sha,
            "upstream_main_ref": upstream_sha,
            "merge_base": "4" * 40,
            "ahead_by": 0,
            "behind_by": 1,
            "compare_status": "behind",
            "compare_url": None,
        },
    )
    unrelated_pr = {
        "number": 77,
        "url": "https://github.com/lomliev/hermes-agent/pull/77",
        "state": "OPEN",
        "headRefName": "codex/upstream-sync-auto-lookalike",
        "headRefOid": "9" * 40,
        "baseRefName": routine.FORK_BRANCH,
        "title": "Automated fork-only upstream sync PR",
        "body": "Upstream main lookalike display text",
    }
    monkeypatch.setattr(
        routine,
        "list_open_fork_prs",
        lambda: [unrelated_pr],
    )
    monkeypatch.setattr(
        routine,
        "discover_created_candidate_pr",
        lambda exact_branch, exact_head: {
            **_fork_repository_identity(routine),
            "number": 91,
            "url": "https://github.com/lomliev/hermes-agent/pull/91",
            "state": "OPEN",
            "headRefName": exact_branch,
            "headRefOid": exact_head,
            "baseRefName": routine.FORK_BRANCH,
        },
    )

    commands: list[tuple[str, ...]] = []

    def fake_run(cmd, *, cwd=None, check=True, timeout=None):
        del cwd, check, timeout
        command = tuple(cmd)
        commands.append(command)
        if command[:2] == ("git", "clone"):
            Path(command[-1]).mkdir(parents=True)
        stdout = ""
        if command[:2] == ("git", "rev-parse"):
            stdout = {
                "origin/main": fork_sha,
                "upstream/main": upstream_sha,
                "HEAD": head_sha,
            }[command[-1]] + "\n"
        elif command[:3] == ("git", "diff", "--name-only"):
            stdout = ""
        elif command[:3] == (str(routine.GH), "pr", "create"):
            stdout = "https://github.com/lomliev/hermes-agent/pull/91\n"
        return routine.CmdResult(list(command), 0, stdout, "")

    monkeypatch.setattr(routine, "run", fake_run)

    assert routine._execute_locked(argparse.Namespace(execute=True)) == 0

    pushes = [
        command
        for command in commands
        if command[0] == "git" and "push" in command
    ]
    creates = [
        command
        for command in commands
        if command[:3] == (str(routine.GH), "pr", "create")
    ]
    assert len(pushes) == 1
    assert routine.FORK_GIT_URL in pushes[0]
    assert routine.UPSTREAM_GIT_URL not in pushes[0]
    assert len(creates) == 1
    assert creates[0][creates[0].index("--repo") + 1] == routine.FORK_REPO
    assert "--draft" in creates[0]
    assert not any(
        command[:3]
        in {
            (str(routine.GH), "pr", "merge"),
            (str(routine.GH), "pr", "close"),
        }
        for command in commands
    )
    assert not any(
        token in {"deploy", "restart", "systemctl"}
        for command in commands
        for token in command
    )


def test_runtime_dedupe_suppresses_unchanged_blocker(tmp_path, monkeypatch):
    routine = _load_routine()
    monkeypatch.setattr(routine, "BLOCKER_DEDUPE_STATE", tmp_path / "dedupe.json")
    report = {
        "status": "blocked_candidate_identity_state",
        "blockers": [
            "candidate_manifest_digest_mismatch",
            "candidate_pr_headRefOid_mismatch",
        ],
    }
    pr = {"number": 91, "headRefOid": "6" * 40}

    assert routine.apply_blocker_notification_dedupe(report, pr) is True
    assert routine.apply_blocker_notification_dedupe(report, pr) is False
    assert (
        report["blocker_notification"]["reason"]
        == "unchanged_selection_suppressed_unconfirmed"
    )
    assert report["blocker_notification"]["delivery_confirmed_at"] is None


def test_runtime_dedupe_treats_merge_conflict_paths_as_stable_identity(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    monkeypatch.setattr(routine, "BLOCKER_DEDUPE_STATE", tmp_path / "dedupe.json")
    report = {
        "status": "blocked_merge_conflicts",
        "fresh_refs": {
            "fork_main_ref": "a" * 40,
            "upstream_main_ref": "1" * 40,
            "behind_by": 196,
        },
        "conflicted_files": ["gateway/run.py", "tools/approval.py"],
    }

    assert routine.apply_blocker_notification_dedupe(report, {}) is True

    # Upstream movement is evidence for the report, not a new blocker. The
    # same conflict set stays suppressed until the 24-hour reminder window.
    report["fresh_refs"] = {
        "fork_main_ref": "a" * 40,
        "upstream_main_ref": "2" * 40,
        "behind_by": 211,
    }
    assert routine.apply_blocker_notification_dedupe(report, {}) is False

    # A materially different conflict set is a new blocker and emits now.
    report["conflicted_files"].append("hermes_cli/config.py")
    assert routine.apply_blocker_notification_dedupe(report, {}) is True

    # A new fork base can change the conflict itself even when path names stay
    # the same, so it is a new blocker identity and must notify immediately.
    assert routine.apply_blocker_notification_dedupe(report, {}) is False
    report["fresh_refs"]["fork_main_ref"] = "b" * 40
    assert routine.apply_blocker_notification_dedupe(report, {}) is True


def test_deploy_marks_planned_stop_before_symlink_swap_and_restart():
    source = (RUNTIME / "muncho-auto-deploy-release").read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    marker = run_deploy.index('marker_output="$(')
    symlink_swap = run_deploy.index('ln -sfn "$new" "$ACTIVE_LINK.next"')
    restart = run_deploy.index('systemctl restart "$SERVICE"')
    verify_consumed = run_deploy.index('planned_stop_marker_not_consumed')

    assert marker < symlink_swap < restart < verify_consumed
    assert 'blocked_planned_restart_helper_missing' in source
    assert 'blocked_planned_stop_marker_failed' in source
    assert 'rollback_release() {' in source
    assert 'ln -sfn "$previous" "$ACTIVE_LINK.rollback"' in source
    assert 'write_status "deploy_rolled_back"' in source
    assert 'REPO_URL="https://github.com/lomliev/hermes-agent.git"' in source
    assert "MUNCHO_REPO_URL" not in source
    assert 'release_identity_matches "$active" "$active_head"' in source
    assert 'release_identity_matches "$new" "$sha"' in source
    assert '"$RELEASES/hermes-agent-${expected_head:0:12}"' in source
    assert 'DEPLOY_HEALTH_WAIT_SECONDS" -gt 300' in source
    assert "previous_release_identity_invalid" in source
    assert '"restored_source":' not in source


def test_release_announcement_is_after_exact_post_restart_health_only():
    source = (RUNTIME / "muncho-auto-deploy-release").read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]

    reserve = run_deploy.index('reserve_muncho_release_mapping "$new" "$sha"')
    restart = run_deploy.index('systemctl restart "$SERVICE"')
    active_health = run_deploy.index('systemctl is-active --quiet "$SERVICE"', restart)
    exact_readback = run_deploy.index('deployed_head="$(')
    exact_gate = run_deploy.index('if [ "$deployed_head" != "$sha" ]')
    restart_prepare = run_deploy.index(
        'prepare_muncho_restart_attestation',
        reserve,
    )
    restart_complete = run_deploy.index(
        'complete_muncho_restart_attestation',
        exact_gate,
    )
    announce = run_deploy.index(
        'release_announcement="$(announce_muncho_release_after_smoke'
    )
    cleanup = run_deploy.index('cleanup_output="$(cleanup_old_releases)"')
    deploy_pass = run_deploy.rindex('write_status "deploy_pass"')

    assert reserve < restart_prepare < restart < active_health < exact_readback
    assert exact_readback < exact_gate < restart_complete < announce
    assert announce < cleanup < deploy_pass
    assert (
        "announce_muncho_release_after_smoke"
        not in source[
            source.index("fail_after_activation() {") : source.index(
                "record_release_packaging_failure() {"
            )
        ]
    )
    assert (
        '\\"release_completion\\": \\"discord_announcement_unconfirmed\\"' in run_deploy
    )


def test_deploy_staging_dependency_package_is_final_address_bound():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    prepare = run_deploy.index(
        'package_production_runtime_dependencies.py" prepare'
    )
    prepare_address = run_deploy.index(
        '--release-address "$new"',
        prepare,
    )
    prepare_revision = run_deploy.index('--revision "$sha"', prepare_address)
    seal = run_deploy.index(
        'seal_agent_browser_config "$tmp" "$sha"',
        prepare_revision,
    )
    build = run_deploy.index(
        'package_production_runtime_dependencies.py" build-manifest',
        seal,
    )
    verify = run_deploy.index(
        'package_production_runtime_dependencies.py" verify',
        build,
    )
    verify_address = run_deploy.index(
        '--release-address "$new"',
        verify,
    )
    move = run_deploy.index(
        'publish_release_staging_directory "$tmp" "$new" "$short"',
        verify_address,
    )

    assert (
        prepare
        < prepare_address
        < prepare_revision
        < seal
        < build
        < verify
        < verify_address
        < move
    )
    syntax = subprocess.run(
        ["bash", "-n", str(helper)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_deploy_packaging_failure_is_terminal_and_cleans_inactive_staging(
    tmp_path,
):
    helper = RUNTIME / "muncho-auto-deploy-release"
    staging = tmp_path / ".hermes-agent-aaaaaaaaaaaa.tmp.123"
    staging.mkdir()
    (staging / "partial-install").write_text("inert", encoding="utf-8")
    status_log = tmp_path / "status.log"
    script = r'''
set -euo pipefail
source "$1"
STATUS_LOG="$2"
RELEASES="$(dirname "$3")"
write_status() {
  printf 'status=%s sha=%s pr=%s extra=%s\n' "$1" "$2" "$3" "$4" > "$STATUS_LOG"
}
set +e
run_release_packaging_step \
  "runtime_dependency_prepare" \
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
  "217" \
  "$3" \
  bash -c 'exit 23'
rc=$?
set -e
printf 'rc=%s\n' "$rc"
'''
    completed = subprocess.run(
        ["bash", "-c", script, "packaging-failure", str(helper), str(status_log), str(staging)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "rc=5"
    assert not staging.exists()
    status = status_log.read_text(encoding="utf-8")
    assert "status=blocked_target_release_packaging_failed" in status
    assert '"stage": "runtime_dependency_prepare"' in status
    assert '"command_exit_code": 23' in status
    assert '"staging_cleanup_succeeded": true' in status


def test_deploy_reinstalls_and_attests_entrypoints_at_final_address():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]

    publish = run_deploy.index(
        'publish_release_staging_directory "$tmp" "$new" "$short"'
    )
    identity = run_deploy.index('release_identity_matches "$new" "$sha"')
    final_install = run_deploy.index(
        'install_target_release_wheel "$new" "$new"',
        identity,
    )
    entrypoint_attest = run_deploy.index(
        'attest_target_release_entrypoints "$new"',
        final_install,
    )
    venv_attest = run_deploy.index(
        'attest_target_release_venv "$new" "$new"',
        entrypoint_attest,
    )
    cutover_attest = run_deploy.index(
        'cutover_artifacts_match "$new" "$sha"',
        venv_attest,
    )
    activate = run_deploy.index('ln -sfn "$new" "$ACTIVE_LINK.next"')

    assert (
        publish
        < identity
        < final_install
        < entrypoint_attest
        < venv_attest
        < cutover_attest
        < activate
    )
    assert '\\"stage\\": \\"final_address_wheel_install\\"' in run_deploy
    assert "blocked_target_release_entrypoints_invalid" in run_deploy


def test_deploy_lock_and_active_release_rechecks_precede_target_mutations():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    lock_function = source[
        source.index("acquire_deploy_lock_at() {") : source.index(
            "gateway_deploy_topology_json() {"
        )
    ]

    lock = run_deploy.index('acquire_deploy_lock "$sha" "$pr"')
    pre_lock_topology = run_deploy.index(
        'require_legacy_deploy_topology "$sha" "$pr" "pre_deploy"'
    )
    post_lock_topology = run_deploy.index(
        'require_legacy_deploy_topology "$sha" "$pr" "post_deploy_lock"'
    )
    active_snapshot = run_deploy.index(
        'active="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"'
    )
    publish_guard = run_deploy.index('"pre_release_publish"')
    publish = run_deploy.index(
        'publish_release_staging_directory "$tmp" "$new" "$short"',
        publish_guard,
    )
    final_install_guard = run_deploy.index('"pre_final_address_wheel_install"')
    final_install = run_deploy.index(
        'install_target_release_wheel "$new" "$new"',
        final_install_guard,
    )
    restart_marker_guard = run_deploy.index('"pre_restart_marker"')
    restart_marker = run_deploy.index('marker_output="$(', restart_marker_guard)
    activation_guard = run_deploy.index('"pre_link_activation"')
    activation = run_deploy.index(
        'ln -sfn "$new" "$ACTIVE_LINK.next"',
        activation_guard,
    )

    assert pre_lock_topology < lock < post_lock_topology < active_snapshot
    assert publish_guard < publish
    assert final_install_guard < final_install
    assert restart_marker_guard < restart_marker
    assert activation_guard < activation
    assert 'DEPLOY_LOCK_PATH="/run/muncho-auto-deploy-release.lock"' in source
    assert 'SYSTEM_FLOCK="/usr/bin/flock"' in source
    assert 'exec 9<>"$lock_path"' in lock_function
    assert '"$SYSTEM_FLOCK" --exclusive --nonblock 9' in lock_function
    assert "blocked_concurrent_deploy" in lock_function


def test_root_owned_release_parent_keeps_staging_and_publish_authority_at_root():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    prepare = source[
        source.index("prepare_release_staging_directory() {") : source.index(
            "publish_release_staging_directory() {"
        )
    ]
    publish = source[
        source.index("publish_release_staging_directory() {") : source.index(
            "gateway_deploy_topology_json() {"
        )
    ]
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]

    prepare_call = run_deploy.index(
        'prepare_release_staging_directory "$tmp" "$short"'
    )
    clone = run_deploy.index(
        'sudo -n -u "$OWNER" git clone --depth 1 --branch main',
        prepare_call,
    )
    publish_guard = run_deploy.index('"pre_release_publish"')
    publish_call = run_deploy.index(
        'publish_release_staging_directory "$tmp" "$new" "$short"',
        publish_guard,
    )

    assert "parent_state.st_uid != 0" in prepare
    assert "parent_state.st_gid != 0" in prepare
    assert "stat.S_IMODE(parent_state.st_mode) != 0o755" in prepare
    assert "os.mkdir(staging, 0o700)" in prepare
    assert "os.chown(staging, owner_uid, owner_gid)" in prepare
    assert '[ -L "$release" ]' in publish
    assert 'mv -T -- "$staging" "$release"' in publish
    assert prepare_call < clone < publish_guard < publish_call
    assert 'sudo -n -u "$OWNER" mv -T "$tmp" "$new"' not in run_deploy


def test_already_active_fast_path_requires_restart_receipt_before_announcement():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    fast_path = run_deploy[
        run_deploy.index('if [ "$active" = "$new" ]; then') : run_deploy.index(
            'require_no_active_voice_call "$sha" "$pr" "pre_release"'
        )
    ]
    venv_attestation = source[
        source.index("attest_target_release_venv() {") : source.index(
            "install_target_release_wheel() {"
        )
    ]
    cutover_attestation = source[
        source.index("cutover_artifacts_match() {") : source.index(
            "cleanup_old_releases() {"
        )
    ]

    entrypoint = fast_path.index('attest_target_release_entrypoints "$active"')
    venv = fast_path.index('attest_target_release_venv "$active" "$active"')
    cutover = fast_path.index('cutover_artifacts_match "$active" "$sha"')
    reserve = fast_path.index('reserve_muncho_release_mapping "$active" "$sha"')
    restart_receipt = fast_path.index('complete_muncho_restart_attestation')
    announce = fast_path.index('announce_muncho_release_after_smoke "$active" "$sha"')
    deploy_pass = fast_path.index('write_status "deploy_pass"')
    completed = fast_path.index("return 0", deploy_pass)

    assert entrypoint < venv < cutover < reserve < restart_receipt
    assert restart_receipt < announce < deploy_pass < completed
    assert "install_target_release_wheel" not in fast_path
    assert " pip " not in fast_path
    assert "ln -sfn" not in fast_path
    assert "mv -T" not in fast_path
    assert "systemctl restart" not in fast_path
    assert "qualifying_restart_unattested" in fast_path
    assert '"already_active\\": true' in fast_path
    assert '"$release/.venv/bin/python" -I -B -P -s -' in venv_attestation
    assert cutover_attestation.count('"$release/.venv/bin/python" -I -B') == 1
    assert (
        'run_cutover_artifact_step \\\n'
        '    "verify" "$release" "$release" "$expected_head"'
        in cutover_attestation
    )


def _write_fake_flock(path: Path) -> None:
    path.write_text(
        f"""\
#!{Path(sys.executable).resolve()}
import fcntl
import sys

descriptor = int(sys.argv[-1])
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _lock_shell_script(*, hold: bool) -> str:
    suffix = (
        'printf locked > "$LOCK_MARKER"\nIFS= read -r _'
        if hold
        else 'printf "rc=0\\n"'
    )
    return f'''
set -euo pipefail
source "$1"
SYSTEM_PYTHON="$2"
SYSTEM_FLOCK="$3"
LOCK_PATH="$4"
LOCK_PARENT="$5"
TRUSTED_UID="$6"
TRUSTED_GID="$7"
STATUS_LOG="$8"
LOCK_MARKER="$9"
write_status() {{
  printf '%s\\n' "$1" >> "$STATUS_LOG"
}}
set +e
acquire_deploy_lock_at \
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
  "203" \
  "$LOCK_PATH" \
  "$LOCK_PARENT" \
  "$TRUSTED_UID" \
  "$TRUSTED_GID"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  printf 'rc=%s\\n' "$rc"
  exit "$rc"
fi
{suffix}
'''


def _lock_process_args(
    helper: Path,
    fake_flock: Path,
    lock_path: Path,
    lock_parent: Path,
    status_log: Path,
    marker: Path,
) -> list[str]:
    return [
        "bash",
        "-c",
        _lock_shell_script(hold=False),
        "deploy-lock",
        str(helper),
        str(Path(sys.executable).resolve()),
        str(fake_flock),
        str(lock_path),
        str(lock_parent),
        str(os.getuid()),
        str(os.getgid()),
        str(status_log),
        str(marker),
    ]


def test_deploy_lock_rejects_concurrent_process_and_allows_bounded_retry(tmp_path):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    lock_parent = root / "trusted-lock-parent"
    lock_parent.mkdir(mode=0o700)
    os.chown(lock_parent, os.getuid(), os.getgid())
    lock_path = lock_parent / "deploy.lock"
    fake_flock = root / "fake-flock"
    _write_fake_flock(fake_flock)
    holder_status = root / "holder-status"
    contender_status = root / "contender-status"
    retry_status = root / "retry-status"
    marker = root / "lock-held"
    holder_args = _lock_process_args(
        helper,
        fake_flock,
        lock_path,
        lock_parent,
        holder_status,
        marker,
    )
    holder_args[2] = _lock_shell_script(hold=True)
    holder = subprocess.Popen(
        holder_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and holder.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("timed out waiting for the first deploy lock holder")
            time.sleep(0.02)
        assert holder.poll() is None, holder.stderr.read() if holder.stderr else ""

        contender = subprocess.run(
            _lock_process_args(
                helper,
                fake_flock,
                lock_path,
                lock_parent,
                contender_status,
                root / "unused-contender-marker",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert contender.returncode == 12
        assert "rc=12" in contender.stdout
        assert contender_status.read_text(encoding="utf-8").splitlines() == [
            "blocked_concurrent_deploy"
        ]

        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, holder_stderr
        assert holder_stdout == ""

        retry = subprocess.run(
            _lock_process_args(
                helper,
                fake_flock,
                lock_path,
                lock_parent,
                retry_status,
                root / "unused-retry-marker",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert retry.returncode == 0, retry.stderr
        assert retry.stdout.strip() == "rc=0"
        assert not retry_status.exists()
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


@pytest.mark.parametrize("artifact_kind", ["symlink", "hardlink", "wrong-mode"])
def test_deploy_lock_rejects_untrusted_lock_file_artifact(tmp_path, artifact_kind):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    lock_parent = root / "trusted-lock-parent"
    lock_parent.mkdir(mode=0o700)
    os.chown(lock_parent, os.getuid(), os.getgid())
    lock_path = lock_parent / "deploy.lock"
    external = root / f"{artifact_kind}-source"
    external.write_text("untrusted lock artifact\n", encoding="utf-8")
    external.chmod(0o600)
    os.chown(external, os.getuid(), os.getgid())
    if artifact_kind == "symlink":
        lock_path.symlink_to(external)
    elif artifact_kind == "hardlink":
        os.link(external, lock_path)
    else:
        lock_path.write_text("wrong mode\n", encoding="utf-8")
        lock_path.chmod(0o644)
        os.chown(lock_path, os.getuid(), os.getgid())
    fake_flock = root / "fake-flock"
    _write_fake_flock(fake_flock)
    status_log = root / "status"

    rejected = subprocess.run(
        _lock_process_args(
            helper,
            fake_flock,
            lock_path,
            lock_parent,
            status_log,
            root / "unused-marker",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert rejected.returncode == 12
    assert "rc=12" in rejected.stdout
    assert status_log.read_text(encoding="utf-8").splitlines() == [
        "blocked_deploy_lock_invalid"
    ]


def _run_entrypoint_attestation(helper: Path, release: Path) -> subprocess.CompletedProcess:
    owner = subprocess.run(
        ["id", "-un"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    script = r'''
set -euo pipefail
source "$1"
OWNER="$2"
release="$3"
sudo() {
  if [ "$1" = "-n" ] && [ "$2" = "-u" ]; then
    shift 3
  fi
  command "$@"
}
attest_target_release_entrypoints "$release"
'''
    return subprocess.run(
        ["bash", "-c", script, "entrypoint-attest", str(helper), owner, str(release)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _write_valid_entrypoint_fixture(release: Path) -> Path:
    release = release.resolve()
    bin_dir = release / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(Path(sys.executable).resolve())
    expected_shebang = f"#!{bin_dir / 'python'}\n"
    for name in RELEASE_ENTRYPOINTS:
        path = bin_dir / name
        path.write_text(
            expected_shebang + "print('entrypoint-fixture-ok')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    return release


def test_release_entrypoint_attestation_rejects_staging_shebang_after_rename(
    tmp_path,
):
    helper = RUNTIME / "muncho-auto-deploy-release"
    staging = tmp_path / ".hermes-agent-deadbeef0000.tmp.123"
    release = tmp_path / "hermes-agent-deadbeef0000"
    subprocess.run(
        [sys.executable, "-m", "venv", str(staging / ".venv")],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    bin_dir = staging / ".venv" / "bin"
    stale_shebang = f"#!{bin_dir / 'python'}\n"
    for name in RELEASE_ENTRYPOINTS:
        path = bin_dir / name
        path.write_text(stale_shebang + "raise SystemExit(0)\n", encoding="utf-8")
        path.chmod(0o755)
    staging.rename(release)

    rejected = _run_entrypoint_attestation(helper, release)

    assert rejected.returncode != 0
    assert "BLOCKED_RELEASE_ENTRYPOINT_INVALID:hermes" in rejected.stderr

    final_shebang = f"#!{release / '.venv/bin/python'}\n"
    for name in RELEASE_ENTRYPOINTS:
        path = release / ".venv" / "bin" / name
        path.write_text(final_shebang + "raise SystemExit(0)\n", encoding="utf-8")
        path.chmod(0o755)

    accepted = _run_entrypoint_attestation(helper, release)

    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "target_release_entrypoints=ok"


def test_release_entrypoint_attestation_rejects_symlinked_bin_without_execution(
    tmp_path,
):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    release = root / "hermes-agent-deadbeef0000"
    external_bin = root / "external-bin"
    marker = root / "external-python-invoked"
    external_bin.mkdir()
    external_python = external_bin / "python"
    external_python.write_text(
        "#!/bin/sh\n"
        f"printf invoked > {str(marker)!r}\n"
        "exit 99\n",
        encoding="utf-8",
    )
    external_python.chmod(0o755)
    for name in RELEASE_ENTRYPOINTS:
        path = external_bin / name
        path.write_text(
            f"#!{external_python}\nraise SystemExit(0)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    (release / ".venv").mkdir(parents=True)
    (release / ".venv" / "bin").symlink_to(external_bin)

    rejected = _run_entrypoint_attestation(helper, release)

    assert rejected.returncode != 0
    assert "BLOCKED_RELEASE_ENTRYPOINT_BIN_INVALID" in rejected.stderr
    assert not marker.exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_release_entrypoint_attestation_rejects_linked_launcher(
    tmp_path,
    link_kind,
):
    helper = RUNTIME / "muncho-auto-deploy-release"
    release = _write_valid_entrypoint_fixture(
        tmp_path.resolve() / f"hermes-agent-{link_kind}"
    )
    launcher = release / ".venv" / "bin" / "hermes"
    linked_path = tmp_path.resolve() / f"{link_kind}-target"

    if link_kind == "symlink":
        linked_path.write_bytes(launcher.read_bytes())
        linked_path.chmod(0o755)
        launcher.unlink()
        launcher.symlink_to(linked_path)
    else:
        os.link(launcher, linked_path)

    rejected = _run_entrypoint_attestation(helper, release)

    assert rejected.returncode != 0
    assert "BLOCKED_RELEASE_ENTRYPOINT_" in rejected.stderr
    assert "hermes" in rejected.stderr


def test_entrypoint_attestation_anchors_directory_and_open_launcher_identity():
    source = (RUNTIME / "muncho-auto-deploy-release").read_text(encoding="utf-8")
    attestation = source[
        source.index("attest_target_release_entrypoints() {") : source.index(
            "release_identity_matches() {"
        )
    ]

    assert 'getattr(os, "O_DIRECTORY", 0)' in attestation
    assert attestation.count('getattr(os, "O_NOFOLLOW", 0)') >= 2
    assert "bin_descriptor = os.open(bin_path, dir_flags)" in attestation
    assert (
        "before = os.stat(name, dir_fd=bin_descriptor, follow_symlinks=False)"
        in attestation
    )
    assert "fd = os.open(name, flags, dir_fd=bin_descriptor)" in attestation
    assert "opened_before = os.fstat(fd)" in attestation
    assert "opened_after = os.fstat(fd)" in attestation
    assert "anchored_after = os.stat(" in attestation
    assert "identity(before) != identity(opened_before)" in attestation
    assert "identity(before) != identity(opened_after)" in attestation
    assert "identity(before) != identity(anchored_after)" in attestation
    assert "os.close(fd)" in attestation


def _pip_install_probe_package(python: Path, source: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--force-reinstall",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _make_local_build_backend_visible(python: Path) -> None:
    """Expose the test runner's setuptools to a nested offline venv.

    ``venv --system-site-packages`` inherits packages from the base interpreter,
    not from the currently active virtualenv.  Managed Python installations can
    therefore create a perfectly valid nested venv whose offline pip cannot see
    the setuptools already available to this test process.  A test-only ``.pth``
    keeps the package build offline while preserving the real pip reinstall and
    generated-console-script path exercised below.
    """
    probe = subprocess.run(
        [str(python), "-c", "import setuptools.build_meta"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode == 0:
        return

    setuptools_spec = importlib.util.find_spec("setuptools")
    assert setuptools_spec is not None and setuptools_spec.origin is not None
    parent_site_packages = Path(setuptools_spec.origin).resolve().parents[1]
    nested_purelib = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    Path(nested_purelib.stdout.strip(), "_hermes_test_build_backend.pth").write_text(
        f"{parent_site_packages}\n",
        encoding="utf-8",
    )
    verification = subprocess.run(
        [str(python), "-c", "import setuptools.build_meta"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert verification.returncode == 0, verification.stderr


def test_real_pip_reinstall_rebinds_all_entrypoints_after_staging_rename():
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools is required for the local no-network package build")

    helper = RUNTIME / "muncho-auto-deploy-release"
    with tempfile.TemporaryDirectory(
        prefix="muncho-entrypoints-",
        dir="/tmp",
    ) as raw_root:
        root = Path(raw_root).resolve()
        staging = root / ".hermes-agent-deadbeef0000.tmp.123"
        release = root / "hermes-agent-deadbeef0000"
        staging.mkdir()
        (staging / "pyproject.toml").write_text(
            """\
[build-system]
requires = []
build-backend = "setuptools.build_meta"

[project]
name = "muncho-entrypoint-rebind-probe"
version = "0.0.1"

[project.scripts]
hermes = "entrypoint_probe:main"
hermes-acp = "entrypoint_probe:main"
hermes-agent = "entrypoint_probe:main"
muncho-ops = "entrypoint_probe:main"
muncho-release = "entrypoint_probe:main"

[tool.setuptools]
py-modules = ["entrypoint_probe"]
""",
            encoding="utf-8",
        )
        (staging / "entrypoint_probe.py").write_text(
            "def main():\n"
            "    print('entrypoint-probe-ok')\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                "--system-site-packages",
                str(staging / ".venv"),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        staging_python = staging / ".venv" / "bin" / "python"
        _make_local_build_backend_visible(staging_python)
        first_install = _pip_install_probe_package(staging_python, staging)
        assert first_install.returncode == 0, first_install.stderr
        os.chown(staging / ".venv" / "bin", os.getuid(), os.getgid())
        for name in RELEASE_ENTRYPOINTS:
            os.chown(
                staging / ".venv" / "bin" / name,
                os.getuid(),
                os.getgid(),
            )
            assert (staging / ".venv" / "bin" / name).read_bytes().splitlines()[0] == (
                f"#!{staging_python}".encode("ascii")
            )

        staging.rename(release)
        stale = _run_entrypoint_attestation(helper, release)
        assert stale.returncode != 0
        assert "BLOCKED_RELEASE_ENTRYPOINT_INVALID:hermes" in stale.stderr

        release_python = release / ".venv" / "bin" / "python"
        final_install = _pip_install_probe_package(release_python, release)
        assert final_install.returncode == 0, final_install.stderr
        os.chown(release / ".venv" / "bin", os.getuid(), os.getgid())
        for name in RELEASE_ENTRYPOINTS:
            os.chown(
                release / ".venv" / "bin" / name,
                os.getuid(),
                os.getgid(),
            )
        accepted = _run_entrypoint_attestation(helper, release)
        assert accepted.returncode == 0, accepted.stderr
        assert accepted.stdout.strip() == "target_release_entrypoints=ok"
        for name in RELEASE_ENTRYPOINTS:
            launcher = release / ".venv" / "bin" / name
            assert launcher.read_bytes().splitlines()[0] == (
                f"#!{release_python}".encode("ascii")
            )
            executed = subprocess.run(
                [str(launcher)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert executed.returncode == 0, executed.stderr
            assert executed.stdout.strip() == "entrypoint-probe-ok"


def _run_active_invariance_check(
    helper: Path,
    active_link: Path,
    expected_active: Path,
    target: Path,
    stage: str,
) -> subprocess.CompletedProcess:
    script = r'''
set -euo pipefail
source "$1"
ACTIVE_LINK="$2"
write_status() {
  printf 'status=%s extra=%s\n' "$1" "$4"
}
set +e
require_inactive_target_with_unchanged_active \
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
  "203" \
  "$3" \
  "$4" \
  "$5"
rc=$?
set -e
printf 'rc=%s\n' "$rc"
exit "$rc"
'''
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "active-invariance",
            str(helper),
            str(active_link),
            str(expected_active),
            str(target),
            stage,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_active_release_invariance_guard_blocks_changed_or_active_target(tmp_path):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    expected_active = root / "hermes-agent-aaaaaaaaaaaa"
    target = root / "hermes-agent-bbbbbbbbbbbb"
    unexpected = root / "hermes-agent-cccccccccccc"
    for path in (expected_active, target, unexpected):
        path.mkdir()
    active_link = root / "active"
    active_link.symlink_to(expected_active)

    unchanged = _run_active_invariance_check(
        helper,
        active_link,
        expected_active,
        target,
        "unchanged",
    )
    assert unchanged.returncode == 0, unchanged.stderr
    assert unchanged.stdout.strip() == "rc=0"

    active_link.unlink()
    active_link.symlink_to(unexpected)
    changed = _run_active_invariance_check(
        helper,
        active_link,
        expected_active,
        target,
        "changed",
    )
    assert changed.returncode == 13
    assert "status=blocked_active_release_changed_during_deploy" in changed.stdout
    assert '"stage": "changed"' in changed.stdout
    assert '"active_unchanged": false' in changed.stdout
    assert '"target_inactive": true' in changed.stdout
    assert "rc=13" in changed.stdout

    active_link.unlink()
    active_link.symlink_to(target)
    target_active = _run_active_invariance_check(
        helper,
        active_link,
        expected_active,
        target,
        "target-active",
    )
    assert target_active.returncode == 13
    assert "status=blocked_active_release_changed_during_deploy" in target_active.stdout
    assert '"target_inactive": false' in target_active.stdout
