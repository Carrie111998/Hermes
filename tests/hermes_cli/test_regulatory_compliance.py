import sqlite3
import time

import pytest

from hermes_cli import regulatory_compliance as compliance


def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    compliance.seed_starter_regimes(conn)
    return conn


def test_relevant_unassessed_regime_blocks_action():
    conn = connection()
    with pytest.raises(compliance.ComplianceGateError, match="Anti-Spam"):
        compliance.authorize_action(
            conn,
            organization_id="org_1",
            context={
                "jurisdictions": ["CA"],
                "activities": ["commercial_email"],
                "data_classes": [],
                "entity_attributes": [],
            },
        )


def test_compliance_records_reject_unknown_or_inactive_regimes():
    conn = connection()
    future = int(time.time()) + 3600
    with pytest.raises(compliance.ComplianceGateError, match="active known"):
        compliance.assess_applicability(
            conn,
            organization_id="org_1",
            regime_id="not-a-regime",
            verdict="applicable",
            rationale="unknown source",
            evidence={"review": "x"},
            assessed_by="advisor:legal",
            expires_at=future,
        )


def test_control_evidence_rejects_stale_expiry():
    conn = connection()
    with pytest.raises(compliance.ComplianceGateError, match="expiry"):
        compliance.record_control_evidence(
            conn,
            organization_id="org_1",
            control_name="control.stale",
            verifier="control:test",
            verdict="pass",
            evidence={"check": "old"},
            expires_at=int(time.time()) - 1,
        )


