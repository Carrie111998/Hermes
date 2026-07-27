"""Behavior contract for the Phase-1 feature-off emergency brake."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from agent.kanban_handoff_scope import decide_gateway_origin
from hermes_cli import kanban_db as kb


_NODE = "policy-brake-node"
_BOOT = "policy-brake-boot"
_PID = 434_343
_START = "12345"


@pytest.fixture
def policy_brake_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(board="default")
    return home


def _enabled_config() -> dict:
    return {
        "agent": {"max_turns": 90},
        "kanban": {
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 1,
                "allowed_workspace_roots": ["/tmp"],
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


def _bind(conn, task_id: str, *, suffix: str) -> None:
    identity = {
        "platform": "feishu",
        "scope_id": "tenant-1",
        "chat_type": "group",
        "chat_id": "group-1",
        "thread_id": "",
        "user_id": "user-1",
        "notifier_profile": "default",
        "session_key": "agent:default:feishu:group:group-1:user-1",
    }
    decision = decide_gateway_origin(_enabled_config(), identity)
    assert decision["authorized"] is True
    assert kb.add_control_binding(
        conn,
        binding_id=f"brake-{suffix}",
        task_id=task_id,
        short_handoff_policy=decision["task_policy"],
        **identity,
    ) is True


def _bind_invalid(conn, task_id: str) -> None:
    conn.execute(
        "INSERT INTO kanban_control_bindings ("
        "binding_id, task_id, platform, scope_id, chat_type, chat_id, "
        "thread_id, user_id, notifier_profile, session_key, "
        "short_handoff_policy, created_at"
        ") VALUES (?, ?, 'feishu', 'tenant-1', 'group', 'group-1', '', "
        "'user-1', 'default', 'session-1', ?, ?)",
        ("brake-invalid", task_id, "{not-json", int(time.time())),
    )


def _install_running(
    conn,
    tmp_path,
    *,
    managed: bool,
    complete_identity: bool = True,
    suffix: str,
) -> dict:
    task_id = kb.create_task(
        conn,
        title=f"running {suffix}",
        assignee="default",
        workspace_kind="dir",
        workspace_path=str(tmp_path),
    )
    claimed = kb.claim_task(conn, task_id, claimer=f"worker-{suffix}")
    assert claimed is not None
    run_id = int(claimed.current_run_id)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (_PID, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, owner_node_id = ?, "
            "owner_boot_id = ?, worker_start_token = ?, worker_pgid = ?, "
            "handoff_safety_required = 1 WHERE id = ?",
            (
                _PID,
                _NODE,
                _BOOT,
                _START if complete_identity else None,
                _PID,
                run_id,
            ),
        )
    if managed:
        _bind(conn, task_id, suffix=suffix)
    return {
        "task_id": task_id,
        "run_id": run_id,
        "claim_lock": str(claimed.claim_lock),
    }


def _events(conn, task_id: str, kind: str) -> list:
    return conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id",
        (task_id, kind),
    ).fetchall()


@pytest.mark.parametrize(
    "status", ["triage", "todo", "scheduled", "ready", "review"]
)
def test_disabled_or_invalid_policy_sticky_blocks_every_waiting_lane(
    policy_brake_home, tmp_path, monkeypatch, status
):
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda _profile: True
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"managed {status}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        if status != "ready":
            conn.execute(
                "UPDATE tasks SET status = ?, resume_lane = ? WHERE id = ?",
                (
                    status,
                    "review" if status == "review" else "implementation",
                    task_id,
                ),
            )
        if status == "review":
            _bind_invalid(conn, task_id)
        else:
            _bind(conn, task_id, suffix=status)

        first = kb.dispatch_once(
            conn,
            max_spawn=10,
            spawn_fn=lambda *_args, **_kwargs: None,
        )
        task = kb.get_task(conn, task_id)
        assert first.short_handoff_policy_disabled is True
        assert first.policy_blocked == [task_id]
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert len(_events(conn, task_id, "blocked")) == 1

        # Repeated disabled ticks are idempotent: no second event or mutation.
        second = kb.dispatch_once(
            conn,
            max_spawn=10,
            spawn_fn=lambda *_args, **_kwargs: None,
        )
        assert second.policy_blocked == []
        assert len(_events(conn, task_id, "blocked")) == 1

        # Re-enabling the feature is not authority to resume a quarantined row.
        monkeypatch.setattr(
            kb, "_short_task_handoff_dispatch_enabled", lambda: True
        )
        resumed = kb.dispatch_once(
            conn,
            max_spawn=10,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail(
                "sticky task must not auto-resume"
            ),
        )
        assert resumed.spawned == []
        assert kb.get_task(conn, task_id).status == "blocked"


def test_policy_brake_parks_then_signals_exact_running_managed_worker(
    policy_brake_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    signals = []
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda identity: signals.append(dict(identity)) or None,
    )
    with kb.connect() as conn:
        managed = _install_running(
            conn,
            tmp_path,
            managed=True,
            suffix="managed",
        )
        ordinary = _install_running(
            conn,
            tmp_path,
            managed=False,
            suffix="ordinary",
        )

        first = kb.dispatch_once(conn, max_spawn=10)
        task = kb.get_task(conn, managed["task_id"])
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (managed["run_id"],)
        ).fetchone()
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE parent_run_id = ?",
            (managed["run_id"],),
        ).fetchone()

        assert first.policy_blocked == [managed["task_id"]]
        assert first.policy_draining == [managed["task_id"]]
        assert first.policy_unverified == []
        assert first.policy_signal_failed == []
        assert len(signals) == 1
        assert signals[0]["worker_pid"] == _PID
        assert signals[0]["worker_pgid"] == _PID
        assert task.status == "blocked"
        assert task.current_run_id is None
        assert task.worker_pid == _PID
        assert task.claim_lock == managed["claim_lock"]
        assert run["status"] == "policy_disabled"
        assert run["ended_at"] is not None
        assert run["worker_pid"] == _PID
        assert run["claim_lock"] == managed["claim_lock"]
        assert gate["gate_kind"] == "control_drain"
        assert gate["released_at"] is None
        # The same tick cannot release the gate it just installed.
        assert first.promoted == 0

        # No durable binding means the ordinary running task is untouched.
        ordinary_task = kb.get_task(conn, ordinary["task_id"])
        assert ordinary_task.status == "running"
        assert ordinary_task.current_run_id == ordinary["run_id"]

        # On a later tick, full-PG exit proof releases ownership but the block
        # remains sticky. No second signal is sent.
        monkeypatch.setattr(
            kb,
            "_exit_gate_release_reason",
            lambda _row: "process_group_exited",
        )
        second = kb.dispatch_once(conn, max_spawn=10)
        drained = kb.get_task(conn, managed["task_id"])
        drained_run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (managed["run_id"],)
        ).fetchone()
        assert second.policy_draining == []
        assert len(signals) == 1
        assert drained.status == "blocked"
        assert drained.worker_pid is None
        assert drained.claim_lock is None
        assert drained_run["worker_pid"] is None
        assert drained_run["claim_lock"] is None


def test_signal_failure_stays_gated_and_is_not_retried_each_tick(
    policy_brake_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    signals = []

    def fail_signal(identity):
        signals.append(dict(identity))
        return "cooperative termination did not drain"

    monkeypatch.setattr(kb, "_signal_verified_process_group", fail_signal)
    monkeypatch.setattr(kb, "_exit_gate_release_reason", lambda _row: None)
    with kb.connect() as conn:
        managed = _install_running(
            conn,
            tmp_path,
            managed=True,
            suffix="signal-failure",
        )
        first = kb.dispatch_once(conn)
        second = kb.dispatch_once(conn)
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE parent_run_id = ?",
            (managed["run_id"],),
        ).fetchone()
        task = kb.get_task(conn, managed["task_id"])

        assert first.policy_signal_failed == [
            (managed["task_id"], "cooperative termination did not drain")
        ]
        assert second.policy_signal_failed == []
        assert len(signals) == 1
        assert len(
            _events(
                conn,
                managed["task_id"],
                "policy_disabled_signal_failed",
            )
        ) == 1
        assert gate["released_at"] is None
        assert task.worker_pid == _PID
        assert task.claim_lock == managed["claim_lock"]


def test_incomplete_identity_blocks_without_signal_or_automatic_release(
    policy_brake_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda _identity: pytest.fail("unverified identity must not be signalled"),
    )
    with kb.connect() as conn:
        managed = _install_running(
            conn,
            tmp_path,
            managed=True,
            complete_identity=False,
            suffix="missing-identity",
        )
        first = kb.dispatch_once(conn)
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE parent_run_id = ?",
            (managed["run_id"],),
        ).fetchone()
        run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (managed["run_id"],)
        ).fetchone()

        assert first.policy_unverified == [managed["task_id"]]
        assert gate["gate_kind"] == "legacy_unknown"
        assert gate["released_at"] is None
        assert run["process_cleanup_unsafe"]

        # Even broad OS-absence stubs cannot turn unknown identity into proof.
        monkeypatch.setattr(kb, "_local_node_id", lambda: _NODE)
        monkeypatch.setattr(kb, "_local_boot_id", lambda: _BOOT)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_process_group_alive", lambda _pgid: False)
        second = kb.dispatch_once(conn)
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE parent_run_id = ?",
            (managed["run_id"],),
        ).fetchone()
        task = kb.get_task(conn, managed["task_id"])
        assert second.policy_draining == []
        assert gate["released_at"] is None
        assert task.worker_pid == _PID
        assert task.claim_lock == managed["claim_lock"]


def test_missing_active_run_gets_nonreleasable_quarantine_witness(
    policy_brake_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda _identity: pytest.fail("corrupt identity must not be signalled"),
    )
    with kb.connect() as conn:
        managed = _install_running(
            conn,
            tmp_path,
            managed=True,
            suffix="missing-run",
        )
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_runs WHERE id = ?", (managed["run_id"],)
            )
            conn.execute(
                "UPDATE tasks SET current_run_id = NULL WHERE id = ?",
                (managed["task_id"],),
            )

        result = kb.dispatch_once(conn)
        task = kb.get_task(conn, managed["task_id"])
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE parent_task_id = ?",
            (managed["task_id"],),
        ).fetchone()
        witness = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (gate["parent_run_id"],)
        ).fetchone()

        assert result.policy_unverified == [managed["task_id"]]
        assert task.status == "blocked"
        assert task.worker_pid == _PID
        assert task.claim_lock == managed["claim_lock"]
        assert gate["gate_kind"] == "legacy_unknown"
        assert gate["released_at"] is None
        assert witness["status"] == "policy_disabled"
        assert witness["worker_pid"] == _PID
        assert witness["claim_lock"] == managed["claim_lock"]


def test_disabled_policy_dry_run_is_read_only_and_keeps_ordinary_preview(
    policy_brake_home, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda _profile: True
    )
    monkeypatch.setattr(
        kb,
        "_signal_verified_process_group",
        lambda _identity: pytest.fail("dry-run must never signal"),
    )
    with kb.connect() as conn:
        managed_waiting = kb.create_task(
            conn,
            title="managed waiting",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        _bind(conn, managed_waiting, suffix="dry-waiting")
        managed_running = _install_running(
            conn,
            tmp_path,
            managed=True,
            suffix="dry-running",
        )
        ordinary = kb.create_task(
            conn,
            title="ordinary ready",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        before = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "tasks",
                "task_runs",
                "task_events",
                "task_exit_gates",
            )
        }

        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=10,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail(
                "dry-run must not invoke spawn"
            ),
        )
        after = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in before
        }

        assert after == before
        assert set(result.policy_blocked) == {
            managed_waiting,
            managed_running["task_id"],
        }
        assert result.policy_draining == [managed_running["task_id"]]
        assert [item[0] for item in result.spawned] == [ordinary]
