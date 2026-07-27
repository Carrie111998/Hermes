from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import objective_service
from hermes_cli import objectives_db as db


def test_ungranted_kanban_result_is_quarantined_from_objective(tmp_path, monkeypatch):
    authority = db.connect(tmp_path / "objectives.db")
    objective = db.create_objective(
        authority,
        desired_outcome="Complete delegated research",
        originator="setup:user",
        permitted_systems=["kanban"],
    )
    db.transition_objective(authority, objective.id, "accepted", actor="setup:user")
    plan_id = db.create_plan(
        authority,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="employee:ceo",
    )
    db.transition_objective(authority, objective.id, "planned", actor="runtime")

    board_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_path))
    with kb.connect_closing() as kanban:
        task_id = kb.create_task(
            kanban,
            title="Research",
            body="Produce evidence",
            assignee="ceo",
            created_by="employee:ceo",
        )
        kb.complete_task(kanban, task_id, summary="Evidence produced")

    action_id = db.propose_action(
        authority,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="kanban.create_task",
        payload={"system": "kanban", "target_resource": "default"},
        expected_outcome="task created",
        required_capability="work.delegate",
        verification_method="kanban.task.created",
        risk_class="low",
        reversible=True,
        proposed_by="employee:ceo",
    )
    # Bridge correlation is based on the durable external reference, regardless
    # of how the original execution was performed.
    authority.execute(
        """
        INSERT INTO permits (
            id, action_id, capability, payload_sha256, policy_version,
            constraints_json, issued_to, issued_at, expires_at, consumed_at
        ) VALUES ('permit_test', ?, 'work.delegate', '', 'test', '{}',
                  'employee:ceo', 1, 9999999999, 1)
        """,
        (action_id,),
    )
    authority.execute(
        """
        INSERT INTO execution_results (
            id, action_id, permit_id, executor, status, external_reference,
            result_json, started_at, finished_at
        ) VALUES ('result_test', ?, 'permit_test', 'employee:ceo', 'succeeded',
                  ?, '{}', 1, 1)
        """,
        (action_id, task_id),
    )
    authority.commit()

    assert objective_service.sync_kanban_events(authority) == 0
    intervention = authority.execute(
        """SELECT category,context_json FROM intervention_queue
            WHERE status='open'"""
    ).fetchone()
    assert intervention["category"] == "delegated_result_authority_invalid"
    assert task_id in intervention["context_json"]
    assert objective_service.sync_kanban_events(authority) == 0
    authority.close()
