from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import (
    accounting_db,
    finance_db,
    objectives_db,
    operational_control,
    organization_db,
    resource_budget,
)
from hermes_cli import objective_adapters as adapters
from hermes_cli import verification_evidence
from hermes_cli.objective_runtime import (
    ActionProposal,
    ExecutionOutcome,
    PlanProposal,
    VerificationOutcome,
)


def response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model="planner-test-model",
        usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45),
    )


def test_auxiliary_planner_returns_typed_bounded_proposal(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: response(
            """
            {
              "assumptions": [],
              "tasks": [{"step": "delegate"}],
              "dependencies": [],
              "risks": [],
              "objective_complete_when_verified": false,
              "actions": [{
                "action_type": "kanban.create_task",
                "payload": {
                  "system": "kanban",
                  "target_resource": "default",
                  "title": "Research market",
                  "body": "Produce evidence",
                  "assignee": "research"
                },
                "expected_outcome": "task exists",
                "required_capability": "work.delegate",
                "verification_method": "kanban.task.created",
                "risk_class": "low",
                "reversible": true,
                "estimated_cost_minor": 0
              }]
            }
            """
        ),
    )
    planner = adapters.AuxiliaryObjectivePlanner(
        action_contracts=[
            adapters.RegisteredActionContract(
                "kanban.create_task",
                "work.delegate",
                "kanban",
                "kanban.task.created",
            )
        ],
    )
    proposal = planner.propose(
        {
            "id": "obj_1",
            "desired_outcome": "Validate market",
            "success_criteria": [],
        },
        {"event_type": "objective.accepted", "payload": {}},
    )
    assert proposal.actions[0].action_type == "kanban.create_task"
    assert proposal.objective_complete_when_verified is False


def test_model_failure_releases_durable_compute_reservation(monkeypatch, tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Planner Recovery Company",
        purpose="Do not strand compute budget after a provider failure",
        profile_name="default",
        charter={"finance": {"base_currency": "USD"}},
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Recover planner failure",
        originator="employee:ceo",
    )

    def fail_call(**kwargs):
        raise RuntimeError("429 rate limit")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fail_call)
    planner = adapters.AuxiliaryObjectivePlanner(
        action_contracts=[],
        authority_conn=conn,
        resource_limits=resource_budget.DEFAULT_LIMITS,
        planner_call_compute_reservation_minor=10,
    )
    with pytest.raises(RuntimeError, match="rate limit"):
        planner.propose(
            {"id": objective.id},
            {"event_type": "objective.accepted"},
        )

    assert resource_budget.compute_reservation_posture(
        conn, organization_id
    )["unreconciled_count"] == 0
    reconciliation = conn.execute(
        "SELECT status,actual_minor,evidence_json "
        "FROM planner_compute_reconciliations"
    ).fetchone()
    assert reconciliation["status"] == "released"
    assert reconciliation["actual_minor"] == 0
    assert "llm_call_failed" in reconciliation["evidence_json"]


