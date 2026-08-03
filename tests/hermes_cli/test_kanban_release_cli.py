"""`hermes kanban release` — the operator surface for the Release/Measure gate.

`release_measure` is deliberately unassigned: no ordinary worker may hold it
(see test_kanban_qualifier). Until this command existed the only code path
that ran `release_product_task` was the worker-side `kanban_complete` tool, so
the human whose gate it is had no way through it — `hermes kanban complete`
calls plain `complete_task`, which correctly refuses for lack of release
orchestration. These tests exercise the real mechanism with a controlled test-created `scripts/run_tests.sh`; they do not run the repository's production verification suite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def release_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_story_branch(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "release@example.com")
    _git(repo, "config", "user.name", "Release Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "run_tests.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _git(repo, "add", "README.md", "scripts/run_tests.sh")
    _git(repo, "commit", "-m", "base")
    branch = "story/release-cli"
    _git(repo, "switch", "-c", branch)
    (repo / "story.txt").write_text("released\n", encoding="utf-8")
    _git(repo, "add", "story.txt")
    _git(repo, "commit", "-m", "story")
    source_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    return repo, branch, source_sha


def _release_board(board: str, repo: Path, *, policy: str = "manual", pull_request_required: bool = False) -> None:
    kb.ensure_product_board_defaults(board, default_workdir=str(repo))
    path = kb.board_metadata_path(board)
    meta = json.loads(path.read_text(encoding="utf-8"))
    workflow = meta.setdefault("product_workflow", {})
    workflow["deployment_policy"] = policy
    if pull_request_required:
        workflow["pull_request_required"] = True
    path.write_text(json.dumps(meta), encoding="utf-8")
    if _git(repo, "status", "--porcelain"):
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore integration worktrees")


def _seed_reviewed_card(
    conn,
    board: str,
    repo: Path,
    branch: str,
    source_sha: str,
    *,
    step: str = "release_measure",
) -> str:
    task_id = kb.create_task(
        conn,
        title="Story: operator release",
        board=board,
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name=branch,
        workflow_template_id="product",
        current_step_key=step,
    )
    with kb.write_txn(conn):
        kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="development",
            metadata={
                "ai_provenance": {
                    "writer": {
                        "agent": "claude-code",
                        "branch": branch,
                        "commit": source_sha,
                    }
                }
            },
        )
        kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="test",
            metadata={
                "workflow_outcome": {"verdict": "passed"},
                "ai_provenance": {"tester": {"agent": "hermes", "result": "passed"}},
            },
        )
        kb._synthesize_ended_run(
            conn,
            task_id,
            outcome="advanced",
            step_key="review",
            metadata={
                "workflow_outcome": {"verdict": "approved"},
                "ai_provenance": {
                    "writer": {"agent": "claude-code"},
                    "reviewer": {
                        "agent": "openai-codex",
                        "verdict": "approved",
                        "reviewed_branch": branch,
                        "reviewed_commit": source_sha,
                    },
                },
            },
        )
    return task_id


def _release(task_id: str, board: str, *extra: str) -> str:
    args = " ".join(extra) or '--note "measured manually"'
    return kc.run_slash(f"--board {board} release {task_id} {args}")


def _release_snapshot(conn, repo: Path, task_id: str):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return (
        _git(repo, "rev-parse", "main"),
        task.status,
        task.current_step_key,
        task.current_run_id,
        tuple((event.kind, event.payload) for event in kb.list_events(conn, task_id)),
    )


def _assert_release_refused(task_id: str, board: str, repo: Path, expected: str):
    with kb.connect(board=board) as conn:
        before = _release_snapshot(conn, repo, task_id)
    out = _release(task_id, board)
    with kb.connect(board=board) as conn:
        after = _release_snapshot(conn, repo, task_id)
    assert expected in out and "no changes were made" in out.lower()
    assert after == before


def test_release_cli_completes_an_evidenced_release_measure_card(
    release_home, tmp_path
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-green"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(conn, board, repo, branch, source_sha)

    out = _release(task_id, board, '--note "Released and measured by operator" --operator-label Ole')

    assert "Released" in out
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        events = [event.kind for event in kb.list_events(conn, task_id)]
    assert task is not None
    assert task.status == "done"
    assert task.current_step_key == "done"
    # Integration really happened — release is orchestration, not a status flip.
    assert (repo / "story.txt").read_text(encoding="utf-8") == "released\n"
    assert "deployment_policy_evaluated" in events
    assert run is not None and isinstance(run.metadata, dict)
    assert run.metadata.get("release_operator_label") == "Ole"
    assert run.metadata.get("release_surface") == "cli"
    assert "released_by" not in run.metadata


def test_release_cli_refuses_invalid_deployment_policy_before_mutation(
    release_home, tmp_path
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-invalid-policy"
    _release_board(board, repo, policy="unsupported")
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(conn, board, repo, branch, source_sha)

    _assert_release_refused(task_id, board, repo, "unsupported deployment policy 'unsupported'")


def test_release_cli_refuses_required_policy_without_adapter_before_mutation(
    release_home, tmp_path
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-required-policy"
    _release_board(board, repo, policy="required")
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(conn, board, repo, branch, source_sha)

    _assert_release_refused(task_id, board, repo, "adapter-backed controller is required")


def test_release_cli_refuses_missing_required_pull_request_before_mutation(
    release_home, tmp_path
):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-required-pr"
    _release_board(board, repo, pull_request_required=True)
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(conn, board, repo, branch, source_sha)

    _assert_release_refused(task_id, board, repo, "missing required pull_request metadata")


def test_release_cli_refuses_a_card_outside_release_measure(release_home, tmp_path):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-wrong-step"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(
            conn, board, repo, branch, source_sha, step="development"
        )
        before = kb.get_task(conn, task_id)

    out = _release(task_id, board)

    assert "release_measure" in out
    with kb.connect(board=board) as conn:
        after = kb.get_task(conn, task_id)
    assert after is not None and before is not None
    assert after.status == before.status
    assert after.current_step_key == "development"


def test_release_cli_refuses_an_unresolved_preflight(release_home, tmp_path):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-preflight"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(conn, board, repo, branch, source_sha)
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Need a human decision",
            kind="needs_input",
            attempted_resolutions=["read docs"],
            expected_run_id=claimed.current_run_id,
            board=board,
            human_escalation_assignee="resolver",
        )
        assert kb.has_unresolved_product_preflight(conn, task_id)

    out = _release(task_id, board)

    assert "preflight" in out
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None and task.status != "done"


def test_release_cli_refuses_inside_a_kanban_worker(
    release_home, tmp_path, monkeypatch
):
    """Release/Measure is a human gate; the env check is defense in depth, not authorization: a caller controlling its environment can bypass it."""
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-worker"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(conn, board, repo, branch, source_sha)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    out = _release(task_id, board)

    assert "human" in out.lower() or "operator" in out.lower()
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None and task.status != "done"


def test_release_cli_requires_a_measurement_note(release_home, tmp_path):
    repo, branch, source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-note"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        task_id = _seed_reviewed_card(conn, board, repo, branch, source_sha)

    out = _release(task_id, board, '--note "   "')

    assert "note" in out.lower()
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None and task.status != "done"


def test_release_cli_reports_missing_release_evidence(release_home, tmp_path):
    """No reviewer approval → the evidence gate refuses, naming what's missing,
    and the card stays exactly where it was."""
    repo, branch, _source_sha = _repo_with_story_branch(tmp_path)
    board = "release-cli-evidence"
    _release_board(board, repo)
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: unevidenced",
            board=board,
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name=branch,
            workflow_template_id="product",
            current_step_key="release_measure",
        )

    out = _release(task_id, board)

    assert "evidence" in out.lower()
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status != "done"
    assert task.current_step_key == "release_measure"
