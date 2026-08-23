"""Evidence-gated dispatch and GitHub completion contracts."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git_repo(path: Path) -> tuple[str, str]:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Evidence Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "remote",
            "add",
            "origin",
            "https://github.com/lightcloud00/evidence-test.git",
        ],
        check=True,
    )
    (path / "README.md").write_text("evidence\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return "lightcloud00/evidence-test", sha


def _github_receipt(
    task_id: str,
    repository: str,
    sha: str,
    *,
    receipt_id: str = "receipt-1",
    verified_at: datetime | None = None,
    action: str = "push",
) -> dict:
    verified = (verified_at or datetime.now(timezone.utc)).replace(microsecond=0)
    created = verified - timedelta(seconds=2)
    verified_text = verified.isoformat().replace("+00:00", "Z")
    pr_url = (
        f"https://github.com/{repository}/pull/7"
        if action.startswith("pr_")
        else None
    )
    effect_url = {
        "push": f"https://github.com/{repository}/commit/{sha}",
        "comment": f"https://github.com/{repository}/issues/7#issuecomment-1",
    }.get(action, pr_url)
    return {
        "schema": "aos.github_action_receipt.v1",
        "receipt_id": receipt_id,
        "request_id": f"request-{task_id}",
        "action_id": f"action-{task_id}",
        "surface": "hermes",
        "profile": "swarm",
        "session_id": None,
        "task_id": task_id,
        "provider_slot": None,
        "model": None,
        "status": "verified",
        "action": action,
        "repository": repository,
        "branch": "main",
        "commit_sha": sha,
        "pr_url": pr_url,
        "effect_url": effect_url,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "verified_at": verified_text,
        "readback": {
            "status": "verified",
            "repository": repository,
            "branch": "main",
            "commit_sha": sha,
            "verified_at": verified_text,
        },
    }


def _admission_receipt(
    path: Path,
    *,
    cloud_concurrency: int = 1,
    running_workers: int = 0,
    running_cloud_workers: int = 0,
) -> Path:
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "schema": "aos.dispatch_admission.v1",
        "receipt_id": "admission-1",
        "status": "pass",
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        ),
        "gates": {
            gate: True
            for gate in (
                "hook_health",
                "hermes_canary",
                "source_installed_hashes",
                "router_acceptance",
                "telemetry_coverage",
                "github_broker_readback",
                "quota_state",
                "worker_count",
            )
        },
        "allowed_classes": ["cloud_priority", "local_only"],
        "max_workers": 5,
        "cloud_concurrency": cloud_concurrency,
        "running_workers": running_workers,
        "running_cloud_workers": running_cloud_workers,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_legacy_migration_holds_existing_cards(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = home / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('old', 'old', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    kb.init_db(db_path=db_path)
    migrated = sqlite3.connect(db_path)
    row = migrated.execute(
        "SELECT admission_class, completion_contract FROM tasks WHERE id='old'"
    ).fetchone()
    migrated.close()
    assert row == ("hold", "standard")


def test_missing_github_receipt_parks_proposal_and_preserves_workspace(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    _git_repo(repo)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="deliver",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(repo),
            admission_class="cloud_priority",
            completion_contract="github_effect_v1",
        )
        context = kb.build_worker_context(conn, task_id)
        assert "Admission class: cloud_priority" in context
        assert "Completion contract: github_effect_v1" in context
        assert "aos.github_action_receipt.v1" in context
        with pytest.raises(kb.GitHubReceiptPendingError) as caught:
            kb.complete_task(conn, task_id, summary="pushed")
        assert caught.value.reason == "missing_receipt"
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind == "receipt_pending"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "blocked"
        assert repo.is_dir()
        proposal = conn.execute(
            "SELECT summary FROM task_completion_proposals WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert proposal["summary"] == "pushed"


def test_stale_worker_cannot_park_successor_run_for_missing_receipt(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    _git_repo(repo)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="compare and swap",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        assert not kb.complete_task(
            conn,
            task_id,
            summary="stale worker",
            expected_run_id=claimed.current_run_id + 1,
        )
        assert kb.get_task(conn, task_id).status == "running"
        assert conn.execute(
            "SELECT 1 FROM task_completion_proposals WHERE task_id = ?",
            (task_id,),
        ).fetchone() is None


def test_exact_head_receipt_completes_once_and_reuse_fails(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    repository, sha = _git_repo(repo)
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="first",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        with pytest.raises(kb.GitHubReceiptPendingError):
            kb.complete_task(conn, first, summary="retained")
        receipt = _github_receipt(first, repository, sha)
        assert kb.complete_task(
            conn,
            first,
            metadata={"github_action_receipt": receipt},
        )
        assert kb.get_task(conn, first).status == "done"
        assert not kb.complete_task(
            conn,
            first,
            metadata={"github_action_receipt": receipt},
        )
        completed_events = [
            event for event in kb.list_events(conn, first) if event.kind == "completed"
        ]
        assert len(completed_events) == 1

        second = kb.create_task(
            conn,
            title="second",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        reused = _github_receipt(
            second, repository, sha, receipt_id=receipt["receipt_id"]
        )
        with pytest.raises(kb.GitHubReceiptPendingError) as caught:
            kb.complete_task(
                conn,
                second,
                summary="second",
                metadata={"github_action_receipt": reused},
            )
        assert caught.value.reason == "reused_receipt"


def test_stale_and_wrong_head_receipts_are_rejected(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    repository, sha = _git_repo(repo)
    with kb.connect() as conn:
        stale_task = kb.create_task(
            conn,
            title="stale",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        stale = _github_receipt(
            stale_task,
            repository,
            sha,
            verified_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        with pytest.raises(kb.GitHubReceiptPendingError) as caught:
            kb.complete_task(
                conn,
                stale_task,
                summary="stale",
                metadata={"github_action_receipt": stale},
            )
        assert caught.value.reason == "stale_receipt"

        wrong_task = kb.create_task(
            conn,
            title="wrong head",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        wrong = _github_receipt(wrong_task, repository, "0" * 40)
        with pytest.raises(kb.GitHubReceiptPendingError) as caught:
            kb.complete_task(
                conn,
                wrong_task,
                summary="wrong",
                metadata={"github_action_receipt": wrong},
            )
        assert caught.value.reason == "wrong_head"


@pytest.mark.parametrize("action", ["push", "pr_create", "pr_merge", "comment"])
def test_supported_exact_head_effect_receipts_complete_once(
    kanban_home, tmp_path, action
):
    repo = tmp_path / "repo"
    repository, sha = _git_repo(repo)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=action,
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        receipt = _github_receipt(
            task_id,
            repository,
            sha,
            receipt_id=f"receipt-{action}",
            action=action,
        )
        assert kb.complete_task(
            conn,
            task_id,
            metadata={"github_action_receipt": receipt},
        )
        assert not kb.complete_task(
            conn,
            task_id,
            metadata={"github_action_receipt": receipt},
        )


def test_wrong_repository_and_incomplete_action_context_are_rejected(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    repository, sha = _git_repo(repo)
    with kb.connect() as conn:
        wrong_repo_task = kb.create_task(
            conn,
            title="wrong repo",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        wrong_repo = _github_receipt(
            wrong_repo_task, "lightcloud00/not-the-repo", sha
        )
        with pytest.raises(kb.GitHubReceiptPendingError) as caught:
            kb.complete_task(
                conn,
                wrong_repo_task,
                metadata={"github_action_receipt": wrong_repo},
            )
        assert caught.value.reason == "wrong_repository"

        incomplete_task = kb.create_task(
            conn,
            title="missing attribution",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        incomplete = _github_receipt(incomplete_task, repository, sha)
        incomplete.pop("surface")
        with pytest.raises(kb.GitHubReceiptPendingError) as caught:
            kb.complete_task(
                conn,
                incomplete_task,
                metadata={"github_action_receipt": incomplete},
            )
        assert caught.value.reason == "missing_surface"


def test_missing_admission_receipt_holds_two_intervals_without_board_mutation(
    kanban_home, tmp_path, all_assignees_spawnable
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="cloud",
            assignee="alice",
            admission_class="cloud_priority",
        )
        before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        results = [
            kb.dispatch_once(
                conn,
                dry_run=True,
                require_admission_receipt=True,
                admission_receipt_path=str(tmp_path / "missing.json"),
            )
            for _ in range(2)
        ]
        after = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        assert [result.admission_blocked_reason for result in results] == [
            "receipt_missing",
            "receipt_missing",
        ]
        assert all(result.spawned == [] for result in results)
        assert kb.get_task(conn, task_id).status == "ready"
        assert before == after


def test_admission_holds_legacy_caps_cloud_and_fills_local(
    kanban_home, tmp_path, all_assignees_spawnable
):
    receipt_path = _admission_receipt(tmp_path / "admission.json")
    with kb.connect() as conn:
        held = kb.create_task(conn, title="held", assignee="alice")
        cloud_one = kb.create_task(
            conn,
            title="cloud one",
            assignee="alice",
            admission_class="cloud_priority",
            priority=3,
        )
        cloud_two = kb.create_task(
            conn,
            title="cloud two",
            assignee="alice",
            admission_class="cloud_priority",
            priority=2,
        )
        local = kb.create_task(
            conn,
            title="local",
            assignee="alice",
            admission_class="local_only",
        )
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=5,
            require_admission_receipt=True,
            admission_receipt_path=str(receipt_path),
        )
    spawned = {task_id for task_id, _, _ in result.spawned}
    assert result.admission_receipt_id == "admission-1"
    assert held in result.skipped_held
    assert cloud_one in spawned
    assert cloud_two in result.skipped_cloud_capped
    assert local in spawned


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ({"status": "fail"}, "gate_failed"),
        (
            {
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-01T00:05:00Z",
            },
            "stale_receipt",
        ),
    ],
)
def test_failed_or_stale_admission_receipt_never_claims(
    kanban_home, tmp_path, all_assignees_spawnable, mutation, expected_reason
):
    receipt_path = _admission_receipt(tmp_path / "admission.json")
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload.update(mutation)
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="must stay ready",
            assignee="alice",
            admission_class="local_only",
        )
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            require_admission_receipt=True,
            admission_receipt_path=str(receipt_path),
        )
        assert result.admission_blocked_reason == expected_reason
        assert result.spawned == []
        assert kb.get_task(conn, task_id).status == "ready"


def test_admission_cap_never_exceeds_five_workers_across_47_boards(
    kanban_home, tmp_path, all_assignees_spawnable
):
    board_names = ["default"] + [f"board-{index:02d}" for index in range(1, 47)]
    for board in board_names[1:]:
        kb.create_board(board)
    for board in board_names[1:6]:
        with kb.connect(board=board) as conn:
            task_id = kb.create_task(
                conn,
                title=f"running on {board}",
                assignee="alice",
                admission_class="local_only",
            )
            assert kb.claim_task(conn, task_id) is not None

    receipt_path = _admission_receipt(tmp_path / "admission.json")
    with kb.connect() as conn:
        waiting = kb.create_task(
            conn,
            title="sixth worker denied",
            assignee="alice",
            admission_class="local_only",
        )
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=5,
            reconcile_orphans=False,
            require_admission_receipt=True,
            admission_receipt_path=str(receipt_path),
        )
        assert result.spawned == []
        assert kb.get_task(conn, waiting).status == "ready"


def test_cloud_cap_counts_running_workers_on_other_boards(
    kanban_home, tmp_path, all_assignees_spawnable
):
    kb.create_board("second")
    receipt_path = _admission_receipt(tmp_path / "admission.json")
    with kb.connect(board="second") as conn:
        running = kb.create_task(
            conn,
            title="already cloud",
            assignee="alice",
            admission_class="cloud_priority",
        )
        assert kb.claim_task(conn, running) is not None

    with kb.connect() as conn:
        cloud = kb.create_task(
            conn,
            title="more cloud",
            assignee="alice",
            admission_class="cloud_priority",
        )
        local = kb.create_task(
            conn,
            title="local fill",
            assignee="alice",
            admission_class="local_only",
        )
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=5,
            require_admission_receipt=True,
            admission_receipt_path=str(receipt_path),
        )
    spawned = {task_id for task_id, _, _ in result.spawned}
    assert cloud in result.skipped_cloud_capped
    assert local in spawned


def test_dispatch_reconciles_broker_receipt_exactly_once(
    kanban_home, tmp_path, all_assignees_spawnable
):
    repo = tmp_path / "repo"
    repository, sha = _git_repo(repo)
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    admission = _admission_receipt(tmp_path / "admission.json")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="reconcile",
            workspace_kind="dir",
            workspace_path=str(repo),
            completion_contract="github_effect_v1",
        )
        with pytest.raises(kb.GitHubReceiptPendingError):
            kb.complete_task(conn, task_id, summary="proposal")
        (receipt_dir / "receipt.json").write_text(
            json.dumps(_github_receipt(task_id, repository, sha)),
            encoding="utf-8",
        )
        first = kb.dispatch_once(
            conn,
            require_admission_receipt=True,
            admission_receipt_path=str(admission),
            github_receipt_dir=str(receipt_dir),
        )
        second = kb.dispatch_once(
            conn,
            require_admission_receipt=True,
            admission_receipt_path=str(admission),
            github_receipt_dir=str(receipt_dir),
        )
        events = [
            event for event in kb.list_events(conn, task_id) if event.kind == "completed"
        ]
    assert first.receipts_completed == [task_id]
    assert second.receipts_completed == []
    assert len(events) == 1