def test_unknown_employee_actor_cannot_propose_in_bootstrapped_org(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Actor Boundary Company",
        purpose="Reject unknown employee identities",
        profile_name="default",
        charter={},
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Test actor scope",
        originator="employee:ceo",
        permitted_systems=["web"],
    )
    objectives_db.transition_objective(conn, objective.id, "accepted", actor="employee:ceo")
    objectives_db.transition_objective(conn, objective.id, "planned", actor="employee:ceo")
    plan_id = objectives_db.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="employee:ceo",
    )
    conn.execute(
        """INSERT INTO employees (
               id,organization_id,profile_name,display_name,title,level,
               department_id,manager_id,status,employment_type,annual_cost_minor,
               currency,hired_for_objective_id,proposed_by,approved_by,created_at,
               updated_at,started_at,ended_at
           ) SELECT 'emp_duplicate_ceo',organization_id,'duplicate-ceo',
               display_name,title,'ceo',department_id,NULL,'active',employment_type,
               annual_cost_minor,currency,hired_for_objective_id,proposed_by,
               approved_by,created_at,updated_at,started_at,ended_at
             FROM employees WHERE id=?""",
        (ceo_id,),
    )
    with pytest.raises(objectives_db.ObjectiveStateError, match="not authorized"):
        objectives_db.propose_action(
            conn,
            objective_id=objective.id,
            plan_id=plan_id,
            action_type="web.read",
            payload={"system": "web", "target_resource": "public"},
            expected_outcome="read completes",
            required_capability="web.read",
            verification_method="web.read.completed",
            risk_class="low",
            reversible=True,
            proposed_by="employee:ceo",
        )
    with pytest.raises(objectives_db.ObjectiveStateError, match="not authorized"):
        objectives_db.propose_action(
            conn,
            objective_id=objective.id,
            plan_id=plan_id,
            action_type="web.read",
            payload={"system": "web", "target_resource": "public"},
            expected_outcome="read completes",
            required_capability="web.read",
            verification_method="web.read.completed",
            risk_class="low",
            reversible=True,
            proposed_by="employee:not-a-real-employee",
        )


def test_planner_cannot_invent_action_surface(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: response(
            """
            {"assumptions":[],"tasks":[],"dependencies":[],"risks":[],
             "actions":[{
               "action_type":"shell.unrestricted",
               "payload":{"system":"host"},
               "expected_outcome":"done",
               "required_capability":"root",
               "verification_method":"model.says_done",
               "risk_class":"critical",
               "reversible":false
             }]}
            """
        ),
    )
    planner = adapters.AuxiliaryObjectivePlanner(
        action_contracts=[
            adapters.RegisteredActionContract(
                "kanban.create_task",
                "work.delegate",
                "kanban",
                "kanban.task.created",
            )
        ],
    )
    with pytest.raises(ValueError, match="unavailable action type"):
        planner.propose({}, {"event_type": "test", "payload": {}})


def test_planner_cannot_relabel_payment_as_delegation(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: response(
            """
            {"assumptions":[],"tasks":[],"dependencies":[],"risks":[],
             "actions":[{
               "action_type":"payments.send",
               "payload":{"system":"payments"},
               "expected_outcome":"paid",
               "required_capability":"work.delegate",
               "verification_method":"payments.provider_readback",
               "risk_class":"low",
               "reversible":false
             }]}
            """
        ),
    )
    planner = adapters.AuxiliaryObjectivePlanner(
        action_contracts=[
            adapters.RegisteredActionContract(
                "payments.send",
                "payments.send",
                "payments",
                "payments.provider_readback",
            )
        ]
    )
    with pytest.raises(ValueError, match="capability does not match"):
        planner.propose({}, {"event_type": "test", "payload": {}})


