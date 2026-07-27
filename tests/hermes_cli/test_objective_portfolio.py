from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import (
    objective_portfolio,
    objective_triggers,
    objectives_db,
    organization_db,
)


def _parent(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Portfolio Company",
        purpose="Manage durable objectives",
        profile_name="default",
        charter={},
    )
    parent = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Launch a verified product",
        originator="employee:ceo",
        permitted_systems=["kanban", "payments"],
        prohibited_actions=["data.delete"],
        max_spend_minor=1_000,
        currency="USD",
        expires_at=int(time.time()) + 7_200,
    )
    objectives_db.transition_objective(
        conn, parent.id, "accepted", actor="employee:ceo"
    )
    return conn, organization_id, parent


def test_child_objective_inherits_scope_budget_and_wake_event(tmp_path):
    conn, organization_id, parent = _parent(tmp_path)
    child_id, created = objective_portfolio.create_child_objective(
        conn,
        parent_objective_id=parent.id,
        desired_outcome="Complete launch research",
        success_criteria=[
            {
                "verifier": "kanban.all_delegated_tasks_completed",
                "params": {},
            }
        ],
        termination_conditions=["research invalidates launch"],
        permitted_systems=["kanban"],
        prohibited_actions=["data.delete", "external.publish"],
        constraints=["use public sources"],
        allocated_budget_minor=400,
        currency="USD",
        expires_at=int(time.time()) + 3_600,
        idempotency_key="child-objective-research-0001",
        created_by="employee:ceo",
        max_active_objectives=10,
    )
    repeated_id, repeated_created = objective_portfolio.create_child_objective(
        conn,
        parent_objective_id=parent.id,
        desired_outcome="ignored idempotent retry",
        success_criteria=[],
        termination_conditions=[],
        permitted_systems=[],
        prohibited_actions=[],
        constraints=[],
        allocated_budget_minor=0,
        currency=None,
        expires_at=int(time.time()) + 1,
        idempotency_key="child-objective-research-0001",
        created_by="employee:ceo",
        max_active_objectives=1,
    )

    assert created is True
    assert repeated_created is False
    assert repeated_id == child_id
    child = objectives_db.objective_to_dict(conn, child_id)
    assert child["organization_id"] == organization_id
    assert child["status"] == "accepted"
    assert child["permitted_systems"] == ["kanban"]
    assert child["prohibited_actions"] == ["data.delete", "external.publish"]
    assert child["max_spend_minor"] == 400
    wake = conn.execute(
        "SELECT status FROM objective_inbox WHERE objective_id=?", (child_id,)
    ).fetchone()
    assert wake["status"] == "pending"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            """UPDATE objective_relationships SET allocated_budget_minor=900
               WHERE child_objective_id=?""",
            (child_id,),
        )


def test_children_cannot_expand_scope_or_oversubscribe_parent_budget(tmp_path):
    conn, _, parent = _parent(tmp_path)
    common = {
        "parent_objective_id": parent.id,
        "success_criteria": [{"verifier": "test.pass", "params": {}}],
        "termination_conditions": [],
        "prohibited_actions": ["data.delete"],
        "constraints": [],
        "currency": "USD",
        "expires_at": int(time.time()) + 3_600,
        "created_by": "employee:ceo",
        "max_active_objectives": 10,
    }
    with pytest.raises(PermissionError, match="expands permitted systems"):
        objective_portfolio.create_child_objective(
            conn,
            desired_outcome="Use an unauthorized system",
            permitted_systems=["git"],
            allocated_budget_minor=0,
            idempotency_key="child-objective-invalid-system-0001",
            **common,
        )

    objective_portfolio.create_child_objective(
        conn,
        desired_outcome="First funded workstream",
        permitted_systems=["kanban"],
        allocated_budget_minor=700,
        idempotency_key="child-objective-budget-one-0001",
        **common,
    )
    with pytest.raises(PermissionError, match="budget allocation"):
        objective_portfolio.create_child_objective(
            conn,
            desired_outcome="Oversubscribed workstream",
            permitted_systems=["payments"],
            allocated_budget_minor=301,
            idempotency_key="child-objective-budget-two-0001",
            **common,
        )


def test_concurrent_child_admission_serializes_active_ceiling(tmp_path):
    conn, _, parent = _parent(tmp_path)
    db_path = tmp_path / "authority.db"

    def create(index):
        worker_conn = objectives_db.connect(db_path)
        try:
            try:
                return objective_portfolio.create_child_objective(
                    worker_conn,
                    parent_objective_id=parent.id,
                    desired_outcome=f"Concurrent workstream {index}",
                    success_criteria=[{"verifier": "test.pass", "params": {}}],
                    termination_conditions=[],
                    permitted_systems=["kanban"],
                    prohibited_actions=["data.delete"],
                    constraints=[],
                    allocated_budget_minor=0,
                    currency="USD",
                    expires_at=int(time.time()) + 3_600,
                    idempotency_key=f"concurrent-child-admission-{index:04d}",
                    created_by="employee:ceo",
                    max_active_objectives=2,
                )
            except PermissionError:
                return None
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert sum(result is not None and result[1] for result in results) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM objectives WHERE organization_id=? AND status='accepted'",
        (parent.organization_id,),
    ).fetchone()[0] == 2


