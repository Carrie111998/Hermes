from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from hermes_cli import employee_provisioning, objectives_db, organization_db
from hermes_cli.hiring_policy import (
    default_hiring_policy,
    evaluate_hiring_case,
    evaluate_hiring_case_from_state,
    materialize_hiring_decision,
)


def organization():
    return {"headcount_limit": 10, "payroll_budget_minor": 1_000_000}


def test_ceo_remains_solo_when_need_is_not_sustained():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_growth",
            "qualified_backlog": 12,
            "capacity_pressure_cycles": 1,
            "ceo_utilization": 1.2,
            "annual_cost_minor": 100,
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "defer"
    assert "solo founder" in decision.reason


def test_sustained_capacity_pressure_warrants_hire():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_growth",
            "qualified_backlog": 8,
            "capacity_pressure_cycles": 3,
            "ceo_utilization": 1.1,
            "annual_cost_minor": 100,
            "expected_duration_cycles": 3,
            "scoped_deliverable": "clear the qualified lead backlog",
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "hire"
    assert decision.employment_class == "contractor"
    assert decision.evidence["qualified_backlog"] == 8


def test_transient_capability_gap_does_not_warrant_hire():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_product",
            "missing_capability": "security.audit",
            "blocked_objectives": 1,
            "capability_gap_cycles": 1,
            "annual_cost_minor": 0,
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "defer"


def test_sustained_blocking_capability_gap_warrants_hire():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_product",
            "missing_capability": "security.audit",
            "blocked_objectives": 2,
            "capability_gap_cycles": 3,
            "annual_cost_minor": 0,
            "recurring_need": True,
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "hire"
    assert decision.employment_class == "fte"
    assert decision.evidence["missing_capability"] == "security.audit"


def test_independent_verifier_can_be_required_before_capacity_threshold():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_publish",
            "separation_of_duty_required": True,
            "separation_duty": "independent publication verification",
            "annual_cost_minor": 0,
            "expected_duration_cycles": 2,
            "scoped_deliverable": "verify the launch publication",
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "hire"
    assert decision.employment_class == "contractor"