def test_planner_receives_live_bounded_workforce_context(monkeypatch):
    captured = {}

    def call_llm(**kwargs):
        captured.update(kwargs)
        return response(
            """{"assumptions":[],"tasks":[],"dependencies":[],"risks":[],
                 "objective_complete_when_verified":false,"actions":[]}"""
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", call_llm)
    current = {
        "workforce": [
            {
                "employee_id": "emp_auditor",
                "profile_name": "security-auditor",
                "mandate": {"capabilities": ["security.audit"]},
            }
        ],
        "delegation_contract": {"credentials_included": False},
    }
    planner = adapters.AuxiliaryObjectivePlanner(
        action_contracts=[],
        context_provider=lambda: current,
    )

    planner.propose({"id": "obj_1"}, {"event_type": "staffing.changed"})

    payload = json.loads(captured["messages"][1]["content"])
    assert payload["operating_context"]["workforce"][0]["profile_name"] == (
        "security-auditor"
    )
    assert payload["operating_context"]["delegation_contract"] == {
        "credentials_included": False
    }
    assert "credentials" not in payload["operating_context"]["workforce"][0]


def test_unknown_executor_fails_closed():
    registry = adapters.ActionExecutorRegistry()
    outcome = registry.execute("shell.unrestricted", {"system": "host"})
    assert outcome.status == "failed"
    assert "no executor" in outcome.result["error"]


def test_executor_rechecks_autonomy_before_external_handler(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    operational_control.ensure_schema(conn)
    called = []
    registry = adapters.ActionExecutorRegistry(authority_conn=conn)
    registry.register(
        "test.external",
        lambda payload: (called.append(payload) or ExecutionOutcome("succeeded", {})),
        required_capability="test.external",
        target_system="test",
        verification_method="test.readback",
    )
    operational_control.set_autonomy_mode(
        conn, mode="paused", actor="advisor", reason="emergency stop"
    )
    outcome = registry.execute("test.external", {"system": "test"})
    assert outcome.status == "failed"
    assert "paused" in outcome.result["error"]
    assert called == []


def test_executor_contract_rejects_manually_injected_mismatched_authority():
    registry = adapters.ActionExecutorRegistry()
    registry.register(
        "payments.send",
        lambda payload: ExecutionOutcome("succeeded", {}),
        required_capability="payments.send",
        target_system="payments",
        verification_method="payments.provider_readback",
    )
    with pytest.raises(ValueError, match="capability"):
        registry.validate_proposal(
            ActionProposal(
                action_type="payments.send",
                payload={"system": "payments"},
                expected_outcome="paid",
                required_capability="work.delegate",
                verification_method="payments.provider_readback",
                risk_class="low",
                reversible=False,
            )
        )


def test_executor_contract_rejects_invalid_payload_before_permit_admission():
    registry = adapters.ActionExecutorRegistry()
    registry.register(
        "market.publish",
        lambda payload: ExecutionOutcome("succeeded", {}),
        contract=adapters.PayloadContract(
            required={"target_resource": str}, optional={}
        ),
        required_capability="market.publish",
        target_system="market",
        verification_method="market.readback",
    )
    with pytest.raises(ValueError, match="payload violates executor contract"):
        registry.validate_proposal(
            ActionProposal(
                action_type="market.publish",
                payload={"system": "market"},
                expected_outcome="published",
                required_capability="market.publish",
                verification_method="market.readback",
                risk_class="low",
                reversible=True,
            )
        )


def test_objective_verifier_requires_structured_registered_criteria():
    verifier = adapters.IndependentVerifierRegistry()
    action = VerificationOutcome("pass", {"ok": True})
    outcome = verifier.verify_objective(
        {"success_criteria": ["looks good"]},
        PlanProposal([], [], [], []),
        [action],
    )
    assert outcome.verdict == "inconclusive"
    assert "structured" in outcome.evidence["facts"]["error"]


def test_objective_verifier_checks_every_criterion():
    verifier = adapters.IndependentVerifierRegistry()
    verifier.register_objective(
        "metric.threshold",
        lambda snapshot, params: VerificationOutcome(
            "pass" if params["actual"] >= params["minimum"] else "fail",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="authoritative_database_readback",
                source_reference=f"metric:{params['actual']}",
                facts={"actual": params["actual"]},
            ),
        ),
    )
    outcome = verifier.verify_objective(
        {
            "success_criteria": [
                {"verifier": "metric.threshold", "params": {"actual": 10, "minimum": 5}},
                {"verifier": "metric.threshold", "params": {"actual": 3, "minimum": 5}},
            ]
        },
        PlanProposal([], [], [], []),
        [],
    )
    assert outcome.verdict == "fail"
    assert len(outcome.evidence["facts"]["criteria"]) == 2


def test_verifier_rejects_executor_echo_without_independent_evidence():
    verifier = adapters.IndependentVerifierRegistry()
    verifier.register_action(
        "echo",
        lambda action, execution: VerificationOutcome(
            "pass", {"result": execution.result}
        ),
    )
    outcome = verifier.verify(
        ActionProposal(
            action_type="test",
            payload={"system": "test"},
            expected_outcome="done",
            required_capability="test",
            verification_method="echo",
            risk_class="low",
            reversible=True,
        ),
        ExecutionOutcome("succeeded", {"claimed": "done"}),
    )
    assert outcome.verdict == "inconclusive"
    assert "invalid independent evidence" in outcome.evidence["facts"]["error"]


def test_verifier_rejects_tampered_fact_hash():
    verifier = adapters.IndependentVerifierRegistry()
    evidence = verification_evidence.build(
        observer=verifier.identity,
        source_kind="provider_readback",
        source_reference="provider:1",
        facts={"status": "succeeded"},
    )
    evidence["facts"]["status"] = "failed"
    verifier.register_action(
        "tampered", lambda action, execution: VerificationOutcome("pass", evidence)
    )
    outcome = verifier.verify(
        ActionProposal(
            action_type="test",
            payload={"system": "test"},
            expected_outcome="done",
            required_capability="test",
            verification_method="tampered",
            risk_class="low",
            reversible=True,
        ),
        ExecutionOutcome("succeeded", {}),
    )
    assert outcome.verdict == "inconclusive"
    assert "hash mismatch" in outcome.evidence["facts"]["error"]


def test_accounting_objective_verifiers_use_authoritative_ledger(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Ledger Company",
        purpose="Earn revenue",
        profile_name="default",
        charter={},
    )
    finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    accounting_db.post_journal(
        conn,
        organization_id=organization_id,
        description="Customer sale",
        source_type="test",
        source_id="sale-1",
        currency="USD",
        lines=(
            {"account_code": "1000", "debit_minor": 1500},
            {"account_code": "4000", "credit_minor": 1500},
        ),
        evidence={"provider_readback": "payment-1"},
    )
    executor = adapters.ActionExecutorRegistry(authority_conn=conn)
    verifier = adapters.IndependentVerifierRegistry()
    adapters.register_payment_adapters(
        executor, verifier, authority_conn=conn
    )
    outcome = verifier.verify_objective(
        {
            "id": "objective_1",
            "success_criteria": [
                {
                    "verifier": "accounting.revenue_at_least",
                    "params": {"amount_minor": 1000, "currency": "USD"},
                },
                {"verifier": "accounting.books_balanced", "params": {}},
            ],
        },
        PlanProposal([], [], [], []),
        [],
    )
    assert outcome.verdict == "pass"
    criteria = outcome.evidence["facts"]["criteria"]
    assert criteria[0]["evidence"]["facts"]["revenue_minor"] == 1500
    assert criteria[1]["evidence"]["facts"]["balanced"] is True


def test_workforce_adapters_evaluate_then_provision_from_recorded_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Adapter Company",
        purpose="Build secure software",
        profile_name="default",
        charter={},
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Complete a product security audit",
        originator="employee:ceo",
        permitted_systems=["organization"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    objectives_db.transition_objective(
        conn, objective.id, "planned", actor="employee:ceo"
    )
    for attempt in range(3):
        plan_id = objectives_db.create_plan(
            conn,
            objective.id,
            assumptions=[],
            tasks=[{"attempt": attempt + 1}],
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
        reason="security.audit capability unavailable",
    )
    executor = adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = adapters.IndependentVerifierRegistry()
    adapters.register_workforce_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config={"agentic": {"organization": {}}},
    )
    evaluation_payload = {
        "system": "organization",
        "target_resource": f"objective:{objective.id}:staffing",
        "idempotency_key": "evaluate-security-hire-0001",
        "case": {
            "missing_capability": "security.audit",
            "annual_cost_minor": 0,
            "expected_duration_cycles": 4,
            "scoped_deliverable": "independent security audit",
        },
    }
    evaluation = executor.execute_governed(
        "action-evaluate",
        objective.id,
        "organization.evaluate_hire",
        evaluation_payload,
    )
    evaluation_action = ActionProposal(
        action_type="organization.evaluate_hire",
        payload=evaluation_payload,
        expected_outcome="staffing decision recorded",
        required_capability="organization.hire.evaluate",
        verification_method="organization.hiring_decision.readback",
        risk_class="low",
        reversible=True,
    )

    assert evaluation.status == "succeeded"
    assert evaluation.result["verdict"] == "hire"
    assert evaluation.result["employment_class"] == "contractor"
    assert verifier.verify(evaluation_action, evaluation).verdict == "pass"

    materialize_payload = {
        "system": "organization",
        "target_resource": f"hiring:{evaluation.result['decision_id']}",
        "idempotency_key": "materialize-security-hire-0001",
        "decision_id": evaluation.result["decision_id"],
        "display_name": "Security Auditor",
        "title": "Contract Security Auditor",
        "level": "individual_contributor",
        "manager_employee_id": ceo_id,
        "profile_name": "security-auditor",
        "mandate": {
            "purpose": "Complete an independent security audit",
            "responsibilities": ["inspect product controls"],
            "decision_rights": ["report findings"],
            "prohibited_actions": ["security.deploy"],
            "capabilities": ["security.audit"],
            "systems": ["security"],
            "kpis": ["verified audit report"],
            "escalation": {"to": ceo_id},
            "toolsets": ["terminal"],
            "expires_at": int(time.time()) + 3_600,
        },
    }
    materialized = executor.execute_governed(
        "action-materialize",
        objective.id,
        "organization.materialize_hire",
        materialize_payload,
    )
    materialize_action = ActionProposal(
        action_type="organization.materialize_hire",
        payload=materialize_payload,
        expected_outcome="employee active",
        required_capability="organization.hire",
        verification_method="organization.employee_profile.readback",
        risk_class="high",
        reversible=False,
    )

    assert materialized.status == "succeeded"
    assert verifier.verify(materialize_action, materialized).verdict == "pass"
    employee = organization_db.get_employee_record(
        conn, materialized.result["employee_id"]
    )
    assert employee["status"] == "active"
    assert employee["manager_id"] == ceo_id


def test_planning_context_uses_live_available_capital_and_ledger_runway(
    tmp_path,
):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Budget Company",
        purpose="Operate within available capital",
        profile_name="default",
        charter={"finance": {"base_currency": "USD"}},
    )
    account_id = finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    finance_db.seed_initial_capital(
        conn,
        account_id=account_id,
        amount_minor=10_000,
        currency="USD",
        actor="human:operator",
    )
    finance_db.record_entry(
        conn,
        account_id=account_id,
        kind="payment",
        amount_minor=-1_000,
        currency="USD",
        idempotency_key="planning-context-expense-0001",
        evidence={"accounting": {"counter_account_code": "6300"}},
        external_reference="provider-payment-1",
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Procure a required capability",
        originator="employee:ceo",
        permitted_systems=["payments"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    objectives_db.transition_objective(
        conn, objective.id, "planned", actor="employee:ceo"
    )
    plan_id = objectives_db.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="employee:ceo",
    )
    action_id = objectives_db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="payments.send",
        payload={"system": "payments", "target_resource": "vendor:1"},
        expected_outcome="vendor paid",
        required_capability="payments.send",
        verification_method="payments.provider_readback",
        risk_class="medium",
        reversible=False,
        proposed_by="employee:ceo",
        estimated_cost_minor=2_500,
    )
    finance_db.reserve_budget(
        conn,
        account_id=account_id,
        objective_id=objective.id,
        action_id=action_id,
        amount_minor=2_500,
        currency="USD",
        expires_at=int(time.time()) + 300,
    )

    context = adapters.organization_planning_context(conn, organization_id)

    assert context["finance"]["treasury"] == [
        {
            "name": "operating",
            "currency": "USD",
            "balance_minor": 9_000,
            "reserved_minor": 2_500,
            "available_minor": 6_500,
        }
    ]
    assert context["finance"]["base_currency_available_minor"] == 6_500
    assert context["finance"]["recent_30_day_expenses_minor"] == 1_000
    assert context["finance"]["runway_days"] == 195
    assert context["finance"]["profit_and_loss"]["expenses_minor"] == 1_000
    assert context["procurement"]["preference_order"] == [
        "existing",
        "foss",
        "build",
        "buy",
        "defer",
    ]
    serialized = json.dumps(context, sort_keys=True)
    for forbidden in (
        "instrument_id",
        "registration_number",
        "external_reference",
        "provider-payment-1",
        "API_KEY",
    ):
        assert forbidden not in serialized