def test_exact_creation_key_cancels_only_its_child(tmp_path):
    conn, _, parent = _parent(tmp_path)
    child_id, _ = objective_portfolio.create_child_objective(
        conn,
        parent_objective_id=parent.id,
        desired_outcome="Reversible workstream",
        success_criteria=[{"verifier": "test.pass", "params": {}}],
        termination_conditions=[],
        permitted_systems=["kanban"],
        prohibited_actions=["data.delete"],
        constraints=[],
        allocated_budget_minor=0,
        currency="USD",
        expires_at=int(time.time()) + 3_600,
        idempotency_key="child-objective-cancellable-0001",
        created_by="employee:ceo",
        max_active_objectives=10,
    )
    cancelled = objective_portfolio.cancel_child_by_creation_key(
        conn,
        parent_objective_id=parent.id,
        idempotency_key="child-objective-cancellable-0001",
        actor="control:compensation",
    )
    repeated = objective_portfolio.cancel_child_by_creation_key(
        conn,
        parent_objective_id=parent.id,
        idempotency_key="child-objective-cancellable-0001",
        actor="control:compensation",
    )
    assert cancelled == repeated == child_id
    assert objectives_db.get_objective(conn, child_id).status == "cancelled"


def test_successor_is_peer_root_and_inherits_cadence_and_authority(tmp_path):
    conn, organization_id, predecessor = _parent(tmp_path)
    schedule_id = objective_triggers.create_schedule(
        conn,
        organization_id=organization_id,
        objective_id=predecessor.id,
        event_type="ceo.operating_review",
        interval_seconds=86_400,
        next_fire_at=int(time.time()) + 100,
        payload={"review": ["runway", "portfolio"]},
        idempotency_key="predecessor-operating-cadence-0001",
    )
    successor_id, created = objective_portfolio.create_successor_objective(
        conn,
        predecessor_objective_id=predecessor.id,
        desired_outcome="Reach ten verified paying customers",
        success_criteria=[
            {"verifier": "accounting.revenue_at_least", "params": {"amount_minor": 1}}
        ],
        termination_conditions=["runway exhausted"],
        permitted_systems=["kanban"],
        prohibited_actions=["data.delete", "external.publish"],
        constraints=["preserve runway"],
        allocated_budget_minor=800,
        currency="USD",
        expires_at=int(time.time()) + 30 * 86_400,
        idempotency_key="successor-objective-growth-0001",
        created_by="employee:ceo",
        max_active_objectives=10,
    )
    repeated_id, repeated_created = objective_portfolio.create_successor_objective(
        conn,
        predecessor_objective_id=predecessor.id,
        desired_outcome="ignored",
        success_criteria=[],
        termination_conditions=[],
        permitted_systems=[],
        prohibited_actions=[],
        constraints=[],
        allocated_budget_minor=0,
        currency=None,
        expires_at=int(time.time()) + 1,
        idempotency_key="successor-objective-growth-0001",
        created_by="employee:ceo",
        max_active_objectives=1,
    )

    assert created is True
    assert repeated_created is False
    assert repeated_id == successor_id
    successor = objectives_db.objective_to_dict(conn, successor_id)
    assert successor["status"] == "accepted"
    assert successor["permitted_systems"] == ["kanban"]
    relationship = conn.execute(
        """SELECT relationship FROM objective_relationships
            WHERE child_objective_id=?""",
        (successor_id,),
    ).fetchone()
    assert relationship["relationship"] == "succeeds"
    assert objective_portfolio.final_root_requires_successor(
        conn, predecessor.id
    ) is False
    schedules = conn.execute(
        """SELECT objective_id,status FROM objective_schedules
            WHERE event_type='ceo.operating_review' ORDER BY objective_id"""
    ).fetchall()
    assert {tuple(row) for row in schedules} == {
        (predecessor.id, "disabled"),
        (successor_id, "active"),
    }
    assert schedule_id
    with pytest.raises(PermissionError, match="different operation"):
        objective_portfolio.create_child_objective(
            conn,
            parent_objective_id=predecessor.id,
            desired_outcome="Try to reinterpret the successor key",
            success_criteria=[{"verifier": "test.pass", "params": {}}],
            termination_conditions=[],
            permitted_systems=["kanban"],
            prohibited_actions=["data.delete"],
            constraints=[],
            allocated_budget_minor=0,
            currency="USD",
            expires_at=int(time.time()) + 3_600,
            idempotency_key="successor-objective-growth-0001",
            created_by="employee:ceo",
            max_active_objectives=10,
        )


def test_successor_cannot_expand_scope_budget_or_remove_prohibitions(tmp_path):
    conn, _, predecessor = _parent(tmp_path)
    common = {
        "predecessor_objective_id": predecessor.id,
        "desired_outcome": "Continue governed operation",
        "success_criteria": [{"verifier": "test.pass", "params": {}}],
        "termination_conditions": [],
        "constraints": [],
        "currency": "USD",
        "expires_at": int(time.time()) + 86_400,
        "created_by": "employee:ceo",
        "max_active_objectives": 10,
    }
    with pytest.raises(PermissionError, match="expands permitted systems"):
        objective_portfolio.create_successor_objective(
            conn,
            permitted_systems=["git"],
            prohibited_actions=["data.delete"],
            allocated_budget_minor=100,
            idempotency_key="successor-invalid-system-0001",
            **common,
        )
    with pytest.raises(PermissionError, match="removes.*prohibition"):
        objective_portfolio.create_successor_objective(
            conn,
            permitted_systems=["kanban"],
            prohibited_actions=[],
            allocated_budget_minor=100,
            idempotency_key="successor-invalid-prohibition-0001",
            **common,
        )
    with pytest.raises(PermissionError, match="exceeds predecessor authority"):
        objective_portfolio.create_successor_objective(
            conn,
            permitted_systems=["kanban"],
            prohibited_actions=["data.delete"],
            allocated_budget_minor=1_001,
            idempotency_key="successor-invalid-budget-0001",
            **common,
        )
