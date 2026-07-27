import time

import pytest

from hermes_cli import objective_service, objectives_db
from hermes_cli.objective_runtime import PlanProposal
from hermes_cli.setup import _bootstrap_agentic_business


def _charter():
    return {
        "operator_role": "advisor",
        "allowed_capabilities": ["market.publish"],
        "allowed_systems": ["market"],
        "forbidden_capabilities": ["company.delete"],
        "max_autonomous_risk": "low",
        "max_action_spend_minor": 1000,
        "finance": {
            "base_currency": "USD",
            "initial_capital_minor": 1000,
            "tax_profile": {
                "legal_entity_type": "unconfigured",
                "jurisdictions": [],
            },
        },
        "initial_mandate": {
            "organization_name": "Bootstrap Company",
            "purpose": "Earn sustainable verified revenue",
            "desired_outcome": "Earn first verified customer revenue",
            "success_criteria": [
                {
                    "verifier": "accounting.revenue_at_least",
                    "params": {"amount_minor": 1000, "currency": "USD"},
                },
                {"verifier": "accounting.books_balanced", "params": {}},
            ],
            "termination_conditions": ["capital exhausted", "objective expires"],
            "duration_days": 90,
        },
    }


def test_bootstrap_is_resumable_and_starts_one_governed_objective(
    tmp_path, monkeypatch
):
    path = tmp_path / "authority.db"
    monkeypatch.setattr("hermes_cli.objectives_db.objectives_db_path", lambda: path)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "bootstrap-test"
    )
    before = int(time.time())
    first = _bootstrap_agentic_business(_charter())
    second = _bootstrap_agentic_business(_charter())
    assert first == second

    conn = objectives_db.connect(path)
    objective = objectives_db.objective_to_dict(conn, first[1])
    assert objective["organization_id"] == first[0]
    assert objective["status"] == "accepted"
    assert objective["max_spend_minor"] == 1000
    assert objective["success_criteria"] == [
        {
            "verifier": "accounting.revenue_at_least",
            "params": {"amount_minor": 1000, "currency": "USD"},
        },
        {"verifier": "accounting.books_balanced", "params": {}},
    ]
    assert objective["termination"] == ["capital exhausted", "objective expires"]
    assert objective["expires_at"] >= before + 90 * 86400
    assert conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM treasury_entries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM objectives").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM objective_inbox").fetchone()[0] == 1
    schedule = conn.execute("SELECT * FROM objective_schedules").fetchone()
    assert schedule["objective_id"] == first[1]
    assert schedule["event_type"] == "ceo.operating_review"
    assert schedule["interval_seconds"] == 86_400
    assert conn.execute("SELECT COUNT(*) FROM objective_schedules").fetchone()[0] == 1


def test_incomplete_mandate_fails_before_creating_business(tmp_path, monkeypatch):
    path = tmp_path / "authority.db"
    monkeypatch.setattr("hermes_cli.objectives_db.objectives_db_path", lambda: path)
    charter = _charter()
    charter["initial_mandate"]["success_criteria"] = []
    with pytest.raises(ValueError, match="success criteria"):
        _bootstrap_agentic_business(charter)
    assert not path.exists()


def test_invalid_operating_cadence_fails_before_creating_business(
    tmp_path, monkeypatch
):
    path = tmp_path / "authority.db"
    monkeypatch.setattr("hermes_cli.objectives_db.objectives_db_path", lambda: path)
    charter = _charter()
    charter["operating_cadence"] = {"enabled": True, "interval_hours": 0}
    with pytest.raises(ValueError, match="interval_hours"):
        _bootstrap_agentic_business(charter)
    assert not path.exists()


def test_bootstrapped_verifier_contract_can_reach_verified_state(
    tmp_path, monkeypatch
):
    path = tmp_path / "authority.db"
    monkeypatch.setattr("hermes_cli.objectives_db.objectives_db_path", lambda: path)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "bootstrap-verified"
    )
    charter = _charter()
    charter["operating_cadence"] = {"enabled": False}
    charter["allowed_capabilities"] = ["work.delegate"]
    charter["allowed_systems"] = ["kanban"]
    charter["initial_mandate"]["success_criteria"] = [
        {"verifier": "accounting.books_balanced", "params": {}}
    ]
    _, objective_id = _bootstrap_agentic_business(charter)
    conn = objectives_db.connect(path)
    runtime = objective_service.build_runtime(
        conn, {"agentic": charter}, board="default"
    )

    class ExistingStatePlanner:
        identity = "employee:ceo"

        def propose(self, snapshot, event):
            return PlanProposal(
                assumptions=[],
                tasks=[],
                dependencies=[],
                risks=[],
                actions=[],
                objective_complete_when_verified=True,
            )

    runtime.planner = ExistingStatePlanner()
    outcome = runtime.tick()

    assert outcome.status == "verified"
    assert objectives_db.get_objective(conn, objective_id).status == "verified"
    evidence = conn.execute(
        """SELECT evidence_json FROM verification_records
           WHERE objective_id=? ORDER BY created_at DESC LIMIT 1""",
        (objective_id,),
    ).fetchone()["evidence_json"]
    assert '"balanced":true' in evidence
