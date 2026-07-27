"""Real state-machine proof across two independently triggered business cycles."""

import time

from hermes_cli import (
    compliance_db,
    finance_db,
    objective_adapters,
    objectives_db,
    organization_db,
    payment_controls,
    verification_evidence,
)
from hermes_cli.payments import PaymentRail, ProviderPayment
from hermes_cli.objective_runtime import (
    ActionProposal,
    ExecutionOutcome,
    ObjectiveRuntime,
    PlanProposal,
    VerificationOutcome,
)


class TwoCyclePlanner:
    identity = "employee:ceo"

    def propose(self, snapshot, event):
        if not snapshot["execution_results"]:
            return PlanProposal(
                assumptions=[],
                tasks=[{"step": "change external state"}],
                dependencies=[],
                risks=[],
                actions=(
                    ActionProposal(
                        action_type="market.publish",
                        payload={
                            "system": "market",
                            "target_resource": "offer:first",
                        },
                        expected_outcome="offer is externally visible",
                        required_capability="market.publish",
                        verification_method="market.readback",
                        risk_class="low",
                        reversible=True,
                    ),
                ),
                objective_complete_when_verified=False,
            )
        return PlanProposal(
            assumptions=["prior provider readback remains authoritative"],
            tasks=[],
            dependencies=[],
            risks=[],
            actions=(),
            objective_complete_when_verified=True,
        )


class ExternalMarket:
    identity = "employee:ceo"

    def __init__(self):
        self.offers = set()

    def execute(self, action_type, payload):
        self.offers.add(payload["target_resource"])
        return ExecutionOutcome(
            "succeeded",
            {"provider_readback": payload["target_resource"] in self.offers},
            external_reference="provider-offer-1",
        )


class MarketVerifier:
    identity = "control:market-verifier"

    def __init__(self, market):
        self.market = market

    def verify(self, action, execution):
        visible = action.payload["target_resource"] in self.market.offers
        return VerificationOutcome(
            "pass" if visible else "fail",
            verification_evidence.build(
                observer=self.identity,
                source_kind="provider_readback",
                source_reference=str(execution.external_reference),
                facts={"visible": visible},
            ),
        )

    def verify_objective(self, snapshot, plan, action_verifications):
        visible = "offer:first" in self.market.offers
        return VerificationOutcome(
            "pass" if visible else "fail",
            verification_evidence.build(
                observer=self.identity,
                source_kind="provider_readback",
                source_reference="market:offer:first",
                facts={"external_market_readback": visible},
            ),
        )


def test_event_driven_business_replans_then_verifies_and_preserves_evidence(tmp_path):
    conn = objectives_db.connect(tmp_path / "business.db")
    charter = {
        "enabled": True,
        "operating_cadence": {"enabled": False},
        "operating_mode": "autonomous",
        "operator_role": "advisor",
        "policy_version": "charter-v1",
        "allowed_capabilities": ["market.publish"],
        "forbidden_capabilities": [],
        "allowed_systems": ["market"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "low",
        "allow_irreversible": False,
        "max_action_spend_minor": 0,
        "permit_ttl_seconds": 300,
        "finance": {"base_currency": "USD"},
    }
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Autonomous Company",
        purpose="Operate a verified market offer",
        profile_name="default",
        charter=charter,
    )
    account = finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    finance_db.seed_initial_capital(
        conn, account_id=account, amount_minor=1000, currency="USD", actor="human"
    )
    objective = objectives_db.create_objective(
        conn,
        desired_outcome="Publish and verify the first market offer",
        originator="initial_setup",
        permitted_systems=["market"],
        success_criteria=[
            {"verifier": "market.external_offer_visible", "params": {"offer": "first"}}
        ],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="initial_setup"
    )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="objective.accepted",
        payload={},
        dedupe_key=f"objective.accepted:{objective.id}",
    )
    market = ExternalMarket()
    runtime = ObjectiveRuntime(
        conn,
        planner=TwoCyclePlanner(),
        executor=market,
        verifier=MarketVerifier(market),
        charter=charter,
        policy_version="charter-v1",
        runtime_id="runtime:e2e",
    )

    first = runtime.tick()
    assert first.status == "progressed"
    assert objectives_db.get_objective(conn, objective.id).status == "planned"
    second = runtime.tick()
    assert second.status == "verified"
    assert objectives_db.get_objective(conn, objective.id).status == "verified"

    snapshot = objectives_db.objective_snapshot(conn, objective.id)
    assert [plan["version"] for plan in snapshot["plans"]] == [1, 2]
    assert snapshot["execution_results"][0]["external_reference"] == "provider-offer-1"
    assert snapshot["verifications"][-1]["verdict"] == "pass"
    assert conn.execute("SELECT COUNT(*) FROM kya_events").fetchone()[0] == 1
    assert runtime.tick().status == "idle"

    foreign_organization = organization_db.create_organization(
        conn, name="Foreign Tenant", purpose="Must remain isolated"
    )
    foreign_objective = objectives_db.create_objective(
        conn,
        desired_outcome="Modify the active company's market",
        originator="foreign",
        organization_id=foreign_organization,
        permitted_systems=["market"],
    )
    objectives_db.transition_objective(
        conn, foreign_objective.id, "accepted", actor="foreign"
    )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=foreign_objective.id,
        event_type="objective.accepted",
        payload={},
    )
    isolated = runtime.tick()
    assert isolated.status == "idle"
    foreign_event = conn.execute(
        """SELECT status,attempts,claimed_by FROM objective_inbox
           WHERE objective_id=?""",
        (foreign_objective.id,),
    ).fetchone()
    assert tuple(foreign_event) == ("pending", 0, None)
    assert objectives_db.get_objective(conn, foreign_objective.id).status == "accepted"
    assert market.offers == {"offer:first"}


