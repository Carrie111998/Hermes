from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import objectives_db
from hermes_cli import organization_db as org
from hermes_cli.profiles import read_profile_meta


@pytest.fixture
def conn(tmp_path):
    connection = objectives_db.connect(tmp_path / "authority.db")
    org.ensure_schema(connection)
    yield connection
    connection.close()


def _company(conn, **overrides):
    values = {
        "name": "Autonomous Co",
        "purpose": "Operate a sustainable business",
        "operator_role": "advisor",
        "headcount_limit": 10,
        "payroll_budget_minor": 100_000,
    }
    values.update(overrides)
    return org.create_organization(conn, **values)


def _ceo(conn, organization_id):
    return org.propose_employee(
        conn,
        organization_id=organization_id,
        display_name="Hermes",
        title="Chief Executive Officer",
        level="ceo",
        manager_id=None,
        proposed_by="setup:user",
    )


def test_data_residency_rejects_unapproved_processing_region(conn):
    company = _company(
        conn,
        data_residency_region="ca-central",
        allowed_processing_regions=["ca-central"],
    )
    org.assert_processing_region(conn, company, "ca-central")
    with pytest.raises(org.OrganizationError, match="violates residency"):
        org.assert_processing_region(conn, company, "us-east")


def test_enterprise_reporting_hierarchy(conn):
    company = _company(conn)
    ceo = _ceo(conn, company)
    org.transition_employee(conn, ceo, "approved", actor="setup:user")
    cto = org.propose_employee(
        conn,
        organization_id=company,
        display_name="Athena",
        title="Chief Technology Officer",
        level="c_suite",
        manager_id=ceo,
        proposed_by="ceo",
    )
    org.transition_employee(conn, cto, "approved", actor="control")
    director = org.propose_employee(
        conn,
        organization_id=company,
        display_name="Daedalus",
        title="Director of Engineering",
        level="director",
        manager_id=cto,
        proposed_by="ceo",
    )
    chart = org.organization_chart(conn, company)
    assert [(row["title"], row["depth"]) for row in chart] == [
        ("Chief Executive Officer", 0),
        ("Chief Technology Officer", 1),
        ("Director of Engineering", 2),
    ]
    assert director


def test_non_ceo_requires_manager_and_manager_must_be_senior(conn):
    company = _company(conn)
    with pytest.raises(org.OrganizationError, match="require a manager"):
        org.propose_employee(
            conn,
            organization_id=company,
            display_name="Orphan",
            title="Engineer",
            level="individual_contributor",
            manager_id=None,
            proposed_by="ceo",
        )

    ceo = _ceo(conn, company)
    org.transition_employee(conn, ceo, "approved", actor="setup:user")
    ic = org.propose_employee(
        conn,
        organization_id=company,
        display_name="Engineer",
        title="Engineer",
        level="individual_contributor",
        manager_id=ceo,
        proposed_by="ceo",
    )
    org.transition_employee(conn, ic, "approved", actor="control")
    with pytest.raises(org.OrganizationError, match="above"):
        org.propose_employee(
            conn,
            organization_id=company,
            display_name="VP",
            title="VP Engineering",
            level="vp",
            manager_id=ic,
            proposed_by="ceo",
        )


def test_employee_requires_current_mandate_and_profile_before_activation(conn):
    company = _company(conn)
    ceo = _ceo(conn, company)
    org.transition_employee(conn, ceo, "approved", actor="setup:user")
    org.transition_employee(conn, ceo, "provisioning", actor="control")
    with pytest.raises(org.OrganizationError, match="without a mandate"):
        org.transition_employee(
            conn, ceo, "active", actor="control", profile_name="ceo"
        )

    org.create_mandate(
        conn,
        ceo,
        purpose="Run the company inside its charter",
        responsibilities=["portfolio management"],
        decision_rights=["hire within budget"],
        prohibited_actions=["expand own charter"],
        capabilities=["objectives.manage"],
        systems=["objectives"],
        kpis=["runway", "revenue"],
        escalation={"to": "operator", "when": "outside charter"},
        toolsets=["kanban"],
        created_by="setup:user",
    )
    active = org.transition_employee(
        conn, ceo, "active", actor="control", profile_name="ceo"
    )
    assert active.status == "active"
    assert active.profile_name == "ceo"


