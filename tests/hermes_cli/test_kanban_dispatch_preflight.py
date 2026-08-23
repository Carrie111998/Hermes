"""Fail-closed dispatch preflight and immutable Git identity gates."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _task(
    conn,
    repo: Path,
    *,
    expected_base_sha: str | None = None,
    candidate_sha: str | None = None,
    clean_workspace_policy: str = "require_clean",
    dispatchable: bool = True,
) -> tuple[str, kb.Task]:
    tid = kb.create_task(
        conn,
        title="immutable lane",
        assignee="developer",
        workspace_kind="dir",
        workspace_path=str(repo),
        branch_name=None,
        expected_base_sha=expected_base_sha,
        candidate_sha=candidate_sha,
        clean_workspace_policy=clean_workspace_policy,
        dispatchable=dispatchable,
    )
    task = kb.get_task(conn, tid)
    assert task is not None
    return tid, task


def _verdict(conn, task: kb.Task, *, lane: str = "ready") -> kb.DispatchPreflightVerdict:
    return kb.evaluate_dispatch_preflight(
        conn,
        task,
        lane=lane,
        profile_exists_fn=lambda _name: True,
    )


def test_immutable_contract_is_exposed_on_cli_tool_and_dashboard_surfaces() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    kc.build_parser(subparsers)
    args = parser.parse_args(
        [
            "kanban", "create", "immutable",
            "--workspace", "dir:/tmp/repo",
            "--branch", "feature/preflight",
            "--expected-base-sha", "a" * 40,
            "--candidate-sha", "b" * 40,
            "--clean-workspace-policy", "require_clean",
            "--non-dispatchable",
        ]
    )
    assert args.branch == "feature/preflight"
    assert args.expected_base_sha == "a" * 40
    assert args.candidate_sha == "b" * 40
    assert args.clean_workspace_policy == "require_clean"
    assert args.non_dispatchable is True

    from tools.kanban_tools import KANBAN_CREATE_SCHEMA

    properties = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert {
        "branch_name",
        "expected_base_sha",
        "candidate_sha",
        "clean_workspace_policy",
        "dispatchable",
    } <= set(properties)

    from plugins.kanban.dashboard.plugin_api import CreateTaskBody

    payload = CreateTaskBody(
        title="immutable",
        branch_name="feature/preflight",
        expected_base_sha="a" * 40,
        candidate_sha="b" * 40,
        clean_workspace_policy="require_clean",
        dispatchable=False,
    )
    assert payload.branch_name == "feature/preflight"
    assert payload.dispatchable is False


@pytest.mark.parametrize("field", ["expected_base_sha", "candidate_sha"])
def test_create_rejects_malformed_or_abbreviated_sha(conn, field: str) -> None:
    with pytest.raises(ValueError, match="40 hexadecimal"):
        kb.create_task(
            conn,
            title="bad sha",
            assignee="developer",
            **{field: "deadbeef"},
        )
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_deleted_assignee_profile_fails_closed(conn, tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    _tid, task = _task(conn, repo, expected_base_sha=head)

    verdict = kb.evaluate_dispatch_preflight(
        conn,
        task,
        lane="ready",
        profile_exists_fn=lambda _name: False,
    )

    assert not verdict.ok
    assert "profile_missing" in verdict.reason_codes


def test_unexpected_probe_error_fails_closed(conn, tmp_path: Path, monkeypatch) -> None:
    repo, head = _repo(tmp_path)
    _tid, task = _task(conn, repo, expected_base_sha=head)
    monkeypatch.setattr(kb, "_git_common_dir", lambda _path: 1 / 0)

    verdict = kb._safe_dispatch_preflight(
        conn, task, lane="ready", board=None
    )

    assert not verdict.ok
    assert verdict.reason_codes == ("preflight_internal_error",)
    assert verdict.evidence["error_type"] == "ZeroDivisionError"


def test_explicit_non_dispatchable_human_lane_is_preserved(conn, tmp_path: Path) -> None:
    repo, _head = _repo(tmp_path)
    _tid, task = _task(conn, repo, dispatchable=False)

    verdict = kb.evaluate_dispatch_preflight(
        conn,
        task,
        lane="ready",
        profile_exists_fn=lambda _name: False,
    )

    assert verdict.ok
    assert verdict.evidence["dispatchable"] is False


def test_missing_dir_and_dir_repo_mismatch_fail_closed(conn, tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    _tid, task = _task(conn, missing, expected_base_sha="a" * 40)
    assert "workspace_missing" in _verdict(conn, task).reason_codes

    plain = tmp_path / "plain"
    plain.mkdir()
    tid = kb.create_task(
        conn,
        title="plain dir",
        assignee="developer",
        workspace_kind="dir",
        workspace_path=str(plain),
        expected_base_sha="a" * 40,
    )
    plain_task = kb.get_task(conn, tid)
    assert plain_task is not None
    assert "git_repository_required" in _verdict(conn, plain_task).reason_codes


def test_dirty_wrong_branch_and_wrong_base_are_reported(conn, tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    tid, task = _task(conn, repo, expected_base_sha="b" * 40)
    conn.execute("UPDATE tasks SET branch_name = 'expected' WHERE id = ?", (tid,))
    conn.commit()
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    task = kb.get_task(conn, tid)
    assert task is not None

    verdict = _verdict(conn, task)

    assert {"workspace_dirty", "branch_mismatch", "expected_base_mismatch"} <= set(
        verdict.reason_codes
    )
    assert verdict.evidence["head_sha"] == head
    assert verdict.evidence["git_common_dir"]


def test_ready_lane_prefers_declared_base_when_candidate_is_also_recorded(
    conn, tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path)
    _tid, task = _task(
        conn,
        repo,
        expected_base_sha=head,
        candidate_sha="c" * 40,
    )

    verdict = _verdict(conn, task, lane="ready")

    assert verdict.ok


def test_review_lane_requires_candidate_even_without_a_base_contract(
    conn, tmp_path: Path,
) -> None:
    repo, _head = _repo(tmp_path)
    _tid, task = _task(
        conn,
        repo,
        clean_workspace_policy="allow_dirty",
    )

    verdict = _verdict(conn, task, lane="review")

    assert not verdict.ok
    assert "candidate_sha_required" in verdict.reason_codes


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_candidate_lane_requires_exact_candidate_sha(
    conn, tmp_path: Path, lane: str,
) -> None:
    repo, head = _repo(tmp_path)
    _tid, task = _task(conn, repo, candidate_sha="c" * 40)

    verdict = _verdict(conn, task, lane=lane)

    assert not verdict.ok
    assert "candidate_sha_mismatch" in verdict.reason_codes
    assert verdict.evidence["head_sha"] == head


def test_active_workspace_branch_lease_allows_only_one_claim(conn, tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    first_id, _first = _task(conn, repo, expected_base_sha=head)
    second_id, second = _task(conn, repo, expected_base_sha=head)
    first = kb.claim_task(conn, first_id)
    assert first is not None

    verdict = _verdict(conn, second)

    assert not verdict.ok
    assert "workspace_lease_conflict" in verdict.reason_codes
    assert kb.claim_task(conn, second_id) is None


def test_new_worktree_checks_anchor_then_materialized_identity(
    conn, tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path)
    tid = kb.create_task(
        conn,
        title="new worktree",
        assignee="developer",
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name="feature/preflight",
        expected_base_sha=head,
        clean_workspace_policy="require_clean",
    )
    task = kb.get_task(conn, tid)
    assert task is not None
    before = _verdict(conn, task)
    assert before.ok and before.evidence["pre_materialization"] is True

    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    workspace, branch = kb._resolve_worktree_workspace(claimed)
    kb.set_workspace_path(conn, tid, workspace)
    kb.set_branch_name(conn, tid, branch)
    materialized = kb.get_task(conn, tid)
    assert materialized is not None

    after = _verdict(conn, materialized)

    assert after.ok
    assert after.evidence["head_sha"] == head
    assert after.evidence["branch"] == "feature/preflight"


def test_parent_reopened_between_preflight_and_claim_cannot_spawn(
    conn, tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path)
    parent_id = kb.create_task(conn, title="parent", assignee="developer")
    assert kb.complete_task(conn, parent_id)
    child_id = kb.create_task(
        conn,
        title="child",
        assignee="developer",
        parents=[parent_id],
        workspace_kind="dir",
        workspace_path=str(repo),
        expected_base_sha=head,
    )
    child = kb.get_task(conn, child_id)
    assert child is not None and _verdict(conn, child).ok

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
            (parent_id,),
        )

    assert kb.claim_task(conn, child_id) is None
    child = kb.get_task(conn, child_id)
    assert child is not None and child.status == "todo"


def test_dispatch_preflight_failure_lands_durable_hold_and_no_spawn(
    conn, tmp_path: Path, monkeypatch,
) -> None:
    repo, head = _repo(tmp_path)
    tid, _task_row = _task(conn, repo, expected_base_sha=head)
    spawned: list[str] = []
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: False)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, _workspace: spawned.append(task.id),
        reconcile_orphans=False,
    )

    task = kb.get_task(conn, tid)
    assert task is not None
    assert spawned == []
    assert result.preflight_held == [tid]
    assert task.status == "blocked"
    assert task.dispatch_hold_reason
    run = kb.list_runs(conn, tid)[-1]
    assert run.outcome == "preflight_failed"
    events = [event for event in kb.list_events(conn, tid) if event.kind == "dispatch_preflight_failed"]
    assert len(events) == 1