def test_ceo_evaluates_then_hires_worker_across_verified_cycles(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    conn = objectives_db.connect(tmp_path / "workforce.db")
    charter = {
        "enabled": True,
        "operating_mode": "autonomous",
        "policy_version": "charter-v1",
        "allowed_capabilities": [
            "organization.hire.evaluate",
            "organization.hire",
        ],
        "forbidden_capabilities": [],
        "allowed_systems": ["organization"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "high",
        "allow_irreversible": True,
        "max_action_spend_minor": 0,
        "permit_ttl_seconds": 300,
        "organization": {},
    }
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Workforce E2E Company",
        purpose="Add workers only from evidence",
        profile_name="default",
        charter=charter,
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Complete a product security audit",
        originator="employee:ceo",
        permitted_systems=["organization", "kanban", "security"],
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
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="staffing.review",
        payload={},
    )

    executor = objective_adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = objective_adapters.IndependentVerifierRegistry()
    objective_adapters.register_workforce_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config={"agentic": charter},
    )

    class WorkforcePlanner:
        identity = f"employee:{ceo_id}"

        def propose(self, snapshot, event):
            if not snapshot["execution_results"]:
                return PlanProposal(
                    assumptions=[],
                    tasks=[{"step": "evaluate staffing evidence"}],
                    dependencies=[],
                    risks=[],
                    actions=(
                        ActionProposal(
                            action_type="organization.evaluate_hire",
                            payload={
                                "system": "organization",
                                "target_resource": f"objective:{objective.id}:staffing",
                                "idempotency_key": "workforce-evaluation-e2e-0001",
                                "case": {
                                    "missing_capability": "security.audit",
                                    "annual_cost_minor": 0,
                                    "expected_duration_cycles": 4,
                                    "scoped_deliverable": "security audit",
                                },
                            },
                            expected_outcome="staffing decision recorded",
                            required_capability="organization.hire.evaluate",
                            verification_method=(
                                "organization.hiring_decision.readback"
                            ),
                            risk_class="low",
                            reversible=True,
                        ),
                    ),
                    objective_complete_when_verified=False,
                )
            decision = conn.execute(
                """SELECT id FROM hiring_decisions
                   WHERE organization_id=? ORDER BY created_at,id LIMIT 1""",
                (organization_id,),
            ).fetchone()
            return PlanProposal(
                assumptions=[],
                tasks=[{"step": "materialize warranted worker"}],
                dependencies=[decision["id"]],
                risks=[],
                actions=(
                    ActionProposal(
                        action_type="organization.materialize_hire",
                        payload={
                            "system": "organization",
                            "target_resource": f"hiring:{decision['id']}",
                            "idempotency_key": "workforce-materialization-e2e-0001",
                            "decision_id": decision["id"],
                            "display_name": "Security Auditor",
                            "title": "Contract Security Auditor",
                            "level": "individual_contributor",
                            "manager_employee_id": ceo_id,
                            "profile_name": "security-auditor",
                            "mandate": {
                                "purpose": "Complete the security audit",
                                "responsibilities": ["inspect controls"],
                                "decision_rights": ["report findings"],
                                "prohibited_actions": ["security.deploy"],
                                "capabilities": ["security.audit"],
                                "systems": ["security"],
                                "kpis": ["verified audit"],
                                "escalation": {"to": ceo_id},
                                "toolsets": ["terminal"],
                                "skills": ["security.audit"],
                                "budget_minor": 100,
                                "expires_at": int(time.time()) + 3_600,
                            },
                        },
                        expected_outcome="worker is active",
                        required_capability="organization.hire",
                        verification_method=(
                            "organization.employee_profile.readback"
                        ),
                        risk_class="high",
                        reversible=False,
                    ),
                ),
                objective_complete_when_verified=False,
            )

    runtime = ObjectiveRuntime(
        conn,
        planner=WorkforcePlanner(),
        executor=executor,
        verifier=verifier,
        charter=charter,
        policy_version="charter-v1",
        runtime_id="runtime:workforce-e2e",
    )

    first = runtime.tick()
    second = runtime.tick()

    assert first.status == "progressed"
    assert second.status == "progressed"
    employees = conn.execute(
        """SELECT * FROM employees WHERE organization_id=? AND level!='ceo'""",
        (organization_id,),
    ).fetchall()
    assert len(employees) == 1
    assert employees[0]["status"] == "active"
    assert employees[0]["manager_id"] == ceo_id
    assert conn.execute(
        "SELECT COUNT(*) FROM verification_records WHERE verdict='pass'"
    ).fetchone()[0] == 2

    planning_context = objective_adapters.organization_planning_context(
        conn, organization_id
    )
    worker_context = next(
        item
        for item in planning_context["workforce"]
        if item["profile_name"] == "security-auditor"
    )
    assert worker_context["manager_employee_id"] == ceo_id
    assert worker_context["mandate"]["capabilities"] == ["security.audit"]
    assert planning_context["delegation_contract"]["credentials_included"] is False

    objective_adapters.register_kanban_adapters(
        executor,
        verifier,
        board="workforce-e2e",
        authority_conn=conn,
        manager_employee_id=ceo_id,
    )
    delegation_payload = {
        "system": "kanban",
        "target_resource": "workforce-e2e",
        "idempotency_key": "delegate-security-audit-e2e-0001",
        "title": "Complete security audit",
        "body": "Inspect product controls and return evidence.",
        "assignee": "security-auditor",
        "skills": ["security.audit"],
        "task_capabilities": ["security.audit"],
        "task_systems": ["security"],
        "task_toolsets": ["terminal"],
        "task_budget_minor": 100,
        "task_expires_at": int(time.time()) + 1_800,
    }
    current_plan = conn.execute(
        """SELECT id FROM plans WHERE objective_id=?
           ORDER BY version DESC LIMIT 1""",
        (objective.id,),
    ).fetchone()["id"]
    delegation_action_id = objectives_db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=current_plan,
        action_type="kanban.create_task",
        payload=delegation_payload,
        expected_outcome="task assigned to security auditor",
        required_capability="work.delegate",
        verification_method="kanban.task.created",
        risk_class="low",
        reversible=True,
        proposed_by=f"employee:{ceo_id}",
    )
    delegated = executor.execute_governed(
        delegation_action_id,
        objective.id,
        "kanban.create_task",
        delegation_payload,
    )
    delegation_action = ActionProposal(
        action_type="kanban.create_task",
        payload=delegation_payload,
        expected_outcome="task assigned to security auditor",
        required_capability="work.delegate",
        verification_method="kanban.task.created",
        risk_class="low",
        reversible=True,
    )
    assert delegated.status == "succeeded"
    assert verifier.verify(delegation_action, delegated).verdict == "pass"