def test_hiring_is_bounded_by_headcount_and_payroll(conn):
    company = _company(conn, headcount_limit=1, payroll_budget_minor=100)
    ceo = org.propose_employee(
        conn,
        organization_id=company,
        display_name="Hermes",
        title="CEO",
        level="ceo",
        manager_id=None,
        proposed_by="setup:user",
        annual_cost_minor=100,
    )
    org.transition_employee(conn, ceo, "approved", actor="setup:user")
    # Proposed hires do not consume budget until approved.
    hire = org.propose_employee(
        conn,
        organization_id=company,
        display_name="CFO",
        title="Chief Financial Officer",
        level="c_suite",
        manager_id=ceo,
        proposed_by="ceo",
        annual_cost_minor=1,
    )
    with pytest.raises(org.OrganizationError, match="headcount"):
        org.transition_employee(conn, hire, "approved", actor="control")


def test_only_one_live_ceo(conn):
    company = _company(conn)
    _ceo(conn, company)
    with pytest.raises(org.OrganizationError, match="already has a CEO"):
        _ceo(conn, company)


def test_employee_mandates_are_append_only(conn):
    company = _company(conn)
    ceo = _ceo(conn, company)
    mandate_id = org.create_mandate(
        conn,
        ceo,
        purpose="Operate",
        responsibilities=[],
        decision_rights=[],
        prohibited_actions=[],
        capabilities=["objectives.manage"],
        systems=["objectives"],
        kpis=[],
        escalation={},
        created_by="setup:user",
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE employee_mandates SET purpose='tampered' WHERE id=?",
            (mandate_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "DELETE FROM employee_mandates WHERE id=?", (mandate_id,)
        )


@pytest.mark.parametrize(
    ("initial_capabilities", "revised_capabilities"),
    [
        (["objectives.manage"], ["objectives.manage", "work.delegate"]),
        (["objectives.manage", "work.delegate"], ["objectives.manage"]),
    ],
)
def test_setup_revision_supersedes_ceo_mandate_and_profile_snapshot(
    tmp_path, monkeypatch, initial_capabilities, revised_capabilities
):
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    connection = objectives_db.connect(root / "objectives.db")
    initial = {
        "operator_role": "advisor",
        "allowed_capabilities": initial_capabilities,
        "allowed_systems": ["objectives", "kanban"],
        "forbidden_capabilities": ["funds.custody"],
        "max_action_spend_minor": 100,
        "solo_founder": {
            "toolsets": ["web"],
            "skills": ["research"],
        },
        "finance": {"base_currency": "USD"},
    }
    organization_id, ceo_id = org.bootstrap_solo_founder(
        connection,
        organization_name="Founder Co",
        purpose="Original purpose",
        profile_name="default",
        charter=initial,
    )
    first = org.get_current_mandate(connection, ceo_id)

    revised = {
        **initial,
        "operator_role": "approver",
        "allowed_capabilities": revised_capabilities,
        "allowed_systems": ["objectives"],
        "max_action_spend_minor": 50,
    }
    repeated_org, repeated_ceo = org.bootstrap_solo_founder(
        connection,
        organization_name="Founder Company",
        purpose="Revised purpose",
        profile_name="default",
        charter=revised,
    )
    second = org.get_current_mandate(connection, ceo_id)
    assert (repeated_org, repeated_ceo) == (organization_id, ceo_id)
    assert second["version"] == first["version"] + 1
    assert second["supersedes_id"] == first["id"]
    assert second["capabilities"] == sorted(revised_capabilities)
    assert second["systems"] == ["objectives"]
    assert second["toolsets"] == ["web"]
    assert second["skills"] == ["research"]
    assert second["budget_minor"] == 50
    assert first["capabilities"] == sorted(initial_capabilities)
    organization = connection.execute(
        "SELECT * FROM organizations WHERE id=?", (organization_id,)
    ).fetchone()
    assert organization["name"] == "Founder Company"
    assert organization["purpose"] == "Revised purpose"
    assert organization["operator_role"] == "approver"
    profile = read_profile_meta(root)
    assert profile["organization_id"] == organization_id
    assert profile["employee_id"] == ceo_id
    assert profile["mandate_id"] == second["id"]
    assert profile["mandate_version"] == second["version"]

    org.bootstrap_solo_founder(
        connection,
        organization_name="Founder Company",
        purpose="Revised purpose",
        profile_name="default",
        charter=revised,
    )
    assert org.get_current_mandate(connection, ceo_id)["version"] == second["version"]
    connection.close()
