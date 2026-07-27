import json
import sqlite3

import httpx
import pytest

from hermes_cli import company_email, objectives_db, organization_db
from hermes_cli.objective_adapters import (
    ActionExecutorRegistry,
    IndependentVerifierRegistry,
    register_email_adapters,
)
from hermes_cli.objective_runtime import ActionProposal
from hermes_cli import objective_runtime


class FakeProvider:
    name = "agentmail"

    def __init__(self):
        self.sent = []
        self.messages = {}

    def send(self, **kwargs):
        self.sent.append(kwargs)
        result = {"message_id": "msg_1", "thread_id": "thread_1"}
        self.messages["msg_1"] = {
            **result,
            "to": kwargs["recipients"],
            "subject": kwargs["subject"],
        }
        return result

    def get_message(self, *, inbox_id, message_id):
        return self.messages[message_id]


class EmailPlanner:
    identity = "employee:ceo"

    def __init__(self, action):
        self.action = action

    def propose(self, snapshot, event):
        return objective_runtime.PlanProposal(
            assumptions=[],
            tasks=[{"step": "send receipt"}],
            dependencies=[],
            risks=[],
            actions=[self.action],
        )


def test_agentmail_http_edge_binds_idempotency_and_reads_back_provider():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"message_id": "msg_1", "thread_id": "thread_1"}
            )
        return httpx.Response(
            200,
            json={
                "message_id": "msg_1",
                "thread_id": "thread_1",
                "to": ["buyer@example.com"],
                "subject": "Invoice",
            },
        )

    provider = company_email.AgentMailProvider(
        "am_secret", transport=httpx.MockTransport(handler)
    )
    sent = provider.send(
        inbox_id="ceo@agentmail.to",
        recipients=["buyer@example.com"],
        subject="Invoice",
        text="Attached",
        html=None,
        idempotency_key="invoice-1",
    )
    observed = provider.get_message(
        inbox_id="ceo@agentmail.to", message_id=sent["message_id"]
    )

    assert requests[0].headers["Idempotency-Key"] == "invoice-1"
    assert json.loads(requests[0].content)["to"] == ["buyer@example.com"]
    assert "/messages/msg_1" in str(requests[1].url)
    assert observed["message_id"] == "msg_1"
    assert "am_secret" not in repr(requests[0].url)


def test_commercial_email_requires_compliance_fields_and_honors_suppression():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    payload = {
        "to": ["lead@example.com"],
        "communication_type": "commercial",
    }
    with pytest.raises(company_email.CompanyEmailError, match="compliance fields"):
        company_email.validate_send(
            conn, organization_id="org_1", payload=payload
        )
    payload.update(
        {
            "consent_basis": "express opt-in consent-1",
            "sender_identity": "Example Inc.",
            "physical_address": "1 Main Street",
            "unsubscribe_url": "https://example.test/unsubscribe",
        }
    )
    company_email.validate_send(conn, organization_id="org_1", payload=payload)
    company_email.suppress(
        conn,
        organization_id="org_1",
        address="lead@example.com",
        reason="unsubscribe",
        source_reference="message:reply-1",
    )
    with pytest.raises(company_email.CompanyEmailError, match="suppressed"):
        company_email.validate_send(
            conn, organization_id="org_1", payload=payload
        )


