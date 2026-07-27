"""Behavior and process-boundary tests for fresh-worker Kanban handoff."""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from agent import kanban_auto_handoff as handoff
from agent import kanban_handoff_scope as handoff_scope
from hermes_cli import kanban_db as kb
from tools.environments.local import LocalEnvironment
from tools.terminal_tool import _short_task_detach_guard, terminal_tool


_REAL_CAPTURE_HANDOFF_WORKER_IDENTITY = kb._capture_handoff_worker_identity
_REAL_CAPTURE_PROCESS_GROUP_IDENTITY = kb._capture_process_group_identity
_FAKE_IDENTITY = {
    "owner_node_id": "test-node",
    "owner_boot_id": "test-boot",
    "worker_pid": os.getpid(),
    "worker_start_token": "test-start",
    "worker_pgid": os.getpid(),
}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        yaml.safe_dump(_config()), encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        kb,
        "_capture_handoff_worker_identity",
        lambda pid: dict(_FAKE_IDENTITY, worker_pid=int(pid), worker_pgid=int(pid)),
    )
    monkeypatch.setattr(
        kb,
        "_capture_process_group_identity",
        lambda pid: dict(_FAKE_IDENTITY, worker_pid=int(pid), worker_pgid=int(pid)),
    )
    monkeypatch.setattr(handoff, "live_dispatcher_policy_enabled", lambda: True)
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: True
    )
    kb.init_db()
    return home


def _policy(*, soft=4, maximum=3):
    return handoff.AutoHandoffPolicy(True, soft, maximum)


def _config(
    *,
    enabled=True,
    soft=4,
    maximum=3,
    hard=90,
    failure_limit=2,
    workspace_root="/tmp",
):
    return {
        "agent": {"max_turns": hard},
        "kanban": {
            "failure_limit": failure_limit,
            "short_task_handoff": {
                "enabled": enabled,
                "soft_iteration_limit": soft,
                "max_handoffs": maximum,
                "allowed_workspace_roots": [
                    str(Path(workspace_root).resolve())
                ],
                "allowed_origins": [
                    {
                        "platform": "feishu",
                        "chat_type": "group",
                        "chat_id": "group-1",
                        "user_id": "user-1",
                    }
                ],
            }
        },
    }


def _control_origin(
    *,
    message_id="create-1",
    operation_slot="tool",
    workspace_root="/tmp",
):
    origin = {
        "platform": "feishu",
        "scope_id": "tenant-1",
        "chat_type": "group",
        "chat_id": "group-1",
        "thread_id": "",
        "user_id": "user-1",
        "notifier_profile": "default",
        "session_key": "agent:default:feishu:group:group-1:user-1",
        "message_id": message_id,
        "operation_slot": operation_slot,
    }
    config = _config(workspace_root=workspace_root)
    decision = handoff_scope.decide_gateway_origin(config, origin)
    assert decision["authorized"] is True
    origin["short_handoff_policy"] = decision["task_policy_json"]
    # Tests that use a per-test explicit workspace also exercise the live
    # dispatcher-policy comparison. Keep that isolated test config aligned
    # with the same exact root frozen into the trusted origin.
    if workspace_root != "/tmp":
        home = (os.environ.get("HERMES_HOME") or "").strip()
        if home:
            Path(home, "config.yaml").write_text(
                yaml.safe_dump(config), encoding="utf-8"
            )
    return origin


def _claim_as_current_process(conn, task_id):
    claimed = kb.claim_task(conn, task_id, claimer="test-worker")
    assert claimed is not None
    kb._set_worker_pid(conn, task_id, os.getpid())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET handoff_safety_required = 1 WHERE id = ?",
            (int(claimed.current_run_id),),
        )
    return kb.get_task(conn, task_id)


def _set_worker_env(monkeypatch, task):
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))


def _handoff(monkeypatch, task, *, summary="checkpoint", maximum=3):
    _set_worker_env(monkeypatch, task)
    return handoff.create_successor_and_close(
        policy=_policy(maximum=maximum),
        summary=summary,
        api_call_count=4,
        max_iterations=90,
    )


def _prove_all_gates_exited(conn, monkeypatch):
    monkeypatch.setattr(kb, "_exit_gate_release_reason", lambda _row: "test_exit")
    return kb.release_handoff_exit_gates(conn)


def _direct_terminal_transition(
    conn, transition, task_id, *, run_id, worker_pid
):
    if transition == "complete":
        return kb.complete_task(
            conn,
            task_id,
            summary="finished",
            expected_run_id=run_id,
            expected_worker_pid=worker_pid,
        )
    if transition == "block":
        return kb.block_task(
            conn,
            task_id,
            reason="waiting for input",
            kind="needs_input",
            expected_run_id=run_id,
            expected_worker_pid=worker_pid,
        )
    return kb.schedule_task(
        conn,
        task_id,
        reason="wait until later",
        expected_run_id=run_id,
        expected_worker_pid=worker_pid,
    )


@pytest.mark.parametrize("operation", ["complete", "block", "schedule"])
def test_managed_worker_cli_terminal_commands_bind_exact_process_identity(
    kanban_home, monkeypatch, operation
):
    """The documented CLI path must satisfy the same exit gate as tools."""
    from hermes_cli import kanban as kanban_cli

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title=f"cli {operation}", assignee="default"
        )
        task = _claim_as_current_process(conn, task_id)
        # A CLI command is a child process, not the dispatcher-owned worker.
        # Force the recorded owner away from this test process so using
        # os.getpid() here would fail and the regression cannot be masked.
        recorded_worker_pid = os.getpid() + 100_000
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (recorded_worker_pid, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (recorded_worker_pid, int(task.current_run_id)),
            )
        task = kb.get_task(conn, task_id)
        _set_worker_env(monkeypatch, task)
        monkeypatch.setattr(
            kanban_cli.kb,
            "connect_closing",
            lambda: contextlib.nullcontext(conn),
        )

        if operation == "complete":
            rc = kanban_cli._cmd_complete(
                SimpleNamespace(
                    task_ids=[task_id], summary="done", result=None, metadata=None
                )
            )
            expected_status = "done"
        elif operation == "block":
            rc = kanban_cli._cmd_block(
                SimpleNamespace(
                    task_id=task_id, ids=[], reason=[], kind=None
                )
            )
            expected_status = "blocked"
        else:
            rc = kanban_cli._cmd_schedule(
                SimpleNamespace(task_id=task_id, ids=[], reason=[])
            )
            expected_status = "scheduled"

        landed = kb.get_task(conn, task_id)
        gates = conn.execute(
            """
            SELECT * FROM task_exit_gates
             WHERE child_task_id = ? AND released_at IS NULL
            """,
            (task_id,),
        ).fetchall()

        assert rc == 0
        assert landed.status == expected_status
        assert landed.current_run_id is None
        assert landed.worker_pid == recorded_worker_pid
        assert landed.claim_lock is not None
        assert len(gates) == 1
        ended_run = kb.latest_run(conn, task_id)
        assert ended_run.id == task.current_run_id
        assert ended_run.ended_at is not None
        assert ended_run.worker_pid == recorded_worker_pid
        assert ended_run.claim_lock == task.claim_lock


@pytest.mark.parametrize("operation", ["complete", "block", "schedule"])
def test_manual_cli_terminal_commands_do_not_invent_worker_identity(
    kanban_home, monkeypatch, operation
):
    """Ordinary operator CLI use remains non-worker behavior."""
    from hermes_cli import kanban as kanban_cli

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    calls = []

    def _record(_conn, _task_id, **kwargs):
        calls.append(kwargs)
        return True

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title=f"manual cli {operation}", assignee="default"
        )
        monkeypatch.setattr(
            kanban_cli.kb,
            "connect_closing",
            lambda: contextlib.nullcontext(conn),
        )
        monkeypatch.setattr(kanban_cli.kb, f"{operation}_task", _record)

        if operation == "complete":
            rc = kanban_cli._cmd_complete(
                SimpleNamespace(
                    task_ids=[task_id], summary=None, result=None, metadata=None
                )
            )
        elif operation == "block":
            rc = kanban_cli._cmd_block(
                SimpleNamespace(task_id=task_id, ids=[], reason=[], kind=None)
            )
        else:
            rc = kanban_cli._cmd_schedule(
                SimpleNamespace(task_id=task_id, ids=[], reason=[])
            )

    assert rc == 0
    assert calls == [
        {
            **({"result": None, "summary": None, "metadata": None} if operation == "complete" else {}),
            **({"reason": None, "kind": None} if operation == "block" else {}),
            **({"reason": None} if operation == "schedule" else {}),
            "expected_run_id": None,
            "expected_worker_pid": None,
        }
    ]


@pytest.mark.parametrize("operation", ["complete", "block", "schedule"])
def test_managed_worker_cli_terminal_commands_reject_wrong_claim(
    kanban_home, monkeypatch, operation
):
    from hermes_cli import kanban as kanban_cli

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title=f"wrong claim {operation}", assignee="default"
        )
        task = _claim_as_current_process(conn, task_id)
        _set_worker_env(monkeypatch, task)
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "wrong-claim")
        monkeypatch.setattr(
            kanban_cli.kb,
            "connect_closing",
            lambda: contextlib.nullcontext(conn),
        )

        if operation == "complete":
            rc = kanban_cli._cmd_complete(
                SimpleNamespace(
                    task_ids=[task_id], summary="done", result=None, metadata=None
                )
            )
        elif operation == "block":
            rc = kanban_cli._cmd_block(
                SimpleNamespace(task_id=task_id, ids=[], reason=[], kind=None)
            )
        else:
            rc = kanban_cli._cmd_schedule(
                SimpleNamespace(task_id=task_id, ids=[], reason=[])
            )

        assert rc == 1
        assert kb.get_task(conn, task_id).status == "running"
        assert conn.execute(
            """
            SELECT 1 FROM task_exit_gates
             WHERE child_task_id = ? AND released_at IS NULL
            """,
            (task_id,),
        ).fetchone() is None


def test_policy_is_disabled_outside_dispatcher_worker(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert handoff.resolve_policy(_config(), max_iterations=90).enabled is False


@pytest.mark.parametrize("value", ["true", 1, {}, None, False])
def test_feature_enable_requires_literal_boolean_true(value):
    config = _config()
    config["kanban"]["short_task_handoff"]["enabled"] = value
    assert handoff.configured_feature_enabled(config, max_iterations=90) is False


def test_enabled_policy_without_allowed_origins_fails_closed():
    config = _config()
    config["kanban"]["short_task_handoff"].pop("allowed_origins")

    snapshot = handoff.build_dispatcher_policy_snapshot(config)

    assert snapshot["enabled"] is False
    assert "allowed_origins" in snapshot["validation_error"]


def test_enabled_policy_with_empty_allowed_origins_fails_closed():
    config = _config()
    config["kanban"]["short_task_handoff"]["allowed_origins"] = []

    snapshot = handoff.build_dispatcher_policy_snapshot(config)

    assert snapshot["enabled"] is False
    assert "allowed_origins" in snapshot["validation_error"]


def test_enabled_policy_with_malformed_allowed_origins_fails_closed():
    config = _config()
    config["kanban"]["short_task_handoff"]["allowed_origins"] = [
        {"platform": "feishu", "chat_type": "group", "chat_id": "group-1"}
    ]

    snapshot = handoff.build_dispatcher_policy_snapshot(config)

    assert snapshot["enabled"] is False
    assert "user_id" in snapshot["validation_error"]


@pytest.mark.parametrize(
    ("soft", "maximum"),
    [(1, 3), (90, 3), (4, 0), ("bad", 3)],
)
def test_invalid_policy_fails_closed_everywhere(monkeypatch, soft, maximum):
    config = _config(soft=soft, maximum=maximum)
    snapshot = handoff.encode_dispatcher_policy_snapshot(config)
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    assert handoff.configured_feature_enabled(config, max_iterations=90) is False
    policy = handoff.resolve_policy(config, max_iterations=90, task_id="t_worker")
    assert policy.enabled is False
    assert policy.validation_error


def test_dispatcher_snapshot_overrides_assignee_profile_and_freezes_hard_limit(monkeypatch):
    snapshot = handoff.encode_dispatcher_policy_snapshot(_config(hard=90))
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)

    worker_policy = handoff.resolve_policy(
        _config(enabled=False, hard=20),
        max_iterations=90,
        task_id="t_worker",
    )
    mismatch = handoff.resolve_policy(
        _config(enabled=False, hard=20),
        max_iterations=20,
        task_id="t_worker",
    )

    assert worker_policy.enabled is True
    assert worker_policy.soft_iteration_limit == 4
    assert mismatch.enabled is False
    assert "does not match" in mismatch.validation_error


def test_schema_two_snapshot_freezes_dispatcher_failure_limit(monkeypatch):
    snapshot = handoff.build_dispatcher_policy_snapshot(
        _config(hard=90), failure_limit=4
    )
    monkeypatch.setenv(handoff.POLICY_SNAPSHOT_ENV, json.dumps(snapshot))

    policy = handoff.resolve_policy(
        _config(enabled=False, hard=20),
        max_iterations=90,
        task_id="t_worker",
    )

    assert snapshot["schema"] == 2
    assert snapshot["failure_limit"] == 4
    assert policy.enabled is True
    assert handoff.worker_failure_limit(strict=True) == 4


@pytest.mark.parametrize(
    "invalid_variant",
    ["legacy_schema", "missing", "boolean", "string", "zero"],
)
def test_invalid_or_legacy_failure_limit_snapshot_fails_closed(
    monkeypatch, invalid_variant
):
    snapshot = handoff.build_dispatcher_policy_snapshot(
        _config(hard=90), failure_limit=4
    )
    if invalid_variant == "legacy_schema":
        snapshot["schema"] = 1
    elif invalid_variant == "missing":
        snapshot.pop("failure_limit")
    elif invalid_variant == "boolean":
        snapshot["failure_limit"] = True
    elif invalid_variant == "string":
        snapshot["failure_limit"] = "4"
    else:
        snapshot["failure_limit"] = 0
    monkeypatch.setenv(handoff.POLICY_SNAPSHOT_ENV, json.dumps(snapshot))

    policy = handoff.resolve_policy(
        _config(), max_iterations=90, task_id="t_worker"
    )

    assert policy.enabled is False
    assert policy.validation_error
    with pytest.raises(RuntimeError, match="no valid failure limit"):
        handoff.worker_failure_limit(strict=True)
    assert handoff.worker_failure_limit(strict=False) == 2