def test_procurement_adapter_verifies_decision_and_blocks_ungoverned_saas(
    tmp_path,
):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Procurement Adapter Company",
        purpose="Prefer FOSS and build before paid software",
        profile_name="default",
        charter={"finance": {"base_currency": "USD"}},
    )
    account_id = finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    finance_db.seed_initial_capital(
        conn,
        account_id=account_id,
        amount_minor=2_000,
        currency="USD",
        actor="human",
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Obtain required infrastructure software",
        originator="employee:ceo",
        permitted_systems=["procurement", "payments"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    executor = adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = adapters.IndependentVerifierRegistry()
    adapters.register_procurement_adapters(
        executor, verifier, authority_conn=conn
    )
    adapters.register_payment_adapters(
        executor, verifier, authority_conn=conn
    )
    procurement_payload = {
        "system": "procurement",
        "target_resource": "capability:managed-database",
        "idempotency_key": "procurement-adapter-decision-0001",
        "case": {
            "foss_fit": 0.1,
            "build_feasible": False,
            "paid_cost_minor": 500,
            "paid_required": True,
            "paid_expected_roi": 2,
            "persistent_need": True,
        },
        "source_evidence": {"reference": "vendor-comparison:v1"},
    }
    evaluated = executor.execute_governed(
        "action-procurement",
        objective.id,
        "procurement.evaluate",
        procurement_payload,
    )
    procurement_action = ActionProposal(
        action_type="procurement.evaluate",
        payload=procurement_payload,
        expected_outcome="buy decision recorded",
        required_capability="procurement.evaluate",
        verification_method="procurement.decision.readback",
        risk_class="low",
        reversible=True,
    )

    assert evaluated.status == "succeeded"
    assert evaluated.result["choice"] == "buy"
    assert verifier.verify(procurement_action, evaluated).verdict == "pass"

    ungoverned_payment = executor.execute_governed(
        "action-payment",
        objective.id,
        "payments.send",
        {
            "system": "payments",
            "target_resource": "vendor:database",
            "idempotency_key": "ungoverned-saas-payment-0001",
            "provider": "fake",
            "amount_minor": 500,
            "currency": "USD",
            "payee": {"provider_payee_id": "vendor-database"},
            "payee_jurisdiction": "US",
            "instrument_id": "instrument-token",
            "merchant_category": "saas",
            "payee_id": "vendor-database",
            "purpose": "managed database",
        },
    )
    assert ungoverned_payment.status == "failed"
    assert "requires a procurement decision" in ungoverned_payment.result["error"]


def test_accounting_actions_execute_and_verify_from_authoritative_records(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Books Company",
        purpose="Keep complete evidence-bound books",
        profile_name="default",
        charter={},
    )
    executor = adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = adapters.IndependentVerifierRegistry()
    adapters.register_accounting_adapters(
        executor, verifier, authority_conn=conn
    )

    period_payload = {
        "system": "accounting",
        "target_resource": "fiscal-period:2026-Q1",
        "idempotency_key": "accounting-open-2026-q1",
        "name": "2026-Q1",
        "starts_at": 100,
        "ends_at": 199,
        "evidence": {"calendar_reference": "board-calendar:v1"},
    }
    opened = executor.execute_governed(
        "action-open-period",
        "objective-books",
        "accounting.open_period",
        period_payload,
    )
    open_action = ActionProposal(
        action_type="accounting.open_period",
        payload=period_payload,
        expected_outcome="period opened",
        required_capability="accounting.manage_periods",
        verification_method="accounting.record.readback",
        risk_class="low",
        reversible=False,
    )
    assert opened.status == "succeeded"
    assert verifier.verify(open_action, opened).verdict == "pass"

    registration = accounting_db.configure_tax_registration(
        conn,
        organization_id=organization_id,
        jurisdiction="CA-ON",
        tax_type="sales",
        filing_frequency="quarterly",
        effective_from=100,
        evidence={"authority": "CRA"},
    )
    tax_payload = {
        "system": "accounting",
        "target_resource": f"tax-registration:{registration}:100:199",
        "idempotency_key": "tax-assessment-2026-q1",
        "registration_id": registration,
        "period_start": 100,
        "period_end": 199,
        "due_at": 250,
        "amount_minor": 0,
        "currency": "CAD",
        "evidence": {"workpaper_reference": "workpaper:2026-Q1"},
    }
    assessed = executor.execute_governed(
        "action-assess-tax",
        "objective-books",
        "accounting.assess_tax_obligation",
        tax_payload,
    )
    tax_action = ActionProposal(
        action_type="accounting.assess_tax_obligation",
        payload=tax_payload,
        expected_outcome="tax obligation assessed",
        required_capability="accounting.assess_tax",
        verification_method="accounting.record.readback",
        risk_class="medium",
        reversible=False,
    )
    assert assessed.status == "succeeded"
    assert verifier.verify(tax_action, assessed).verdict == "pass"

    filing_payload = {
        "system": "accounting",
        "target_resource": f"tax-obligation:{assessed.result['tax_obligation_id']}",
        "idempotency_key": "tax-file-2026-q1",
        "tax_obligation_id": assessed.result["tax_obligation_id"],
        "filed_at": 220,
        "evidence": {"authority_receipt": "CRA:filed:2026-Q1"},
    }
    filed = executor.execute_governed(
        "action-file-tax",
        "objective-books",
        "accounting.record_tax_filing",
        filing_payload,
    )
    filing_action = ActionProposal(
        action_type="accounting.record_tax_filing",
        payload=filing_payload,
        expected_outcome="tax filing recorded",
        required_capability="accounting.file_tax",
        verification_method="accounting.record.readback",
        risk_class="high",
        reversible=False,
    )
    assert filed.status == "succeeded"
    assert verifier.verify(filing_action, filed).verdict == "pass"

    payment_payload = {
        "system": "accounting",
        "target_resource": f"tax-obligation:{assessed.result['tax_obligation_id']}",
        "idempotency_key": "tax-payment-record-2026-q1",
        "tax_obligation_id": assessed.result["tax_obligation_id"],
        "paid_at": 225,
        "payment_intent_id": "not_required:zero_balance",
        "evidence": {"authority_balance": "zero"},
    }
    paid = executor.execute_governed(
        "action-record-tax-payment",
        "objective-books",
        "accounting.record_tax_payment",
        payment_payload,
    )
    payment_action = ActionProposal(
        action_type="accounting.record_tax_payment",
        payload=payment_payload,
        expected_outcome="tax payment recorded",
        required_capability="accounting.record_tax_payment",
        verification_method="accounting.record.readback",
        risk_class="high",
        reversible=False,
    )
    assert paid.status == "succeeded"
    assert verifier.verify(payment_action, paid).verdict == "pass"

    close_payload = {
        "system": "accounting",
        "target_resource": f"fiscal-period:{opened.result['period_id']}",
        "idempotency_key": "accounting-close-2026-q1",
        "period_id": opened.result["period_id"],
        "evidence": {"trial_balance_reference": "trial-balance:2026-Q1"},
    }
    closed = executor.execute_governed(
        "action-close-period",
        "objective-books",
        "accounting.close_period",
        close_payload,
    )
    close_action = ActionProposal(
        action_type="accounting.close_period",
        payload=close_payload,
        expected_outcome="period closed",
        required_capability="accounting.close_period",
        verification_method="accounting.record.readback",
        risk_class="high",
        reversible=False,
    )
    assert closed.status == "succeeded"
    assert verifier.verify(close_action, closed).verdict == "pass"