def test_compliance_evidence_records_are_append_only():
    conn = connection()
    future = int(time.time()) + 3600
    assessment_id = compliance.assess_applicability(
        conn,
        organization_id="org_1",
        regime_id="casl",
        verdict="not_applicable",
        rationale="No Canadian recipients",
        evidence={"review": "r1"},
        assessed_by="advisor:legal",
        expires_at=future,
    )
    evidence_id = compliance.record_control_evidence(
        conn,
        organization_id="org_1",
        control_name="control.append_only",
        verifier="control:test",
        verdict="pass",
        evidence={"check": "current"},
        expires_at=future,
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE compliance_applicability SET rationale='changed' WHERE id=?",
            (assessment_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "DELETE FROM compliance_control_evidence WHERE id=?", (evidence_id,)
        )
    conn.execute(
        "UPDATE compliance_regimes SET status='retired' WHERE id='casl'"
    )
    with pytest.raises(compliance.ComplianceGateError, match="active known"):
        compliance.register_obligation(
            conn,
            organization_id="org_1",
            regime_id="casl",
            name="Retired regime obligation",
            action_tags=[],
            required_control="control.retired",
            effective_from=1,
            evidence={"source": "stale"},
        )


def test_compliance_supersession_preserves_lineage_and_current_authority():
    conn = connection()
    future = int(time.time()) + 3600
    applicability_id = compliance.assess_applicability(
        conn,
        organization_id="org_1",
        regime_id="casl",
        verdict="applicable",
        rationale="Canadian commercial messages are planned",
        evidence={"review": "r1"},
        assessed_by="advisor:legal",
        expires_at=future,
    )
    compliance.register_obligation(
        conn,
        organization_id="org_1",
        regime_id="casl",
        name="Consent control",
        action_tags=["commercial_email"],
        required_control="email.consent",
        effective_from=1,
        evidence={"source": "policy-1"},
    )
    passing_id = compliance.record_control_evidence(
        conn,
        organization_id="org_1",
        control_name="email.consent",
        verifier="control:test",
        verdict="pass",
        evidence={"check": "ok"},
        expires_at=future,
    )
    context = {
        "jurisdictions": ["CA"],
        "activities": ["commercial_email"],
        "data_classes": [],
        "entity_attributes": [],
    }
    assert compliance.authorize_action(
        conn, organization_id="org_1", context=context
    )["regimes"] == [{"regime": "casl", "verdict": "applicable"}]

    failed_id = compliance.record_control_evidence(
        conn,
        organization_id="org_1",
        control_name="email.consent",
        verifier="control:test",
        verdict="fail",
        evidence={"check": "revoked"},
        expires_at=future,
        supersedes_id=passing_id,
        supersession_reason="Consent record was revoked",
    )
    assert conn.execute(
        "SELECT supersedes_id FROM compliance_control_evidence WHERE id=?",
        (failed_id,),
    ).fetchone()["supersedes_id"] == passing_id
    with pytest.raises(compliance.ComplianceGateError, match="no current"):
        compliance.authorize_action(
            conn, organization_id="org_1", context=context
        )

    current_id = compliance.assess_applicability(
        conn,
        organization_id="org_1",
        regime_id="casl",
        verdict="not_applicable",
        rationale="The campaign was cancelled",
        evidence={"review": "r2"},
        assessed_by="advisor:legal",
        expires_at=future,
        supersedes_id=applicability_id,
        supersession_reason="Business scope changed",
    )
    assert conn.execute(
        "SELECT supersedes_id FROM compliance_applicability WHERE id=?",
        (current_id,),
    ).fetchone()["supersedes_id"] == applicability_id
    assert compliance.authorize_action(
        conn, organization_id="org_1", context=context
    )["regimes"] == [{"regime": "casl", "verdict": "not_applicable"}]


def test_compliance_supersession_requires_same_scope_and_reason():
    conn = connection()
    future = int(time.time()) + 3600
    prior = compliance.assess_applicability(
        conn,
        organization_id="org_1",
        regime_id="casl",
        verdict="not_applicable",
        rationale="Initial review",
        evidence={"review": "r1"},
        assessed_by="advisor:legal",
        expires_at=future,
    )
    with pytest.raises(compliance.ComplianceGateError, match="same organization"):
        compliance.assess_applicability(
            conn,
            organization_id="org_2",
            regime_id="casl",
            verdict="not_applicable",
            rationale="Different tenant",
            evidence={"review": "r2"},
            assessed_by="advisor:legal",
            expires_at=future,
            supersedes_id=prior,
            supersession_reason="wrong scope",
        )
    with pytest.raises(compliance.ComplianceGateError, match="requires a reason"):
        compliance.assess_applicability(
            conn,
            organization_id="org_1",
            regime_id="casl",
            verdict="not_applicable",
            rationale="Updated review",
            evidence={"review": "r3"},
            assessed_by="advisor:legal",
            expires_at=future,
            supersedes_id=prior,
        )
def test_schema_check_does_not_commit_active_authority_transaction():
    conn = connection()
    conn.execute("CREATE TABLE authority_sentinel (value TEXT NOT NULL)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO authority_sentinel(value) VALUES ('uncommitted')")
    compliance.ensure_schema(conn)
    conn.rollback()
    assert conn.execute("SELECT * FROM authority_sentinel").fetchall() == []


def test_applicable_regime_requires_mapped_control_and_current_evidence():
    conn = connection()
    future = int(time.time()) + 3600
    compliance.assess_applicability(
        conn,
        organization_id="org_1",
        regime_id="casl",
        verdict="applicable",
        rationale="Commercial electronic messages are sent to Canada",
        evidence={"legal_review": "review-1"},
        assessed_by="advisor:legal",
        expires_at=future,
    )
    compliance.register_obligation(
        conn,
        organization_id="org_1",
        regime_id="casl",
        name="Consent and unsubscribe",
        action_tags=["commercial_email"],
        required_control="commercial_email.consent_and_unsubscribe",
        effective_from=1,
        evidence={"source": "CRTC"},
    )
    context = {
        "jurisdictions": ["CA"],
        "activities": ["commercial_email"],
        "data_classes": [],
        "entity_attributes": [],
    }
    with pytest.raises(compliance.ComplianceGateError, match="no current"):
        compliance.authorize_action(
            conn, organization_id="org_1", context=context
        )
    compliance.record_control_evidence(
        conn,
        organization_id="org_1",
        control_name="commercial_email.consent_and_unsubscribe",
        verifier="control:email-policy",
        verdict="pass",
        evidence={"template_hash": "abc", "consent_readback": True},
        expires_at=future,
    )
    result = compliance.authorize_action(
        conn, organization_id="org_1", context=context
    )
    assert {"regime": "casl", "verdict": "applicable"} in result["regimes"]


def test_sox_not_triggered_merely_because_books_exist():
    conn = connection()
    assert not any(
        item["id"] == "sox"
        for item in compliance.relevant_regimes(
            conn,
            {
                "jurisdictions": ["US"],
                "activities": ["financial_reporting"],
                "entity_attributes": ["private_company"],
            },
        )
    )
