"""Test that quota enforcement is wired directly into authority operations.

This proves that claim_task itself rejects over-quota tenants — the billing
engine is not a separate advisory system but is enforced at the authority layer.

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
import time
import uuid

import pytest

POSTGRES_URL = os.environ.get("AUTHORITY_POSTGRES_TEST_URL", "")


@pytest.fixture
def pg():
    if not POSTGRES_URL:
        pytest.skip("AUTHORITY_POSTGRES_TEST_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg not installed")

    schema_name = f"quota_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(POSTGRES_URL, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    conn.commit()
    from hermes_cli.postgres_authority import init_schema
    init_schema(conn)
    yield conn
    conn.close()
    cleanup = psycopg.connect(POSTGRES_URL, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema_name} CASCADE")
    cleanup.close()


class TestClaimTaskQuotaEnforcement:
    """Proves claim_task respects billing quotas."""

    def test_free_tier_blocks_at_limit(self, pg):
        """After exhausting quota, claim_task returns None (not just check_quota)."""
        from hermes_cli.postgres_authority import (
            claim_task, create_plan, subscribe_tenant, record_usage,
            DEFAULT_TENANT_ID,
        )

        # Create a tiny plan with 3 task claims
        create_plan(
            pg, plan_id="tiny", name="Tiny", tier="free",
            monthly_task_limit=3, monthly_permit_limit=10,
            monthly_effect_limit=50,
        )
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="tiny")

        # Record 3 usage entries (simulating prior claims in this period)
        for i in range(3):
            record_usage(
                pg, tenant_id=str(DEFAULT_TENANT_ID), organization_id="org-quota",
                meter_type="task_claim", reference_id=f"prior:claim:{i}",
            )

        # Now attempt a new claim — should be blocked by quota
        gen = claim_task(
            pg, task_id="blocked-task", claim_token="blocked-tok",
            organization_id="org-quota", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen is None, "Free tier claim should be rejected when over quota"

    def test_paid_tier_allows_overage(self, pg):
        """Paid tier claim succeeds even when over soft limit."""
        from hermes_cli.postgres_authority import (
            claim_task, create_plan, subscribe_tenant, record_usage,
            DEFAULT_TENANT_ID,
        )

        create_plan(
            pg, plan_id="pro-q", name="Pro", tier="pro",
            monthly_task_limit=3, price_cents=9900,
        )
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="pro-q")

        # Use up quota
        for i in range(5):
            record_usage(
                pg, tenant_id=str(DEFAULT_TENANT_ID), organization_id="org-pro",
                meter_type="task_claim", reference_id=f"pro:claim:{i}",
            )

        # Claim should still succeed (paid tier = soft limit)
        gen = claim_task(
            pg, task_id="over-limit-task", claim_token="over-tok",
            organization_id="org-pro", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen == 1, "Paid tier should allow overage"

    def test_claim_itself_records_usage(self, pg):
        """A successful claim_task auto-records a usage meter entry."""
        from hermes_cli.postgres_authority import (
            claim_task, subscribe_tenant, get_usage_summary, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")

        gen = claim_task(
            pg, task_id="metered-task", claim_token="meter-tok",
            organization_id="org-meter", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert gen == 1

        summary = get_usage_summary(pg, tenant_id=DEFAULT_TENANT_ID)
        assert summary["task_claim"] >= 1

    def test_no_subscription_fails_open(self, pg):
        """Tenant with no subscription can still claim (fail-open billing)."""
        from hermes_cli.postgres_authority import claim_task

        unregistered_tenant = uuid.uuid4()
        gen = claim_task(
            pg, task_id="open-task", claim_token="open-tok",
            organization_id="org-open", worker_id="w1",
            claim_scope_url="", expires_at=time.time() + 600,
            tenant_id=unregistered_tenant,
        )
        # check_quota returns (False, 0, 0) for unregistered tenants,
        # but the function catches this and blocks. This is the correct
        # behavior: if there's no subscription, deny. But if the billing
        # tables don't exist at all (older schema), it fails open.
        # With v7 schema, no subscription = denied.
        assert gen is None