def test_hire_without_objective_is_denied():
    decision = evaluate_hiring_case(
        case={
            "qualified_backlog": 20,
            "capacity_pressure_cycles": 20,
            "ceo_utilization": 2.0,
            "annual_cost_minor": 0,
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "deny"


def test_budget_and_headcount_are_hard_limits():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_growth",
            "separation_of_duty_required": True,
            "separation_duty": "audit",
            "annual_cost_minor": 1,
        },
        organization={"headcount_limit": 1, "payroll_budget_minor": 100},
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "deny"
    assert "headcount" in decision.reason


def test_strategic_recurring_role_warrants_fte():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_product",
            "qualified_backlog": 8,
            "capacity_pressure_cycles": 3,
            "ceo_utilization": 1.2,
            "annual_cost_minor": 10,
            "expected_duration_cycles": 6,
            "recurring_need": True,
            "strategic_core": True,
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "hire"
    assert decision.employment_class == "fte"


def test_temporary_scoped_role_warrants_contractor():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_research",
            "missing_capability": "market.research",
            "blocked_objectives": 1,
            "capability_gap_cycles": 3,
            "annual_cost_minor": 10,
            "expected_duration_cycles": 4,
            "scoped_deliverable": "market landscape report",
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "hire"
    assert decision.employment_class == "contractor"


def test_unclassified_role_is_deferred_even_when_workload_exists():
    decision = evaluate_hiring_case(
        case={
            "objective_id": "obj_growth",
            "qualified_backlog": 8,
            "capacity_pressure_cycles": 3,
            "ceo_utilization": 1.2,
            "annual_cost_minor": 10,
        },
        organization=organization(),
        current_headcount=1,
        current_payroll_minor=0,
        policy=default_hiring_policy(),
    )
    assert decision.verdict == "defer"
    assert "contractor versus FTE" in decision.reason


def _governed_hiring_state(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Hiring Company",
        purpose="Deliver secure products",
        profile_name="default",
        charter={},
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Complete the security audit",
        originator="employee:ceo",
        permitted_systems=["security"],
        success_criteria=[{"verifier": "audit.complete", "params": {}}],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    objectives_db.transition_objective(
        conn, objective.id, "planned", actor="employee:ceo"
    )
    for version in range(3):
        plan_id = objectives_db.create_plan(
            conn,
            objective.id,
            assumptions=[],
            tasks=[{"attempt": version + 1}],
            dependencies=[],
            risks=[],
            created_by="employee:ceo",
        )
        objectives_db.propose_action(
            conn,
            objective_id=objective.id,
            plan_id=plan_id,
            action_type="security.audit",
            payload={"system": "security", "target_resource": "product:1"},
            expected_outcome="audit complete",
            required_capability="security.audit",
            verification_method="security.audit.readback",
            risk_class="low",
            reversible=True,
            proposed_by="employee:ceo",
        )
    objectives_db.transition_objective(
        conn,
        objective.id,
        "blocked",
        actor="control:policy",
        reason="missing security.audit capability",
    )
    return conn, organization_id, objective


def test_authoritative_state_warrants_time_bounded_contractor(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    conn, organization_id, objective = _governed_hiring_state(tmp_path)
    decision_id, decision = evaluate_hiring_case_from_state(
        conn,
        organization_id=organization_id,
        case={
            "objective_id": objective.id,
            "missing_capability": "security.audit",
            "blocked_objectives": 999,
            "capability_gap_cycles": 999,
            "annual_cost_minor": 100,
            "expected_duration_cycles": 4,
            "scoped_deliverable": "independent product security audit",
        },
        policy=default_hiring_policy(),
        idempotency_key="hire-security-auditor-0001",
        evaluated_by="control:hiring",
    )

    assert decision.verdict == "hire"
    assert decision.employment_class == "contractor"
    assert decision.evidence["authoritative_blocked_objectives"] == 1
    assert decision.evidence["authoritative_capability_gap_cycles"] == 3
    row = conn.execute(
        "SELECT * FROM hiring_decisions WHERE id=?", (decision_id,)
    ).fetchone()
    assert row["evidence_sha256"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE hiring_decisions SET verdict='deny' WHERE id=?",
            (decision_id,),
        )

    ceo = organization_db.active_ceo(conn)
    employee_id = materialize_hiring_decision(
        conn,
        decision_id,
        display_name="Security Auditor",
        title="Contract Security Auditor",
        level="individual_contributor",
        manager_id=ceo["id"],
        mandate={
            "purpose": "Complete the independent security audit",
            "responsibilities": ["inspect product controls"],
            "decision_rights": ["report audit findings"],
            "prohibited_actions": ["security.deploy"],
            "capabilities": ["security.audit"],
            "systems": ["security"],
            "kpis": ["verified audit report"],
            "escalation": {"to": ceo["id"]},
            "toolsets": ["terminal"],
            "expires_at": int(time.time()) + 3_600,
        },
        actor="control:hiring",
    )
    repeated_employee_id = materialize_hiring_decision(
        conn,
        decision_id,
        display_name="Ignored Retry Name",
        title="Ignored Retry Title",
        level="individual_contributor",
        manager_id=ceo["id"],
        mandate={},
        actor="control:hiring",
    )
    employee = organization_db.get_employee_record(conn, employee_id)
    assert repeated_employee_id == employee_id
    assert employee["status"] == "approved"
    assert employee["employment_type"] == "contractor"
    assert employee["hired_for_objective_id"] == objective.id
    engagement = conn.execute(
        "SELECT * FROM hiring_engagements WHERE decision_id=?", (decision_id,)
    ).fetchone()
    assert engagement["employee_id"] == employee_id

    profile_dir = employee_provisioning.provision_employee_profile(
        conn,
        employee_id,
        actor="control:provisioning",
        profile_name="security-auditor",
    )
    assert organization_db.get_employee_record(conn, employee_id)["status"] == "active"
    profile = yaml.safe_load((profile_dir / "profile.yaml").read_text())
    assert profile["employment_class"] == "contractor"
    assert profile["manager_employee_id"] == ceo["id"]
    chart = organization_db.organization_chart(conn, organization_id)
    assert [(item["title"], item["depth"]) for item in chart] == [
        ("Chief Executive Officer", 0),
        ("Contract Security Auditor", 1),
    ]


def test_planner_cannot_invent_sustained_hiring_evidence(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Solo Company",
        purpose="Remain lean",
        profile_name="default",
        charter={},
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Explore a temporary task",
        originator="employee:ceo",
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )

    first_id, decision = evaluate_hiring_case_from_state(
        conn,
        organization_id=organization_id,
        case={
            "objective_id": objective.id,
            "missing_capability": "market.research",
            "blocked_objectives": 500,
            "capability_gap_cycles": 500,
            "annual_cost_minor": 0,
            "expected_duration_cycles": 3,
            "scoped_deliverable": "research report",
        },
        policy=default_hiring_policy(),
        idempotency_key="hire-market-researcher-0001",
        evaluated_by="control:hiring",
    )
    second_id, repeated = evaluate_hiring_case_from_state(
        conn,
        organization_id=organization_id,
        case={"objective_id": "ignored-on-idempotent-retry"},
        policy={},
        idempotency_key="hire-market-researcher-0001",
        evaluated_by="control:hiring",
    )

    assert decision.verdict == "defer"
    assert decision.evidence["authoritative_blocked_objectives"] == 0
    assert decision.evidence["authoritative_capability_gap_cycles"] == 0
    assert second_id == first_id
    assert repeated == decision
    with pytest.raises(PermissionError, match="positive hiring decision"):
        materialize_hiring_decision(
            conn,
            first_id,
            display_name="Unwarranted Hire",
            title="Researcher",
            level="individual_contributor",
            manager_id=organization_db.active_ceo(conn)["id"],
            mandate={},
            actor="control:hiring",
        )


def test_concurrent_hiring_materialization_serializes_headcount(tmp_path):
    conn, organization_id, objective = _governed_hiring_state(tmp_path)
    conn.execute(
        "UPDATE organizations SET headcount_limit=2, payroll_budget_minor=1000 "
        "WHERE id=?",
        (organization_id,),
    )
    conn.commit()
    decisions = []
    for index in (1, 2):
        decision_id, decision = evaluate_hiring_case_from_state(
            conn,
            organization_id=organization_id,
            case={
                "objective_id": objective.id,
                "missing_capability": "security.audit",
                "blocked_objectives": 999,
                "capability_gap_cycles": 999,
                "annual_cost_minor": 100,
                "expected_duration_cycles": 4,
                "scoped_deliverable": f"audit {index}",
            },
            policy=default_hiring_policy(),
            idempotency_key=f"concurrent-hire-decision-{index:04d}",
            evaluated_by="control:hiring",
        )
        assert decision.verdict == "hire"
        decisions.append(decision_id)
    ceo_id = organization_db.active_ceo(conn)["id"]
    database = tmp_path / "authority.db"

    def materialize(decision_id):
        worker_conn = objectives_db.connect(database)
        try:
            try:
                return materialize_hiring_decision(
                    worker_conn,
                    decision_id,
                    display_name=f"Auditor {decision_id[-4:]}",
                    title="Contract Security Auditor",
                    level="individual_contributor",
                    manager_id=ceo_id,
                    mandate={
                        "purpose": "Complete an independent audit",
                        "responsibilities": ["audit"],
                        "decision_rights": ["report findings"],
                        "prohibited_actions": ["security.deploy"],
                        "capabilities": ["security.audit"],
                        "systems": ["security"],
                        "kpis": ["verified report"],
                        "escalation": {"to": ceo_id},
                        "toolsets": ["terminal"],
                        "expires_at": int(time.time()) + 3_600,
                    },
                    actor="control:hiring",
                )
            except PermissionError:
                return None
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        employees = list(pool.map(materialize, decisions))
    assert sum(employee is not None for employee in employees) == 1
    refreshed = objectives_db.connect(database)
    try:
        assert refreshed.execute(
            "SELECT COUNT(*) FROM employees WHERE organization_id=? AND status!='rejected'",
            (organization_id,),
        ).fetchone()[0] == 2
    finally:
        refreshed.close()