def test_governed_email_send_has_provider_readback_and_immutable_lineage(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Mail Company",
        purpose="Serve customers",
        profile_name="default",
        charter={},
    )
    provider = FakeProvider()
    executor = ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = IndependentVerifierRegistry()
    register_email_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config={},
        provider_config=company_email.EmailConfiguration(
            "ceo@agentmail.to", provider
        ),
    )
    payload = {
        "system": "agentmail",
        "target_resource": "buyer@example.com",
        "idempotency_key": "receipt-1",
        "to": ["buyer@example.com"],
        "subject": "Your receipt",
        "text": "Thank you",
        "communication_type": "transactional",
    }
    outcome = executor.execute_governed(
        "action_1", "objective_1", "email.send", payload
    )
    action = ActionProposal(
        action_type="email.send",
        payload=payload,
        expected_outcome="receipt sent",
        required_capability="email.send",
        verification_method="email.provider_readback",
        risk_class="low",
        reversible=False,
    )
    verification = verifier.verify(action, outcome)

    assert outcome.status == "succeeded"
    assert verification.verdict == "pass"
    row = conn.execute("SELECT * FROM company_email_operations").fetchone()
    assert row["organization_id"] == organization_id
    assert row["message_id"] == "msg_1"
    assert "Thank you" not in row["provider_evidence_json"]
    assert company_email.record_send(
        conn,
        organization_id=organization_id,
        objective_id="objective_1",
        action_id="action_1",
        inbox_id="ceo@agentmail.to",
        payload=payload,
        response={"message_id": "msg_1", "thread_id": "thread_1"},
    ) == row["id"]
    with pytest.raises(company_email.CompanyEmailError, match="different send parameters"):
        company_email.record_send(
            conn,
            organization_id=organization_id,
            objective_id="objective_1",
            action_id="action_1",
            inbox_id="ceo@agentmail.to",
            payload={**payload, "text": "changed"},
            response={"message_id": "msg_1"},
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE company_email_operations SET status='failed' WHERE id=?",
            (row["id"],),
        )


def test_autonomous_email_cycle_requires_permit_and_provider_readback(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    _, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Mail Company",
        purpose="Serve customers",
        profile_name="default",
        charter={},
    )
    objective = objectives_db.create_objective(
        conn,
        desired_outcome="Send purchased customer receipt",
        originator="order-system",
        permitted_systems=["agentmail"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="order-system"
    )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="order.paid",
        payload={"order_id": "order-1"},
    )
    provider = FakeProvider()
    executor = ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = IndependentVerifierRegistry()
    register_email_adapters(
        executor,
        verifier,
        authority_conn=conn,
        config={},
        provider_config=company_email.EmailConfiguration(
            "ceo@agentmail.to", provider
        ),
    )
    action = ActionProposal(
        action_type="email.send",
        payload={
            "system": "agentmail",
            "target_resource": "buyer@example.com",
            "idempotency_key": "order-1-receipt",
            "to": ["buyer@example.com"],
            "subject": "Your receipt",
            "text": "Thank you",
            "communication_type": "transactional",
        },
        expected_outcome="receipt exists at provider",
        required_capability="email.send",
        verification_method="email.provider_readback",
        risk_class="low",
        reversible=False,
    )
    runtime = objective_runtime.ObjectiveRuntime(
        conn,
        planner=EmailPlanner(action),
        executor=executor,
        verifier=verifier,
        charter={
            "enabled": True,
            "operating_mode": "autonomous",
            "allowed_capabilities": ["email.send"],
            "forbidden_capabilities": [],
            "allowed_systems": ["agentmail"],
            "approval_required_capabilities": [],
            "max_autonomous_risk": "low",
            "allow_irreversible": True,
            "max_action_spend_minor": 0,
            "permit_ttl_seconds": 300,
        },
        policy_version="charter-v1",
        runtime_id="runtime:email",
    )

    outcome = runtime.tick()

    assert outcome.status == "progressed"
    assert len(provider.sent) == 1
    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 1
    verification = conn.execute(
        "SELECT verdict,evidence_json FROM verification_records"
    ).fetchone()
    assert verification["verdict"] == "pass"
    assert "provider_readback" in verification["evidence_json"]


def test_schema_check_does_not_commit_active_authority_transaction():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    company_email.ensure_schema(conn)
    conn.execute("CREATE TABLE authority_sentinel (value TEXT NOT NULL)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO authority_sentinel(value) VALUES ('uncommitted')")
    company_email.ensure_schema(conn)
    conn.rollback()
    assert conn.execute("SELECT * FROM authority_sentinel").fetchall() == []