def test_snapshot_read_failure_preserves_frozen_dispatcher_limit(
    tmp_path, monkeypatch
):
    def unreadable_config():
        raise RuntimeError("synthetic unreadable config")

    monkeypatch.setattr(
        "hermes_cli.config.load_config_current_strict", unreadable_config
    )

    snapshot = handoff.load_current_dispatcher_policy_snapshot(
        policy_home=str(tmp_path), failure_limit=4
    )

    assert snapshot["schema"] == 2
    assert snapshot["enabled"] is False
    assert snapshot["failure_limit"] == 4
    assert "could not be read" in snapshot["validation_error"]


def test_soft_limit_requires_terminal_free_eligible_leaf():
    assert handoff.should_request_handoff(
        policy=_policy(),
        api_call_count=4,
        messages=[{"role": "user", "content": "continue"}],
        eligibility_check=lambda: True,
    )
    assert not handoff.should_request_handoff(
        policy=_policy(),
        api_call_count=4,
        messages=[{"role": "tool", "name": "kanban_complete", "content": "ok"}],
        eligibility_check=lambda: True,
    )
    assert handoff.should_request_handoff(
        policy=_policy(),
        api_call_count=4,
        messages=[{"role": "tool", "name": "kanban_heartbeat", "content": '{"ok": true}'}],
        eligibility_check=lambda: True,
    )
    assert not handoff.should_request_handoff(
        policy=_policy(),
        api_call_count=4,
        messages=[
            {
                "role": "tool",
                "name": "kanban_heartbeat",
                "content": '{"ok": true, "waiting_for_user_control": true}',
            }
        ],
        eligibility_check=lambda: True,
    )
    assert not handoff.should_request_handoff(
        policy=_policy(),
        api_call_count=4,
        messages=[{"role": "user", "content": "continue"}],
        eligibility_check=lambda: False,
    )


def test_worker_preflight_rejects_scratch_and_nonleaf_before_summary(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        persistent_id = kb.create_task(
            conn,
            title="persistent leaf",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        persistent = _claim_as_current_process(conn, persistent_id)
    _set_worker_env(monkeypatch, persistent)
    assert handoff.worker_task_is_handoff_eligible() is True

    with kb.connect() as conn:
        kb.create_task(
            conn,
            title="existing downstream",
            assignee="default",
            parents=[persistent_id],
        )
    assert handoff.worker_task_is_handoff_eligible() is False

    with kb.connect() as conn:
        scratch_id = kb.create_task(conn, title="scratch", assignee="default")
        scratch_task = kb.get_task(conn, scratch_id)
        scratch_path = kb.resolve_workspace(scratch_task)
        kb.set_workspace_path(conn, scratch_id, str(scratch_path))
        scratch = _claim_as_current_process(conn, scratch_id)
    _set_worker_env(monkeypatch, scratch)
    assert handoff.worker_task_is_handoff_eligible() is False


def test_handoff_is_atomic_todo_until_durable_exit_then_promotes(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="bounded feature",
            body="original objective",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            session_id="origin-session",
        )
        parent = _claim_as_current_process(conn, parent_id)

    result = _handoff(monkeypatch, parent, summary="implemented A; verify B")
    child_id = result["successor_task_id"]
    with kb.connect() as conn:
        parent_after = kb.get_task(conn, parent_id)
        child = kb.get_task(conn, child_id)
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE child_task_id = ?", (child_id,)
        ).fetchone()
        assert parent_after.status == "done"
        assert parent_after.worker_pid == os.getpid()
        assert child.status == "todo"
        assert child.resume_lane == "implementation"
        assert child.workspace_path == str(workspace)
        assert child.session_id == "origin-session"
        assert gate is not None and gate["released_at"] is None
        assert kb.claim_task(conn, child_id) is None
        assert kb.has_spawnable_ready(conn) is False

        assert _prove_all_gates_exited(conn, monkeypatch) == 1
        assert kb.get_task(conn, child_id).status == "ready"
        assert kb.get_task(conn, parent_id).worker_pid is None
        assert kb.claim_task(conn, child_id, claimer="fresh") is not None


@pytest.mark.parametrize(
    ("transition", "terminal_status"),
    [
        ("complete", "done"),
        ("block", "blocked"),
        ("schedule", "scheduled"),
    ],
)
def test_managed_terminal_transition_retains_identity_until_exit_proof(
    kanban_home, tmp_path, monkeypatch, transition, terminal_status
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"managed {transition}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        original_claim = task.claim_lock

        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0]

        # Missing, stale-run, and wrong-process callers all fail without a
        # partial write. A run id alone is never authority to close a managed
        # process-owned run.
        missing_identity_ok = _direct_terminal_transition(
            conn,
            transition,
            task_id,
            run_id=task.current_run_id,
            worker_pid=None,
        )
        assert missing_identity_ok is False
        assert _direct_terminal_transition(
            conn,
            transition,
            task_id,
            run_id=int(task.current_run_id) + 1,
            worker_pid=os.getpid(),
        ) is False
        assert _direct_terminal_transition(
            conn,
            transition,
            task_id,
            run_id=task.current_run_id,
            worker_pid=os.getpid() + 1,
        ) is False
        assert kb.get_task(conn, task_id).status == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == event_count

        ok = _direct_terminal_transition(
            conn,
            transition,
            task_id,
            run_id=task.current_run_id,
            worker_pid=os.getpid(),
        )
        assert ok is True

        ended_task = kb.get_task(conn, task_id)
        ended_run = kb.latest_run(conn, task_id)
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE child_task_id = ? "
            "AND parent_task_id = ? AND released_at IS NULL",
            (task_id, task_id),
        ).fetchone()
        assert ended_task.status == terminal_status
        assert ended_task.current_run_id is None
        assert ended_task.worker_pid == os.getpid()
        assert ended_task.claim_lock == original_claim
        assert ended_run.ended_at is not None
        assert ended_run.worker_pid == os.getpid()
        assert ended_run.claim_lock == original_claim
        assert gate is not None and gate["gate_kind"] == "control_drain"

        if transition in {"block", "schedule"}:
            assert kb.unblock_task(conn, task_id) is True
            waiting = kb.get_task(conn, task_id)
            assert waiting.status == "todo"
            assert waiting.worker_pid == os.getpid()
            assert waiting.claim_lock == original_claim
            assert kb.recompute_ready(conn) == 0

        _prove_all_gates_exited(conn, monkeypatch)
        released_task = kb.get_task(conn, task_id)
        released_run = kb.latest_run(conn, task_id)
        assert released_task.worker_pid is None
        assert released_task.claim_lock is None
        assert released_run.worker_pid is None
        assert released_run.claim_lock is None


@pytest.mark.parametrize("transition", ["complete", "block", "schedule"])
def test_managed_terminal_gate_insert_fault_rolls_back_everything(
    kanban_home, tmp_path, transition
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"atomic {transition}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        original_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        conn.execute(
            "CREATE TRIGGER fail_terminal_exit_gate "
            "BEFORE INSERT ON task_exit_gates "
            "BEGIN SELECT RAISE(ABORT, 'terminal gate fault'); END"
        )

        with pytest.raises(Exception, match="terminal gate fault"):
            _direct_terminal_transition(
                conn,
                transition,
                task_id,
                run_id=task.current_run_id,
                worker_pid=os.getpid(),
            )

        unchanged = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert unchanged.status == "running"
        assert unchanged.current_run_id == task.current_run_id
        assert unchanged.worker_pid == os.getpid()
        assert unchanged.claim_lock == task.claim_lock
        assert run.status == "running"
        assert run.ended_at is None
        assert run.worker_pid == os.getpid()
        assert run.claim_lock == task.claim_lock
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == original_events


