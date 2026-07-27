"""Durable recovery matrix for the internal independent-review lane."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from agent import kanban_handoff_scope as handoff_scope
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_short_task_handoff_dispatch_enabled", lambda: True)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.init_db()
    return home


def _set_review_waiting(conn, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = 'review', resume_lane = 'review', "
        "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
        "current_run_id = NULL WHERE id = ?",
        (task_id,),
    )


def _claim_review(conn, *, max_runtime_seconds=None):
    task_id = kb.create_task(
        conn,
        title="review recovery",
        assignee="default",
        max_runtime_seconds=max_runtime_seconds,
    )
    _set_review_waiting(conn, task_id)
    task = kb.claim_review_task(conn, task_id, claimer=kb._claimer_id())
    assert task is not None
    return task


def _assert_review_retry(conn, task_id: str) -> None:
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "review"
    assert task.resume_lane == "review"
    assert task.current_run_id is None
    assert task.claim_lock is None


def test_legacy_migration_backfills_and_repairs_resume_lane():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, tenant TEXT, "
        "result TEXT, idempotency_key TEXT, branch_name TEXT, "
        "consecutive_failures INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER, "
        "last_failure_error TEXT, max_runtime_seconds INTEGER, "
        "last_heartbeat_at INTEGER, current_run_id INTEGER, "
        "workflow_template_id TEXT, current_step_key TEXT, skills TEXT, "
        "max_retries INTEGER, session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)"
    )
    conn.executemany(
        "INSERT INTO tasks (id, status) VALUES (?, ?)",
        [
            ("review-old", "review"),
            ("ready-old", "ready"),
            ("running-review", "running"),
            ("blocked-review", "blocked"),
            ("scheduled-review", "scheduled"),
            ("triage-review", "triage"),
            ("retry-review", "ready"),
            ("newer-implementation", "running"),
            ("malformed-history", "blocked"),
            ("done-review-history", "done"),
        ],
    )
    review_claim = json.dumps({"source_status": "review"})
    for task_id in (
        "running-review",
        "blocked-review",
        "scheduled-review",
        "triage-review",
        "retry-review",
        "newer-implementation",
        "done-review-history",
    ):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'claimed', ?, 1)",
            (task_id, review_claim),
        )
    # Old ordinary claims carried no positive implementation marker. A newer
    # empty claim therefore cannot disprove sticky review evidence.
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES ('newer-implementation', 'claimed', '{}', 2)"
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES ('malformed-history', 'claimed', '{bad', 1)"
    )

    kb._migrate_add_optional_columns(conn)
    assert dict(
        conn.execute("SELECT id, resume_lane FROM tasks").fetchall()
    ) == {
        "review-old": "review",
        "ready-old": "implementation",
        "running-review": "review",
        "blocked-review": "review",
        "scheduled-review": "review",
        "triage-review": "review",
        "retry-review": "review",
        "newer-implementation": "review",
        "malformed-history": "implementation",
        "done-review-history": "implementation",
    }
    assert dict(conn.execute("SELECT id, status FROM tasks").fetchall())[
        "retry-review"
    ] == "review"

    conn.execute(
        "UPDATE tasks SET resume_lane = 'corrupt' WHERE id = 'review-old'"
    )
    conn.execute(
        "UPDATE tasks SET resume_lane = 'corrupt' WHERE id = 'ready-old'"
    )
    kb._migrate_add_optional_columns(conn)
    assert dict(
        conn.execute("SELECT id, resume_lane FROM tasks").fetchall()
    )["review-old"] == "review"
    assert dict(
        conn.execute("SELECT id, resume_lane FROM tasks").fetchall()
    )["ready-old"] == "implementation"


def _drop_lane_and_reinitialize_real_board() -> None:
    db_path = kb.kanban_db_path()
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("ALTER TABLE tasks DROP COLUMN resume_lane")
        raw.commit()
    finally:
        raw.close()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()


@pytest.mark.parametrize(
    "legacy_status",
    ["running", "blocked", "scheduled", "triage", "todo", "ready"],
)
def test_real_legacy_review_migration_recovers_every_nonterminal_state(
    kanban_home, legacy_status
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"legacy {legacy_status} reviewer",
            assignee="default",
        )
        _set_review_waiting(conn, task_id)
        claimed = kb.claim_review_task(
            conn, task_id, claimer=f"legacy-{legacy_status}"
        )
        assert claimed is not None
        if legacy_status != "running":
            assert not kb._record_spawn_failure(
                conn, task_id, "legacy retry", failure_limit=99
            )
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (legacy_status, task_id),
            )
        event_count = len(kb.list_events(conn, task_id))

    _drop_lane_and_reinitialize_real_board()
    with kb.connect() as conn:
        migrated = kb.get_task(conn, task_id)
        assert migrated.resume_lane == "review"
        assert migrated.status == (
            "review" if legacy_status == "ready" else legacy_status
        )
        assert len(kb.list_events(conn, task_id)) == event_count

    # A repeated public initialization is idempotent and event-free.
    db_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).resume_lane == "review"
        assert len(kb.list_events(conn, task_id)) == event_count

        if legacy_status == "running":
            assert kb.reclaim_task(conn, task_id, reason="migration retry")
            assert kb.get_task(conn, task_id).status == "review"
        elif legacy_status in {"blocked", "scheduled"}:
            assert kb.unblock_task(conn, task_id)
            assert kb.get_task(conn, task_id).status == "review"
        elif legacy_status == "triage":
            assert kb.specify_triage_task(
                conn, task_id, title="specified legacy review"
            )
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, task_id).status == "review"
        elif legacy_status == "todo":
            assert kb.recompute_ready(conn) == 1
            assert kb.get_task(conn, task_id).status == "review"
        else:
            assert kb.claim_task(conn, task_id) is None
            assert kb.claim_review_task(conn, task_id) is not None


def test_real_legacy_review_migration_uses_only_exact_durable_evidence(
    kanban_home,
):
    with kb.connect() as conn:
        ids = {
            name: kb.create_task(
                conn,
                title=name,
                assignee="default",
                initial_status="blocked",
            )
            for name in (
                "event-request",
                "run-status",
                "run-outcome",
                "run-metadata",
                "malformed",
                "done-history",
                "archived-history",
            )
        }
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'review_requested', '{}', 1)",
            (ids["event-request"],),
        )
        conn.executemany(
            "INSERT INTO task_runs "
            "(task_id, status, started_at, outcome, metadata) "
            "VALUES (?, ?, 1, ?, ?)",
            [
                (ids["run-status"], "review_requested", None, None),
                (ids["run-outcome"], "done", "review_requested", None),
                (
                    ids["run-metadata"],
                    "done",
                    "reclaimed",
                    json.dumps(
                        {"_hermes_run_lane": "independent_review"}
                    ),
                ),
                (ids["malformed"], "done", "reclaimed", "{bad"),
            ],
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'claimed', '{bad', 1)",
            (ids["malformed"],),
        )
        conn.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ?",
            (ids["done-history"],),
        )
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?",
            (ids["archived-history"],),
        )
        for terminal in ("done-history", "archived-history"):
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, kind, payload, created_at) "
                "VALUES (?, 'review_requested', '{}', 1)",
                (ids[terminal],),
            )
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events"
        ).fetchone()[0]

    _drop_lane_and_reinitialize_real_board()
    with kb.connect() as conn:
        for name in (
            "event-request",
            "run-status",
            "run-outcome",
            "run-metadata",
        ):
            assert kb.get_task(conn, ids[name]).resume_lane == "review"
        for name in ("malformed", "done-history", "archived-history"):
            assert kb.get_task(conn, ids[name]).resume_lane == "implementation"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events"
        ).fetchone()[0] == before_events


def test_ready_review_migration_waits_in_todo_for_unfinished_parent(
    kanban_home,
):
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn, title="unfinished parent", assignee="default"
        )
        child_id = kb.create_task(
            conn, title="legacy ready reviewer", assignee="default"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'claimed', ?, 1)",
            (child_id, json.dumps({"source_status": "review"})),
        )
        kb.link_tasks(conn, parent_id, child_id)
        # Model the old recovery bug that put the reviewer in ready despite
        # its unfinished dependency.
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (child_id,)
        )

    _drop_lane_and_reinitialize_real_board()
    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child.resume_lane == "review"
        assert child.status == "todo"

        parent = kb.claim_task(conn, parent_id, claimer="finish-parent")
        assert parent is not None
        assert kb.complete_task(
            conn,
            parent_id,
            summary="parent done",
            expected_run_id=parent.current_run_id,
        )
        assert kb.get_task(conn, child_id).status == "review"


def _phase1_origin(workspace: Path) -> dict:
    config = {
        "agent": {"max_turns": 90},
        "kanban": {
            "failure_limit": 3,
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 2,
                "allowed_workspace_roots": [str(workspace.resolve())],
                "allowed_origins": [
                    {
                        "platform": "feishu",
                        "chat_type": "group",
                        "chat_id": "review-lane",
                        "user_id": "operator",
                    }
                ],
            },
        },
    }
    origin = {
        "platform": "feishu",
        "scope_id": "tenant",
        "chat_type": "group",
        "chat_id": "review-lane",
        "thread_id": "",
        "user_id": "operator",
        "notifier_profile": "default",
        "session_key": "agent:default:feishu:group:review-lane:operator",
        "message_id": "submit-review-lane",
        "operation_slot": "tool",
    }
    decision = handoff_scope.decide_gateway_origin(config, origin)
    assert decision["authorized"] is True
    origin["short_handoff_policy"] = decision["task_policy_json"]
    return origin


def test_managed_creation_requires_the_exact_configured_pilot_workspace(
    kanban_home, tmp_path
):
    approved = tmp_path / "approved-pilot"
    unapproved = tmp_path / "unapproved-pilot"
    approved.mkdir()
    unapproved.mkdir()
    origin = _phase1_origin(approved)

    with kb.connect() as conn:
        with pytest.raises(ValueError, match="exact approved pilot directory"):
            kb.create_task(
                conn,
                title="must stay in configured pilot",
                assignee="default",
                workspace_kind="dir",
                workspace_path=str(unapproved),
                validation_class="text_mechanism",
                control_origin=origin,
            )
        assert kb.list_tasks(conn) == []


def test_submit_review_atomically_sets_durable_lane_and_self_gate(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "review-pilot"
    workspace.mkdir()
    identity = {
        "owner_node_id": "node",
        "owner_boot_id": "boot",
        "worker_pid": os.getpid(),
        "worker_start_token": "start",
        "worker_pgid": os.getpid(),
    }
    monkeypatch.setattr(
        kb,
        "_capture_process_group_identity",
        lambda pid: dict(identity, worker_pid=int(pid), worker_pgid=int(pid)),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="submit review",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=_phase1_origin(workspace),
        )
        task = kb.claim_task(conn, task_id, claimer="implementation")
        assert task is not None
        assert kb._set_worker_pid(conn, task_id, os.getpid())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET handoff_safety_required = 1 "
                "WHERE id = ?",
                (task.current_run_id,),
            )
        frozen = kb._task_short_handoff_worker_policy(conn, task_id)
        monkeypatch.setenv(
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
            json.dumps(frozen, sort_keys=True, separators=(",", ":")),
        )
        assert kb.submit_task_for_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        submitted = kb.get_task(conn, task_id)
        assert submitted.status == "review"
        assert submitted.resume_lane == "review"
        assert submitted.current_run_id is None
        assert conn.execute(
            "SELECT 1 FROM task_exit_gates WHERE child_task_id = ? "
            "AND released_at IS NULL",
            (task_id,),
        ).fetchone() is not None
        assert [event.kind for event in kb.list_events(conn, task_id)].count(
            "review_requested"
        ) == 1
        assert "completed" not in [
            event.kind for event in kb.list_events(conn, task_id)
        ]


@pytest.mark.parametrize(
    "recovery",
    ["spawn_failure", "policy_pause", "ttl", "manual_reclaim", "timeout", "stale", "crash"],
)
def test_review_run_recovery_matrix_returns_only_to_review(
    kanban_home, monkeypatch, recovery
):
    with kb.connect() as conn:
        task = _claim_review(
            conn, max_runtime_seconds=1 if recovery == "timeout" else None
        )
        if recovery == "spawn_failure":
            assert not kb._record_spawn_failure(
                conn, task.id, "spawn failed", failure_limit=3
            )
        elif recovery == "policy_pause":
            assert kb._requeue_unstarted_claim_after_policy_pause(
                conn,
                task.id,
                expected_run_id=task.current_run_id,
                expected_claim_lock=task.claim_lock,
                reason="policy paused",
            )
        elif recovery == "ttl":
            conn.execute(
                "UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task.id,)
            )
            assert kb.release_stale_claims(conn) == 1
        elif recovery == "manual_reclaim":
            assert kb.reclaim_task(conn, task.id, reason="operator retry")
        else:
            fake_pid = 987654
            old = int(time.time()) - 10_000
            conn.execute(
                "UPDATE tasks SET worker_pid = ?, started_at = ?, "
                "last_heartbeat_at = NULL WHERE id = ?",
                (fake_pid, old, task.id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ?, started_at = ?, "
                "last_heartbeat_at = NULL WHERE id = ?",
                (fake_pid, old, task.current_run_id),
            )
            if recovery == "timeout":
                assert kb.enforce_max_runtime(
                    conn, signal_fn=lambda *_args: None, failure_limit=3
                ) == [task.id]
            elif recovery == "stale":
                assert kb.detect_stale_running(
                    conn,
                    stale_timeout_seconds=1,
                    signal_fn=lambda *_args: None,
                ) == [task.id]
            else:
                monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
                assert kb.detect_crashed_workers(conn, failure_limit=3) == [
                    task.id
                ]
        _assert_review_retry(conn, task.id)


def test_review_failure_breaker_always_blocks(kanban_home):
    with kb.connect() as conn:
        task = _claim_review(conn)
        assert kb._record_spawn_failure(
            conn, task.id, "permanent spawn failure", failure_limit=1
        )
        blocked = kb.get_task(conn, task.id)
        assert blocked.status == "blocked"
        assert blocked.resume_lane == "review"


@pytest.mark.parametrize("transition", ["blocked", "dependency", "scheduled", "triage"])
def test_review_wait_transitions_preserve_lane_and_resume_review(
    kanban_home, transition
):
    with kb.connect() as conn:
        task = _claim_review(conn)
        if transition == "blocked":
            assert kb.block_task(
                conn,
                task.id,
                reason="needs decision",
                kind="needs_input",
                expected_run_id=task.current_run_id,
            )
            assert kb.unblock_task(conn, task.id)
        elif transition == "dependency":
            assert kb.block_task(
                conn,
                task.id,
                reason="wait",
                kind="dependency",
                expected_run_id=task.current_run_id,
            )
            kb.recompute_ready(conn)
        elif transition == "scheduled":
            assert kb.schedule_task(
                conn,
                task.id,
                reason="later",
                expected_run_id=task.current_run_id,
            )
            assert kb.unblock_task(conn, task.id)
        else:
            conn.execute(
                "UPDATE tasks SET block_kind = 'needs_input', "
                "block_recurrences = ? WHERE id = ?",
                (kb.BLOCK_RECURRENCE_LIMIT - 1, task.id),
            )
            assert kb.block_task(
                conn,
                task.id,
                reason="loop",
                kind="needs_input",
                expected_run_id=task.current_run_id,
            )
            assert kb.get_task(conn, task.id).status == "triage"
            assert kb.specify_triage_task(conn, task.id, body="resolved")
        resumed = kb.get_task(conn, task.id)
        assert resumed.status == "review"
        assert resumed.resume_lane == "review"


def test_review_dependency_late_add_and_claim_recheck_keep_review_lane(
    kanban_home,
):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        child = kb.create_task(conn, title="review child", assignee="default")
        _set_review_waiting(conn, child)
        kb.link_tasks(conn, parent, child)
        assert kb.get_task(conn, child).status == "todo"
        assert kb.get_task(conn, child).resume_lane == "review"
        assert kb.complete_task(conn, parent, summary="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "review"

        later_parent = kb.create_task(
            conn, title="later parent", assignee="default"
        )
        # Model a parent inserted by an older integration after review became
        # visible, bypassing link_tasks' eager demotion. Claim must re-check.
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (later_parent, child),
        )
        assert kb.claim_review_task(conn, child) is None
        demoted = kb.get_task(conn, child)
        assert demoted.status == "todo"
        assert demoted.resume_lane == "review"


def test_manual_promote_and_redirect_never_change_review_lane(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="manual", assignee="default")
        conn.execute(
            "UPDATE tasks SET status = 'blocked', resume_lane = 'review' "
            "WHERE id = ?",
            (task_id,),
        )
        promoted, reason = kb.promote_task(
            conn, task_id, actor="operator", reason="retry"
        )
        assert promoted is True and reason is None
        assert kb.get_task(conn, task_id).status == "review"

        result = kb.persist_handoff_control(
            conn,
            control_id="review-redirect",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="redirect",
            message="continue with corrected direction",
            phase="before_start",
        )
        assert result["status"] == "recorded"
        redirected = kb.get_task(conn, task_id)
        assert redirected.status == "review"
        assert redirected.resume_lane == "review"


def _claim_managed_lane_as_current_process(conn, task_id: str, *, review: bool):
    claimed = (
        kb.claim_review_task(conn, task_id, claimer="reviewer")
        if review
        else kb.claim_task(conn, task_id, claimer="implementer")
    )
    assert claimed is not None
    assert kb._set_worker_pid(conn, task_id, os.getpid())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET handoff_safety_required = 1, "
            "owner_node_id = 'node', owner_boot_id = 'boot', "
            "worker_start_token = 'start', worker_pgid = ? WHERE id = ?",
            (os.getpid(), claimed.current_run_id),
        )
    return kb.get_task(conn, task_id)


def _managed_policy_env(monkeypatch, conn, task_id: str, *, review: bool) -> None:
    frozen = kb._task_short_handoff_worker_policy(conn, task_id)
    assert frozen is not None
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.current_run_id is not None and task.current_run_id > 0
    snapshot = dict(frozen)
    if review:
        snapshot["enabled"] = False
        snapshot["inactive_reason"] = kb._SHORT_TASK_REVIEW_INACTIVE_REASON
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv(
        "HERMES_KANBAN_MANAGED_LANE",
        "review" if review else "implementation",
    )
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1" if review else "0")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False)


def _release_self_gate(conn, task_id: str, monkeypatch) -> None:
    # Scope the dispatcher-owned proof of process exit to this release call.
    # The emergency-brake hardening deliberately refuses to infer death from
    # an unverified/missing identity, so this lifecycle test supplies the same
    # explicit release witness used by the dispatcher's other gate tests.
    with monkeypatch.context() as release_patch:
        release_patch.setattr(
            kb, "_exit_gate_release_reason", lambda _row: "test_worker_exited"
        )
        kb.release_handoff_exit_gates(conn)
    assert conn.execute(
        "SELECT 1 FROM task_exit_gates WHERE child_task_id = ? "
        "AND released_at IS NULL",
        (task_id,),
    ).fetchone() is None


def _create_managed_review_run(
    conn,
    workspace: Path,
    monkeypatch,
    *,
    validation_class: str,
):
    task_id = kb.create_task(
        conn,
        title="managed validation",
        assignee="default",
        workspace_kind="dir",
        workspace_path=str(workspace),
        validation_class=validation_class,
        control_origin=_phase1_origin(workspace),
    )
    implementation = _claim_managed_lane_as_current_process(
        conn, task_id, review=False
    )
    _managed_policy_env(monkeypatch, conn, task_id, review=False)
    assert kb.submit_task_for_review(
        conn,
        task_id,
        summary="implementation submitted",
        expected_run_id=implementation.current_run_id,
        expected_worker_pid=os.getpid(),
    )
    _release_self_gate(conn, task_id, monkeypatch)
    reviewer = _claim_managed_lane_as_current_process(
        conn, task_id, review=True
    )
    _managed_policy_env(monkeypatch, conn, task_id, review=True)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace.resolve()))
    return task_id, implementation, reviewer


def test_managed_review_rejection_returns_to_fresh_implementation_then_review(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "review-return-pilot"
    workspace.mkdir()
    (workspace / "candidate.txt").write_text("needs repair\n", encoding="utf-8")
    with kb.connect() as conn:
        task_id, _implementation, first_reviewer = _create_managed_review_run(
            conn,
            workspace,
            monkeypatch,
            validation_class="text_mechanism",
        )
        returned = kb.return_review_to_implementation(
            conn,
            task_id,
            reason="candidate.txt still needs repair",
            expected_run_id=first_reviewer.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert returned is not None
        waiting = kb.get_task(conn, task_id)
        assert waiting.status == "todo"
        assert waiting.resume_lane == "implementation"
        assert waiting.current_run_id is None
        assert kb.claim_task(conn, task_id) is None

        _release_self_gate(conn, task_id, monkeypatch)
        repaired = _claim_managed_lane_as_current_process(
            conn, task_id, review=False
        )
        assert repaired.current_run_id != first_reviewer.current_run_id
        _managed_policy_env(monkeypatch, conn, task_id, review=False)
        assert kb.submit_task_for_review(
            conn,
            task_id,
            summary="repair submitted",
            expected_run_id=repaired.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        _release_self_gate(conn, task_id, monkeypatch)
        second_reviewer = _claim_managed_lane_as_current_process(
            conn, task_id, review=True
        )
        assert second_reviewer.current_run_id not in {
            first_reviewer.current_run_id,
            repaired.current_run_id,
        }
        assert [event.kind for event in kb.list_events(conn, task_id)].count(
            "review_changes_requested"
        ) == 1


def test_code_class_review_cannot_complete_without_isolated_verifier(
    kanban_home, tmp_path, monkeypatch
):
    from tools import managed_file_tools

    workspace = tmp_path / "code-review-pilot"
    workspace.mkdir()
    (workspace / "candidate.txt").write_text("looks textual\n", encoding="utf-8")
    with kb.connect() as conn:
        task_id, _implementation, reviewer = _create_managed_review_run(
            conn,
            workspace,
            monkeypatch,
            validation_class="code",
        )
        assert "looks textual" in managed_file_tools.read_file_tool(
            "candidate.txt"
        )
        assert not kb.complete_task(
            conn,
            task_id,
            summary="must not complete",
            expected_run_id=reviewer.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        still_running = kb.get_task(conn, task_id)
        assert still_running.status == "running"
        assert still_running.resume_lane == "review"
        rejected = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "completion_rejected"
        ]
        assert rejected[-1].payload["action"] == "block_for_verifier"


def test_code_class_kanban_complete_routes_to_capability_block(
    kanban_home, tmp_path, monkeypatch
):
    from tools import kanban_tools as kt

    workspace = tmp_path / "code-tool-review-pilot"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id, _implementation, reviewer = _create_managed_review_run(
            conn,
            workspace,
            monkeypatch,
            validation_class="code",
        )

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(reviewer.current_run_id))
    monkeypatch.setattr(kt, "_review_completion_process_error", lambda: None)
    response = json.loads(
        kt._handle_complete({"summary": "looks good but has no verifier"})
    )
    assert response["ok"] is True
    assert response["status"] == "blocked"
    assert response["block_kind"] == "capability"
    assert response["verification_required"] is True

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert [event.kind for event in kb.list_events(conn, task_id)].count(
            "completed"
        ) == 0


def test_text_mechanism_review_requires_real_read_hash_and_rejects_command_claims(
    kanban_home, tmp_path, monkeypatch
):
    from tools import managed_file_tools

    workspace = tmp_path / "text-review-pilot"
    workspace.mkdir()
    candidate = workspace / "candidate.txt"
    candidate.write_text("mechanism evidence\n", encoding="utf-8")
    with kb.connect() as conn:
        task_id, _implementation, reviewer = _create_managed_review_run(
            conn,
            workspace,
            monkeypatch,
            validation_class="text_mechanism",
        )
        no_read = kb.managed_review_completion_decision(conn, task_id)
        assert no_read["allowed"] is False
        assert no_read["action"] == "retry"

        assert "mechanism evidence" in managed_file_tools.read_file_tool(
            "candidate.txt"
        )
        command_claim = kb.managed_review_completion_decision(
            conn,
            task_id,
            metadata={"commands_run": 0},
        )
        assert command_claim["allowed"] is False
        assert "commands_run" in command_claim["reason"]

        assert kb.complete_task(
            conn,
            task_id,
            summary="read-only mechanism review passed",
            metadata={"phase": "review"},
            expected_run_id=reviewer.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        completed = kb.get_task(conn, task_id)
        assert completed.status == "done"
        run = kb.latest_run(conn, task_id)
        evidence = run.metadata[kb._MANAGED_REVIEW_EVIDENCE_KEY]
        assert evidence["validation_class"] == "text_mechanism"
        assert evidence["read_files"][0]["path"] == "candidate.txt"
