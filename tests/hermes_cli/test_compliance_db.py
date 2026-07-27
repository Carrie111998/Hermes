import sqlite3
import time

import pytest

from hermes_cli import compliance_db


def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    compliance_db.ensure_schema(conn)
    return conn


def test_compliance_schema_read_preserves_active_transaction():
    conn = connection()
    conn.execute("BEGIN IMMEDIATE")
    compliance_db.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


def test_payment_provider_authority_is_jurisdictional_and_expires():
    conn = connection()
    compliance_db.configure_profile(
        conn,
        organization_id="org_1",
        legal_entity_type="corporation",
        home_jurisdiction="CA-ON",
    )
    with pytest.raises(compliance_db.ComplianceError, match="assessment"):
        compliance_db.authorize_payment_provider(
            conn,
            organization_id="org_1",
            provider="rail",
            direction="outbound",
            jurisdiction="CA",
        )
    assessment = compliance_db.verify_payment_provider(
        conn,
        organization_id="org_1",
        provider="rail",
        direction="outbound",
        jurisdiction="CA",
        registry_authority="Bank of Canada",
        registry_reference="PSP-123",
        aml_screening_delegated=True,
        sanctions_screening_delegated=True,
        verified_at=int(time.time()) - 1,
        expires_at=int(time.time()) + 3600,
        evidence={"registry_readback": True},
    )
    assert compliance_db.authorize_payment_provider(
        conn,
        organization_id="org_1",
        provider="rail",
        direction="outbound",
        jurisdiction="CA",
    ) == assessment


def test_payment_provider_assessment_supersession_is_explicit_and_fail_closed():
    conn = connection()
    compliance_db.configure_profile(
        conn,
        organization_id="org_1",
        legal_entity_type="corporation",
        home_jurisdiction="CA-ON",
    )
    future = int(time.time()) + 3600
    prior = compliance_db.verify_payment_provider(
        conn,
        organization_id="org_1",
        provider="rail",
        direction="outbound",
        jurisdiction="CA",
        registry_authority="Bank of Canada",
        registry_reference="PSP-123",
        aml_screening_delegated=True,
        sanctions_screening_delegated=True,
        verified_at=int(time.time()) - 1,
        expires_at=future,
        evidence={"registry_readback": True},
    )
    replacement = compliance_db.verify_payment_provider(
        conn,
        organization_id="org_1",
        provider="rail",
        direction="outbound",
        jurisdiction="CA",
        registry_authority="Bank of Canada",
        registry_reference="PSP-123-revoked",
        aml_screening_delegated=False,
        sanctions_screening_delegated=True,
        verified_at=int(time.time()),
        expires_at=future,
        evidence={"registry_readback": False},
        supersedes_id=prior,
        supersession_reason="AML delegation was revoked",
    )
    assert replacement != prior
    with pytest.raises(compliance_db.ComplianceError, match="screening"):
        compliance_db.authorize_payment_provider(
            conn,
            organization_id="org_1",
            provider="rail",
            direction="outbound",
            jurisdiction="CA",
        )
    with pytest.raises(compliance_db.ComplianceError, match="current record"):
        compliance_db.verify_payment_provider(
            conn,
            organization_id="org_1",
            provider="rail",
            direction="outbound",
            jurisdiction="CA",
            registry_authority="Bank of Canada",
            registry_reference="PSP-123-branch",
            aml_screening_delegated=True,
            sanctions_screening_delegated=True,
            verified_at=int(time.time()),
            expires_at=future,
            evidence={"registry_readback": True},
            supersedes_id=prior,
            supersession_reason="Conflicting branch",
        )


def test_kya_log_forms_a_tamper_evident_hash_chain():
    conn = connection()
    first = compliance_db.append_kya_event(
        conn,
        organization_id="org_1",
        agent_id="ceo",
        agent_version="1",
        event_type="payment.proposed",
        payload={"amount_minor": 100},
        evidence={"action_id": "a1"},
    )
    compliance_db.append_kya_event(
        conn,
        organization_id="org_1",
        agent_id="ceo",
        agent_version="1",
        event_type="payment.executed",
        payload={"amount_minor": 100},
        evidence={"provider_receipt": "r1"},
    )
    rows = conn.execute(
        "SELECT id, previous_hash, event_hash FROM kya_events ORDER BY sequence"
    ).fetchall()
    first_hash = next(row["event_hash"] for row in rows if row["id"] == first)
    second = next(row for row in rows if row["id"] != first)
    assert second["previous_hash"] == first_hash