def test_managed_completion_rollback_removes_staged_artifact_copy(
    kanban_home,
):
    """A failed DB commit leaves neither an orphan copy nor mutated evidence."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="atomic artifact completion")
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task_id, workspace)
        artifact = workspace / "report.md"
        artifact.write_text("candidate", encoding="utf-8")
        claimed = _claim_as_current_process(conn, task_id)
        metadata = {"artifacts": [str(artifact)], "tests_run": ["focused"]}
        original_metadata = json.loads(json.dumps(metadata))
        conn.execute(
            "CREATE TRIGGER fail_artifact_terminal_gate "
            "BEFORE INSERT ON task_exit_gates "
            "BEGIN SELECT RAISE(ABORT, 'artifact terminal gate fault'); END"
        )

        with pytest.raises(Exception, match="artifact terminal gate fault"):
            kb.complete_task(
                conn,
                task_id,
                result="candidate complete",
                metadata=metadata,
                expected_run_id=claimed.current_run_id,
                expected_worker_pid=os.getpid(),
            )

        assert kb.get_task(conn, task_id).status == "running"
        assert kb.list_attachments(conn, task_id) == []
        assert metadata == original_metadata
        assert artifact.exists()
        attachment_dir = kb.task_attachments_dir(task_id)
        assert not attachment_dir.exists() or not any(attachment_dir.iterdir())


@pytest.mark.parametrize(
    ("kind", "prepare_recurrence", "terminal_status"),
    [
        ("dependency", False, "todo"),
        ("needs_input", True, "triage"),
    ],
)
def test_every_managed_block_route_keeps_the_exit_gate(
    kanban_home, tmp_path, monkeypatch, kind, prepare_recurrence,
    terminal_status
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"managed {terminal_status}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        if prepare_recurrence:
            conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = ? "
                "WHERE id = ?",
                (kind, kb.BLOCK_RECURRENCE_LIMIT - 1, task_id),
            )
        task = _claim_as_current_process(conn, task_id)

        assert kb.block_task(
            conn,
            task_id,
            reason="bounded managed block",
            kind=kind,
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )

        blocked = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert blocked.status == terminal_status
        assert blocked.worker_pid == os.getpid()
        assert blocked.claim_lock == task.claim_lock
        assert run.ended_at is not None
        assert run.worker_pid == os.getpid()
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ? "
            "AND parent_task_id = ? AND released_at IS NULL",
            (task_id, task_id),
        ).fetchone()[0] == 1

        _prove_all_gates_exited(conn, monkeypatch)
        assert kb.get_task(conn, task_id).worker_pid is None
        assert kb.latest_run(conn, task_id).worker_pid is None


def test_dependency_block_lifecycle_hook_observes_committed_state(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    observations = []
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="dependency hook ordering",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)

        def _observe(_event, observed_task_id, **_fields):
            observations.append(conn.in_transaction)
            with kb.connect() as observer:
                assert kb.get_task(observer, observed_task_id).status == "todo"
                assert observer.execute(
                    "SELECT COUNT(*) FROM task_exit_gates "
                    "WHERE child_task_id = ? AND released_at IS NULL",
                    (observed_task_id,),
                ).fetchone()[0] == 1

        monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", _observe)
        assert kb.block_task(
            conn,
            task_id,
            reason="wait for parent",
            kind="dependency",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )

    assert observations == [False]


@pytest.mark.parametrize("kind", ["dependency", "needs_input"])
def test_block_remains_successful_when_postcommit_hook_fails(
    kanban_home, tmp_path, monkeypatch, kind
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"hook failure {kind}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        monkeypatch.setattr(
            kb,
            "_fire_kanban_lifecycle_hook",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("hook failed")
            ),
        )

        assert kb.block_task(
            conn,
            task_id,
            reason="durable first",
            kind=kind,
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert kb.get_task(conn, task_id).status == (
            "todo" if kind == "dependency" else "blocked"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates "
            "WHERE child_task_id = ? AND released_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 1


def test_open_managed_gate_rejects_second_terminal_mutation(
    kanban_home, tmp_path
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="single terminal transition",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="wait for input",
            kind="needs_input",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        first_state = kb.get_task(conn, task_id)
        first_run = kb.latest_run(conn, task_id)

        assert kb.complete_task(conn, task_id, summary="must not override") is False
        assert kb.schedule_task(conn, task_id, reason="must not override") is False

        unchanged = kb.get_task(conn, task_id)
        unchanged_run = kb.latest_run(conn, task_id)
        assert unchanged.status == "blocked"
        assert unchanged.worker_pid == first_state.worker_pid == os.getpid()
        assert unchanged.claim_lock == first_state.claim_lock
        assert unchanged_run.id == first_run.id
        assert unchanged_run.worker_pid == first_run.worker_pid == os.getpid()
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ? "
            "AND released_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 1


def test_gated_successor_rejects_direct_schedule_until_parent_exit(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="handoff parent",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        parent = _claim_as_current_process(conn, parent_id)
    child_id = _handoff(monkeypatch, parent)["successor_task_id"]

    with kb.connect() as conn:
        assert kb.schedule_task(
            conn, child_id, reason="must route through control"
        ) is False
        child = kb.get_task(conn, child_id)
        assert child.status == "todo"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ? "
            "AND released_at IS NULL",
            (child_id,),
        ).fetchone()[0] == 1


def test_unknown_legacy_exit_gate_rejects_all_direct_terminal_mutations(
    kanban_home
):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="legacy parent", assignee="default")
        task_id = kb.create_task(conn, title="legacy gated child", assignee="default")
        before = kb.get_task(conn, task_id)
        conn.execute(
            "INSERT INTO task_exit_gates ("
            "gate_id, gate_kind, child_task_id, parent_task_id, parent_run_id, "
            "owner_node_id, owner_boot_id, worker_pid, worker_start_token, "
            "worker_pgid, created_at"
            ") VALUES ('legacy-gate', 'legacy_unknown', ?, ?, 999999, "
            "'unknown-node', 'unknown-boot', 424242, 'unknown-start', "
            "424242, 1)",
            (task_id, parent_id),
        )

        assert kb.complete_task(conn, task_id, summary="must wait") is False
        assert kb.block_task(
            conn, task_id, reason="must wait", kind="needs_input"
        ) is False
        assert kb.schedule_task(conn, task_id, reason="must wait") is False

        after = kb.get_task(conn, task_id)
        assert after.status == before.status
        assert after.current_run_id is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0


def test_managed_triage_cannot_specify_or_decompose_until_worker_exit(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="managed triage root",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        conn.execute(
            "UPDATE tasks SET block_kind = 'needs_input', "
            "block_recurrences = ? WHERE id = ?",
            (kb.BLOCK_RECURRENCE_LIMIT - 1, task_id),
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="requires decomposition",
            kind="needs_input",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        count_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        assert kb.specify_triage_task(
            conn, task_id, body="must wait for old worker"
        ) is False
        assert kb.decompose_triage_task(
            conn,
            task_id,
            root_assignee="default",
            children=[{"title": "unsafe early child"}],
        ) is None
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == count_before
        assert kb.get_task(conn, task_id).status == "triage"

        _prove_all_gates_exited(conn, monkeypatch)
        child_ids = kb.decompose_triage_task(
            conn,
            task_id,
            root_assignee="default",
            children=[{"title": "safe child after exit"}],
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        assert kb.get_task(conn, child_ids[0]).status == "todo"


def test_managed_parent_completion_blocks_child_until_parent_exit(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="managed parent",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        child_id = kb.create_task(
            conn,
            title="downstream child",
            assignee="default",
            parents=[parent_id],
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        parent = _claim_as_current_process(conn, parent_id)

        assert kb.complete_task(
            conn,
            parent_id,
            summary="logical work is finished",
            expected_run_id=parent.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert kb.get_task(conn, child_id).status == "todo"
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, child_id, claimer="too-early") is None
        assert kb.has_spawnable_ready(conn) is False

        assert _prove_all_gates_exited(conn, monkeypatch) == 1
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child_id).status == "ready"
        assert kb.claim_task(conn, child_id, claimer="fresh") is not None


def test_parent_exit_gate_cannot_be_bypassed_by_dependency_mutations(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="managed dependency parent",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        parent = _claim_as_current_process(conn, parent_id)
        assert kb.complete_task(
            conn,
            parent_id,
            summary="logical parent done",
            expected_run_id=parent.current_run_id,
            expected_worker_pid=os.getpid(),
        )

        child_id = kb.create_task(
            conn,
            title="created after logical completion",
            assignee="default",
            parents=[parent_id],
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        linked_child_id = kb.create_task(
            conn,
            title="linked after logical completion",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        kb.link_tasks(conn, parent_id, linked_child_id)
        archive_child_id = kb.create_task(
            conn,
            title="archive must wait",
            assignee="default",
            parents=[parent_id],
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        delete_child_id = kb.create_task(
            conn,
            title="delete must wait",
            assignee="default",
            parents=[parent_id],
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        grandchild_id = kb.create_task(
            conn,
            title="grandchild cannot escape",
            assignee="default",
            parents=[child_id],
            workspace_kind="dir",
            workspace_path=str(workspace),
        )

        assert kb.get_task(conn, child_id).status == "todo"
        assert kb.get_task(conn, linked_child_id).status == "todo"
        assert kb.unlink_tasks(conn, parent_id, linked_child_id) is False
        assert parent_id in kb.parent_ids(conn, linked_child_id)

        # Even a stale/manual writer forcing ready cannot terminate the child
        # and thereby release its own descendants while the parent still runs.
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id IN (?, ?)",
            (child_id, archive_child_id),
        )
        assert kb.complete_task(
            conn, child_id, summary="must wait for parent process"
        ) is False
        assert kb.archive_task(conn, archive_child_id) is False
        assert kb.delete_task(conn, delete_child_id) is False
        assert kb.get_task(conn, delete_child_id) is not None
        assert kb.get_task(conn, grandchild_id).status == "todo"
        assert kb.claim_task(conn, grandchild_id, claimer="too-early") is None

        assert _prove_all_gates_exited(conn, monkeypatch) == 2
        assert kb.recompute_ready(conn) == 0
        assert kb.complete_task(conn, child_id, summary="now safe") is True
        assert kb.get_task(conn, grandchild_id).status == "ready"
        assert kb.archive_task(conn, archive_child_id) is True
        assert kb.delete_task(conn, delete_child_id) is True
        assert kb.unlink_tasks(conn, parent_id, linked_child_id) is True


def test_exit_gate_release_rechecks_late_cleanup_veto_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="late cleanup veto",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb._record_task_failure(
            conn,
            task_id,
            error="bounded retry",
            outcome="timed_out",
            failure_limit=2,
            release_claim=True,
            end_run=True,
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
            expected_claim_lock=task.claim_lock,
        ) is False

        def _late_veto(_row):
            assert kb.mark_run_process_cleanup_unsafe(
                conn,
                task_id=task_id,
                run_id=int(task.current_run_id),
                claim_lock=task.claim_lock,
                worker_pid=os.getpid(),
                reason="terminal group still alive",
            )
            return "process_group_exited"

        monkeypatch.setattr(kb, "_exit_gate_release_reason", _late_veto)
        assert kb.release_handoff_exit_gates(conn) == 0
        still_parked = kb.get_task(conn, task_id)
        assert still_parked.status == "todo"
        assert still_parked.worker_pid == os.getpid()
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates "
            "WHERE child_task_id = ? AND released_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 1
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, task_id, claimer="unsafe-overlap") is None


def test_managed_scratch_cleanup_waits_for_exit_proof(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="managed scratch cleanup",
            assignee="default",
            workspace_kind="scratch",
        )
        created = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(created)
        kb.set_workspace_path(conn, task_id, str(workspace))
        marker = workspace / "deliverable.txt"
        marker.write_text("kept until worker exit", encoding="utf-8")
        task = _claim_as_current_process(conn, task_id)

        assert kb.complete_task(
            conn,
            task_id,
            summary="finished scratch task",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert marker.exists()

        _prove_all_gates_exited(conn, monkeypatch)
        assert not workspace.exists()


def test_iteration_failure_parks_managed_worker_behind_self_gate(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="budget exhausted",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        original_claim = task.claim_lock

        tripped = kb._record_task_failure(
            conn,
            task_id,
            error="iteration budget exhausted",
            outcome="timed_out",
            failure_limit=2,
            release_claim=True,
            end_run=True,
        )

        assert tripped is False
        parked = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert parked.status == "todo"
        assert parked.current_run_id is None
        assert parked.worker_pid == os.getpid()
        assert parked.claim_lock == original_claim
        assert parked.consecutive_failures == 1
        assert run.status == "timed_out"
        assert run.ended_at is not None
        assert run.worker_pid == os.getpid()
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ? "
            "AND parent_task_id = ? AND released_at IS NULL",
            (task_id, task_id),
        ).fetchone()[0] == 1
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, task_id, claimer="too-early") is None

        assert _prove_all_gates_exited(conn, monkeypatch) == 1
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, task_id, claimer="fresh") is not None


@pytest.mark.parametrize(
    ("failure_limit", "max_retries"),
    [(1, None), (99, 1)],
)
def test_iteration_failure_breaker_is_atomic_and_stays_blocked_after_exit(
    kanban_home, tmp_path, monkeypatch, failure_limit, max_retries
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="budget breaker",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            max_retries=max_retries,
        )
        task = _claim_as_current_process(conn, task_id)

        tripped = kb._record_task_failure(
            conn,
            task_id,
            error="iteration budget exhausted",
            outcome="timed_out",
            failure_limit=failure_limit,
            release_claim=True,
            end_run=True,
        )

        assert tripped is True
        parked = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert parked.status == "blocked"
        assert parked.consecutive_failures == 1
        assert parked.worker_pid == os.getpid()
        assert parked.claim_lock == task.claim_lock
        assert run.status == "timed_out"
        assert run.worker_pid == os.getpid()
        assert [event.kind for event in kb.list_events(conn, task_id)].count(
            "gave_up"
        ) == 1

        _prove_all_gates_exited(conn, monkeypatch)
        assert kb.get_task(conn, task_id).status == "blocked"


@pytest.mark.parametrize(
    (
        "initial_failures",
        "max_retries",
        "expected_status",
        "expected_failures",
        "expected_tripped",
    ),
    [
        (2, None, "todo", 3, False),
        (0, 1, "blocked", 1, True),
    ],
    ids=["dispatcher-limit-four", "task-override-one"],
)
def test_managed_90_of_90_uses_frozen_limit_and_task_override(
    kanban_home,
    tmp_path,
    monkeypatch,
    initial_failures,
    max_retries,
    expected_status,
    expected_failures,
    expected_tripped,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    snapshot = handoff.build_dispatcher_policy_snapshot(
        _config(hard=90), failure_limit=4
    )
    monkeypatch.setenv(handoff.POLICY_SNAPSHOT_ENV, json.dumps(snapshot))

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="managed 90/90 accounting",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            max_retries=max_retries,
        )
        task = _claim_as_current_process(conn, task_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
                (initial_failures, task_id),
            )

        result = kb._record_managed_task_failure_exact(
            conn,
            task_id,
            error="Iteration budget exhausted (90/90)",
            outcome="timed_out",
            summary="bounded checkpoint",
            expected_run_id=int(task.current_run_id),
            expected_worker_pid=os.getpid(),
            expected_claim_lock=str(task.claim_lock),
            failure_limit=handoff.worker_failure_limit(strict=True),
            event_payload_extra={"budget_used": 90, "budget_max": 90},
        )

        parked = kb.get_task(conn, task_id)
        assert result["status"] == "recorded"
        assert result["failure_tripped"] is expected_tripped
        assert parked.status == expected_status
        assert parked.consecutive_failures == expected_failures
        gave_up = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "gave_up"
        ]
        if expected_tripped:
            assert gave_up[-1].payload["limit_source"] == "task"
            assert gave_up[-1].payload["effective_limit"] == 1
        else:
            assert gave_up == []


@pytest.mark.parametrize("kind", ["steer", "stop"])
def test_user_control_winning_before_iteration_accounting_is_not_overwritten(
    kanban_home, tmp_path, monkeypatch, kind
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"control wins {kind}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        control = kb.persist_handoff_control(
            conn,
            control_id=f"control-wins-{kind}",
            source_task_id=task_id,
            target_task_id=task_id,
            kind=kind,
            message="new user direction",
            phase="before_commit",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert control["status"] == "recorded"
        counts_before = {
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()[0],
            "failures": kb.get_task(conn, task_id).consecutive_failures,
        }

        tripped = kb._record_task_failure(
            conn,
            task_id,
            error="iteration budget exhausted",
            outcome="timed_out",
            summary="stale fallback summary",
            failure_limit=1,
            release_claim=True,
            end_run=True,
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
            expected_claim_lock=task.claim_lock,
        )

        assert tripped is False
        assert kb.get_task(conn, task_id).status == (
            "blocked" if kind == "stop" else "todo"
        )
        assert kb.latest_run(conn, task_id).outcome == (
            "blocked" if kind == "stop" else "released"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == counts_before["events"]
        assert kb.get_task(conn, task_id).consecutive_failures == counts_before[
            "failures"
        ]
        assert not any(
            event.kind in {"timed_out", "gave_up"}
            for event in kb.list_events(conn, task_id)
        )
        assert kb.claim_task(conn, task_id, claimer="must-not-run") is None


def test_iteration_failure_accounting_rolls_back_with_gate_on_error(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="atomic budget accounting",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        real_append_event = kb._append_event

        def fail_gave_up(*args, **kwargs):
            kind = args[2] if len(args) > 2 else kwargs.get("kind")
            if kind == "gave_up":
                raise RuntimeError("injected event failure")
            return real_append_event(*args, **kwargs)

        monkeypatch.setattr(kb, "_append_event", fail_gave_up)
        with pytest.raises(RuntimeError, match="injected event failure"):
            kb._record_task_failure(
                conn,
                task_id,
                error="iteration budget exhausted",
                outcome="timed_out",
                failure_limit=1,
                release_claim=True,
                end_run=True,
            )
        monkeypatch.setattr(kb, "_append_event", real_append_event)

        unchanged = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert unchanged.status == "running"
        assert unchanged.current_run_id == task.current_run_id
        assert unchanged.worker_pid == os.getpid()
        assert unchanged.consecutive_failures == 0
        assert run.status == "running"
        assert run.ended_at is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ?",
            (task_id,),
        ).fetchone()[0] == 0


def test_live_policy_disable_before_handoff_commit_leaves_parent_unchanged(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="rollback at commit boundary",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        parent = _claim_as_current_process(conn, parent_id)
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "gates": conn.execute(
                "SELECT COUNT(*) FROM task_exit_gates"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
        }

    _set_worker_env(monkeypatch, parent)
    monkeypatch.setattr(
        handoff, "live_dispatcher_policy_enabled", lambda: False
    )
    with pytest.raises(RuntimeError, match="policy is now disabled"):
        handoff.create_successor_and_close(
            policy=_policy(),
            summary="must not persist",
            api_call_count=4,
            max_iterations=90,
        )

    with kb.connect() as conn:
        current = kb.get_task(conn, parent_id)
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "gates": conn.execute(
                "SELECT COUNT(*) FROM task_exit_gates"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
        }
        assert current.status == "running"
        assert current.current_run_id == parent.current_run_id
        assert current.worker_pid == os.getpid()
        assert after == before


def test_handoff_fault_rolls_back_parent_child_gate_run_and_subscription(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="atomic fault",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="feishu",
            chat_id="group",
        )
        parent = _claim_as_current_process(conn, parent_id)
        conn.execute(
            "CREATE TRIGGER fail_exit_gate BEFORE INSERT ON task_exit_gates "
            "BEGIN SELECT RAISE(ABORT, 'gate fault'); END"
        )
        with pytest.raises(Exception, match="gate fault"):
            kb.handoff_task(
                conn,
                parent_id,
                title="child",
                idempotency_key=f"kanban-auto-handoff:{parent_id}",
                summary="checkpoint",
                metadata={"auto_handoff": {"generation": 1}},
                expected_run_id=parent.current_run_id,
                expected_worker_pid=os.getpid(),
            )
        assert kb.get_task(conn, parent_id).status == "running"
        assert kb.child_ids(conn, parent_id) == []
        assert conn.execute("SELECT COUNT(*) FROM task_exit_gates").fetchone()[0] == 0
        assert kb.list_runs(conn, parent_id)[-1].status == "running"
        assert len(kb.list_notify_subs(conn, parent_id)) == 1


def test_managed_handoff_policy_change_at_final_commit_rolls_back_everything(
    kanban_home, tmp_path, monkeypatch
):
    """A config change after the first in-txn read is a zero-write veto."""
    workspace = tmp_path / "policy-race-project"
    workspace.mkdir()
    origin = _control_origin(
        message_id="handoff-policy-race",
        workspace_root=str(workspace.resolve()),
    )
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="policy race",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="feishu",
            chat_id="group",
        )
        parent = _claim_as_current_process(conn, parent_id)
        frozen = kb._task_short_handoff_worker_policy(conn, parent_id)
        assert frozen is not None
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "gates": conn.execute(
                "SELECT COUNT(*) FROM task_exit_gates"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
            "bindings": conn.execute(
                "SELECT COUNT(*) FROM kanban_control_bindings"
            ).fetchone()[0],
        }

        reads = 0

        def changing_policy(**_kwargs):
            nonlocal reads
            reads += 1
            if reads == 1:
                return dict(frozen)
            changed = dict(frozen)
            changed["enabled"] = False
            changed["validation_error"] = "disabled during commit"
            return changed

        monkeypatch.setattr(
            handoff,
            "load_current_dispatcher_policy_snapshot",
            changing_policy,
        )
        with pytest.raises(
            kb._ShortTaskPolicyChangedDuringHandoff,
            match="policy changed during handoff",
        ):
            kb.handoff_task(
                conn,
                parent_id,
                title="must roll back",
                idempotency_key=f"kanban-auto-handoff:{parent_id}",
                summary="must not persist",
                metadata={"auto_handoff": {"generation": 1}},
                expected_run_id=parent.current_run_id,
                expected_worker_pid=os.getpid(),
            )

        assert reads == 2
        current = kb.get_task(conn, parent_id)
        assert current.status == "running"
        assert current.current_run_id == parent.current_run_id
        assert kb.child_ids(conn, parent_id) == []
        assert kb.list_runs(conn, parent_id)[-1].status == "running"
        assert len(kb.list_notify_subs(conn, parent_id)) == 1
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "gates": conn.execute(
                "SELECT COUNT(*) FROM task_exit_gates"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
            "bindings": conn.execute(
                "SELECT COUNT(*) FROM kanban_control_bindings"
            ).fetchone()[0],
        }
        assert after == before


def test_nonleaf_handoff_fails_closed_without_releasing_downstream(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="parent",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        downstream_id = kb.create_task(
            conn, title="deploy later", assignee="default", parents=[parent_id]
        )
        parent = _claim_as_current_process(conn, parent_id)
    _set_worker_env(monkeypatch, parent)

    with pytest.raises(RuntimeError, match="leaf task"):
        handoff.create_successor_and_close(
            policy=_policy(), summary="unsafe", api_call_count=4, max_iterations=90
        )
    with kb.connect() as conn:
        assert kb.get_task(conn, parent_id).status == "running"
        assert kb.get_task(conn, downstream_id).status == "todo"
        assert kb.child_ids(conn, parent_id) == [downstream_id]
        assert conn.execute("SELECT COUNT(*) FROM task_exit_gates").fetchone()[0] == 0


def test_scratch_handoff_is_rejected_without_deleting_artifacts(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="scratch", assignee="default")
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task_id, str(workspace))
        artifact = workspace / "result.txt"
        artifact.write_text("keep", encoding="utf-8")
        task = _claim_as_current_process(conn, task_id)
    _set_worker_env(monkeypatch, task)

    with pytest.raises(RuntimeError, match="ephemeral scratch"):
        handoff.create_successor_and_close(
            policy=_policy(), summary="checkpoint", api_call_count=4, max_iterations=90
        )
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.child_ids(conn, task_id) == []
    assert artifact.read_text(encoding="utf-8") == "keep"


def test_second_handoff_increments_generation_sequentially(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="long implementation",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    first = _handoff(monkeypatch, root)
    with kb.connect() as conn:
        _prove_all_gates_exited(conn, monkeypatch)
        first_child = _claim_as_current_process(conn, first["successor_task_id"])
    second = _handoff(monkeypatch, first_child)

    assert second["generation"] == 2
    with kb.connect() as conn:
        assert kb.get_task(conn, second["successor_task_id"]).status == "todo"
        assert kb.get_task(conn, second["successor_task_id"]).title.endswith(
            "自动接力 2"
        )


def test_handoff_generation_cap_survives_dependency_unlink(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="immutable generation chain",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    first = _handoff(monkeypatch, root, maximum=2)
    with kb.connect() as conn:
        _prove_all_gates_exited(conn, monkeypatch)
        first_task = _claim_as_current_process(conn, first["successor_task_id"])
    second = _handoff(monkeypatch, first_task, maximum=2)
    with kb.connect() as conn:
        _prove_all_gates_exited(conn, monkeypatch)
        second_task = _claim_as_current_process(conn, second["successor_task_id"])
        assert kb.unlink_tasks(
            conn, first["successor_task_id"], second["successor_task_id"]
        )

    stopped = _handoff(monkeypatch, second_task, maximum=2)

    assert stopped["status"] == "safety_limit"
    assert stopped["generation"] == 3
    with kb.connect() as conn:
        assert kb.get_task(conn, second_task.id).status == "blocked"
        assert kb.child_ids(conn, second_task.id) == []


def test_missing_generation_proof_pauses_chain_instead_of_resetting(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="corrupt generation proof",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    first = _handoff(monkeypatch, root, maximum=2)
    with kb.connect() as conn:
        _prove_all_gates_exited(conn, monkeypatch)
        child = _claim_as_current_process(conn, first["successor_task_id"])
        created = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'created' ORDER BY id ASC LIMIT 1",
            (child.id,),
        ).fetchone()
        payload = json.loads(created["payload"])
        payload.pop("handoff_generation", None)
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps(payload), int(created["id"])),
        )

    with pytest.raises(RuntimeError, match="generation proof"):
        _handoff(monkeypatch, child, maximum=2)
    with kb.connect() as conn:
        assert kb.get_task(conn, child.id).status == "running"
        assert kb.child_ids(conn, child.id) == []


def test_safety_limit_blocks_instead_of_looping(kanban_home, tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="bounded chain",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    first = _handoff(monkeypatch, root, maximum=1)
    with kb.connect() as conn:
        _prove_all_gates_exited(conn, monkeypatch)
        child = _claim_as_current_process(conn, first["successor_task_id"])
    stopped = _handoff(monkeypatch, child, summary="limit", maximum=1)

    assert stopped["status"] == "safety_limit"
    with kb.connect() as conn:
        assert kb.get_task(conn, child.id).status == "blocked"
        assert kb.child_ids(conn, child.id) == []


@pytest.mark.parametrize("drift", ["foreign", "ended", "stale"])
def test_run_identity_drift_is_zero_write(
    kanban_home, tmp_path, monkeypatch, drift
):
    workspace = tmp_path / drift
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="CAS protected",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        expected = task.current_run_id
        if drift == "foreign":
            other_id = kb.create_task(
                conn,
                title="other",
                assignee="default",
                workspace_kind="dir",
                workspace_path=str(workspace),
            )
            other = _claim_as_current_process(conn, other_id)
            conn.execute(
                "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                (other.current_run_id, task_id),
            )
        elif drift == "ended":
            conn.execute(
                "UPDATE task_runs SET status='done', ended_at=1 WHERE id = ?",
                (expected,),
            )
        else:
            expected += 999

        result = kb.handoff_task(
            conn,
            task_id,
            title="child",
            idempotency_key=f"kanban-auto-handoff:{task_id}",
            summary="must not commit",
            metadata={"auto_handoff": {"generation": 1}},
            expected_run_id=expected,
            expected_worker_pid=os.getpid(),
        )
        assert result["status"] == "conflict"
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.child_ids(conn, task_id) == []
        assert conn.execute("SELECT COUNT(*) FROM task_exit_gates").fetchone()[0] == 0


def test_concurrent_replay_converges_on_one_child_and_gate(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="concurrent checkpoint",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        parent = _claim_as_current_process(conn, parent_id)
    _set_worker_env(monkeypatch, parent)

    def handoff_once(_index):
        return handoff.create_successor_and_close(
            policy=_policy(),
            summary="same checkpoint",
            api_call_count=4,
            max_iterations=90,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(handoff_once, range(8)))
    assert len({item["successor_task_id"] for item in results}) == 1
    assert any(item["idempotent_replay"] for item in results)
    with kb.connect() as conn:
        assert len(kb.child_ids(conn, parent_id)) == 1
        assert conn.execute("SELECT COUNT(*) FROM task_exit_gates").fetchone()[0] == 1
        assert [e.kind for e in kb.list_events(conn, parent_id)].count("handed_off") == 1


def test_notification_follows_successor_without_intermediate_completion(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="notify at true finish",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="feishu",
            chat_id="group-1",
            thread_id="thread-1",
        )
        parent = _claim_as_current_process(conn, parent_id)
    result = _handoff(monkeypatch, parent, summary="continue verification")
    child_id = result["successor_task_id"]

    with kb.connect() as conn:
        assert kb.list_notify_subs(conn, parent_id) == []
        assert len(kb.list_notify_subs(conn, child_id)) == 1
        assert "completed" not in [e.kind for e in kb.list_events(conn, parent_id)]
        assert "continue verification" in kb.build_worker_context(conn, child_id)
        _prove_all_gates_exited(conn, monkeypatch)
        child = kb.claim_task(conn, child_id, claimer="fresh")
        assert kb.complete_task(
            conn,
            child_id,
            result="done",
            summary="final result",
            expected_run_id=child.current_run_id,
        )
        _cursor, events = kb.unseen_events_for_sub(
            conn,
            task_id=child_id,
            platform="feishu",
            chat_id="group-1",
            thread_id="thread-1",
            kinds=["completed"],
        )
        assert [event.kind for event in events] == ["completed"]


def test_open_gate_blocks_manual_ready_review_health_claim_and_dispatch(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="guard all entry points",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        parent = _claim_as_current_process(conn, parent_id)
    child_id = _handoff(monkeypatch, parent)["successor_task_id"]

    spawned = []
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status='ready' WHERE id = ?", (child_id,))
        assert kb.has_spawnable_ready(conn) is False
        assert kb.claim_task(conn, child_id) is None
        conn.execute("UPDATE tasks SET status='review' WHERE id = ?", (child_id,))
        assert kb.has_spawnable_review(conn) is False
        assert kb.claim_review_task(conn, child_id) is None
        dry = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=2,
            spawn_fn=lambda *_a, **_kw: spawned.append(True),
        )
        assert dry.spawned == []
    assert spawned == []


def test_draining_parent_consumes_global_and_profile_capacity(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="draining",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    _handoff(monkeypatch, root)
    with kb.connect() as conn:
        other = kb.create_task(
            conn,
            title="other ready",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=1,
            max_in_progress=1,
            max_in_progress_per_profile=1,
        )
        assert result.spawned == []
        assert kb.get_task(conn, other).status == "ready"


def test_draining_capacity_stays_with_immutable_run_profile_after_reassign(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="draining then reassigned",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    _handoff(monkeypatch, root)

    with kb.connect() as conn:
        assert kb.assign_task(conn, root_id, "other") is True
        ready_default = kb.create_task(
            conn, title="default waiting", assignee="default"
        )
        ready_other = kb.create_task(
            conn, title="other available", assignee="other"
        )
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=3,
            max_in_progress_per_profile=1,
        )

        assert [item[0] for item in result.spawned] == [ready_other]
        assert result.skipped_per_profile_capped == [
            (ready_default, "default", 1)
        ]


def test_precommit_redirect_is_durable_requeue_with_self_exit_gate(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="redirected",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        first = kb.persist_handoff_control(
            conn,
            control_id="hc_redirect",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="redirect",
            message="use the safer migration",
            phase="before_commit",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        replay = kb.persist_handoff_control(
            conn,
            control_id="hc_redirect",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="redirect",
            message="use the safer migration",
            phase="before_commit",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert first["status"] == "recorded"
        assert replay["status"] == "already_recorded"
        assert kb.get_task(conn, task_id).status == "todo"
        assert kb.list_runs(conn, task_id)[-1].outcome == "released"
        assert "safer migration" in kb.build_worker_context(conn, task_id)
        assert kb.claim_task(conn, task_id) is None
        _prove_all_gates_exited(conn, monkeypatch)
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.list_runs(conn, task_id)[-1].worker_pid is None


def test_postcommit_stop_blocks_gated_successor(kanban_home, tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="stop after commit",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    child_id = _handoff(monkeypatch, root)["successor_task_id"]
    with kb.connect() as conn:
        result = kb.persist_handoff_control(
            conn,
            control_id="hc_stop",
            source_task_id=root_id,
            target_task_id=child_id,
            kind="stop",
            message="stop this chain",
            phase="after_commit",
        )
        assert result["status"] == "recorded"
        assert kb.get_task(conn, child_id).status == "blocked"
        _prove_all_gates_exited(conn, monkeypatch)
        assert kb.get_task(conn, child_id).status == "blocked"
        assert "stop this chain" in kb.build_worker_context(conn, child_id)


def test_close_uncertainty_replays_original_phase_and_same_control_id(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="close uncertainty",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)

    real_connect = kb.connect
    connect_count = 0

    class _CloseUncertainConnection:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            self._inner.close()
            raise RuntimeError("close result was uncertain")

    def flaky_connect():
        nonlocal connect_count
        connect_count += 1
        conn = real_connect()
        return _CloseUncertainConnection(conn) if connect_count == 1 else conn

    monkeypatch.setattr(kb, "connect", flaky_connect)
    result = handoff.persist_worker_handoff_control(
        {
            "control_id": "hc-close-original-phase",
            "source_task_id": task_id,
            "target_task_id": task_id,
            "kind": "redirect",
            "message": "retain the original phase",
            "phase": "before_commit",
            "expected_run_id": task.current_run_id,
            "expected_worker_pid": os.getpid(),
        },
        attempts=2,
    )

    assert result["status"] == "already_recorded"
    assert result["attempts"] == 2
    with real_connect() as conn:
        rows = conn.execute(
            "SELECT phase FROM task_handoff_controls WHERE control_id = ?",
            ("hc-close-original-phase",),
        ).fetchall()
        assert [row["phase"] for row in rows] == ["before_commit"]


def test_superseded_receipt_is_exactly_once_without_task_state_change(
    kanban_home, tmp_path
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="stop winner",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb.persist_handoff_control(
            conn,
            control_id="hc-stop-winner",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="stop",
            message="stop wins",
            phase="before_commit",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )["status"] == "recorded"
        before = tuple(
            conn.execute(
                "SELECT status, current_run_id, worker_pid, block_kind "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        )

        first = kb.persist_superseded_handoff_control(
            conn,
            control_id="hc-lower-redirect",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="redirect",
            message="continue anyway",
            superseded_by_control_id="hc-stop-winner",
        )
        replay = kb.persist_superseded_handoff_control(
            conn,
            control_id="hc-lower-redirect",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="redirect",
            message="continue anyway",
            superseded_by_control_id="hc-stop-winner",
        )
        after = tuple(
            conn.execute(
                "SELECT status, current_run_id, worker_pid, block_kind "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        )

        assert first["status"] == "recorded"
        assert replay["status"] == "already_recorded"
        assert first["consumed"] is True
        assert before == after
        assert conn.execute(
            "SELECT COUNT(*) FROM task_handoff_controls WHERE control_id = ?",
            ("hc-lower-redirect",),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'handoff_control_superseded'",
            (task_id,),
        ).fetchone()[0] == 1


def test_postcommit_outage_keeps_gate_until_exact_control_recovers(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="postcommit outage",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    child_id = _handoff(monkeypatch, root)["successor_task_id"]

    real_persist = handoff.persist_worker_handoff_control
    attempt_started = threading.Event()
    allow_recovery = threading.Event()

    def recover_after_outage(control, *, attempts=2):
        attempt_started.set()
        assert allow_recovery.wait(timeout=5)
        return real_persist(control, attempts=attempts)

    monkeypatch.setattr(
        handoff, "persist_worker_handoff_control", recover_after_outage
    )
    handoff.stage_pending_handoff_control(
        {
            "control_id": "hc-postcommit-outage",
            "source_task_id": root_id,
            "target_task_id": child_id,
            "kind": "stop",
            "message": "stop while storage is unavailable",
            "phase": "after_commit",
        },
        error="database unavailable",
    )
    assert attempt_started.wait(timeout=5)
    assert handoff.pending_handoff_control_count() == 1
    assert os.environ.get(handoff.PENDING_CONTROL_ENV) == "1"
    with kb.connect() as conn:
        assert kb.claim_task(conn, child_id) is None
        assert conn.execute(
            "SELECT released_at FROM task_exit_gates WHERE child_task_id = ?",
            (child_id,),
        ).fetchone()["released_at"] is None

    allow_recovery.set()
    assert handoff.wait_for_pending_handoff_controls(timeout=5)
    assert os.environ.get(handoff.PENDING_CONTROL_ENV) is None
    with kb.connect() as conn:
        assert kb.get_task(conn, child_id).status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_handoff_controls WHERE control_id = ?",
            ("hc-postcommit-outage",),
        ).fetchone()[0] == 1


def test_postveto_outage_keeps_self_gate_until_exact_control_recovers(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="postveto outage",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb.persist_handoff_control(
            conn,
            control_id="hc-initial-veto",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="redirect",
            message="first direction",
            phase="before_commit",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )["status"] == "recorded"
    _set_worker_env(monkeypatch, task)

    real_persist = handoff.persist_worker_handoff_control
    attempt_started = threading.Event()
    allow_recovery = threading.Event()

    def recover_after_outage(control, *, attempts=2):
        attempt_started.set()
        assert allow_recovery.wait(timeout=5)
        return real_persist(control, attempts=attempts)

    monkeypatch.setattr(
        handoff, "persist_worker_handoff_control", recover_after_outage
    )
    handoff.stage_pending_handoff_control(
        {
            "control_id": "hc-postveto-outage",
            "source_task_id": task_id,
            "target_task_id": task_id,
            "kind": "stop",
            "message": "stop the vetoed task",
            "phase": "after_terminal",
        },
        error="database unavailable",
    )
    assert attempt_started.wait(timeout=5)
    with kb.connect() as conn:
        assert kb.claim_task(conn, task_id) is None
        assert conn.execute(
            "SELECT released_at FROM task_exit_gates "
            "WHERE child_task_id = ? AND parent_task_id = ?",
            (task_id, task_id),
        ).fetchone()["released_at"] is None

    allow_recovery.set()
    assert handoff.wait_for_pending_handoff_controls(timeout=5)
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_handoff_controls WHERE control_id = ?",
            ("hc-postveto-outage",),
        ).fetchone()[0] == 1


def test_published_receipt_survives_signal_style_stack_unwind(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="signal unwind",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
    _set_worker_env(monkeypatch, task)
    monkeypatch.setattr(handoff, "_HARD_EXIT_COMMITTED", False)

    replay_started = threading.Event()
    allow_replay = threading.Event()

    def replay_after_unwind(control, *, attempts=2):
        replay_started.set()
        assert allow_replay.wait(timeout=5)
        return {
            "status": "recorded",
            "control_id": control["control_id"],
        }

    monkeypatch.setattr(
        handoff, "persist_worker_handoff_control", replay_after_unwind
    )
    original_refresh = handoff._refresh_pending_control_env_locked
    first_refresh = True

    class _SyntheticSignal(KeyboardInterrupt):
        pass

    def interrupt_once_after_publication():
        nonlocal first_refresh
        original_refresh()
        if first_refresh:
            first_refresh = False
            raise _SyntheticSignal()

    monkeypatch.setattr(
        handoff,
        "_refresh_pending_control_env_locked",
        interrupt_once_after_publication,
    )
    control = {
        "control_id": "hc-signal-unwind",
        "source_task_id": task_id,
        "target_task_id": task_id,
        "kind": "redirect",
        "message": "keep this direction",
        "phase": "before_commit",
        "expected_run_id": task.current_run_id,
        "expected_worker_pid": os.getpid(),
    }

    with pytest.raises(_SyntheticSignal):
        handoff.stage_pending_handoff_control(control)

    assert replay_started.wait(timeout=5)
    assert handoff.pending_handoff_control_count() == 1
    assert handoff.try_commit_handoff_control_hard_exit() is False
    allow_replay.set()
    assert handoff.wait_for_pending_handoff_controls(timeout=5)


def test_atomic_hard_exit_claim_rejects_later_control_admission(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-exiting")
    monkeypatch.setattr(handoff, "_HARD_EXIT_COMMITTED", False)

    assert handoff.try_commit_handoff_control_hard_exit() is True
    with pytest.raises(RuntimeError, match="hard exit is already committed"):
        handoff.stage_pending_handoff_control(
            {
                "control_id": "hc-too-late",
                "source_task_id": "task-exiting",
                "target_task_id": "task-exiting",
                "kind": "stop",
                "message": "too late",
                "phase": "after_terminal",
            }
        )
    assert handoff.pending_handoff_control_count() == 0


def test_nonmanaged_agent_never_enters_durable_receipt_registry(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    with pytest.raises(RuntimeError, match="requires a managed worker"):
        handoff.stage_pending_handoff_control(
            {
                "control_id": "hc-ordinary-agent",
                "source_task_id": "task-ordinary",
                "target_task_id": "task-ordinary",
                "kind": "steer",
                "message": "ordinary steer",
                "phase": "after_terminal",
            }
        )
    assert handoff.pending_handoff_control_count() == 0


def test_outage_recovery_replays_primary_followers_fifo_and_exactly_once(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="fifo controls",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
    _set_worker_env(monkeypatch, task)

    real_persist = handoff.persist_worker_handoff_control
    first_attempt_started = threading.Event()
    allow_recovery = threading.Event()
    replay_order: list[str] = []

    def recover_in_order(control, *, attempts=2):
        if not first_attempt_started.is_set():
            first_attempt_started.set()
            assert allow_recovery.wait(timeout=5)
        result = real_persist(control, attempts=attempts)
        if result.get("status") in {"recorded", "already_recorded"}:
            replay_order.append(control["control_id"])
        return result

    monkeypatch.setattr(
        handoff, "persist_worker_handoff_control", recover_in_order
    )
    controls = [
        {
            "control_id": "hc-fifo-redirect",
            "source_task_id": task_id,
            "target_task_id": task_id,
            "kind": "redirect",
            "message": "change direction",
            "phase": "before_commit",
            "expected_run_id": task.current_run_id,
            "expected_worker_pid": os.getpid(),
        },
        {
            "control_id": "hc-fifo-stop",
            "source_task_id": task_id,
            "target_task_id": task_id,
            "kind": "stop",
            "message": "stop wins",
            "phase": "after_terminal",
        },
        {
            "control_id": "hc-fifo-consumed",
            "source_task_id": task_id,
            "target_task_id": task_id,
            "kind": "steer",
            "message": "lower priority",
            "phase": "superseded",
            "superseded_by_control_id": "hc-fifo-stop",
        },
    ]

    handoff.stage_pending_handoff_control(controls[0])
    assert first_attempt_started.wait(timeout=5)
    handoff.stage_pending_handoff_control(controls[1])
    handoff.stage_pending_handoff_control(controls[2])
    assert handoff.pending_handoff_control_count() == 3
    assert handoff.try_commit_handoff_control_hard_exit() is False

    allow_recovery.set()
    assert handoff.wait_for_pending_handoff_controls(timeout=5)
    assert replay_order == [control["control_id"] for control in controls]
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
        rows = conn.execute(
            "SELECT control_id, phase FROM task_handoff_controls "
            "WHERE control_id LIKE 'hc-fifo-%' ORDER BY created_at, rowid"
        ).fetchall()
        assert [(row["control_id"], row["phase"]) for row in rows] == [
            ("hc-fifo-redirect", "before_commit"),
            ("hc-fifo-stop", "after_terminal"),
            ("hc-fifo-consumed", "superseded"),
        ]


def test_chat_steer_cannot_resume_non_handoff_capability_block(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="human capability gate", assignee="default")
        assert kb.block_task(
            conn,
            task_id,
            reason="operator capability is required",
            kind="capability",
        )
        before = {
            "controls": conn.execute(
                "SELECT COUNT(*) FROM task_handoff_controls"
            ).fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM task_comments"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
        }
        result = kb.persist_handoff_control(
            conn,
            control_id="must-not-bypass-capability",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="steer",
            message="please continue anyway",
            phase="before_start",
        )
        assert result["status"] == "conflict"
        assert kb.get_task(conn, task_id).status == "blocked"
        assert {
            "controls": conn.execute(
                "SELECT COUNT(*) FROM task_handoff_controls"
            ).fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM task_comments"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
        } == before


def test_task_archive_refuses_live_worker_and_open_exit_gate(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="archive safety",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
        assert kb.archive_task(conn, root_id) is False
    child_id = _handoff(monkeypatch, root)["successor_task_id"]
    with kb.connect() as conn:
        assert kb.archive_task(conn, child_id) is False
        assert _prove_all_gates_exited(conn, monkeypatch) == 1
        assert kb.archive_task(conn, child_id) is True


def test_exit_probe_identity_matrix(monkeypatch):
    row = {
        "owner_node_id": "node",
        "owner_boot_id": "boot",
        "worker_pid": 123,
        "worker_start_token": "10",
        "worker_pgid": 123,
    }
    monkeypatch.setattr(kb, "_local_node_id", lambda: "node")
    monkeypatch.setattr(kb, "_local_boot_id", lambda: "boot")
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: 10)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kb, "_process_group_alive", lambda _pgid: True)
    assert kb._exit_gate_release_reason(row) is None

    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: 11)
    monkeypatch.setattr(kb, "_process_group_alive", lambda _pgid: False)
    assert kb._exit_gate_release_reason(row) == "pid_reused_and_group_exited"

    monkeypatch.setattr(kb, "_local_node_id", lambda: "foreign")
    assert kb._exit_gate_release_reason(row) is None
    monkeypatch.setattr(kb, "_local_node_id", lambda: "node")
    monkeypatch.setattr(kb, "_local_boot_id", lambda: "new-boot")
    assert kb._exit_gate_release_reason(row) == "owner_rebooted"


def test_owner_reboot_releases_gate_and_clears_sticky_cleanup_veto(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="reboot recovery",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        root = _claim_as_current_process(conn, root_id)
    child_id = _handoff(monkeypatch, root)["successor_task_id"]
    with kb.connect() as conn:
        conn.execute(
            "UPDATE task_runs SET process_cleanup_unsafe = ? WHERE id = ?",
            ("old foreground group was unproven", root.current_run_id),
        )
        monkeypatch.setattr(kb, "_local_node_id", lambda: "test-node")
        monkeypatch.setattr(kb, "_local_boot_id", lambda: "new-boot")
        monkeypatch.setattr(
            kb,
            "_pid_alive",
            lambda _pid: pytest.fail("reboot proof must not inspect a reused PID"),
        )
        monkeypatch.setattr(
            kb,
            "_process_group_alive",
            lambda _pgid: pytest.fail("reboot proof must not inspect an old PGID"),
        )
        assert kb.release_handoff_exit_gates(conn) == 1
        assert kb.get_task(conn, child_id).status == "ready"
        assert conn.execute(
            "SELECT process_cleanup_unsafe FROM task_runs WHERE id = ?",
            (root.current_run_id,),
        ).fetchone()[0] is None


def test_model_control_delayed_replay_is_neutral_after_newer_steer(
    kanban_home, monkeypatch
):
    from tools import kanban_tools as kt

    origin = _control_origin(message_id="create-replay")
    identity = {
        key: origin[key]
        for key in (
            "platform",
            "scope_id",
            "chat_type",
            "chat_id",
            "thread_id",
            "user_id",
            "notifier_profile",
            "session_key",
            "message_id",
        )
    }
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="control replay truth",
            assignee="default",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )

    monkeypatch.setattr(kt, "_require_orchestrator_tool", lambda _name: None)
    monkeypatch.setattr(kt, "_check_kanban_control_mode", lambda: True)
    monkeypatch.setattr(
        kt, "_trusted_gateway_control_identity", lambda: dict(identity)
    )
    first = json.loads(
        kt._handle_control_locked(
            {"task_id": task_id, "kind": "stop", "message": "pause"}
        )
    )
    assert first["ok"] is True

    identity["message_id"] = "newer-steer"
    steered = json.loads(
        kt._handle_control_locked(
            {"task_id": task_id, "kind": "steer", "message": "use path B"}
        )
    )
    assert steered["ok"] is True
    with kb.connect() as conn:
        status_after_steer = kb.get_task(conn, task_id).status
        counts_after_steer = (
            conn.execute("SELECT COUNT(*) FROM task_handoff_controls").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
        )

    identity["message_id"] = "create-replay"
    replay = json.loads(
        kt._handle_control_locked(
            {"task_id": task_id, "kind": "stop", "message": "pause"}
        )
    )
    assert replay["already_processed"] is True
    assert replay["worker_exit_pending"] is False
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == status_after_steer
        assert (
            conn.execute("SELECT COUNT(*) FROM task_handoff_controls").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
        ) == counts_after_steer


@pytest.mark.parametrize(
    (
        "allow_short_handoff",
        "require_exit_safety",
        "review_mode",
        "expected_enabled",
    ),
    [(True, False, False, True), (False, True, True, False)],
    ids=["implementation-worker", "review-worker"],
)
def test_default_spawn_injects_shared_policy_and_isolated_process_group(
    kanban_home,
    tmp_path,
    monkeypatch,
    allow_short_handoff,
    require_exit_safety,
    review_mode,
    expected_enabled,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="spawn snapshot",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = kb.get_task(conn, task_id)

    captured = {}

    class DummyProcess:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return DummyProcess()

    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _p: str(kanban_home))
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(
        kb,
        "_task_short_handoff_worker_policy",
        lambda _conn, _task_id: handoff.build_dispatcher_policy_snapshot(
            _config(soft=4, maximum=1, hard=90), failure_limit=4
        ),
    )
    loaded_policy = {}

    def load_policy(**kwargs):
        loaded_policy.update(kwargs)
        return handoff.build_dispatcher_policy_snapshot(
            _config(soft=20, maximum=8, hard=90),
            failure_limit=kwargs.get("failure_limit"),
        )

    monkeypatch.setattr(
        handoff, "load_current_dispatcher_policy_snapshot", load_policy
    )
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert kb._default_spawn(
        task,
        str(workspace),
        failure_limit=4,
        allow_short_handoff=allow_short_handoff,
        require_exit_safety=require_exit_safety,
        review_mode=review_mode,
    ) == 4242
    try:
        assert loaded_policy["failure_limit"] == 4
        assert captured["start_new_session"] is True
        assert captured["cwd"] == str(workspace)
        snapshot = json.loads(
            captured["env"]["HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY"]
        )
        assert snapshot["enabled"] is expected_enabled
        assert snapshot["max_iterations"] == 90
        assert snapshot["failure_limit"] == 4
        if expected_enabled:
            # Current global settings were broadened after task creation;
            # the worker still receives the exact task-bound limits.
            assert snapshot["soft_iteration_limit"] == 4
            assert snapshot["max_handoffs"] == 1
        if review_mode:
            assert snapshot["inactive_reason"] == (
                "goal/review workers are outside Phase-1 handoff scope"
            )
            assert captured["env"]["HERMES_KANBAN_REVIEW_MODE"] == "1"
            assert captured["env"]["HERMES_KANBAN_MANAGED_LANE"] == "review"
        assert captured["env"][handoff.POLICY_HOME_ENV] == str(kanban_home)
        if expected_enabled:
            assert captured["cmd"][captured["cmd"].index("--max-turns") + 1] == "90"
            barrier_fd = int(
                captured["env"]["HERMES_KANBAN_START_BARRIER_FD"]
            )
            assert captured["pass_fds"] == (barrier_fd,)
            assert kb._board_has_pending_worker_start("default") is True
        else:
            assert "--max-turns" not in captured["cmd"]
        # Both the bound implementation worker and its independent reviewer
        # are process-safe managed lanes, even though review cannot hand off.
        barrier_fd = int(captured["env"]["HERMES_KANBAN_START_BARRIER_FD"])
        assert captured["pass_fds"] == (barrier_fd,)
        assert kb._board_has_pending_worker_start("default") is True
    finally:
        pending = kb._take_pending_worker_start(4242)
        if pending is not None:
            os.close(pending[1])


def test_legacy_review_real_dispatch_stays_on_pre_phase_one_contract(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "legacy-review"
    workspace.mkdir()
    captured = {}

    class DummyProcess:
        pid = 4243

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return DummyProcess()

    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda _profile: True
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda _p: str(kanban_home)
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        lambda **_kwargs: handoff.build_dispatcher_policy_snapshot(_config()),
    )
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy review",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', resume_lane = 'review' "
                "WHERE id = ?",
                (task_id,),
            )
        result = kb.dispatch_once(conn)

    assert [item[0] for item in result.spawned] == [task_id]
    skills_index = captured["cmd"].index("--skills")
    assert captured["cmd"][skills_index + 1] == "sdlc-review"
    assert "--max-turns" not in captured["cmd"]
    assert captured["pass_fds"] == ()
    assert "HERMES_KANBAN_START_BARRIER_FD" not in captured["env"]
    assert "HERMES_KANBAN_REVIEW_MODE" not in captured["env"]
    assert "HERMES_KANBAN_MANAGED_LANE" not in captured["env"]
    snapshot = json.loads(
        captured["env"][handoff.POLICY_SNAPSHOT_ENV]
    )
    assert snapshot["enabled"] is False
    assert snapshot["inactive_reason"] == (
        "task is outside the trusted short-task scope"
    )


def test_default_spawn_rejects_contradictory_review_switches(
    kanban_home, tmp_path
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review", assignee="default")
        task = kb.get_task(conn, task_id)

    with pytest.raises(ValueError, match="review_mode requires"):
        kb._default_spawn(task, str(workspace), review_mode=True)


def test_legacy_control_binding_does_not_gain_handoff_authority(kanban_home):
    legacy_origin = _control_origin(message_id="legacy-control-only")
    legacy_origin.pop("short_handoff_policy")

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy binding",
            assignee="default",
            control_origin=legacy_origin,
        )

        assert conn.execute(
            "SELECT 1 FROM kanban_control_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone() is not None
        assert kb._task_short_handoff_worker_policy(conn, task_id) is None
        assert kb._task_requires_enabled_short_handoff_policy(conn, task_id) is False


def test_allowed_origin_policy_is_frozen_and_moves_to_successor(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "frozen-project"
    workspace.mkdir()
    origin = _control_origin(
        message_id="frozen-create",
        workspace_root=str(workspace.resolve()),
    )
    expected_policy = json.loads(origin["short_handoff_policy"])["worker_policy"]

    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn,
            title="frozen source",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
        )
        parent = _claim_as_current_process(conn, parent_id)
        assert kb._task_short_handoff_worker_policy(conn, parent_id) == expected_policy

    result = _handoff(monkeypatch, parent, maximum=3)
    assert result["status"] == "handed_off"
    child_id = result["successor_task_id"]

    with kb.connect() as conn:
        assert kb._task_short_handoff_worker_policy(conn, parent_id) is None
        assert kb._task_short_handoff_worker_policy(conn, child_id) == expected_policy


@pytest.mark.parametrize(
    ("status", "expected_short_handoff", "expected_review_mode"),
    [("ready", True, False), ("review", False, True)],
)
def test_dispatch_passes_managed_lane_safety_contract(
    kanban_home,
    tmp_path,
    monkeypatch,
    status,
    expected_short_handoff,
    expected_review_mode,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    captured = []

    def spawn(
        task,
        task_workspace,
        *,
        failure_limit,
        allow_short_handoff=True,
        require_exit_safety=False,
        review_mode=False,
    ):
        captured.append(
            {
                "task_id": task.id,
                "workspace": task_workspace,
                "failure_limit": failure_limit,
                "allow_short_handoff": allow_short_handoff,
                "require_exit_safety": require_exit_safety,
                "review_mode": review_mode,
            }
        )

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"{status} dispatch policy",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=_control_origin(
                message_id=f"dispatch-contract-{status}",
                workspace_root=str(workspace.resolve()),
            ),
        )
        if status == "review":
            conn.execute(
                "UPDATE tasks SET status = 'review', resume_lane = 'review' "
                "WHERE id = ?",
                (task_id,),
            )

        result = kb.dispatch_once(
            conn, spawn_fn=spawn, max_spawn=1, failure_limit=4
        )

    assert [entry[0] for entry in result.spawned] == [task_id]
    assert captured == [
        {
            "task_id": task_id,
            "workspace": str(workspace),
            "failure_limit": 4,
            "allow_short_handoff": expected_short_handoff,
            "require_exit_safety": True,
            "review_mode": expected_review_mode,
        }
    ]


def test_phase1_review_spawn_callback_missing_review_mode_fails_closed(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    called = []

    def unsafe_spawn(
        task,
        task_workspace,
        *,
        allow_short_handoff,
        require_exit_safety,
    ):
        called.append(task.id)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="managed review callback must carry every switch",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=_control_origin(
                message_id="missing-review-mode",
                workspace_root=str(workspace.resolve()),
            ),
        )
        conn.execute(
            "UPDATE tasks SET status = 'review', resume_lane = 'review' "
            "WHERE id = ?",
            (task_id,),
        )

        result = kb.dispatch_once(
            conn, spawn_fn=unsafe_spawn, failure_limit=3
        )
        task = kb.get_task(conn, task_id)

    assert called == []
    assert result.spawned == []
    assert task.status == "review"
    assert task.resume_lane == "review"
    assert task.consecutive_failures == 1
    assert "review_mode" in (task.last_failure_error or "")


@pytest.mark.parametrize("status", ["ready", "review"])
@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    [("malformed", "malformed"), ("ambiguous", "multiple")],
)
def test_invalid_managed_policy_never_downgrades_to_legacy_spawn(
    kanban_home,
    tmp_path,
    monkeypatch,
    status,
    corruption,
    expected_error,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    called = []

    def legacy_two_argument_spawn(task, task_workspace):
        called.append((task.id, task_workspace))

    origin = _control_origin(
        message_id=f"invalid-policy-{status}-{corruption}",
        workspace_root=str(workspace.resolve()),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"{status} with {corruption} policy",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
        )
        if status == "review":
            conn.execute(
                "UPDATE tasks SET status = 'review', resume_lane = 'review' "
                "WHERE id = ?",
                (task_id,),
            )
        if corruption == "malformed":
            conn.execute(
                "UPDATE kanban_control_bindings "
                "SET short_handoff_policy = '{bad' WHERE task_id = ?",
                (task_id,),
            )
        else:
            # The public writer prevents duplicate authority. Model an
            # externally damaged/partially restored DB with two distinct
            # identity rows that both assert a non-empty policy.
            conn.execute(
                "INSERT INTO kanban_control_bindings ("
                "binding_id, task_id, platform, scope_id, chat_type, chat_id, "
                "thread_id, user_id, notifier_profile, session_key, "
                "short_handoff_policy, created_at) "
                "SELECT ?, task_id, platform, scope_id, chat_type, chat_id, "
                "'corrupt-duplicate-thread', user_id, notifier_profile, "
                "session_key || ':corrupt-duplicate', "
                "short_handoff_policy, created_at "
                "FROM kanban_control_bindings WHERE task_id = ?",
                (f"duplicate-{status}", task_id),
            )

        result = kb.dispatch_once(
            conn,
            spawn_fn=legacy_two_argument_spawn,
            failure_limit=3,
        )
        task = kb.get_task(conn, task_id)

        assert called == []
        assert result.spawned == []
        assert result.respawn_guarded == [
            (task_id, task.last_failure_error)
        ]
        assert task.status == status
        assert task.resume_lane == (
            "review" if status == "review" else "implementation"
        )
        assert task.consecutive_failures == 1
        assert expected_error in (task.last_failure_error or "")
        assert (
            kb.has_spawnable_review(conn)
            if status == "review"
            else kb.has_spawnable_ready(conn)
        ) is False

        tripped = kb.dispatch_once(
            conn,
            spawn_fn=legacy_two_argument_spawn,
            failure_limit=2,
        )
        assert called == []
        assert tripped.spawned == []
        assert tripped.auto_blocked == [task_id]
        assert kb.get_task(conn, task_id).status == "blocked"


def test_managed_task_creation_rejects_goal_mode(
    kanban_home,
    tmp_path,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    origin = _control_origin(
        message_id="managed-goal-mode-create",
        workspace_root=str(workspace.resolve()),
    )
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="goal mode"):
            kb.create_task(
                conn,
                title="managed goal mode",
                assignee="default",
                workspace_kind="dir",
                workspace_path=str(workspace),
                control_origin=origin,
                goal_mode=True,
            )


@pytest.mark.parametrize("status", ["ready", "review"])
def test_managed_goal_mode_task_never_downgrades_to_legacy_spawn(
    kanban_home,
    tmp_path,
    monkeypatch,
    status,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    called = []

    def legacy_two_argument_spawn(task, task_workspace):
        called.append((task.id, task_workspace))

    origin = _control_origin(
        message_id=f"managed-goal-mode-{status}",
        workspace_root=str(workspace.resolve()),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"{status} managed goal mode",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
        )
        conn.execute("UPDATE tasks SET goal_mode = 1 WHERE id = ?", (task_id,))
        if status == "review":
            conn.execute(
                "UPDATE tasks SET status = 'review', resume_lane = 'review' "
                "WHERE id = ?",
                (task_id,),
            )

        result = kb.dispatch_once(
            conn,
            spawn_fn=legacy_two_argument_spawn,
            failure_limit=3,
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

        assert called == []
        assert result.spawned == []
        assert result.respawn_guarded == [
            (task_id, kb._MANAGED_GOAL_MODE_BLOCK_REASON)
        ]
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert task.last_failure_error is None
        assert any(
            event.kind == "blocked"
            and event.payload["source"] == "short_task_policy_goal_mode_guard"
            for event in events
        )


def test_real_parent_remains_alive_during_blocked_dispatch_then_child_starts(
    kanban_home, tmp_path, monkeypatch
):
    """No-network E2E proving dispatch cannot overlap two checkout writers."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    first_go = tmp_path / "first.go"
    parent_exit = tmp_path / "parent.exit"
    handoff_done = tmp_path / "handoff.done"
    second_go = tmp_path / "second.go"
    source_root = Path(__file__).resolve().parents[2]
    process_records = []
    (kanban_home / "config.yaml").write_text(
        yaml.safe_dump(_config(), sort_keys=False),
        encoding="utf-8",
    )
    # This test crosses a real process boundary. Persist the child's actual
    # node/boot/start-token identity so its in-process commit-boundary proof can
    # match the dispatcher row; the fixture's deterministic fake is only valid
    # for same-process unit tests.
    monkeypatch.setattr(
        kb,
        "_capture_handoff_worker_identity",
        _REAL_CAPTURE_HANDOFF_WORKER_IDENTITY,
    )
    monkeypatch.setattr(
        kb,
        "_capture_process_group_identity",
        _REAL_CAPTURE_PROCESS_GROUP_IDENTITY,
    )
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="real process chain",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )

    first_code = r"""
import os, time
from pathlib import Path
from agent.kanban_auto_handoff import AutoHandoffPolicy, create_successor_and_close
while not Path(os.environ['FIRST_GO']).exists(): time.sleep(0.01)
create_successor_and_close(
    policy=AutoHandoffPolicy(True, 4, 3),
    summary='checkpoint from real process',
    api_call_count=4,
    max_iterations=90,
)
Path(os.environ['HANDOFF_DONE']).touch()
while not Path(os.environ['PARENT_EXIT']).exists(): time.sleep(0.01)
"""
    second_code = r"""
import os, time
from pathlib import Path
from hermes_cli import kanban_db as kb
while not Path(os.environ['SECOND_GO']).exists(): time.sleep(0.01)
with kb.connect() as conn:
    ok = kb.complete_task(
        conn,
        os.environ['HERMES_KANBAN_TASK'],
        result='finished by fresh process',
        summary='final synthetic verification',
        expected_run_id=int(os.environ['HERMES_KANBAN_RUN_ID']),
        expected_worker_pid=os.getpid(),
    )
if not ok: raise SystemExit(3)
"""

    def spawn(task, task_workspace, board=None):
        first = task.id == root_id
        env = dict(os.environ)
        env.update(
            {
                "HERMES_KANBAN_TASK": task.id,
                "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
                handoff.POLICY_HOME_ENV: str(kanban_home),
                "FIRST_GO": str(first_go),
                "PARENT_EXIT": str(parent_exit),
                "HANDOFF_DONE": str(handoff_done),
                "SECOND_GO": str(second_go),
                "PYTHONPATH": str(source_root),
                "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", first_code if first else second_code],
            cwd=task_workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        process_records.append((task.id, proc))
        return proc.pid

    try:
        with kb.connect() as conn:
            first_tick = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=2)
        assert [item[0] for item in first_tick.spawned] == [root_id]
        # This custom no-network spawn bypasses _default_spawn's real pipe
        # barrier. Its separate barrier behavior is covered by the dispatcher
        # exit-gate suite; mark this synthetic run as barrier-qualified so this
        # test remains focused on parent-exit ordering.
        with kb.connect() as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET handoff_safety_required = 1 "
                    "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?) "
                    "AND task_id = ?",
                    (root_id, root_id),
                )
        first_go.touch()
        deadline = time.time() + 20
        while time.time() < deadline and not handoff_done.exists():
            if process_records[0][1].poll() is not None:
                break
            time.sleep(0.02)
        parent_proc = process_records[0][1]
        if not handoff_done.exists():
            stdout, stderr = parent_proc.communicate(timeout=5)
            pytest.fail(
                "parent failed before durable handoff marker: "
                f"rc={parent_proc.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        assert parent_proc.poll() is None

        # This is the proof point missing from the earlier test: dispatch while
        # the handed-off parent process is deliberately still alive.
        with kb.connect() as conn:
            children = kb.child_ids(conn, root_id)
            assert len(children) == 1
            child_id = children[0]
            assert kb.get_task(conn, child_id).status == "todo"
            blocked_tick = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=2)
            assert blocked_tick.spawned == []
            assert kb.get_task(conn, child_id).status == "todo"
        assert len(process_records) == 1
        assert parent_proc.poll() is None

        parent_exit.touch()
        stdout, stderr = parent_proc.communicate(timeout=20)
        assert parent_proc.returncode == 0, stdout + stderr
        with kb.connect() as conn:
            second_tick = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=2)
            assert [item[0] for item in second_tick.spawned] == [child_id]
            assert kb.get_task(conn, child_id).status == "running"

        second_go.touch()
        child_proc = process_records[1][1]
        stdout, stderr = child_proc.communicate(timeout=20)
        assert child_proc.returncode == 0, stdout + stderr
        assert child_proc.pid != parent_proc.pid
        with kb.connect() as conn:
            assert kb.get_task(conn, child_id).status == "done"
            assert kb.get_task(conn, child_id).workspace_path == str(workspace)
    finally:
        for _task_id, proc in process_records:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


