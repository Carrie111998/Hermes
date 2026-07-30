from __future__ import annotations

import json
import subprocess
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


def evidence(head: str, repo: Path) -> dict:
    return {
        "head_sha": head,
        "worktree_path": str(repo),
        "clean": True,
        "implementation_complete": True,
        "source": "frozen-head-cli",
    }


def test_db_accepts_completed_clean_exact_head_without_claim(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="frozen", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, task_id)
        assert kb.submit_task_for_review(
            conn, task_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo)
        )
        assert kb.get_task(conn, task_id).status == "review"
        event = kb.list_events(conn, task_id)[-1]
        assert event.kind == "review_submitted_frozen_head"
        assert event.payload["head_sha"] == head
        assert event.payload["implementation_run_id"]


def test_scheduled_ready_recovery_can_reach_frozen_review(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="scheduled recovery", workspace_kind="dir", workspace_path=str(repo))
        assert kb.schedule_task(conn, task_id, reason="today's frozen head")
        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "ready"
        complete_with_evidence(conn, task_id)
        assert kb.submit_task_for_review(
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
            kb.submit_task_for_review(
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
            kb.submit_task_for_review(conn, task_id, head_sha="", worktree_path=str(repo), evidence=valid)
        with pytest.raises(kb.FrozenHeadReviewError, match="full 40-character"):
            kb.submit_task_for_review(conn, task_id, head_sha="not-a-sha", worktree_path=str(repo), evidence=valid)

        unrelated_id = kb.create_task(conn, title="not done", workspace_kind="dir", workspace_path=str(repo))
        with pytest.raises(kb.FrozenHeadReviewError, match="must be done"):
            kb.submit_task_for_review(conn, unrelated_id, head_sha=head, worktree_path=str(repo), evidence=valid)


def test_db_rejects_dirty_head_live_claim_and_missing_implementation_evidence(kanban_home, tmp_path):
    repo, head = make_repo(tmp_path)
    with kb.connect_closing() as conn:
        dirty_id = kb.create_task(conn, title="dirty", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, dirty_id)
        (repo / "dirty.txt").write_text("uncommitted\n")
        with pytest.raises(kb.FrozenHeadReviewError, match="dirty"):
            kb.submit_task_for_review(conn, dirty_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo))
        (repo / "dirty.txt").unlink()

        live_id = kb.create_task(conn, title="live", workspace_kind="dir", workspace_path=str(repo))
        complete_with_evidence(conn, live_id)
        conn.execute("UPDATE tasks SET claim_lock='live', worker_pid=1234 WHERE id=?", (live_id,))
        conn.commit()
        with pytest.raises(kb.FrozenHeadReviewError, match="live claim"):
            kb.submit_task_for_review(conn, live_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo))

        no_evidence_id = kb.create_task(conn, title="no evidence", workspace_kind="dir", workspace_path=str(repo))
        assert kb.complete_task(conn, no_evidence_id, result="done")
        with pytest.raises(kb.FrozenHeadReviewError, match="metadata is malformed"):
            kb.submit_task_for_review(conn, no_evidence_id, head_sha=head, worktree_path=str(repo), evidence=evidence(head, repo))


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