def test_ceo_evaluates_procurement_before_autonomous_software_payment(
    tmp_path, monkeypatch
):
    class SoftwareRail(PaymentRail):
        name = "software-rail"

        def __init__(self):
            self.payments = {}

        def create_receivable(self, **kwargs):
            raise NotImplementedError

        def send_payment(self, **kwargs):
            payment = ProviderPayment(
                "software-payment-1",
                "succeeded",
                kwargs["amount_minor"],
                kwargs["currency"],
                evidence={"provider_readback": True},
            )
            self.payments[payment.reference] = payment
            return payment

        def get_payment(self, reference):
            return self.payments[reference]

    rail = SoftwareRail()
    monkeypatch.setattr(
        "hermes_cli.payments.load_outbound_payment_rails",
        lambda: {"software-rail": rail},
    )
    conn = objectives_db.connect(tmp_path / "procurement.db")
    charter = {
        "enabled": True,
        "operating_mode": "autonomous",
        "policy_version": "charter-v1",
        "allowed_capabilities": ["procurement.evaluate", "payments.send"],
        "forbidden_capabilities": [],
        "allowed_systems": ["procurement", "payments"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "high",
        "allow_irreversible": True,
        "max_action_spend_minor": 500,
        "permit_ttl_seconds": 300,
        "finance": {"base_currency": "USD"},
    }
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Procurement E2E Company",
        purpose="Purchase only after governed comparison",
        profile_name="default",
        charter=charter,
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
    compliance_db.configure_profile(
        conn,
        organization_id=organization_id,
        legal_entity_type="corporation",
        home_jurisdiction="CA-ON",
    )
    compliance_db.verify_payment_provider(
        conn,
        organization_id=organization_id,
        provider="software-rail",
        direction="outbound",
        jurisdiction="GLOBAL",
        registry_authority="test-registry",
        registry_reference="software-rail-outbound",
        aml_screening_delegated=True,
        sanctions_screening_delegated=True,
        verified_at=int(time.time()) - 1,
        expires_at=int(time.time()) + 3_600,
        evidence={"test": True},
    )
    instrument_id = payment_controls.register_tokenized_instrument(
        conn,
        organization_id=organization_id,
        provider="software-rail",
        provider_instrument_id="provider-token-1",
        rail_type="virtual_card",
        currency="USD",
        label="Software purchases",
    )
    payment_controls.set_spend_controls(
        conn,
        instrument_id=instrument_id,
        max_transaction_minor=500,
        max_daily_minor=500,
        allowed_merchant_categories=["saas"],
        allowed_payees=["database-vendor"],
        policy_version="charter-v1",
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Acquire managed database service",
        originator="employee:ceo",
        permitted_systems=["procurement", "payments"],
        max_spend_minor=500,
        currency="USD",
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="capability.required",
        payload={"capability": "managed_database"},
    )
    executor = objective_adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = objective_adapters.IndependentVerifierRegistry()
    objective_adapters.register_procurement_adapters(
        executor, verifier, authority_conn=conn
    )
    objective_adapters.register_payment_adapters(
        executor, verifier, authority_conn=conn
    )

    class ProcurementPlanner:
        identity = f"employee:{ceo_id}"

        def propose(self, snapshot, event):
            if not snapshot["execution_results"]:
                return PlanProposal(
                    assumptions=[],
                    tasks=[{"step": "compare existing, FOSS, build, and buy"}],
                    dependencies=[],
                    risks=[],
                    actions=(
                        ActionProposal(
                            action_type="procurement.evaluate",
                            payload={
                                "system": "procurement",
                                "target_resource": "capability:managed-database",
                                "idempotency_key": "procurement-e2e-decision-0001",
                                "case": {
                                    "existing_capability_sufficient": False,
                                    "foss_fit": 0.2,
                                    "build_feasible": False,
                                    "paid_cost_minor": 500,
                                    "paid_required": True,
                                    "paid_expected_roi": 2,
                                    "persistent_need": True,
                                },
                                "source_evidence": {
                                    "reference": "option-analysis:v1"
                                },
                            },
                            expected_outcome="procurement decision recorded",
                            required_capability="procurement.evaluate",
                            verification_method="procurement.decision.readback",
                            risk_class="low",
                            reversible=True,
                        ),
                    ),
                    objective_complete_when_verified=False,
                )
            decision = conn.execute(
                """SELECT id FROM procurement_decisions
                   WHERE objective_id=? ORDER BY created_at,id LIMIT 1""",
                (objective.id,),
            ).fetchone()
            return PlanProposal(
                assumptions=[],
                tasks=[{"step": "purchase exact approved option"}],
                dependencies=[decision["id"]],
                risks=[],
                actions=(
                    ActionProposal(
                        action_type="payments.send",
                        payload={
                            "system": "payments",
                            "target_resource": "vendor:database-vendor",
                            "idempotency_key": "procurement-e2e-payment-0001",
                            "provider": "software-rail",
                            "amount_minor": 500,
                            "currency": "USD",
                            "payee": {
                                "provider_payee_id": "database-vendor"
                            },
                            "payee_jurisdiction": "US",
                            "instrument_id": instrument_id,
                            "merchant_category": "saas",
                            "payee_id": "database-vendor",
                            "purpose": "managed database subscription",
                            "procurement_decision_id": decision["id"],
                        },
                        expected_outcome="software vendor paid",
                        required_capability="payments.send",
                        verification_method="payments.provider_readback",
                        risk_class="high",
                        reversible=False,
                        estimated_cost_minor=500,
                    ),
                ),
                objective_complete_when_verified=False,
            )

    runtime = ObjectiveRuntime(
        conn,
        planner=ProcurementPlanner(),
        executor=executor,
        verifier=verifier,
        charter=charter,
        policy_version="charter-v1",
        runtime_id="runtime:procurement-e2e",
    )

    first = runtime.tick()
    second = runtime.tick()

    assert first.status == "progressed"
    assert second.status == "progressed"
    decision = conn.execute("SELECT * FROM procurement_decisions").fetchone()
    assert decision["choice"] == "buy"
    assert decision["available_budget_minor"] == 2_000
    commitment = conn.execute(
        "SELECT * FROM procurement_commitments"
    ).fetchone()
    assert commitment["decision_id"] == decision["id"]
    assert commitment["amount_minor"] == 500
    assert finance_db.account_balance(conn, account_id) == 1_500


def test_ceo_decomposes_parent_into_governed_child_objective(tmp_path):
    conn = objectives_db.connect(tmp_path / "portfolio.db")
    charter = {
        "enabled": True,
        "operating_mode": "autonomous",
        "policy_version": "charter-v1",
        "allowed_capabilities": ["objectives.create", "objectives.cancel"],
        "forbidden_capabilities": [],
        "allowed_systems": ["objectives"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "medium",
        "allow_irreversible": False,
        "max_action_spend_minor": 0,
        "permit_ttl_seconds": 300,
        "organization": {"max_active_objectives": 5},
        "security": {"require_compensation_for_reversible_actions": True},
    }
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Portfolio E2E Company",
        purpose="Decompose durable business work",
        profile_name="default",
        charter=charter,
    )
    parent = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Launch a verified product",
        originator="employee:ceo",
        permitted_systems=["objectives", "kanban"],
        prohibited_actions=["data.delete"],
        max_spend_minor=1_000,
        currency="USD",
        expires_at=int(time.time()) + 7_200,
    )
    objectives_db.transition_objective(
        conn, parent.id, "accepted", actor="employee:ceo"
    )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=parent.id,
        event_type="strategy.decomposition.required",
        payload={},
    )
    executor = objective_adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = objective_adapters.IndependentVerifierRegistry()
    verifier.register_objective(
        "portfolio.child_complete",
        lambda snapshot, params: VerificationOutcome(
            "inconclusive",
            verification_evidence.build(
                observer=verifier.identity,
                source_kind="deterministic_check",
                source_reference=f"child:{snapshot['id']}",
                facts={"complete": False},
            ),
        ),
    )
    objective_adapters.register_portfolio_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config={"agentic": charter},
    )
    creation_key = "portfolio-e2e-child-creation-0001"

    class PortfolioPlanner:
        identity = f"employee:{ceo_id}"

        def propose(self, snapshot, event):
            return PlanProposal(
                assumptions=[],
                tasks=[{"step": "run launch research as a durable workstream"}],
                dependencies=[],
                risks=[],
                actions=(
                    ActionProposal(
                        action_type="objectives.create_child",
                        payload={
                            "system": "objectives",
                            "target_resource": f"objective:{parent.id}:children",
                            "idempotency_key": creation_key,
                            "desired_outcome": "Complete launch research",
                            "success_criteria": [
                                {
                                    "verifier": "portfolio.child_complete",
                                    "params": {},
                                }
                            ],
                            "termination_conditions": [
                                "research invalidates launch"
                            ],
                            "permitted_systems": ["kanban"],
                            "prohibited_actions": [
                                "data.delete",
                                "external.publish",
                            ],
                            "constraints": ["use public evidence"],
                            "allocated_budget_minor": 400,
                            "currency": "USD",
                            "expires_at": int(time.time()) + 3_600,
                        },
                        expected_outcome="child objective accepted and awake",
                        required_capability="objectives.create",
                        verification_method="objectives.child.readback",
                        risk_class="medium",
                        reversible=True,
                        compensation={
                            "action_type": "objectives.cancel_child",
                            "payload": {
                                "system": "objectives",
                                "target_resource": (
                                    f"objective:{parent.id}:children"
                                ),
                                "idempotency_key": (
                                    "portfolio-e2e-child-cancel-0001"
                                ),
                                "creation_idempotency_key": creation_key,
                            },
                            "required_capability": "objectives.cancel",
                            "verification_method": (
                                "objectives.cancellation.readback"
                            ),
                        },
                    ),
                ),
                objective_complete_when_verified=False,
            )

    runtime = ObjectiveRuntime(
        conn,
        planner=PortfolioPlanner(),
        executor=executor,
        verifier=verifier,
        charter=charter,
        policy_version="charter-v1",
        runtime_id="runtime:portfolio-e2e",
    )

    outcome = runtime.tick()

    assert outcome.status == "progressed"
    relationship = conn.execute(
        "SELECT * FROM objective_relationships"
    ).fetchone()
    child = objectives_db.objective_to_dict(
        conn, relationship["child_objective_id"]
    )
    assert child["status"] == "accepted"
    assert child["max_spend_minor"] == 400
    assert child["permitted_systems"] == ["kanban"]
    assert conn.execute(
        """SELECT COUNT(*) FROM objective_inbox
           WHERE objective_id=? AND status='pending'""",
        (child["id"],),
    ).fetchone()[0] == 1
    verification = conn.execute(
        """SELECT verdict FROM verification_records
           WHERE action_id=?""",
        (outcome.action_ids[0],),
    ).fetchone()
    assert verification["verdict"] == "pass"
    context = objective_adapters.organization_planning_context(
        conn, organization_id
    )
    assert context["portfolio"]["relationships"] == [
        {
            "parent_objective_id": parent.id,
            "child_objective_id": child["id"],
            "relationship": "decomposes_to",
            "allocated_budget_minor": 400,
            "currency": "USD",
        }
    ]


