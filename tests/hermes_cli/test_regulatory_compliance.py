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
