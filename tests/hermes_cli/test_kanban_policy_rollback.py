"""Fail-closed rollback tests for the short-task handoff dispatcher policy."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

from agent import kanban_auto_handoff as handoff
from hermes_cli import kanban_db as kb


def _enabled_config() -> dict:
    return {
        "agent": {"max_turns": 90},
        "kanban": {
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 2,
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


def _bind_managed_task(conn, task_id: str, *, binding_id: str) -> None:
    """Attach the same frozen policy a trusted gateway launch would store."""
    from agent.kanban_handoff_scope import decide_gateway_origin

    identity = {
        "platform": "feishu",
        "scope_id": "tenant-1",
        "chat_type": "group",
        "chat_id": "group-1",
        "thread_id": "",
        "user_id": "user-1",
        "notifier_profile": "default",
        "session_key": "session-1",
    }
    decision = decide_gateway_origin(_enabled_config(), identity)
    assert decision["authorized"] is True
    assert kb.add_control_binding(
        conn,
        binding_id=binding_id,
        task_id=task_id,
        short_handoff_policy=decision["task_policy"],
        **identity,
    ) is True


def test_live_policy_rejects_a_newly_corrupted_dispatcher_config(
    tmp_path, monkeypatch
):
    """A worker must not reuse load_config's stale enabled fallback."""
    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(yaml.safe_dump(_enabled_config()), encoding="utf-8")
    monkeypatch.setenv(handoff.POLICY_HOME_ENV, str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert handoff.live_dispatcher_policy_enabled() is True
    assert kb._short_task_handoff_dispatch_enabled() is True

    # Deliberately use a different file size so both config caches must inspect
    # this new on-disk state. Parse failure is a safety stop, not permission to
    # continue from the process-local last-known-good value.
    config_path.write_text(
        "kanban:\n  short_task_handoff: [broken, yaml\n",
        encoding="utf-8",
    )

    assert handoff.live_dispatcher_policy_enabled() is False
    assert kb._short_task_handoff_dispatch_enabled() is False


@pytest.mark.parametrize(
    ("root_enabled", "profile_enabled"),
    [(True, False), (False, True)],
)
def test_dispatcher_process_home_owns_policy_under_profile_override(
    tmp_path, monkeypatch, root_enabled, profile_enabled
):
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    root_home = tmp_path / "root-home"
    profile_home = tmp_path / "profile-home"
    root_home.mkdir()
    profile_home.mkdir()
    root_config = _enabled_config()
    profile_config = _enabled_config()
    root_config["kanban"]["short_task_handoff"]["enabled"] = root_enabled
    profile_config["kanban"]["short_task_handoff"]["enabled"] = (
        profile_enabled
    )
    (root_home / "config.yaml").write_text(
        yaml.safe_dump(root_config), encoding="utf-8"
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(profile_config), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(root_home))

    token = set_hermes_home_override(profile_home)
    try:
        snapshot = handoff.load_current_dispatcher_policy_snapshot()
    finally:
        reset_hermes_home_override(token)

    assert snapshot["enabled"] is root_enabled
    assert snapshot["validation_error"] is None


def test_disabled_policy_pauses_trusted_ready_and_review_but_not_legacy(
    tmp_path, monkeypatch
):
    """Rollback isolates a managed chain without breaking ordinary Kanban."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda _profile: True
    )
    kb.init_db()

    with kb.connect() as conn:
        ordinary_id = kb.create_task(
            conn,
            title="ordinary legacy lane",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        successor_id = kb.create_task(
            conn,
            title="managed successor",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            idempotency_key="kanban-auto-handoff:parent-1",
        )
        _bind_managed_task(
            conn, successor_id, binding_id="binding-successor"
        )
        review_id = kb.create_task(
            conn,
            title="trusted review",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        _bind_managed_task(conn, review_id, binding_id="binding-review")
        conn.execute(
            "UPDATE tasks SET status = 'review', resume_lane = 'review' "
            "WHERE id = ?",
            (review_id,),
        )

        dry = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=10,
            spawn_fn=lambda *_args, **_kwargs: None,
        )
        assert [item[0] for item in dry.spawned] == [ordinary_id]
        assert {item[0] for item in dry.respawn_guarded} == {
            successor_id,
            review_id,
        }
        assert kb.has_spawnable_ready(conn) is True
        assert kb.has_spawnable_review(conn) is False

        assert kb.claim_task(conn, successor_id, claimer="manual") is None
        assert kb.claim_review_task(conn, review_id, claimer="manual") is None

        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (ordinary_id,)
        )
        assert kb.has_spawnable_ready(conn) is False


def test_disabled_policy_keeps_legacy_manual_claims_compatible(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: False
    )
    kb.init_db()

    with kb.connect() as conn:
        ready_id = kb.create_task(
            conn,
            title="ordinary ready",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        review_id = kb.create_task(
            conn,
            title="ordinary review",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        conn.execute(
            "UPDATE tasks SET status = 'review', resume_lane = 'review' "
            "WHERE id = ?",
            (review_id,),
        )

        assert kb.claim_task(conn, ready_id, claimer="legacy") is not None
        assert (
            kb.claim_review_task(conn, review_id, claimer="legacy-review")
            is not None
        )


@pytest.mark.parametrize("queued_status", ["ready", "review"])
def test_policy_change_after_claim_requeues_without_starting_worker(
    tmp_path, monkeypatch, queued_status
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: True
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda _profile: True
    )
    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        lambda **_kwargs: handoff.build_dispatcher_policy_snapshot(
            {
                "agent": {"max_turns": 90},
                "kanban": {"short_task_handoff": {"enabled": False}},
            }
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )
    kb.init_db()

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"policy race {queued_status}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        _bind_managed_task(
            conn,
            task_id,
            binding_id=f"binding-{queued_status}",
        )
        if queued_status == "review":
            conn.execute(
                "UPDATE tasks SET status = 'review', resume_lane = 'review' "
                "WHERE id = ?",
                (task_id,),
            )

        result = kb.dispatch_once(conn, max_spawn=1)
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

        assert result.spawned == []
        assert result.respawn_guarded == [
            (
                task_id,
                "short-task handoff policy changed; trusted chain remains paused",
            )
        ]
        assert task.status == queued_status
        assert task.resume_lane == (
            "review" if queued_status == "review" else "implementation"
        )
        assert task.current_run_id is None
        assert task.worker_pid is None
        assert task.consecutive_failures == 0
        assert run["status"] == "reclaimed"
        assert run["ended_at"] is not None


@pytest.mark.parametrize("queued_status", ["ready", "review"])
@pytest.mark.parametrize("policy_change", ["disabled", "corrupt"])
@pytest.mark.skipif(
    os.name == "nt", reason="Phase 1 worker start barrier is POSIX-only"
)
def test_policy_change_after_popen_aborts_before_barrier_release(
    tmp_path, monkeypatch, queued_status, policy_change
):
    """The last strict read must happen while the exact child is still gated."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        kb, "_short_task_handoff_dispatch_enabled", lambda: True
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda _profile: True
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb, "_resolve_worker_cli_toolsets", lambda _home: None
    )
    monkeypatch.setattr(
        kb,
        "_capture_process_group_identity",
        lambda pid: {
            "owner_node_id": "node",
            "owner_boot_id": "boot",
            "worker_start_token": "start",
            "worker_pgid": int(pid),
        },
    )

    enabled = handoff.build_dispatcher_policy_snapshot(_enabled_config())
    if policy_change == "disabled":
        changed_config = _enabled_config()
        changed_config["kanban"]["short_task_handoff"]["enabled"] = False
        changed = handoff.build_dispatcher_policy_snapshot(changed_config)
    else:
        changed = {
            "schema": 1,
            "enabled": False,
            "validation_error": "config is malformed",
        }
    snapshots = iter([enabled, changed])
    policy_reads = []

    def load_policy(**_kwargs):
        snapshot = next(snapshots)
        policy_reads.append(snapshot)
        return snapshot

    monkeypatch.setattr(
        handoff,
        "load_current_dispatcher_policy_snapshot",
        load_policy,
    )

    class FakeProcess:
        pid = 424242

        def __init__(self):
            self.waited = 0

        def wait(self, timeout=None):
            self.waited += 1
            return 0

    fake_process = FakeProcess()
    popen_calls = []

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return fake_process

    monkeypatch.setattr(
        subprocess, "Popen", fake_popen
    )
    monkeypatch.setattr(
        kb,
        "_release_pending_worker_start",
        lambda _pending: pytest.fail("start barrier must stay closed"),
    )
    kb.init_db()

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=f"post-popen policy race {queued_status}",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        _bind_managed_task(
            conn,
            task_id,
            binding_id=f"post-popen-{policy_change}-{queued_status}",
        )
        if queued_status == "review":
            conn.execute(
                "UPDATE tasks SET status = 'review', resume_lane = 'review' "
                "WHERE id = ?",
                (task_id,),
            )
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1, "
            "last_failure_error = 'prior failure' WHERE id = ?",
            (task_id,),
        )

        result = kb.dispatch_once(conn, max_spawn=1)
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

        assert result.spawned == []
        assert result.respawn_guarded == [
            (
                task_id,
                "short-task handoff policy changed before worker release; "
                "trusted chain remains paused",
            )
        ]
        assert fake_process.waited >= 1
        assert len(popen_calls) == 1
        assert policy_reads == [enabled, changed]
        assert task.status == queued_status
        assert task.resume_lane == (
            "review" if queued_status == "review" else "implementation"
        )
        assert task.current_run_id is None
        assert task.worker_pid is None
        assert task.consecutive_failures == 1
        assert task.last_failure_error == "prior failure"
        assert run["status"] == "reclaimed"
        assert run["worker_pid"] is None
        assert run["ended_at"] is not None
        event_kinds = [event.kind for event in kb.list_events(conn, task_id)]
        assert "spawned" not in event_kinds
        assert event_kinds.count("policy_paused_before_worker_release") == 1
        assert kb._take_pending_worker_start(fake_process.pid) is None