def test_ceo_opens_accounting_period_through_governed_runtime(tmp_path):
    conn = objectives_db.connect(tmp_path / "accounting-runtime.db")
    charter = {
        "enabled": True,
        "operating_cadence": {"enabled": False},
        "operating_mode": "autonomous",
        "operator_role": "advisor",
        "policy_version": "charter-v1",
        "allowed_capabilities": ["accounting.manage_periods"],
        "forbidden_capabilities": [],
        "allowed_systems": ["accounting"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "low",
        "allow_irreversible": True,
        "max_action_spend_minor": 0,
        "permit_ttl_seconds": 300,
    }
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Accounting Runtime Company",
        purpose="Operate evidence-bound books",
        profile_name="default",
        charter=charter,
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Open the first fiscal accounting period",
        originator=f"employee:{ceo_id}",
        permitted_systems=["accounting"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor=f"employee:{ceo_id}"
    )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="accounting.period.required",
        payload={
            "calendar_evidence": {
                "source": "incorporation-record",
                "reference": "calendar:2026-Q1",
            }
        },
    )
    executor = objective_adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = objective_adapters.IndependentVerifierRegistry()
    objective_adapters.register_accounting_adapters(
        executor, verifier, authority_conn=conn
    )

    class AccountingPlanner:
        identity = f"employee:{ceo_id}"

        def propose(self, snapshot, event):
            return PlanProposal(
                assumptions=[],
                tasks=[{"step": "open fiscal period"}],
                dependencies=[],
                risks=[],
                actions=(
                    ActionProposal(
                        action_type="accounting.open_period",
                        payload={
                            "system": "accounting",
                            "target_resource": "fiscal-period:2026-Q1",
                            "idempotency_key": "accounting-runtime-open-2026-q1",
                            "name": "2026-Q1",
                            "starts_at": 100,
                            "ends_at": 199,
                            "evidence": event["payload"]["calendar_evidence"],
                        },
                        expected_outcome="fiscal period is open",
                        required_capability="accounting.manage_periods",
                        verification_method="accounting.record.readback",
                        risk_class="low",
                        reversible=False,
                    ),
                ),
            )

    runtime = ObjectiveRuntime(
        conn,
        planner=AccountingPlanner(),
        executor=executor,
        verifier=verifier,
        charter=charter,
        policy_version="charter-v1",
        runtime_id="runtime:accounting-e2e",
    )
    outcome = runtime.tick()

    assert outcome.status == "progressed"
    period = conn.execute(
        """SELECT * FROM fiscal_periods
           WHERE organization_id=? AND name='2026-Q1'""",
        (organization_id,),
    ).fetchone()
    assert period["status"] == "open"
    verification = conn.execute(
        """SELECT verdict,evidence_json FROM verification_records
           WHERE action_id=?""",
        (outcome.action_ids[0],),
    ).fetchone()
    assert verification["verdict"] == "pass"
    assert "fiscal-period:" in verification["evidence_json"]