@pytest.mark.parametrize(
    "backend", ["docker", "ssh", "modal", "daytona", "singularity"]
)
def test_policy_refuses_nonlocal_terminal_backend(monkeypatch, backend):
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    config = _config()
    config["terminal"] = {"backend": backend}

    snapshot = handoff.build_dispatcher_policy_snapshot(config)

    assert snapshot["enabled"] is False
    assert "terminal.backend=local" in snapshot["validation_error"]


def test_explicit_terminal_env_is_the_authoritative_backend(monkeypatch):
    config = _config()
    config["terminal"] = {"backend": "local"}
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    assert handoff.build_dispatcher_policy_snapshot(config)["enabled"] is False

    monkeypatch.setenv("TERMINAL_ENV", "local")
    config["terminal"] = {"backend": "docker"}
    assert handoff.build_dispatcher_policy_snapshot(config)["enabled"] is True


def test_policy_refuses_windows_phase_one(monkeypatch):
    class _WindowsOsProxy:
        name = "nt"

        def __getattr__(self, attribute):
            return getattr(os, attribute)

    monkeypatch.setattr(handoff, "os", _WindowsOsProxy())
    snapshot = handoff.build_dispatcher_policy_snapshot(_config())
    assert snapshot["enabled"] is False
    assert "POSIX" in snapshot["validation_error"]


