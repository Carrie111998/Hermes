"""Tests for the authority bridge (SQLite workflow → Postgres coordination).

Tests the AuthorityBridge class against a live Postgres instance.

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import uuid

import pytest

POSTGRES_URL = os.environ.get("AUTHORITY_POSTGRES_TEST_URL", "")


@pytest.fixture
def pg_schema(monkeypatch):
    """Create an isolated Postgres schema and set the env var."""
    if not POSTGRES_URL:
        pytest.skip("AUTHORITY_POSTGRES_TEST_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg not installed")

    schema_name = f"bridge_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(POSTGRES_URL, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
    conn.commit()
    conn.close()

    modified_url = f"{POSTGRES_URL} options=-csearch_path={schema_name}"
    monkeypatch.setenv("AUTHORITY_POSTGRES_URL", modified_url)
    monkeypatch.delenv("HERMES_TENANT_ID", raising=False)

    yield schema_name

    cleanup = psycopg.connect(POSTGRES_URL, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema_name} CASCADE")
    cleanup.close()


class TestAuthorityBridge:
    def test_bridge_inactive_without_postgres(self, monkeypatch):
        monkeypatch.delenv("AUTHORITY_POSTGRES_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        from hermes_cli.authority_bridge import AuthorityBridge

        bridge = AuthorityBridge(organization_id="org-1", worker_id="w1")
        assert bridge.active is False
        assert bridge.claim(task_id="t1", claim_token="tok") is None
        assert bridge.complete(outcome="success") is None

    def test_full_lifecycle(self, pg_schema):
        from hermes_cli.authority_bridge import AuthorityBridge

        bridge = AuthorityBridge(organization_id="org-test", worker_id="w-bridge")
        assert bridge.active is True

        gen = bridge.claim(task_id="task-bridge", claim_token="tok-b1")
        assert gen == 1
        assert bridge.has_claim is True

        permit_id = bridge.issue_permit(
            actor="ceo", executor="w-bridge",
            capability="payment:send", action_type="stripe.charge",
            target_resource="cust:123",
            action_payload={"amount": 100},
        )
        assert permit_id is not None

        consumed = bridge.consume_permit(
            permit_id=permit_id,
            action_payload={"amount": 100},
        )
        assert consumed is True

        recorded = bridge.record_effect(
            effect_key="bridge:effect:1",
            effect_type="payment.sent",
            permit_id=permit_id,
            provider="stripe",
            provider_ref="pi_123",
            payload={"charged": True},
        )
        assert recorded is True

        # Idempotent replay
        replayed = bridge.record_effect(
            effect_key="bridge:effect:1",
            effect_type="payment.sent",
            permit_id=permit_id,
            provider="stripe",
            provider_ref="pi_123",
            payload={"charged": True},
        )
        assert replayed is False

        completed = bridge.complete(outcome="success")
        assert completed is True
        assert bridge.has_claim is False

        bridge.close()

    def test_claim_exclusivity(self, pg_schema):
        from hermes_cli.authority_bridge import AuthorityBridge

        bridge1 = AuthorityBridge(organization_id="org-race", worker_id="w1")
        bridge2 = AuthorityBridge(organization_id="org-race", worker_id="w2")

        gen1 = bridge1.claim(task_id="race-task", claim_token="tok-1")
        gen2 = bridge2.claim(task_id="race-task", claim_token="tok-2")

        assert gen1 == 1
        assert gen2 is None

        bridge1.close()
        bridge2.close()

    def test_release_claim(self, pg_schema):
        from hermes_cli.authority_bridge import AuthorityBridge

        bridge = AuthorityBridge(organization_id="org-rel", worker_id="w-rel")
        bridge.claim(task_id="rel-task", claim_token="tok-rel")
        assert bridge.has_claim is True

        released = bridge.release()
        assert released is True
        assert bridge.has_claim is False

        bridge.close()
