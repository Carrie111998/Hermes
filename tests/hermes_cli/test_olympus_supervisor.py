"""Phase A Olympus supervisor behavior and fail-closed contracts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.olympus_supervisor import (
    CheckpointDriftError,
    DuplicateSupervisorError,
    OlympusSupervisor,
    OlympusSupervisorError,
    QueueDriftError,
    QueueValidationError,
    StopRequested,
    SupervisorSettings,
    mission_control_projection,
    olympus_supervisor_command,
)
from hermes_cli.subcommands.olympus_supervisor import (
    build_olympus_supervisor_parser,
)


class FakeClock:
    def __init__(self, value: float = 2_000_000_000.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


def _empty_diagnostics(**_kwargs):
    return {"generated_at": 0, "jobs": [], "issues": []}


def _settings(**overrides) -> SupervisorSettings:
    value = {
        "board": "olympus",
        "tenant": "olympus",
        "heartbeat_interval_seconds": 10,
        "stale_supervisor_seconds": 30,
        "stale_task_seconds": 1000,
        "stale_job_seconds": 100,
        "cycle_interval_seconds": 10,
        "idle_backoff_initial_seconds": 20,
        "idle_backoff_max_seconds": 80,
        "idle_backoff_factor": 2,
        "stop_poll_seconds": 5,
        "notification_repeat_seconds": 86400,
        "max_selected_candidates": 6,
        "max_risk": "medium",
        "max_task_estimated_cost_usd": 0,
        "max_cycle_estimated_cost_usd": 0,
        "providers": {
            "codex": {"capacity": 2, "available": True},
            "claude": {"capacity": 2, "available": True},
            "grok": {"capacity": 1, "available": True},
            "hermes": {"capacity": 1, "available": True},
        },
    }
    for key, item in overrides.items():
        if key == "providers":
            value["providers"] = item
        else:
            value[key] = item
    return SupervisorSettings.from_mapping(value)


def _metadata(
    clock: FakeClock,
    *,
    enabled: bool = True,
    risk: str = "low",
    providers: list[str] | None = None,
    authority_status: str = "active",
    recommendation_allowed: bool = True,
    expires_at: float | None = None,
    approval_required: bool = False,
    approval_status: str | None = None,
    assigned_provider: str | None = None,
    assigned_slot: str | None = None,
    objective: str = "Produce the bounded Olympus deliverable.",
    estimated_cost_usd: float = 0,
) -> str:
    value = {
        "schema_version": "olympus-kanban-task/1",
        "enabled": enabled,
        "risk": risk,
        "providers": providers or ["codex"],
        "estimated_cost_usd": estimated_cost_usd,
        "authority": {
            "status": authority_status,
            "recommendation_allowed": recommendation_allowed,
            "authority_id": "authority-test",
            "revision": 1,
            "expires_at": (expires_at if expires_at is not None else clock() + 10000),
        },
        "approval": {
            "required": approval_required,
            "status": (
                approval_status
                if approval_status is not None
                else ("pending" if approval_required else "not_required")
            ),
            "decision_id": "decision-test" if approval_required else "",
        },
        "goal": {
            "objective": objective,
            "max_turns": 10,
            "timeout_seconds": 600,
            "allowed_paths": [],
            "forbidden_actions": ["push", "deploy"],
            "deliverables": ["result.md"],
        },
    }
    if assigned_provider is not None:
        value["assigned_provider"] = assigned_provider
    if assigned_slot is not None:
        value["assigned_slot"] = assigned_slot
    return json.dumps(value, sort_keys=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "olympus.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kb.connect(db_path)
    conn.close()
    return {
        "home": home,
        "db_path": db_path,
        "state": tmp_path / "state",
        "clock": FakeClock(),
    }


def _write_task(
    board,
    *,
    title: str,
    body: str | None = None,
    priority: int = 0,
    parents=(),
    tenant: str = "olympus",
) -> str:
    conn = kb.connect(board["db_path"])
    try:
        return kb.create_task(
            conn,
            title=title,
            body=body if body is not None else _metadata(board["clock"]),
            assignee="default",
            tenant=tenant,
            priority=priority,
            parents=parents,
            workspace_kind="dir",
            workspace_path=str(board["home"] / "workspace"),
        )
    finally:
        conn.close()


def _claim(board, task_id: str) -> None:
    conn = kb.connect(board["db_path"])
    try:
        assert kb.claim_task(conn, task_id, claimer=f"test:{task_id}") is not None
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        heartbeat = int(board["clock"]())
        expires = heartbeat + 1000
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ?, claim_expires = ? WHERE id = ?",
            (heartbeat, expires, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET last_heartbeat_at = ?, claim_expires = ? "
            "WHERE id = ?",
            (heartbeat, expires, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _supervisor(board, **kwargs) -> OlympusSupervisor:
    return OlympusSupervisor(
        kwargs.pop("settings", _settings()),
        state_root=kwargs.pop("state_root", board["state"]),
        db_path=board["db_path"],
        clock=kwargs.pop("clock", board["clock"]),
        sleeper=kwargs.pop("sleeper", board["clock"].sleep),
        diagnostics_provider=kwargs.pop("diagnostics_provider", _empty_diagnostics),
        **kwargs,
    )


def _reason_codes(item) -> set[str]:
    return {reason["code"] for reason in item["reasons"]}


def _outbox(supervisor: OlympusSupervisor) -> list[dict]:
    return supervisor.store.load_outbox()["messages"]


def test_deterministic_task_ranking_is_explainable(board):
    older = _write_task(
        board,
        title="older",
        body=_metadata(board["clock"], providers=["claude"]),
        priority=10,
    )
    safer = _write_task(
        board,
        title="safer",
        body=_metadata(board["clock"], risk="low"),
        priority=10,
    )
    riskier = _write_task(
        board,
        title="riskier",
        body=_metadata(board["clock"], risk="medium"),
        priority=10,
    )
    conn = kb.connect(board["db_path"])
    try:
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (1, older))
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (2, safer))
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (0, riskier))
        conn.commit()
    finally:
        conn.close()

    supervisor = _supervisor(board)
    first = supervisor.read_queue()
    second = supervisor.read_queue()

    assert [item["task_id"] for item in first["ranked_queue"]] == [
        older,
        safer,
        riskier,
    ]
    assert first["ranked_queue"] == second["ranked_queue"]
    assert all(
        "priority desc" in item["ranking_explanation"] for item in first["ranked_queue"]
    )


def test_dependency_blocking_uses_canonical_task_links(board):
    parent = _write_task(board, title="parent")
    child = _write_task(board, title="child", parents=[parent], priority=100)

    value = _supervisor(board).read_queue()
    blocked = next(
        item for item in value["blocked_candidates"] if item["task_id"] == child
    )

    assert "unresolved_dependencies" in _reason_codes(blocked)
    assert blocked["unresolved_dependencies"] == [parent]


def test_risk_and_authority_filtering_fail_closed(board):
    high = _write_task(
        board,
        title="high risk",
        body=_metadata(board["clock"], risk="high"),
    )
    expired = _write_task(
        board,
        title="expired",
        body=_metadata(board["clock"], expires_at=board["clock"]() - 1),
    )
    unauthorized = _write_task(
        board,
        title="unauthorized",
        body=_metadata(board["clock"], recommendation_allowed=False),
    )

    blocked = {
        item["task_id"]: _reason_codes(item)
        for item in _supervisor(board).read_queue()["blocked_candidates"]
    }
    assert "risk_exceeds_limit" in blocked[high]
    assert "authority_expired" in blocked[expired]
    assert "recommendation_not_authorized" in blocked[unauthorized]


def test_task_and_cycle_spending_limits_filter_proposals(board):
    too_expensive = _write_task(
        board,
        title="too expensive",
        body=_metadata(board["clock"], estimated_cost_usd=2),
        priority=100,
    )
    first = _write_task(
        board,
        title="first",
        body=_metadata(board["clock"], estimated_cost_usd=0.75),
        priority=50,
    )
    second = _write_task(
        board,
        title="second",
        body=_metadata(board["clock"], estimated_cost_usd=0.75),
        priority=40,
    )
    supervisor = _supervisor(
        board,
        settings=_settings(
            max_task_estimated_cost_usd=1,
            max_cycle_estimated_cost_usd=1,
        ),
    )

    value = supervisor.read_queue()
    blocked = {
        item["task_id"]: _reason_codes(item) for item in value["blocked_candidates"]
    }

    assert value["selected_candidates"][0]["task_id"] == first
    assert "task_spending_limit" in blocked[too_expensive]
    assert "cycle_spending_limit" in blocked[second]


def test_pending_approval_is_projected_and_drafted_without_consumption(board):
    task_id = _write_task(
        board,
        title="approval",
        body=_metadata(board["clock"], approval_required=True),
    )
    supervisor = _supervisor(board)
    checkpoint = supervisor.run_once()

    assert checkpoint["status"] == "blocked"
    assert checkpoint["pending_operator_decisions"] == [
        {
            "task_id": task_id,
            "kind": "approval",
            "decision_id": "decision-test",
            "reason_codes": ["operator_approval_required"],
            "reason": "approval is pending",
        }
    ]
    assert any(
        item["type"] == "operator_approval_required" and item["sent"] is False
        for item in _outbox(supervisor)
    )
    assert "_metadata" not in checkpoint["blocked_candidates"][0]


def test_provider_slot_exhaustion_blocks_candidate(board):
    for index in (1, 2):
        task_id = _write_task(
            board,
            title=f"active {index}",
            body=_metadata(
                board["clock"],
                assigned_provider="codex",
                assigned_slot=f"codex:{index}",
            ),
        )
        _claim(board, task_id)
    candidate = _write_task(board, title="candidate")

    value = _supervisor(board).read_queue()
    blocked = next(
        item for item in value["blocked_candidates"] if item["task_id"] == candidate
    )

    assert "provider_unavailable" in _reason_codes(blocked)
    assert value["provider_availability"]["codex"]["free"] == 0
    assert value["status"] == "working"


def test_parallel_proposals_use_unique_configured_slots(board):
    for index in range(7):
        _write_task(
            board,
            title=f"candidate {index}",
            body=_metadata(
                board["clock"],
                providers=["codex", "claude", "grok", "hermes"],
            ),
        )

    value = _supervisor(board).read_queue()
    selected = value["selected_candidates"]
    slots = [item["proposed_slot"] for item in selected]

    assert len(selected) == 6
    assert len(set(slots)) == 6
    assert set(slots) == {
        "codex:1",
        "codex:2",
        "claude:1",
        "claude:2",
        "grok:1",
        "hermes:1",
    }
    assert all(item["bounded_goal"]["launch_authorized"] is False for item in selected)


@pytest.mark.parametrize("status", ["working", "stale"])
def test_unleased_active_diagnostic_makes_provider_occupancy_ambiguous(board, status):
    task_id = _write_task(board, title="candidate")

    def diagnostics(**_kwargs):
        return {
            "jobs": [
                {
                    "job_id": "olympus-test",
                    "lanes": {
                        "lane": {
                            "lane_id": "lane",
                            "task_id": task_id,
                            "provider": "codex",
                            "effective_status": status,
                        }
                    },
                }
            ],
            "issues": [],
        }

    with pytest.raises(QueueValidationError, match="active job has no Kanban lease"):
        _supervisor(board, diagnostics_provider=diagnostics).read_queue()


def test_malformed_queue_refuses_selection(board):
    _write_task(board, title="bad", body="{not-json")
    supervisor = _supervisor(board)

    with pytest.raises(QueueValidationError, match="malformed_task"):
        supervisor.read_queue()
    with pytest.raises(QueueValidationError, match="malformed_task"):
        supervisor.run_forever(max_cycles=1)
    assert (
        supervisor.store.read_json(supervisor.store.failure_path)["state"] == "failed"
    )


def test_conflicting_lease_refuses_selection(board):
    task_ids = []
    for index in (1, 2):
        task_id = _write_task(
            board,
            title=f"active {index}",
            body=_metadata(
                board["clock"],
                assigned_provider="codex",
                assigned_slot=f"codex:{index}",
            ),
        )
        _claim(board, task_id)
        task_ids.append(task_id)
    conn = kb.connect(board["db_path"])
    try:
        first_lock = conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (task_ids[0],)
        ).fetchone()[0]
        second_run = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_ids[1],)
        ).fetchone()[0]
        conn.execute(
            "UPDATE tasks SET claim_lock = ? WHERE id = ?",
            (first_lock, task_ids[1]),
        )
        conn.execute(
            "UPDATE task_runs SET claim_lock = ? WHERE id = ?",
            (first_lock, second_run),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(QueueValidationError, match="shared"):
        _supervisor(board).read_queue()


def test_task_and_run_lease_metadata_must_match(board):
    task_id = _write_task(
        board,
        title="active",
        body=_metadata(
            board["clock"],
            assigned_provider="codex",
            assigned_slot="codex:1",
        ),
    )
    _claim(board, task_id)
    conn = kb.connect(board["db_path"])
    try:
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE task_runs SET claim_expires = claim_expires + 1 WHERE id = ?",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(QueueValidationError, match="claim expiries disagree"):
        _supervisor(board).read_queue()


def test_duplicate_supervisor_is_refused_even_without_live_lock(board):
    supervisor = _supervisor(board, identity_status=lambda _identity: "alive")
    supervisor.store.write_json(
        supervisor.store.lease_path,
        {
            "schema_version": "olympus-supervisor-lease/1",
            "run_id": "other-run",
            "status": "active",
            "state": "working",
            "process": {"pid": 99999},
            "heartbeat_at": board["clock"](),
        },
    )

    with pytest.raises(DuplicateSupervisorError):
        supervisor.run_once()


def test_malformed_supervisor_lease_fails_closed(board):
    supervisor = _supervisor(board)
    supervisor.store.write_json(
        supervisor.store.lease_path,
        {"schema_version": "wrong", "status": "released"},
    )

    with pytest.raises(OlympusSupervisorError, match="malformed_state"):
        supervisor.run_once()


def test_restart_from_checkpoint_preserves_generation_and_suppresses_action(board):
    task_id = _write_task(board, title="candidate")
    first_supervisor = _supervisor(board)
    first = first_supervisor.run_once()
    second_supervisor = _supervisor(board)
    second = second_supervisor.run_once()

    assert second["completed_cycles"] == 2
    assert second["generation"] == first["generation"]
    assert second["previous_run_id"] == first["run_id"]
    assert second["selected_candidates"][0]["task_id"] == task_id
    assert (
        second["selected_candidates"][0]["action_id"]
        == first["selected_candidates"][0]["action_id"]
    )
    assert second["selected_candidates"][0]["new_recommendation"] is False


def test_checkpoint_drift_is_refused(board):
    supervisor = _supervisor(board)
    _write_task(board, title="candidate")
    checkpoint = supervisor.run_once()
    checkpoint["restart_checkpoint"]["generation"] += 1
    supervisor.store.write_json(supervisor.store.checkpoint_path, checkpoint)

    with pytest.raises(CheckpointDriftError):
        supervisor.store.load_checkpoint()


def test_queue_drift_during_cycle_refuses_checkpoint(board):
    task_id = _write_task(board, title="candidate")

    def mutate_after_snapshot(stage):
        if stage != "after_snapshot":
            return
        conn = kb.connect(board["db_path"])
        try:
            conn.execute(
                "UPDATE tasks SET priority = priority + 1 WHERE id = ?",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()

    supervisor = _supervisor(board, stage_hook=mutate_after_snapshot)
    with pytest.raises(QueueDriftError):
        supervisor.run_once()
    assert not supervisor.store.checkpoint_path.exists()


def test_duplicate_action_and_telegram_recommendation_are_suppressed(board):
    _write_task(board, title="candidate", priority=100)
    supervisor = _supervisor(board)
    first = supervisor.run_once()
    _write_task(board, title="lower priority queue change", priority=0)
    second = supervisor.run_once()

    recommendations = [
        item for item in _outbox(supervisor) if item["type"] == "new_recommended_task"
    ]
    assert len(recommendations) == 1
    assert recommendations[0]["sent"] is False
    assert (
        first["selected_candidates"][0]["action_id"]
        == second["selected_candidates"][0]["action_id"]
    )
    assert first["queue_snapshot_identity"] != second["queue_snapshot_identity"]


def test_stale_job_detection_reports_kanban_heartbeat(board):
    task_id = _write_task(
        board,
        title="stale",
        body=_metadata(
            board["clock"],
            assigned_provider="codex",
            assigned_slot="codex:1",
        ),
    )
    _claim(board, task_id)
    stale_at = int(board["clock"]() - 500)
    conn = kb.connect(board["db_path"])
    try:
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ?, claim_expires = ? WHERE id = ?",
            (stale_at, int(board["clock"]() + 500), task_id),
        )
        conn.execute(
            "UPDATE task_runs SET last_heartbeat_at = ?, claim_expires = ? WHERE id = ?",
            (stale_at, int(board["clock"]() + 500), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    value = _supervisor(board).read_queue()

    assert value["reconciliation"]["stale_jobs"][0]["task_id"] == task_id
    assert value["reconciliation"]["stale_jobs"][0]["reason"] == "heartbeat stale"


def test_stale_job_telegram_draft_ignores_changing_age(board):
    task_id = _write_task(
        board,
        title="active stale",
        body=_metadata(
            board["clock"],
            assigned_provider="codex",
            assigned_slot="codex:1",
        ),
    )
    _claim(board, task_id)
    conn = kb.connect(board["db_path"])
    try:
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (board["clock"]() - 1000, task_id),
        )
        conn.commit()
    finally:
        conn.close()

    supervisor = _supervisor(board, settings=_settings(stale_job_seconds=100))
    supervisor.run_once()
    board["clock"].value += 10
    supervisor.run_once()

    drafts = [
        item for item in _outbox(supervisor) if item["type"] == "stale_provider_job"
    ]
    assert len(drafts) == 1


def test_dead_job_reuses_why_slow_and_resume_plan_surfaces(board):
    def diagnostics(**_kwargs):
        return {
            "jobs": [
                {
                    "job_id": "olympus-dead",
                    "lanes": {
                        "provider": {
                            "lane_id": "provider",
                            "task_id": "",
                            "platform": "olympus",
                            "provider": "codex",
                            "effective_status": "dead",
                            "current_step": "provider response",
                            "next_expected_action": "validate the checkpoint",
                            "timing": {"model_wait": 120},
                        }
                    },
                }
            ],
            "issues": [],
        }

    value = _supervisor(board, diagnostics_provider=diagnostics).read_queue()
    dead = value["reconciliation"]["dead_jobs"][0]
    resumable = value["reconciliation"]["resumable_jobs"][0]

    assert dead["why_slow_command"] == (
        "hermes jobs why-slow olympus-dead --lane provider"
    )
    assert resumable["resume_plan_command"] == (
        "hermes jobs resume-plan olympus-dead --lane provider"
    )
    assert resumable["safe_to_resume"] is None


def test_blocked_job_is_persisted_and_drives_blocked_state(board):
    task_id = _write_task(
        board,
        title="blocked provider lane",
        body=_metadata(
            board["clock"],
            assigned_provider="codex",
            assigned_slot="codex:1",
        ),
    )
    _claim(board, task_id)

    def diagnostics(**_kwargs):
        return {
            "jobs": [
                {
                    "job_id": "olympus-blocked",
                    "lanes": {
                        "provider": {
                            "lane_id": "provider",
                            "task_id": task_id,
                            "platform": "olympus",
                            "provider": "codex",
                            "effective_status": "blocked",
                            "current_step": "await operator",
                            "next_expected_action": "review the blocker",
                            "blocker": "missing external decision",
                            "timing": {"blocked": 120},
                        }
                    },
                }
            ],
            "issues": [],
        }

    supervisor = _supervisor(board, diagnostics_provider=diagnostics)
    checkpoint = supervisor.run_once()
    blocked = checkpoint["blocked_jobs"][0]

    assert checkpoint["status"] == "blocked"
    assert blocked["task_id"] == task_id
    assert blocked["blocker"] == "missing external decision"
    assert blocked["why_slow_command"] == (
        "hermes jobs why-slow olympus-blocked --lane provider"
    )
    projection = supervisor.store.read_json(supervisor.store.mission_control_path)
    assert projection["blocked_jobs"] == checkpoint["blocked_jobs"]


def test_safe_idle_uses_bounded_backoff(board):
    supervisor = _supervisor(board)
    first = supervisor.run_once()
    second = supervisor.run_once()

    assert first["status"] == "idle"
    assert first["backoff"]["current_seconds"] == 20
    assert second["backoff"]["current_seconds"] == 40
    assert second["backoff"]["current_seconds"] <= 80


def test_global_stop_preserves_checkpoint_and_prevents_new_cycle(board):
    _write_task(board, title="candidate")
    supervisor = _supervisor(board)
    supervisor.run_once()
    before = supervisor.store.checkpoint_path.read_bytes()
    stop = supervisor.request_stop("maintenance")

    with pytest.raises(StopRequested):
        supervisor.run_once()

    assert stop["reason"] == "maintenance"
    assert supervisor.store.checkpoint_path.read_bytes() == before
    assert any(item["type"] == "emergency_stop" for item in _outbox(supervisor))
    resumed = supervisor.resume()
    assert resumed["resumed"] is True
    assert resumed["cycle_started"] is False
    assert supervisor.store.checkpoint_path.read_bytes() == before


def test_global_stop_remains_authoritative_when_draft_projection_fails(board):
    supervisor = _supervisor(board)
    supervisor.store.write_json(
        supervisor.store.checkpoint_path,
        {"schema_version": "not-a-supervisor-checkpoint"},
    )

    result = supervisor.request_stop("emergency disable")

    stop = supervisor.store.stop_reason()
    assert stop["reason"] == "emergency disable"
    assert result["draft_prepared"] is False
    assert result["draft_error"]["code"] == "checkpoint_drift"
    assert not supervisor.store.telegram_outbox_path.exists()


def test_continuous_loop_exits_cleanly_when_stop_is_already_active(board):
    supervisor = _supervisor(board)
    supervisor.request_stop("maintenance")

    assert supervisor.run_forever() == 0
    heartbeat = supervisor.store.read_json(supervisor.store.heartbeat_path)
    assert heartbeat["state"] == "stopped"
    assert heartbeat["detail"] == "global stop control is active"
    assert not supervisor.store.checkpoint_path.exists()


def test_resume_refuses_while_supervisor_lease_may_still_be_active(board):
    supervisor = _supervisor(board, identity_status=lambda _identity: "alive")
    supervisor.request_stop("maintenance")
    supervisor.store.write_json(
        supervisor.store.lease_path,
        {
            "schema_version": "olympus-supervisor-lease/1",
            "run_id": "active-run",
            "status": "active",
            "state": "stopped",
            "process": {"pid": 12345},
            "heartbeat_at": board["clock"](),
        },
    )

    with pytest.raises(DuplicateSupervisorError, match="resume_refused"):
        supervisor.resume()
    assert supervisor.store.stop_path.exists()


def test_stop_is_checked_during_cycle(board):
    _write_task(board, title="candidate")
    supervisor = None

    def hook(stage):
        if stage == "after_snapshot":
            supervisor.store.request_stop("mid-cycle stop")

    supervisor = _supervisor(board, stage_hook=hook)
    with pytest.raises(StopRequested):
        supervisor.run_once()
    assert not supervisor.store.checkpoint_path.exists()


def test_unavailable_provider_is_not_selected(board):
    _write_task(board, title="candidate")
    providers = {
        "codex": {"capacity": 2, "available": False},
        "claude": {"capacity": 2, "available": True},
        "grok": {"capacity": 1, "available": True},
        "hermes": {"capacity": 1, "available": True},
    }
    supervisor = _supervisor(board, settings=_settings(providers=providers))
    value = supervisor.read_queue()

    assert not value["selected_candidates"]
    assert "provider_unavailable" in _reason_codes(value["blocked_candidates"][0])


def test_telegram_drafts_are_low_noise_and_never_live(board):
    _write_task(board, title="candidate")
    supervisor = _supervisor(board)
    supervisor.run_once()
    supervisor.run_once()
    messages = _outbox(supervisor)

    assert len([item for item in messages if item["type"] == "supervisor_healthy"]) == 1
    assert len([item for item in messages if item["type"] == "daily_summary"]) == 1
    assert all(item["delivery_mode"] == "draft_only" for item in messages)
    assert all(item["sent"] is False for item in messages)

    board["clock"].value += 86401
    supervisor.run_once()
    repeated = _outbox(supervisor)
    assert len([item for item in repeated if item["type"] == "supervisor_healthy"]) == 2


def test_mission_control_projection_matches_checkpoint(board):
    task_id = _write_task(board, title="candidate")
    supervisor = _supervisor(board)
    checkpoint = supervisor.run_once()
    projection = json.loads(
        supervisor.store.mission_control_path.read_text(encoding="utf-8")
    )

    assert projection == mission_control_projection(checkpoint)
    assert projection["authoritative"] is False
    assert projection["queue_authority"]["kind"] == "hermes_kanban"
    assert projection["selected_next_task"]["task_id"] == task_id
    assert (
        projection["supervisor"]["source_checkpoint_digest"]
        == checkpoint["checkpoint_digest"]
    )


def test_crash_during_checkpoint_write_preserves_prior_checkpoint(board):
    _write_task(board, title="candidate")
    first_supervisor = _supervisor(board)
    first = first_supervisor.run_once()
    original = first_supervisor.store.checkpoint_path.read_bytes()

    def crash(phase, path):
        if phase == "before_replace" and path.name == "checkpoint.json":
            raise RuntimeError("injected checkpoint crash")

    crashing = _supervisor(board, crash_injector=crash)
    with pytest.raises(RuntimeError, match="injected checkpoint crash"):
        crashing.run_once()

    assert crashing.store.checkpoint_path.read_bytes() == original
    assert crashing.health()["healthy"] is False
    assert (
        crashing.store.load_checkpoint()["checkpoint_digest"]
        == first["checkpoint_digest"]
    )


def test_reboot_style_restart_uses_new_run_and_same_restart_point(board):
    _write_task(board, title="candidate")
    first = _supervisor(board).run_once()
    board["clock"].value += 5
    rebooted = _supervisor(board)
    second = rebooted.run_once()

    assert second["run_id"] != first["run_id"]
    assert second["previous_run_id"] == first["run_id"]
    assert (
        second["restart_checkpoint"]["queue_snapshot_identity"]
        == first["queue_snapshot_identity"]
    )
    assert second["completed_cycles"] == first["completed_cycles"] + 1


def test_cycle_does_not_mutate_kanban_repository_or_runtime(board, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    sentinel = repository / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    task_id = _write_task(
        board,
        title="candidate",
        body=_metadata(board["clock"], objective="Inspect only."),
    )
    queue_artifacts = [
        board["db_path"],
        Path(str(board["db_path"]) + "-wal"),
        Path(str(board["db_path"]) + "-shm"),
    ]
    conn = sqlite3.connect(board["db_path"])
    try:
        before_rows = conn.execute(
            "SELECT id, status, claim_lock, current_run_id FROM tasks ORDER BY id"
        ).fetchall()
        before_changes = conn.total_changes
    finally:
        conn.close()
    db_before = board["db_path"].read_bytes()
    queue_before = {
        str(path): path.read_bytes() for path in queue_artifacts if path.exists()
    }

    checkpoint = _supervisor(board).run_once()

    conn = sqlite3.connect(board["db_path"])
    try:
        after_rows = conn.execute(
            "SELECT id, status, claim_lock, current_run_id FROM tasks ORDER BY id"
        ).fetchall()
        assert conn.total_changes == 0
    finally:
        conn.close()
    assert after_rows == before_rows
    assert before_changes == 0
    assert board["db_path"].read_bytes() == db_before
    assert {
        str(path): path.read_bytes() for path in queue_artifacts if path.exists()
    } == queue_before
    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert checkpoint["selected_candidates"][0]["task_id"] == task_id
    assert (
        checkpoint["selected_candidates"][0]["bounded_goal"]["launch_authorized"]
        is False
    )
    assert not list(repository.glob(".git/worktrees/*"))


def test_run_once_is_bounded_and_never_sleeps(board):
    _write_task(board, title="candidate")
    stages = []

    def no_sleep(_seconds):
        raise AssertionError("run-once must not sleep")

    supervisor = _supervisor(
        board,
        sleeper=no_sleep,
        stage_hook=stages.append,
    )
    checkpoint = supervisor.run_once()

    assert checkpoint["completed_cycles"] == 1
    assert stages == [
        "before_cycle",
        "before_snapshot",
        "after_snapshot",
        "after_reconciliation",
        "after_selection",
        "before_checkpoint",
        "before_commit",
    ]


def test_24_hour_accelerated_loop_remains_idle_and_suppresses_noise(board):
    clock = board["clock"]
    settings = _settings(
        heartbeat_interval_seconds=600,
        cycle_interval_seconds=3600,
        idle_backoff_initial_seconds=3600,
        idle_backoff_max_seconds=3600,
        stop_poll_seconds=3600,
        notification_repeat_seconds=86400,
    )
    supervisor = _supervisor(
        board,
        settings=settings,
        clock=clock,
        sleeper=clock.sleep,
    )
    started = clock()

    assert supervisor.run_forever(max_cycles=25) == 25
    checkpoint = supervisor.store.load_checkpoint()
    messages = _outbox(supervisor)

    assert clock() - started == 24 * 3600
    assert checkpoint["completed_cycles"] == 25
    assert checkpoint["status"] == "idle"
    assert len([item for item in messages if item["type"] == "daily_summary"]) == 2
    assert not checkpoint["selected_candidates"]
    assert not checkpoint["active_leases_observed"]


def test_parser_wires_all_phase_a_commands():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    handler = lambda args: args  # noqa: E731
    build_olympus_supervisor_parser(
        subparsers,
        cmd_olympus_supervisor=handler,
    )

    actions = (
        "run",
        "run-once",
        "inspect",
        "queue",
        "explain-next",
        "checkpoint",
        "health",
        "stop",
        "resume",
        "render-mission-control",
        "telegram-preview",
    )
    for action in actions:
        namespace = parser.parse_args(["olympus-supervisor", action])
        assert namespace.func is handler
        assert namespace.olympus_supervisor_action == action


def test_cli_run_once_smoke_uses_injected_supervisor(board, capsys):
    _write_task(board, title="candidate")
    supervisor = _supervisor(board)
    args = SimpleNamespace(
        olympus_supervisor_action="run-once",
        json=False,
        board=None,
        state_dir=None,
    )

    assert olympus_supervisor_command(args, supervisor=supervisor) == 0
    output = capsys.readouterr().out
    assert "cycle complete" in output
    assert "prepared only, not launched" in output
