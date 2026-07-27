from __future__ import annotations

import json
import time

from hermes_cli import accounting_db
from hermes_cli import compliance_deadlines
from hermes_cli import objectives_db
from hermes_cli import organization_db
from hermes_cli import regulatory_compliance


def _organization(conn, name: str) -> str:
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name=name,
        purpose="Operate compliantly",
        profile_name=name.lower().replace(" ", "-"),
        charter={},
    )
    return organization_id


def _root_objective(conn, organization_id: str) -> str:
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Operate a compliant business",
        originator="employee:ceo",
        permitted_systems=["accounting"],
        prohibited_actions=["compliance.bypass"],
        expires_at=int(time.time()) + 86_400,
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    return objective.id


def _tax_due(
    conn,
    organization_id: str,
    *,
    due_at: int,
    registration_number: str = "SECRET-TAX-ID",
) -> str:
    registration_id = accounting_db.configure_tax_registration(
        conn,
        organization_id=organization_id,
        jurisdiction="CA",
        tax_type="sales_tax",
        filing_frequency="quarterly",
        effective_from=1,
        registration_number=registration_number,
        evidence={"private_document": "secret-registration-evidence"},
    )
    return accounting_db.record_tax_obligation(
        conn,
        organization_id=organization_id,
        registration_id=registration_id,
        period_start=1,
        period_end=10,
        due_at=due_at,
        amount_minor=1_234,
        currency="cad",
        evidence={"private_calculation": "secret-tax-evidence"},
    )


def test_tax_deadline_wakes_root_once_without_sensitive_evidence(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id = _organization(conn, "Deadline Company")
    objective_id = _root_objective(conn, organization_id)
    now = int(time.time())
    obligation_id = _tax_due(conn, organization_id, due_at=now + 600)

    first = compliance_deadlines.dispatch_deadlines(
        conn, organization_id=organization_id, horizon_seconds=3_600, now=now
    )
    second = compliance_deadlines.dispatch_deadlines(
        conn, organization_id=organization_id, horizon_seconds=3_600, now=now
    )

    assert first["events_enqueued"] == 1
    assert second["events_enqueued"] == 0
    row = conn.execute(
        """SELECT objective_id,event_type,payload_json,status
             FROM objective_inbox
            WHERE event_type='compliance.deadline.approaching'"""
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert row["objective_id"] == objective_id
    assert row["status"] == "pending"
    assert payload == {
        "amount_minor": 1234,
        "currency": "CAD",
        "due_at": now + 600,
        "kind": "tax_obligation",
        "organization_id": organization_id,
        "overdue": False,
        "record_id": obligation_id,
        "status": "accrued",
    }
    assert "SECRET" not in row["payload_json"]
    assert "private_" not in row["payload_json"]


def test_unowned_deadline_raises_one_bounded_advisor_intervention(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id = _organization(conn, "Unowned Company")
    now = int(time.time())
    _tax_due(conn, organization_id, due_at=now - 1)

    first = compliance_deadlines.dispatch_deadlines(
        conn, organization_id=organization_id, horizon_seconds=0, now=now
    )
    second = compliance_deadlines.dispatch_deadlines(
        conn, organization_id=organization_id, horizon_seconds=0, now=now
    )

    assert first["interventions_raised"] == 1
    assert second["interventions_raised"] == 0
    row = conn.execute(
        """SELECT * FROM intervention_queue
            WHERE category='compliance_deadline_unowned'"""
    ).fetchone()
    assert row["organization_id"] == organization_id
    assert row["objective_id"] is None
    assert row["status"] == "open"
    assert json.loads(row["context_json"])["overdue"] is True
    assert [item["id"] for item in json.loads(row["options_json"])] == [
        "create_objective",
        "review",
        "manual",
    ]


def test_latest_compliance_expiries_and_regime_review_are_routed(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id = _organization(conn, "Controls Company")
    _root_objective(conn, organization_id)
    now = int(time.time())
    regulatory_compliance.seed_starter_regimes(conn)
    conn.execute(
        "UPDATE compliance_regimes SET review_due_at=? WHERE id='casl'",
        (now + 100,),
    )
    regulatory_compliance.assess_applicability(
        conn,
        organization_id=organization_id,
        regime_id="casl",
        verdict="applicable",
        rationale="Canadian commercial email",
        evidence={"review": "legal-1"},
        assessed_by="advisor:legal",
        expires_at=now + 200,
    )
    regulatory_compliance.record_control_evidence(
        conn,
        organization_id=organization_id,
        control_name="commercial_email.consent",
        verifier="control:email",
        verdict="pass",
        evidence={"check": "pass"},
        expires_at=now + 300,
    )

    result = compliance_deadlines.dispatch_deadlines(
        conn, organization_id=organization_id, horizon_seconds=400, now=now
    )

    assert result["events_enqueued"] == 3
    payloads = [
        json.loads(row["payload_json"])
        for row in conn.execute(
            """SELECT payload_json FROM objective_inbox
                WHERE event_type='compliance.deadline.approaching'"""
        ).fetchall()
    ]
    assert {payload["kind"] for payload in payloads} == {
        "regime_review",
        "applicability_assessment",
        "control_evidence",
    }


def test_dispatch_is_tenant_scoped(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    active_organization = _organization(conn, "Active Tenant")
    foreign_organization = _organization(conn, "Foreign Tenant")
    objective_id = _root_objective(conn, active_organization)
    now = int(time.time())
    _tax_due(conn, foreign_organization, due_at=now + 10)

    result = compliance_deadlines.dispatch_deadlines(
        conn,
        organization_id=active_organization,
        horizon_seconds=3_600,
        now=now,
    )

    assert result["events_enqueued"] == 0
    assert conn.execute(
        """SELECT COUNT(*) FROM objective_inbox
            WHERE objective_id=? AND event_type='compliance.deadline.approaching'""",
        (objective_id,),
    ).fetchone()[0] == 0
