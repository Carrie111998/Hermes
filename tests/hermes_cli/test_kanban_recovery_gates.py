"""Deterministic recovery gates for shared Kanban worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _git_worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "recovery@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Recovery Test"],
        check=True,
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    return repo


def _ready_task(
    conn,
    *,
    title: str,
    workspace: Path,
    idempotency_key: str | None = None,
) -> str:
    return kb.create_task(
        conn,
        title=title,
        assignee="worker",
        workspace_kind="worktree",
        workspace_path=str(workspace),
        idempotency_key=idempotency_key,
        max_retries=10,
    )


def _host_claimer(suffix: str = "worker") -> str:
    return f"{socket.gethostname()}:{suffix}"


def _crash_current_run(conn, task_id: str, monkeypatch) -> None:
    kb._set_worker_pid(conn, task_id, 98765)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    assert kb.detect_crashed_workers(conn) == [task_id]


def test_schema_migrates_recovery_ledger_and_run_ownership(kanban_home):
    with kb.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        }
        task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
        }

    assert "recovery_checkpoints" in tables
    assert {
        "canonical_worktree",
        "dispatch_key",
        "run_role",
        "owner_task_id",
        "checkpoint_id",
    } <= run_columns
    assert "recovery_cause" in task_columns


def test_aliases_and_shared_paths_have_exactly_one_live_owner(
    kanban_home, tmp_path, monkeypatch
):
    repo = _git_worktree(tmp_path)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    nested = repo / "nested"
    nested.mkdir()

    with kb.connect() as conn:
        first = _ready_task(conn, title="first", workspace=alias)
        second = _ready_task(conn, title="second", workspace=nested)

        claimed = kb.claim_task(conn, first, claimer=_host_claimer("first"))
        assert claimed is not None
        kb._set_worker_pid(conn, first, os.getpid())
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == os.getpid())

        assert kb.claim_task(
            conn, second, claimer=_host_claimer("second")
        ) is None
        live = conn.execute(
            "SELECT canonical_worktree, dispatch_key FROM task_runs "
            "WHERE ended_at IS NULL AND run_role='owner'"
        ).fetchall()
        assert len(live) == 1
        assert live[0]["canonical_worktree"] == str(repo.resolve())
        assert live[0]["dispatch_key"] == kb.dispatch_idempotency_key(
            first, str(repo.resolve())
        )


def test_dispatcher_spawns_one_owner_and_deduplicates_shared_worktree(
    kanban_home, tmp_path, monkeypatch, all_assignees_spawnable
):
    repo = _git_worktree(tmp_path)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    spawned: list[str] = []

    def spawn(task, _workspace):
        spawned.append(task.id)
        return os.getpid()

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == os.getpid())
    with kb.connect() as conn:
        first = _ready_task(conn, title="first", workspace=repo)
        second = _ready_task(conn, title="second", workspace=alias)
        conn.execute("UPDATE tasks SET priority=10 WHERE id=?", (first,))
        conn.commit()

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=2)
        assert spawned == [first]
        assert [task_id for task_id, _profile, _path in result.spawned] == [
            first
        ]
        assert kb.get_task(conn, first).status == "running"
        assert kb.get_task(conn, second).status == "ready"
        dedup = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='dispatch_deduplicated'",
            (second,),
        ).fetchone()
        assert "live_owner_exists" in dedup["payload"]


def test_dead_owner_is_checkpointed_before_an_alias_can_claim(
    kanban_home, tmp_path, monkeypatch
):
    repo = _git_worktree(tmp_path)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)

    with kb.connect() as conn:
        first = _ready_task(conn, title="first", workspace=repo)
        second = _ready_task(conn, title="second", workspace=alias)
        assert kb.claim_task(conn, first, claimer=_host_claimer()) is not None
        _crash_current_run(conn, first, monkeypatch)

        checkpoint = conn.execute(
            "SELECT * FROM recovery_checkpoints WHERE task_id=? ORDER BY id DESC",
            (first,),
        ).fetchone()
        assert checkpoint is not None
        assert checkpoint["owner_run_id"] is not None
        assert checkpoint["owner_pid"] == 98765
        assert checkpoint["canonical_worktree"] == str(repo.resolve())

        assert kb.claim_task(conn, second, claimer=_host_claimer("next")) is not None


def test_migrated_live_run_without_identity_still_blocks_alias(
    kanban_home, tmp_path
):
    repo = _git_worktree(tmp_path)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    with kb.connect() as conn:
        first = _ready_task(conn, title="legacy owner", workspace=repo)
        second = _ready_task(conn, title="alias", workspace=alias)
        assert kb.claim_task(conn, first, claimer=_host_claimer("legacy"))
        run = kb.latest_run(conn, first)
        conn.execute(
            "UPDATE task_runs SET canonical_worktree=NULL, dispatch_key=NULL "
            "WHERE id=?",
            (run.id,),
        )
        conn.commit()

        assert kb.claim_task(conn, second, claimer=_host_claimer("alias")) is None


def test_checkpoint_is_immutable_and_contains_digests_not_private_contents(
    kanban_home, tmp_path, monkeypatch
):
    repo = _git_worktree(tmp_path)
    private = repo / "private-token.txt"
    private.write_text("TOP-SECRET-CONTENTS\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    with kb.connect() as conn:
        task_id = _ready_task(conn, title="privacy", workspace=repo)
        assert kb.claim_task(conn, task_id, claimer=_host_claimer()) is not None
        assert kb.heartbeat_worker(
            conn,
            task_id,
            remaining_defects=[
                "acceptance test for retry policy is still failing"
            ],
        )
        _crash_current_run(conn, task_id, monkeypatch)

        row = conn.execute(
            "SELECT * FROM recovery_checkpoints WHERE task_id=?", (task_id,)
        ).fetchone()
        assert row["head_sha"]
        assert len(row["porcelain_digest"]) == 64
        assert len(row["untracked_digest"]) == 64
        assert len(row["remaining_defects_digest"]) == 64
        assert json.loads(row["remaining_defects"]) == [
            "acceptance test for retry policy is still failing"
        ]
        assert "TOP-SECRET-CONTENTS" not in json.dumps(dict(row))
        assert "private-token.txt" not in json.dumps(dict(row))

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE recovery_checkpoints SET owner_pid=1 WHERE id=?",
                (row["id"],),
            )


def test_one_unchanged_retry_then_causal_classification_is_required(
    kanban_home, tmp_path, monkeypatch
):
    repo = _git_worktree(tmp_path)
    with kb.connect() as conn:
        task_id = _ready_task(conn, title="retry", workspace=repo)
        assert kb.claim_task(conn, task_id, claimer=_host_claimer("initial"))
        _crash_current_run(conn, task_id, monkeypatch)

        assert kb.claim_task(conn, task_id, claimer=_host_claimer("retry-1"))
        _crash_current_run(conn, task_id, monkeypatch)

        assert kb.claim_task(
            conn, task_id, claimer=_host_claimer("retry-2")
        ) is None
        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='recovery_gate_blocked' ORDER BY id DESC",
            (task_id,),
        ).fetchone()
        assert "causal_classification_required" in event["payload"]
        checkpoint_count = conn.execute(
            "SELECT COUNT(*) FROM recovery_checkpoints WHERE task_id=?",
            (task_id,),
        ).fetchone()[0]
        blocked_event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id=? AND kind='recovery_gate_blocked'",
            (task_id,),
        ).fetchone()[0]
        assert kb.claim_task(
            conn, task_id, claimer=_host_claimer("same-held-retry")
        ) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM recovery_checkpoints WHERE task_id=?",
            (task_id,),
        ).fetchone()[0] == checkpoint_count
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id=? AND kind='recovery_gate_blocked'",
            (task_id,),
        ).fetchone()[0] == blocked_event_count

        assert kb.classify_recovery_cause(
            conn, task_id, "dependency_state_was_not_refreshed"
        )
        assert kb.claim_task(conn, task_id, claimer=_host_claimer("classified"))


def test_changed_checkpoint_is_semantic_progress_but_heartbeat_is_not(
    kanban_home, tmp_path, monkeypatch
):
    repo = _git_worktree(tmp_path)
    with kb.connect() as conn:
        task_id = _ready_task(conn, title="progress", workspace=repo)
        assert kb.claim_task(conn, task_id, claimer=_host_claimer("initial"))
        assert kb.heartbeat_worker(conn, task_id, note="still alive")
        _crash_current_run(conn, task_id, monkeypatch)

        assert kb.claim_task(conn, task_id, claimer=_host_claimer("retry-1"))
        assert kb.heartbeat_worker(conn, task_id, note="still alive again")
        _crash_current_run(conn, task_id, monkeypatch)

        assert kb.claim_task(
            conn, task_id, claimer=_host_claimer("heartbeat-only")
        ) is None

        (repo / "tracked.txt").write_text("measurable progress\n", encoding="utf-8")
        assert kb.claim_task(conn, task_id, claimer=_host_claimer("progressed"))


def test_reclaim_stops_and_confirms_descendants_before_owner(
    kanban_home, tmp_path, monkeypatch
):
    repo = _git_worktree(tmp_path)
    alive = {100, 101, 102}
    signals: list[tuple[int, int]] = []

    def fake_signal(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(kb, "_descendant_pids", lambda _pid: [102, 101])
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in alive)

    with kb.connect() as conn:
        task_id = _ready_task(conn, title="tree", workspace=repo)
        assert kb.claim_task(conn, task_id, claimer=_host_claimer())
        kb._set_worker_pid(conn, task_id, 100)

        assert kb.reclaim_task(conn, task_id, signal_fn=fake_signal)
        assert signals == [
            (102, signal.SIGTERM),
            (101, signal.SIGTERM),
            (100, signal.SIGTERM),
        ]
        assert kb.get_task(conn, task_id).status == "ready"


def test_reclaim_refuses_to_orphan_a_confirmed_live_descendant(
    kanban_home, tmp_path, monkeypatch
):
    repo = _git_worktree(tmp_path)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(kb, "_descendant_pids", lambda _pid: [201])
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in {200, 201})
    monkeypatch.setattr(kb.time, "sleep", lambda _seconds: None)

    with kb.connect() as conn:
        task_id = _ready_task(conn, title="tree", workspace=repo)
        assert kb.claim_task(conn, task_id, claimer=_host_claimer())
        kb._set_worker_pid(conn, task_id, 200)

        assert not kb.reclaim_task(
            conn,
            task_id,
            signal_fn=lambda pid, sig: signals.append((pid, sig)),
        )
        assert all(pid == 201 for pid, _sig in signals)
        assert kb.get_task(conn, task_id).status == "running"


def test_terminal_dispatch_key_deduplicates_same_outcome_and_worktree(
    kanban_home, tmp_path
):
    repo = _git_worktree(tmp_path)
    outcome_key = "ship-auth-fix"
    with kb.connect() as conn:
        first = _ready_task(
            conn,
            title="first",
            workspace=repo,
            idempotency_key=outcome_key,
        )
        assert kb.claim_task(conn, first, claimer=_host_claimer("first"))
        first_run = kb.latest_run(conn, first)
        assert kb.complete_task(
            conn, first, summary="done", expected_run_id=first_run.id
        )
        assert kb.archive_task(conn, first)

        second = _ready_task(
            conn,
            title="duplicate",
            workspace=repo,
            idempotency_key=outcome_key,
        )
        assert kb.claim_task(
            conn, second, claimer=_host_claimer("duplicate")
        ) is None

        keys = conn.execute(
            "SELECT DISTINCT dispatch_key FROM task_runs "
            "WHERE canonical_worktree=?",
            (str(repo.resolve()),),
        ).fetchall()
        expected = hashlib.sha256(
            f"kanban-dispatch-v1\0{outcome_key}\0{repo.resolve()}".encode()
        ).hexdigest()
        assert [row["dispatch_key"] for row in keys] == [expected]


def test_non_owner_runs_and_cards_cannot_finalize_owning_task(
    kanban_home, tmp_path
):
    repo = _git_worktree(tmp_path)
    with kb.connect() as conn:
        owner = _ready_task(conn, title="owner", workspace=repo)
        child = kb.create_task(
            conn,
            title="child",
            assignee="worker",
            parents=(owner,),
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        assert kb.claim_task(conn, owner, claimer=_host_claimer("owner"))
        owner_run = kb.latest_run(conn, owner)

        conn.execute(
            "UPDATE task_runs SET run_role='helper' WHERE id=?", (owner_run.id,)
        )
        conn.commit()
        assert not kb.complete_task(
            conn, owner, summary="helper says done", expected_run_id=owner_run.id
        )
        assert not kb.block_task(
            conn, owner, reason="helper says blocked", expected_run_id=owner_run.id
        )

        conn.execute(
            "UPDATE task_runs SET run_role='owner' WHERE id=?", (owner_run.id,)
        )
        conn.commit()
        assert kb.complete_task(
            conn, owner, summary="owner done", expected_run_id=owner_run.id
        )
        assert kb.claim_task(conn, child, claimer=_host_claimer("child"))
        child_run = kb.latest_run(conn, child)
        assert not kb.complete_task(
            conn, owner, summary="child says parent done", expected_run_id=child_run.id
        )

        review = _ready_task(conn, title="review", workspace=tmp_path / "review")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
        conn.commit()
        assert kb.claim_review_task(conn, review, claimer=_host_claimer("review"))
        review_run = kb.latest_run(conn, review)
        assert review_run.run_role == "reviewer"
        assert not kb.complete_task(
            conn, review, summary="reviewer says done", expected_run_id=review_run.id
        )
        assert not kb.block_task(
            conn, review, reason="reviewer blocks", expected_run_id=review_run.id
        )

        assert kb.archive_task(conn, child)
        assert not kb.complete_task(conn, child, summary="archived child")