def test_short_task_detach_guard_blocks_even_quoted_git_text(
    kanban_home, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    assert "cannot safely continue" in _short_task_detach_guard(
        'git commit -m "docs: explain A & B without nohup"',
        background=False,
    )


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/setsid sleep 30",
        '"setsid" sleep 30',
        r"set\sid sleep 30",
        'no"hup" sleep 30',
        "'daemonize' sleep 30",
        'env -S "setsid sleep 30"',
        "python - <<'PY'\nimport os\nos.setsid()\nPY",
        'python -c "import subprocess; subprocess.Popen([\'sleep\', \'30\'])"',
    ],
)
def test_short_task_detach_guard_blocks_explicit_session_escape(
    kanban_home, monkeypatch, command
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    assert "cannot safely continue" in _short_task_detach_guard(
        command, background=False
    )


def test_short_task_detach_guard_invalid_snapshot_fails_closed(
    kanban_home, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(handoff.POLICY_SNAPSHOT_ENV, "invalid")

    assert "cannot safely continue" in _short_task_detach_guard(
        r"set\sid sleep 30", background=False
    )
    result = json.loads(
        terminal_tool(command=r"set\sid sleep 30", background=False, timeout=1)
    )
    assert result["status"] == "blocked"


def test_bound_managed_task_cannot_complete_without_review_lane(
    kanban_home, tmp_path
):
    workspace = tmp_path / "managed-workspace"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="managed direct complete must fail",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace.resolve()),
            validation_class="text_mechanism",
            control_origin=_control_origin(workspace_root=str(workspace)),
        )

        assert not kb.complete_task(
            conn,
            task_id,
            summary="must not bypass independent review",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_rejected'",
            (task_id,),
        ).fetchall()
        assert rows
        assert any(
            json.loads(row["payload"]).get("reason")
            == "managed_review_required"
            for row in rows
        )


