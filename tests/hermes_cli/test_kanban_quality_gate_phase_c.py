"""Phase C durable quality-gate contracts at the existing review boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.conftest import write_valid_model_routing_config


@pytest.fixture
def routed_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    write_valid_model_routing_config(home)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def _review_run(conn, *, quality_enabled: bool = True):
    task_id = kb.create_task(conn, title="quality-gated change", assignee="builder")
    if quality_enabled:
        assert kb.set_quality_policy(conn, task_id, {"enabled": True, "required": True})
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None and implementation.current_run_id is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="implementation complete",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
    assert review is not None and review.current_run_id is not None
    return task_id, implementation.current_run_id, review.current_run_id


def test_workers_cannot_self_certify_quality_pass(routed_conn):
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn)

    assert not kb.complete_task(routed_conn, task_id, expected_run_id=review_run_id)
    assert kb.get_task(routed_conn, task_id).status == "escalation_required"
    assert not kb.record_quality_gate(
        routed_conn,
        task_id=task_id,
        run_id=review_run_id,
        passed=True,
        result={"checks": ["focused tests"], "approval": "human"},
    )
    assert not kb.complete_task(routed_conn, task_id, expected_run_id=review_run_id)


def test_operator_configured_gate_executes_in_task_workspace_and_records_same_run_pass(
    routed_conn, tmp_path: Path,
):
    workspace = tmp_path / "quality-workspace"
    workspace.mkdir()
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn)
    kb.set_workspace_path(routed_conn, task_id, str(workspace))
    assert kb.set_quality_policy(
        routed_conn,
        task_id,
        {
            "enabled": True,
            "required": True,
            "gates": [{"command": ["/bin/sh", "-c", "printf gate-ok"], "timeout_seconds": 5}],
        },
    )

    assert kb.complete_task(routed_conn, task_id, expected_run_id=review_run_id)

    run = kb.get_run(routed_conn, review_run_id)
    assert run is not None
    assert run.metadata["quality_gate"] == {
        "passed": True,
        "gates": [{"command": ["/bin/sh", "-c", "printf gate-ok"], "status": "passed", "output": "gate-ok"}],
    }


def test_quality_failure_is_durable_then_requires_router_escalation_without_self_upgrade(
    routed_conn, tmp_path: Path,
):
    task_id, implementation_run_id, review_run_id = _review_run(routed_conn)
    workspace = tmp_path / "quality-failure"
    workspace.mkdir()
    kb.set_workspace_path(routed_conn, task_id, str(workspace))
    assert kb.set_quality_policy(routed_conn, task_id, {
        "enabled": True, "required": True,
        "gates": [{"command": ["/bin/sh", "-c", "printf broken; exit 7"]}],
    })
    prior = routed_conn.execute(
        "SELECT route_snapshot, attempt_number FROM task_runs WHERE id = ?",
        (review_run_id,),
    ).fetchone()
    prior_route = json.loads(prior["route_snapshot"])

    assert kb.execute_quality_gates(routed_conn, task_id=task_id, run_id=review_run_id)

    task = kb.get_task(routed_conn, task_id)
    assert task is not None and task.status == "escalation_required"
    run = kb.get_run(routed_conn, review_run_id)
    assert run is not None and run.outcome == "quality_failed"
    assert run.error == "quality gate exited 7"
    events = kb.list_events(routed_conn, task_id)
    failure = [event for event in events if event.kind == "quality_failed"][-1]
    escalation = [event for event in events if event.kind == "escalation_required"][-1]
    assert failure.run_id == review_run_id
    assert failure.payload == {
        "reason": "quality gate exited 7",
        "result": {"gates": [{"command": ["/bin/sh", "-c", "printf broken; exit 7"], "status": "nonzero", "output": "broken", "exit_code": 7}]},
    }
    assert escalation.run_id == review_run_id
    assert escalation.payload["prior_run_id"] == review_run_id
    assert escalation.payload["prior_model"] == prior_route["model"]
    assert escalation.payload["failure_reason"] == "quality gate exited 7"
    assert escalation.payload["quality_result"] == failure.payload["result"]
    assert escalation.payload["retry_count"] == prior["attempt_number"]
    # Router owns the decision and merely escalates; the worker cannot mutate routes.
    assert escalation.payload["decision"]["action"] == "human_escalation_required"
    assert task.model_override is None
    assert task.provider_override is None
    assert implementation_run_id != review_run_id


def test_missing_operator_gate_config_fails_closed_and_escalates(routed_conn):
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn)

    assert kb.execute_quality_gates(routed_conn, task_id=task_id, run_id=review_run_id)

    task = kb.get_task(routed_conn, task_id)
    run = kb.get_run(routed_conn, review_run_id)
    assert task is not None and task.status == "escalation_required"
    assert run is not None and run.outcome == "quality_failed"
    assert run.metadata["quality_gate"] == {
        "passed": False, "gates": [], "status": "configuration_failed",
    }


def test_quality_gate_spawn_error_is_durable_and_bounded(routed_conn, tmp_path: Path):
    workspace = tmp_path / "quality-workspace"
    workspace.mkdir()
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn)
    kb.set_workspace_path(routed_conn, task_id, str(workspace))
    assert kb.set_quality_policy(routed_conn, task_id, {
        "enabled": True, "required": True,
        "gates": [{"command": ["/definitely/not/a/quality-command"]}],
    })

    assert kb.execute_quality_gates(routed_conn, task_id=task_id, run_id=review_run_id)

    run = kb.get_run(routed_conn, review_run_id)
    assert run is not None and run.outcome == "quality_failed"
    gate = run.metadata["quality_gate"]["gates"][0]
    assert gate["status"] == "spawn_error"
    assert len(gate["output"]) <= kb.QUALITY_GATE_MAX_OUTPUT


def test_quality_gate_timeout_fails_closed(routed_conn, tmp_path: Path):
    workspace = tmp_path / "quality-workspace"
    workspace.mkdir()
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn)
    kb.set_workspace_path(routed_conn, task_id, str(workspace))
    assert kb.set_quality_policy(routed_conn, task_id, {
        "enabled": True, "required": True,
        "gates": [{"command": ["/bin/sh", "-c", "sleep 2"], "timeout_seconds": 1}],
    })

    assert kb.execute_quality_gates(routed_conn, task_id=task_id, run_id=review_run_id)

    run = kb.get_run(routed_conn, review_run_id)
    assert run is not None and run.outcome == "quality_failed"
    assert run.metadata["quality_gate"]["gates"][0]["status"] == "timeout"


def test_quality_gate_rejects_stale_or_non_review_worker(routed_conn):
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn)

    assert not kb.execute_quality_gates(
        routed_conn, task_id=task_id, run_id=review_run_id + 100,
    )
    assert kb.get_task(routed_conn, task_id).status == "escalation_required"


def test_self_certification_rejects_stale_or_non_review_worker(routed_conn):
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn)

    assert not kb.record_quality_gate(
        routed_conn, task_id=task_id, run_id=review_run_id + 100, passed=True,
    )
    assert kb.get_task(routed_conn, task_id).status == "running"
    assert not kb.record_quality_gate(
        routed_conn, task_id=task_id, run_id=review_run_id, passed=False,
        reason="   ",
    )
    assert kb.get_task(routed_conn, task_id).status == "running"


def test_quality_policy_is_opt_in_and_default_completion_remains_compatible(routed_conn):
    task_id, _implementation_run_id, review_run_id = _review_run(routed_conn, quality_enabled=False)

    assert kb.complete_task(routed_conn, task_id, expected_run_id=review_run_id)
    assert kb.get_task(routed_conn, task_id).status == "done"
