from __future__ import annotations

import yaml

from hermes_cli import employee_provisioning
from hermes_cli import objectives_db
from hermes_cli import organization_db as org


def test_employee_profile_is_mandate_bound_and_does_not_clone_secrets(
    tmp_path, monkeypatch
):
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    conn = objectives_db.connect(root / "objectives.db")
    organization_id = org.create_organization(
        conn,
        name="Solo Co",
        purpose="Build sustainably",
        headcount_limit=5,
        payroll_budget_minor=1000,
    )
    ceo = org.propose_employee(
        conn,
        organization_id=organization_id,
        display_name="Charterforge",
        title="CEO",
        level="ceo",
        manager_id=None,
        proposed_by="setup:user",
    )
    org.transition_employee(conn, ceo, "approved", actor="setup:user")
    hire = org.propose_employee(
        conn,
        organization_id=organization_id,
        display_name="Ada",
        title="Contract Researcher",
        level="individual_contributor",
        manager_id=ceo,
        proposed_by="employee:ceo",
        employment_type="contractor",
        hired_for_objective_id="obj_market",
    )
    org.create_mandate(
        conn,
        hire,
        purpose="Deliver a market report",
        responsibilities=["research"],
        decision_rights=["read public sources"],
        prohibited_actions=["external publication"],
        capabilities=["web.read"],
        systems=["web"],
        toolsets=["web"],
        kpis=["report delivered"],
        escalation={"to": ceo},
        expires_at=9999999999,
        created_by="employee:ceo",
    )
    org.transition_employee(conn, hire, "approved", actor="control")

    profile_dir = employee_provisioning.provision_employee_profile(
        conn,
        hire,
        actor="control",
        profile_name="contract-research",
        source_config={"model": {"provider": "nous", "model": "test-model"}},
    )

    assert org.get_employee_record(conn, hire)["status"] == "active"
    meta = yaml.safe_load((profile_dir / "profile.yaml").read_text())
    assert meta["employee_id"] == hire
    assert meta["manager_employee_id"] == ceo
    assert meta["employment_class"] == "contractor"
    config = yaml.safe_load((profile_dir / "config.yaml").read_text())
    assert config["platform_toolsets"]["kanban"] == ["web"]
    env_text = (profile_dir / ".env").read_text()
    assert "API_KEY=" not in env_text
    assert not (profile_dir / "memories" / "MEMORY.md").exists()
    conn.close()