def test_legacy_review_marker_without_managed_lane_keeps_terminal_behavior(
    kanban_home, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_legacy_review")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_LANE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", raising=False)
    monkeypatch.delenv(
        "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", raising=False
    )
    monkeypatch.delenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
    )

    assert _short_task_detach_guard("pwd", background=False) is None


@pytest.mark.parametrize(
    "source",
    [
        "print('bounded')\n",
        "import os\nos.setsid()\n",
    ],
)
def test_short_task_detach_guard_rejects_direct_project_scripts(
    kanban_home, tmp_path, monkeypatch, source
):
    script = tmp_path / "escape.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    assert "cannot safely continue" in _short_task_detach_guard(
        f"python {script.name}", background=False, cwd=str(tmp_path)
    )


def test_short_task_detach_guard_rejects_unknown_executable(
    kanban_home, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )

    assert "cannot safely continue" in _short_task_detach_guard(
        "custom-project-runner --check", background=False
    )
    assert "cannot safely continue" in _short_task_detach_guard(
        "./echo ok", background=False
    )


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "git status",
        "python -m pytest -q",
        "python -m compileall .",
        "python -m mypy .",
        "python -m unittest discover",
        "python -m venv .venv",
    ],
)
def test_short_task_detach_guard_blocks_all_terminal_commands(
    kanban_home, monkeypatch, command
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    assert "cannot safely continue" in _short_task_detach_guard(
        command, background=False
    )


@pytest.mark.parametrize(
    "command",
    [
        'python -c "print(1)"',
        'node -e "console.log(1)"',
        'python -c "__import__(\'os\').setsid()"',
        'python -c "getattr(__import__(\'os\'), \'setsid\')()"',
        'python -c "exec(open(\'daemonize.py\').read())"',
        "python -X presite=escape -m pytest",
        "python -m pip install .",
        "python -m coverage run safe.py",
        "node -r ./escape.js -m pytest",
        'sh -c "python daemonize.py"',
        "python - <<'PY'\nprint(1)\nPY",
    ],
)
def test_short_task_detach_guard_rejects_dynamic_eval_and_stdin_forms(
    kanban_home, monkeypatch, command
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    assert "cannot safely continue" in _short_task_detach_guard(
        command, background=False
    )


@pytest.mark.parametrize(
    "command",
    [
        "env python daemonize.py",
        "/usr/bin/env python daemonize.py",
        "command python daemonize.py",
        'sh -c "python daemonize.py"',
        "bash daemon.sh",
        "./daemon.sh",
    ],
)
def test_terminal_surface_blocks_wrapped_or_direct_detaching_scripts(
    kanban_home, tmp_path, monkeypatch, command
):
    (tmp_path / "daemonize.py").write_text(
        "import os, time\nos.setsid()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    (tmp_path / "daemon.sh").write_text(
        "#!/bin/sh\nsetsid sleep 30\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    result = json.loads(
        terminal_tool(
            command=command,
            background=False,
            timeout=5,
            workdir=str(tmp_path),
        )
    )
    assert result["status"] == "blocked"
    assert "cannot safely continue" in result["error"]


@pytest.mark.parametrize(
    "command",
    ["python -m daemonmodule", "python -m daemonpkg", "env python -m daemonmodule"],
)
def test_terminal_surface_blocks_cwd_local_detaching_python_modules(
    kanban_home, tmp_path, monkeypatch, command
):
    (tmp_path / "daemonmodule.py").write_text(
        "import os\nos.setsid()\n",
        encoding="utf-8",
    )
    package = tmp_path / "daemonpkg"
    package.mkdir()
    (package / "__main__.py").write_text(
        "import subprocess\nsubprocess.Popen(['sleep', '30'], start_new_session=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    result = json.loads(
        terminal_tool(
            command=command,
            background=False,
            timeout=5,
            workdir=str(tmp_path),
        )
    )
    assert result["status"] == "blocked"
    assert "cannot safely continue" in result["error"]


@pytest.mark.parametrize(
    "command",
    [
        "echo ok; python daemonize.py",
        "true && python daemonize.py",
        "false || python daemonize.py",
        "echo ok | python daemonize.py",
        "(python daemonize.py)",
        "FOO=1 python daemonize.py",
        'echo "$(python daemonize.py)"',
    ],
)
def test_terminal_surface_blocks_compound_or_prefixed_detaching_commands(
    kanban_home, tmp_path, monkeypatch, command
):
    (tmp_path / "daemonize.py").write_text(
        "import os, time\nos.setsid()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    result = json.loads(
        terminal_tool(
            command=command,
            background=False,
            timeout=5,
            workdir=str(tmp_path),
        )
    )
    assert result["status"] == "blocked"
    assert "cannot safely continue" in result["error"]


@pytest.mark.parametrize(
    "command",
    [
        'eval "python daemonize.py"',
        "exec python daemonize.py",
        "source daemon.sh",
        ". daemon.sh",
        "time python daemonize.py",
        "timeout 5 python daemonize.py",
        "nice python daemonize.py",
    ],
)
def test_terminal_surface_blocks_uninspectable_shell_and_native_wrappers(
    kanban_home, tmp_path, monkeypatch, command
):
    (tmp_path / "daemonize.py").write_text(
        "import os\nos.setsid()\n",
        encoding="utf-8",
    )
    (tmp_path / "daemon.sh").write_text(
        "#!/bin/sh\nsetsid sleep 30\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_guard")
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    result = json.loads(
        terminal_tool(
            command=command,
            background=False,
            timeout=5,
            workdir=str(tmp_path),
        )
    )
    assert result["status"] == "blocked"
    assert "cannot safely continue" in result["error"]


def test_trusted_creation_is_replay_safe_and_semantic_drift_conflicts(kanban_home):
    origin = _control_origin()
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="one implementation chain",
            assignee="default",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )
        replay = kb.create_task(
            conn,
            title="one implementation chain",
            assignee="default",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )
        with pytest.raises(ValueError, match="different task semantics"):
            kb.create_task(
                conn,
                title="changed intent",
                assignee="default",
                workspace_kind="dir",
                workspace_path="/tmp",
                control_origin=origin,
            )

        assert replay == first
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_control_bindings"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_control_creations"
        ).fetchone()[0] == 1


