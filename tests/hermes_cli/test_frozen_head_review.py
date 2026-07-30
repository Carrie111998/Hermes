from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def git_tree(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "implementation"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "implemented.txt").write_text("done\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "implementation"],
        check=True,
    )
    return repo, git_head(repo)


def complete_with_evidence(conn, task_id: str) -> None:
    assert kb.complete_task(
        conn, task_id, summary="implementation complete",
        metadata={"changed_files": ["implemented.txt"], "tests_run": 3},
    )


def evidence(head: str, repo: Path, *, include_tree: bool = False) -> dict:
    result = {
        "head_sha": head,
        "worktree_path": str(repo),
        "clean": True,
        "implementation_complete": True,
        "source": "frozen-head-cli",
    }
    if include_tree:
        result["tree_sha"] = git_tree(repo)
    return result


def test_db_accepts_completed_clean_exact_head_without_claim(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="frozen", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, task_id)
        assert kb.submit_frozen_head_for_review(
            conn, task_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo)
        )
        assert kb.get_task(conn, task_id).status == "review"
        event = kb.list_events(conn, task_id)[-1]
        assert event.kind == "review_submitted_frozen_head"
        assert event.payload["head_sha"] == head
        assert event.payload["tree_sha"] == git_tree(repo)
        assert event.payload["implementation_run_id"]


@pytest.mark.parametrize("status", ["scheduled", "ready"])
def test_scheduled_ready_submit_directly_without_transition_dance(kanban_home, tmp_path, status):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="scheduled recovery", workspace_kind="dir", workspace_path=str(repo))
        assert kb.schedule_task(conn, task_id, reason="today's frozen head")
        if status == "ready":
            assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == status
        kb._synthesize_ended_run(
            conn, task_id, outcome="completed",
            summary="implementation complete",
            metadata={"changed_files": ["implemented.txt"], "tests_run": 3},
        )
        assert kb.submit_frozen_head_for_review(
            conn, task_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo)
        )


@pytest.mark.parametrize("status", ["scheduled", "ready", "todo", "done"])
def test_every_accepted_dormant_state_is_explicitly_supported(kanban_home, tmp_path, status):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title=f"accepted {status}", workspace_kind="dir", workspace_path=str(repo))
        if status == "done":
            complete_with_evidence(conn, task_id)
        else:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
            conn.commit()
            kb._synthesize_ended_run(
                conn, task_id, outcome="completed", summary="implementation complete",
                metadata={"changed_files": ["implemented.txt"], "tests_run": 3},
            )
        assert kb.submit_frozen_head_for_review(
            conn, task_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo)
        )


@pytest.mark.parametrize("status", ["triage", "running", "archived"])
def test_non_dormant_states_are_rejected(kanban_home, tmp_path, status):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title=f"reject {status}", workspace_kind="dir", workspace_path=str(repo))
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        conn.commit()
        with pytest.raises(kb.FrozenHeadReviewError, match="cannot be submitted"):
            kb.submit_frozen_head_for_review(
                conn, task_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo)
            )


@pytest.mark.parametrize(
    "mutator, expected",
    [
        (lambda e, r: {**e, "head_sha": "0" * 40}, "evidence head_sha"),
        (lambda e, r: {**e, "clean": False}, "clean must be true"),
        (lambda e, r: {**e, "implementation_complete": False}, "implementation_complete"),
    ],
)
def test_db_rejects_malformed_or_mismatched_evidence(kanban_home, tmp_path, mutator, expected):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="reject", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, task_id)
        with pytest.raises(kb.FrozenHeadReviewError, match=expected):
            kb.submit_frozen_head_for_review(
                conn, task_id, head_sha=head, worktree_path=str(repo), evidence=mutator(evidence(head, repo), repo)
            )
        assert kb.get_task(conn, task_id).status == "done"
        assert kb.list_events(conn, task_id)[-1].kind == "review_submission_rejected"


