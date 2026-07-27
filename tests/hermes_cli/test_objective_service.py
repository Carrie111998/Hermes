from __future__ import annotations

from hermes_cli import (
    business_security,
    finance_db,
    objective_service,
    operational_control,
    organization_db,
)
from hermes_cli import objectives_db as db


def test_disabled_service_does_not_open_runtime_store(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"agentic": {"enabled": False}}
    )
    outcome = objective_service.tick_once()
    assert outcome.status == "disabled"


def test_security_block_creates_deduplicated_advisor_handoff(tmp_path, monkeypatch):
    database = tmp_path / "security-readiness.db"
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agentic": {"enabled": True}},
    )

    first = objective_service.tick_once(db_path=database)
    second = objective_service.tick_once(db_path=database)

    assert first.status == second.status == "security_blocked"
    conn = db.connect(database)
    interventions = operational_control.list_interventions(conn)
    assert len(interventions) == 1
    assert interventions[0]["category"] == "security_readiness_blocked"
    assert interventions[0]["context"]["authority_boundary"] == "No action was attempted"
    assert interventions[0]["context"]["violations"]


def test_missing_ceo_authority_creates_deduplicated_advisor_handoff(
    tmp_path, monkeypatch
):
    database = tmp_path / "readiness.db"
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agentic": {"enabled": True}},
    )
    monkeypatch.setattr(
        "hermes_cli.business_security.evaluate_security_readiness",
        lambda _config: business_security.SecurityReadiness(True, ()),
    )

    first = objective_service.tick_once(db_path=database)
    second = objective_service.tick_once(db_path=database)

    assert first.status == second.status == "configuration_blocked"
    with db.connect(database) as conn:
        interventions = operational_control.list_interventions(conn)
    assert len(interventions) == 1
    assert interventions[0]["category"] == "ceo_authority_missing"
    assert {option["id"] for option in interventions[0]["options"]} == {
        "bootstrap",
        "repair",
        "manual",
    }


def test_runtime_build_exposes_only_registered_actions(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    config = {
        "agentic": {
            "enabled": True,
            "allowed_capabilities": ["work.delegate"],
            "allowed_systems": ["kanban"],
            "policy_version": "test",
            "permit_ttl_seconds": 30,
            "operating_mode": "autonomous",
            "forbidden_capabilities": [],
            "approval_required_capabilities": [],
            "max_autonomous_risk": "low",
            "allow_irreversible": False,
            "max_action_spend_minor": 0,
        },
        "auxiliary": {"objective_planner": {"timeout": 10}},
    }
    runtime = objective_service.build_runtime(conn, config, board="default")
    assert runtime.planner.action_types == ["kanban.create_task"]
    assert runtime.planner.capabilities == ["work.delegate"]
    assert runtime.planner.systems == ["kanban"]
    assert runtime.planner.verification_methods == ["kanban.task.created"]
    conn.close()


def test_runtime_filters_actions_by_exact_charter_contract(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    runtime = objective_service.build_runtime(
        conn,
        {
            "agentic": {
                "allowed_capabilities": ["work.delegate"],
                "allowed_systems": ["payments"],
            }
        },
        board="default",
    )
    assert runtime.planner.action_types == []
    assert runtime.planner.action_contracts == {}
    assert runtime.unavailable_allowed_capabilities == ("work.delegate",)
    conn.close()


def test_runtime_exposes_workforce_actions_only_under_exact_charter(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Workforce Company",
        purpose="Scale only when warranted",
        profile_name="default",
        charter={},
    )
    runtime = objective_service.build_runtime(
        conn,
        {
            "agentic": {
                "allowed_capabilities": [
                    "organization.hire.evaluate",
                    "organization.hire",
                ],
                "allowed_systems": ["organization"],
                "organization": {},
            }
        },
        board="default",
    )

    assert runtime.planner.action_types == [
        "organization.evaluate_hire",
        "organization.materialize_hire",
    ]
    assert runtime.planner.verification_methods == [
        "organization.employee_profile.readback",
        "organization.hiring_decision.readback",
    ]
    assert runtime.unavailable_allowed_capabilities == ()
    conn.close()


def test_runtime_exposes_procurement_evaluation_under_exact_charter(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Procurement Company",
        purpose="Evaluate before buying",
        profile_name="default",
        charter={},
    )
    finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    runtime = objective_service.build_runtime(
        conn,
        {
            "agentic": {
                "allowed_capabilities": ["procurement.evaluate"],
                "allowed_systems": ["procurement"],
            }
        },
        board="default",
    )

    assert runtime.planner.action_types == ["procurement.evaluate"]
    assert runtime.planner.verification_methods == [
        "procurement.decision.readback"
    ]


def test_runtime_exposes_accounting_lifecycle_only_under_exact_charter(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Accounting Company",
        purpose="Maintain complete books",
        profile_name="default",
        charter={},
    )
    runtime = objective_service.build_runtime(
        conn,
        {
            "agentic": {
                "allowed_capabilities": [
                    "accounting.manage_periods",
                    "accounting.assess_tax",
                    "accounting.close_period",
                ],
                "allowed_systems": ["accounting"],
            }
        },
        board="default",
    )

    assert runtime.planner.action_types == [
        "accounting.assess_tax_obligation",
        "accounting.close_period",
        "accounting.open_period",
    ]
    assert runtime.planner.verification_methods == ["accounting.record.readback"]
    assert runtime.unavailable_allowed_capabilities == ()
    conn.close()
    assert runtime.unavailable_allowed_capabilities == ()
    conn.close()


def test_runtime_readiness_rejects_unverifiable_active_objective(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Unreachable Company",
        purpose="Test readiness",
        profile_name="default",
        charter={},
    )
    finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Become successful",
        originator="legacy-setup",
        success_criteria=["the business looks successful"],
    )
    runtime = objective_service.build_runtime(
        conn,
        {
            "agentic": {
                "allowed_capabilities": ["work.delegate"],
                "allowed_systems": ["kanban"],
            }
        },
        board="default",
    )
    assert runtime.unreachable_objectives[0]["reason"].startswith(
        "unregistered or malformed"
    )
    conn.close()


def test_runtime_readiness_ignores_unverifiable_foreign_objective(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    active_organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Active Company",
        purpose="Operate autonomously",
        profile_name="default",
        charter={},
    )
    finance_db.create_treasury_account(
        conn, organization_id=active_organization_id, currency="USD"
    )
    db.create_objective(
        conn,
        organization_id=active_organization_id,
        desired_outcome="Keep the books balanced",
        originator="initial-setup",
        success_criteria=[
            {"verifier": "accounting.books_balanced", "params": {}}
        ],
    )
    foreign_organization_id = organization_db.create_organization(
        conn,
        name="Foreign Company",
        purpose="Remain isolated",
    )
    db.create_objective(
        conn,
        organization_id=foreign_organization_id,
        desired_outcome="Use a legacy criterion",
        originator="legacy-setup",
        success_criteria=["the business looks successful"],
    )

    runtime = objective_service.build_runtime(
        conn,
        {
            "agentic": {
                "allowed_capabilities": ["work.delegate"],
                "allowed_systems": ["kanban"],
            }
        },
        board="default",
    )

    assert runtime.unreachable_objectives == ()
    conn.close()