def test_trusted_creation_replay_after_archive_or_delete_is_a_tombstone(
    kanban_home,
):
    origin = _control_origin(message_id="create-tombstone")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="one durable request",
            assignee="default",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )
        assert kb.archive_task(conn, task_id)
        with pytest.raises(ValueError, match="task is archived"):
            kb.create_task(
                conn,
                title="one durable request",
                assignee="default",
                workspace_kind="dir",
                workspace_path="/tmp",
                control_origin=origin,
            )
        assert kb.delete_archived_task(conn, task_id)
        with pytest.raises(ValueError, match="task was later deleted"):
            kb.create_task(
                conn,
                title="one durable request",
                assignee="default",
                workspace_kind="dir",
                workspace_path="/tmp",
                control_origin=origin,
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_control_creations"
        ).fetchone()[0] == 1


def test_trusted_creation_replay_after_whole_board_archive_never_recreates(
    kanban_home,
):
    origin = _control_origin(message_id="create-archived-board")
    kb.create_board("alpha")
    kb.create_board("beta")
    with kb.connect(board="alpha") as conn:
        kb.create_task(
            conn,
            title="archived board request",
            assignee="default",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )
    archived = kb.remove_board("alpha", archive=True)
    assert archived["action"] == "archived"

    with kb.connect(board="beta") as conn:
        with pytest.raises(ValueError, match="another board"):
            kb.create_task(
                conn,
                title="archived board request",
                assignee="default",
                workspace_kind="dir",
                workspace_path="/tmp",
                control_origin=origin,
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_controlled_board_cannot_be_hard_deleted_and_lose_replay_history(
    kanban_home,
):
    origin = _control_origin(message_id="create-hard-delete")
    kb.create_board("alpha")
    with kb.connect(board="alpha") as conn:
        kb.create_task(
            conn,
            title="must retain replay journal",
            assignee="default",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )

    with pytest.raises(ValueError, match="cannot be hard-deleted"):
        kb.remove_board("alpha", archive=False)
    assert kb.board_dir("alpha").exists()
    assert kb.remove_board("alpha", archive=True)["action"] == "archived"


@pytest.mark.skipif(os.name == "nt", reason="Phase 1 is POSIX-only")
def test_trusted_creation_is_exactly_once_across_processes_and_boards(
    kanban_home, tmp_path
):
    kb.create_board("alpha")
    kb.create_board("beta")
    start = tmp_path / "start"
    source_root = Path(__file__).resolve().parents[2]
    code = r"""
import json, os, time
from pathlib import Path
from hermes_cli import kanban_db as kb
while not Path(os.environ['START_FILE']).exists(): time.sleep(0.005)
try:
    with kb.connect(board=os.environ['BOARD']) as conn:
        task_id = kb.create_task(
            conn,
            title='cross-process exact create',
            assignee='default',
            workspace_kind='dir',
            workspace_path='/tmp',
            control_origin=json.loads(os.environ['CONTROL_ORIGIN']),
        )
    print('ok:' + task_id, flush=True)
except Exception as exc:
    print('error:' + str(exc), flush=True)
"""
    processes = []
    for board in ("alpha", "alpha", "beta", "beta"):
        env = dict(os.environ)
        env.update(
            {
                "HERMES_HOME": str(kanban_home),
                "BOARD": board,
                "START_FILE": str(start),
                "CONTROL_ORIGIN": json.dumps(_control_origin()),
                "PYTHONPATH": str(source_root),
                "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            }
        )
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", code],
                cwd=source_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    start.touch()
    outputs = []
    try:
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=20)
            assert proc.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)

    successful_ids = {
        output.split("ok:", 1)[1]
        for output in outputs
        if output.startswith("ok:")
    }
    assert len(successful_ids) == 1
    totals = {"tasks": 0, "bindings": 0, "receipts": 0}
    for board in ("alpha", "beta"):
        with kb.connect(board=board) as conn:
            totals["tasks"] += conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            totals["bindings"] += conn.execute(
                "SELECT COUNT(*) FROM kanban_control_bindings"
            ).fetchone()[0]
            totals["receipts"] += conn.execute(
                "SELECT COUNT(*) FROM kanban_control_creations"
            ).fetchone()[0]
    assert totals == {"tasks": 1, "bindings": 1, "receipts": 1}