def test_db_rejects_missing_or_malformed_head_and_unrelated_state(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="head validation", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, task_id)
        valid = evidence(head, repo)
        with pytest.raises(kb.FrozenHeadReviewError, match="full 40-character"):
            kb.submit_frozen_head_for_review(conn, task_id, head_sha="", worktree_path=str(repo), evidence=valid)
        with pytest.raises(kb.FrozenHeadReviewError, match="full 40-character"):
            kb.submit_frozen_head_for_review(conn, task_id, head_sha="not-a-sha", worktree_path=str(repo), evidence=valid)

        unrelated_id = kb.create_task(conn, title="not done", workspace_kind="dir", workspace_path=str(repo))
        with pytest.raises(kb.FrozenHeadReviewError, match="completed implementation evidence is absent"):
            kb.submit_frozen_head_for_review(conn, unrelated_id, head_sha=head, worktree_path=str(repo), evidence=valid)


def test_db_rejects_dirty_head_live_claim_and_missing_implementation_evidence(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        dirty_id = kb.create_task(conn, title="dirty", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, dirty_id)
        (repo / "dirty.txt").write_text("uncommitted\n")
        with pytest.raises(kb.FrozenHeadReviewError, match="dirty"):
            kb.submit_frozen_head_for_review(conn, dirty_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo))
        (repo / "dirty.txt").unlink()

        live_id = kb.create_task(conn, title="live", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, live_id)
        conn.execute("UPDATE tasks SET claim_lock='live', worker_pid=1234 WHERE id=?", (live_id,))
        conn.commit()
        with pytest.raises(kb.FrozenHeadReviewError, match="live claim"):
            kb.submit_frozen_head_for_review(conn, live_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo))

        no_evidence_id = kb.create_task(conn, title="no evidence", workspace_kind="dir", workspace_path=str(repo))
        assert kb.complete_task(conn, no_evidence_id, result="done")
        with pytest.raises(kb.FrozenHeadReviewError, match="metadata is malformed"):
            kb.submit_frozen_head_for_review(conn, no_evidence_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo))


def test_cli_submit_review_uses_same_fail_closed_route(kanban_home, tmp_path, capsys):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="cli", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, task_id)
    parser = __import__("argparse").ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args([
        "kanban", "submit-review", task_id, "--head-sha", head,
        "--worktree", str(repo), "--evidence", json.dumps(evidence(head, repo)),
    ])
    assert kc.kanban_command(args) == 0
    assert "Submitted" in capsys.readouterr().out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "review"


def test_missing_task_is_frozen_head_error_without_fk_event_failure(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        with pytest.raises(kb.FrozenHeadReviewError, match="not found"):
            kb.submit_frozen_head_for_review(
                conn, "t_missing", head_sha=head, worktree_path=str(repo),
                evidence=evidence(head, repo),
            )


def test_os_writer_freeze_rejects_helper_with_cwd_and_writable_fd(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    helper = subprocess.Popen(
        [os.environ.get("HERMES_PYTHON", "python"), "-c",
         "import time; f=open('implemented.txt','r+'); time.sleep(30)"],
        cwd=repo,
    )
    try:
        time.sleep(0.1)
        with kb.connect_closing() as conn:
            task_id = kb.create_task(conn, title="writer", workspace_kind="dir", workspace_path=str(repo))
            complete_with_evidence(conn, task_id)
            with pytest.raises(kb.FrozenHeadReviewError, match="active OS writers"):
                kb.submit_frozen_head_for_review(
                    conn, task_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo)
                )
    finally:
        helper.terminate()
        helper.wait(timeout=5)


def test_legacy_submit_task_for_review_keeps_running_and_blocked_contract(kanban_home):
    with kb.connect_closing() as conn:
        running_id = kb.create_task(conn, title="legacy running", assignee="programmer")
        kb.claim_task(conn, running_id, claimer="programmer")
        reviewed = kb.submit_task_for_review(conn, running_id, "reviewer")
        assert reviewed is not None and reviewed.status == "review"

        blocked_id = kb.create_task(conn, title="legacy blocked", assignee="programmer")
        kb.claim_task(conn, blocked_id, claimer="programmer")
        run_id = kb.get_task(conn, blocked_id).current_run_id
        assert kb.block_task(conn, blocked_id, reason="review-required: inspect", expected_run_id=run_id)
        reviewed = kb.submit_task_for_review(conn, blocked_id, "reviewer")
        assert reviewed is not None and reviewed.status == "review"