@pytest.mark.parametrize("waiting_status", ["triage", "scheduled", "review"])
def test_trusted_stop_controls_every_discoverable_waiting_status(
    kanban_home, waiting_status
):
    origin = _control_origin(message_id=f"create-{waiting_status}")
    identity = {key: origin[key] for key in (
        "platform", "scope_id", "chat_type", "chat_id", "thread_id",
        "user_id", "notifier_profile", "session_key",
    )}
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"waiting {waiting_status}",
            assignee="default",
            triage=waiting_status == "triage",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )
        if waiting_status != "triage":
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (waiting_status, task_id),
            )
        match = kb.control_bound_active_tasks(conn, **identity)
        assert [item["task_id"] for item in match] == [task_id]
        binding_id = match[0]["binding_id"]
        result = kb.route_task_control(
            conn,
            task_id=task_id,
            control_id=f"control-{waiting_status}",
            kind="stop",
            message="stop this waiting task",
            binding_id=binding_id,
            require_binding=True,
            **identity,
        )
        assert result["status"] == "recorded"
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_handoff_controls WHERE control_id = ?",
            (f"control-{waiting_status}",),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ? ",
            (task_id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize("terminal_status", ["triage", "scheduled", "done"])
def test_trusted_stop_can_drain_every_managed_terminal_status(
    kanban_home, tmp_path, monkeypatch, terminal_status
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    if terminal_status == "done":
        (workspace / "completion-evidence.md").write_text(
            "managed completion is ready for review\n", encoding="utf-8"
        )
    origin = _control_origin(
        message_id=f"drain-{terminal_status}",
        workspace_root=str(workspace.resolve()),
    )
    identity = {
        key: origin[key]
        for key in (
            "platform", "scope_id", "chat_type", "chat_id", "thread_id",
            "user_id", "notifier_profile", "session_key",
        )
    }
    signals = []
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda value, **_kwargs: signals.append(value) or None,
    )
    with kb.connect() as conn:
        create_options = (
            {"validation_class": "text_mechanism"}
            if terminal_status == "done"
            else {}
        )
        task_id = kb.create_task(
            conn,
            title=f"drain {terminal_status}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
            **create_options,
        )
        if terminal_status == "triage":
            conn.execute(
                "UPDATE tasks SET block_kind = 'needs_input', "
                "block_recurrences = ? WHERE id = ?",
                (kb.BLOCK_RECURRENCE_LIMIT - 1, task_id),
            )
        task = _claim_as_current_process(conn, task_id)
        if terminal_status == "done":
            frozen_policy = kb._task_short_handoff_worker_policy(
                conn, task_id
            )
            assert frozen_policy is not None
            monkeypatch.setenv(
                "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
                json.dumps(frozen_policy, sort_keys=True, separators=(",", ":")),
            )
            assert kb.submit_task_for_review(
                conn,
                task_id,
                summary="implementation ready for review",
                expected_run_id=task.current_run_id,
                expected_worker_pid=os.getpid(),
            )
            _prove_all_gates_exited(conn, monkeypatch)
            assert conn.execute(
                "SELECT 1 FROM task_exit_gates "
                "WHERE child_task_id = ? AND released_at IS NULL",
                (task_id,),
            ).fetchone() is None
            task = kb.claim_review_task(
                conn, task_id, claimer="test-reviewer"
            )
            assert task is not None
            assert kb._set_worker_pid(conn, task_id, os.getpid())
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET handoff_safety_required = 1 "
                    "WHERE id = ?",
                    (int(task.current_run_id),),
                )
            task = kb.get_task(conn, task_id)
            review_snapshot = dict(frozen_policy)
            review_snapshot["enabled"] = False
            review_snapshot["inactive_reason"] = (
                kb._SHORT_TASK_REVIEW_INACTIVE_REASON
            )
            monkeypatch.setenv(
                "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
                json.dumps(
                    review_snapshot, sort_keys=True, separators=(",", ":")
                ),
            )
            _set_worker_env(monkeypatch, task)
            monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace.resolve()))
            monkeypatch.setenv("HERMES_KANBAN_MANAGED_LANE", "review")
            monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1")
            monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
            monkeypatch.setenv(
                "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1"
            )
            monkeypatch.delenv(
                "HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False
            )
            from tools import managed_file_tools

            read_result = managed_file_tools.read_file_tool(
                "completion-evidence.md"
            )
            assert "managed completion is ready for review" in read_result
            assert kb.complete_task(
                conn,
                task_id,
                summary="completed but process is draining",
                expected_run_id=task.current_run_id,
                expected_worker_pid=os.getpid(),
            )
        elif terminal_status == "scheduled":
            assert kb.schedule_task(
                conn,
                task_id,
                reason="scheduled but process is draining",
                expected_run_id=task.current_run_id,
                expected_worker_pid=os.getpid(),
            )
        else:
            assert kb.block_task(
                conn,
                task_id,
                reason="triage but process is draining",
                kind="needs_input",
                expected_run_id=task.current_run_id,
                expected_worker_pid=os.getpid(),
            )

        matches = kb.control_bound_active_tasks(conn, **identity)
        assert [item["task_id"] for item in matches] == [task_id]
        result = kb.route_task_control(
            conn,
            task_id=task_id,
            control_id=f"stop-{terminal_status}",
            kind="stop",
            message="Stop now",
            binding_id=matches[0]["binding_id"],
            require_binding=True,
            **identity,
        )

        assert result["status"] == "recorded"
        assert result["worker_exit_pending"] is True
        assert len(signals) == 1
        after = kb.get_task(conn, task_id)
        assert after.status == (
            "done" if terminal_status == "done" else "blocked"
        )
        assert after.worker_pid == os.getpid()


@pytest.mark.parametrize("terminal_status", ["triage", "scheduled"])
@pytest.mark.parametrize("release_before_steer", [False, True])
def test_trusted_steer_requeues_managed_terminal_status_but_keeps_gate(
    kanban_home, tmp_path, monkeypatch, terminal_status, release_before_steer
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    origin = _control_origin(
        message_id=f"steer-{terminal_status}",
        workspace_root=str(workspace.resolve()),
    )
    identity = {
        key: origin[key]
        for key in (
            "platform", "scope_id", "chat_type", "chat_id", "thread_id",
            "user_id", "notifier_profile", "session_key",
        )
    }
    signals = []
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda value, **_kwargs: signals.append(value) or None,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"steer {terminal_status}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
        )
        if terminal_status == "triage":
            conn.execute(
                "UPDATE tasks SET block_kind = 'needs_input', "
                "block_recurrences = ? WHERE id = ?",
                (kb.BLOCK_RECURRENCE_LIMIT - 1, task_id),
            )
        task = _claim_as_current_process(conn, task_id)
        if terminal_status == "scheduled":
            assert kb.schedule_task(
                conn,
                task_id,
                reason="waiting",
                expected_run_id=task.current_run_id,
                expected_worker_pid=os.getpid(),
            )
        else:
            assert kb.block_task(
                conn,
                task_id,
                reason="needs revised direction",
                kind="needs_input",
                expected_run_id=task.current_run_id,
                expected_worker_pid=os.getpid(),
            )
        if release_before_steer:
            _prove_all_gates_exited(conn, monkeypatch)
        matches = kb.control_bound_active_tasks(conn, **identity)
        result = kb.route_task_control(
            conn,
            task_id=task_id,
            control_id=f"steer-control-{terminal_status}",
            kind="steer",
            message="Use the corrected direction",
            binding_id=matches[0]["binding_id"],
            require_binding=True,
            **identity,
        )

        assert result["status"] == "recorded"
        assert kb.get_task(conn, task_id).status == "todo"
        assert kb.get_task(conn, task_id).worker_pid == (
            None if release_before_steer else os.getpid()
        )
        assert len(signals) == (0 if release_before_steer else 1)
        if not release_before_steer:
            assert kb.recompute_ready(conn) == 0


@pytest.mark.parametrize("release_before_steer", [False, True])
def test_chat_steer_recovers_managed_iteration_breaker_and_resets_budget(
    kanban_home, tmp_path, monkeypatch, release_before_steer
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    origin = _control_origin(
        message_id=f"iteration-breaker-{release_before_steer}",
        workspace_root=str(workspace.resolve()),
    )
    identity = {
        key: origin[key]
        for key in (
            "platform", "scope_id", "chat_type", "chat_id", "thread_id",
            "user_id", "notifier_profile", "session_key",
        )
    }
    signals = []
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda value, **_kwargs: signals.append(value) or None,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="recover bounded timeout",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb._record_task_failure(
            conn,
            task_id,
            error="iteration budget exhausted",
            outcome="timed_out",
            summary="checkpoint before user correction",
            failure_limit=1,
            release_claim=True,
            end_run=True,
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
            expected_claim_lock=task.claim_lock,
        ) is True
        assert kb.get_task(conn, task_id).status == "blocked"
        if release_before_steer:
            assert _prove_all_gates_exited(conn, monkeypatch) == 0

        matches = kb.control_bound_active_tasks(conn, **identity)
        result = kb.route_task_control(
            conn,
            task_id=task_id,
            control_id=f"recover-timeout-{release_before_steer}",
            kind="steer",
            message="continue with this correction",
            binding_id=matches[0]["binding_id"],
            require_binding=True,
            **identity,
        )

        recovered = kb.get_task(conn, task_id)
        assert result["status"] == "recorded"
        assert result["phase"] == (
            "before_start" if release_before_steer else "after_terminal"
        )
        assert len(signals) == (0 if release_before_steer else 1)
        assert recovered.status == "todo"
        assert recovered.consecutive_failures == 0
        assert recovered.last_failure_error is None
        assert recovered.worker_pid == (
            None if release_before_steer else os.getpid()
        )
        assert "continue with this correction" in kb.build_worker_context(
            conn, task_id
        )


@pytest.mark.parametrize("release_before_steer", [False, True])
def test_chat_steer_recovers_safety_limit_even_after_gate_release(
    kanban_home, tmp_path, monkeypatch, release_before_steer
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    origin = _control_origin(
        message_id=f"safety-limit-{release_before_steer}",
        workspace_root=str(workspace.resolve()),
    )
    identity = {
        key: origin[key]
        for key in (
            "platform", "scope_id", "chat_type", "chat_id", "thread_id",
            "user_id", "notifier_profile", "session_key",
        )
    }
    signals = []
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda value, **_kwargs: signals.append(value) or None,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="recover handoff cap",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            control_origin=origin,
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb.pause_task_at_handoff_limit(
            conn,
            task_id,
            reason="automatic handoff limit reached",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 2, "
            "last_failure_error = 'old budget failure' WHERE id = ?",
            (task_id,),
        )
        if release_before_steer:
            assert _prove_all_gates_exited(conn, monkeypatch) == 0

        matches = kb.control_bound_active_tasks(conn, **identity)
        result = kb.route_task_control(
            conn,
            task_id=task_id,
            control_id=f"recover-limit-{release_before_steer}",
            kind="steer",
            message="use the revised scope",
            binding_id=matches[0]["binding_id"],
            require_binding=True,
            **identity,
        )

        assert result["status"] == "recorded"
        assert result["phase"] == (
            "before_start" if release_before_steer else "after_terminal"
        )
        assert len(signals) == (0 if release_before_steer else 1)
        recovered = kb.get_task(conn, task_id)
        assert recovered.status == "todo"
        assert recovered.consecutive_failures == 0
        assert recovered.last_failure_error is None


def test_old_recoverable_evidence_cannot_bypass_new_cleanup_capability_block(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="new capability beats old handoff evidence",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)
        assert kb.pause_task_at_handoff_limit(
            conn,
            task_id,
            reason="old handoff limit",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert _prove_all_gates_exited(conn, monkeypatch) == 0
        assert kb.unblock_task(conn, task_id)
        conn.execute(
            "UPDATE task_runs SET process_cleanup_unsafe = 'leaked child' "
            "WHERE id = ?",
            (int(task.current_run_id),),
        )
        assert kb.claim_task(conn, task_id, claimer="must-be-vetoed") is None
        blocked = kb.get_task(conn, task_id)
        assert blocked.status == "blocked"
        assert blocked.block_kind == "capability"
        before = {
            "controls": conn.execute(
                "SELECT COUNT(*) FROM task_handoff_controls"
            ).fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM task_comments"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
        }

        result = kb.persist_handoff_control(
            conn,
            control_id="must-not-reuse-old-limit",
            source_task_id=task_id,
            target_task_id=task_id,
            kind="steer",
            message="continue anyway",
            phase="before_start",
        )

        assert result["status"] == "conflict"
        assert kb.get_task(conn, task_id).status == "blocked"
        assert {
            "controls": conn.execute(
                "SELECT COUNT(*) FROM task_handoff_controls"
            ).fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM task_comments"
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM task_events"
            ).fetchone()[0],
        } == before


def test_cleanup_uncertainty_is_durable_and_vetoes_handoff_and_retry(
    kanban_home, tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="unsafe subprocess cleanup",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = _claim_as_current_process(conn, task_id)

    _set_worker_env(monkeypatch, task)
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(_config()),
    )
    env = LocalEnvironment(cwd=str(workspace), timeout=10)
    monkeypatch.setattr(
        env,
        "_cleanup_process_group_with_proof",
        lambda _proc: "foreground process group survived cleanup",
    )
    try:
        result = env.execute("true", timeout=5)
    finally:
        env.cleanup()
    assert result["returncode"] == 125
    assert os.environ.get("HERMES_KANBAN_PROCESS_CLEANUP_UNSAFE")

    with kb.connect() as conn:
        run = kb.latest_run(conn, task_id)
        assert "survived cleanup" in run.process_cleanup_unsafe
        refused = kb.handoff_task(
            conn,
            task_id,
            title="must not start",
            idempotency_key=f"kanban-auto-handoff:{task_id}",
            summary="unsafe",
            metadata={"auto_handoff": {"generation": 1}},
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert refused["status"] == "conflict"
        assert kb.complete_task(
            conn,
            task_id,
            summary="must not complete",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        ) is False
        assert kb.block_task(
            conn,
            task_id,
            reason="operator must inspect subprocesses",
            kind="capability",
            expected_run_id=task.current_run_id,
            expected_worker_pid=os.getpid(),
        )
        assert kb.unblock_task(conn, task_id) is False
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        assert kb.claim_task(conn, task_id, claimer="fresh") is None
        assert kb.get_task(conn, task_id).status == "blocked"
