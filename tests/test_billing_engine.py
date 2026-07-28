"""Tests for the billing engine (v0.24.0).

Covers:
- Plan CRUD
- Subscription management
- Usage metering (idempotency, per-tenant isolation)
- Quota enforcement (free tier hard limit, paid tier soft limit, enterprise unlimited)
- Invoice generation (base fee, overage calculation, idempotency)
- Full billing cycle acceptance (subscribe → use → invoice → pay)

Requires:
  AUTHORITY_POSTGRES_TEST_URL="host=/var/run/postgresql port=5433 user=postgres dbname=charterforge_test"
"""

import os
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

    schema_name = f"billing_{uuid.uuid4().hex[:12]}"
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


class TestBillingPlans:
    def test_create_plan(self, pg):
        from hermes_cli.postgres_authority import create_plan, get_plan

        plan = create_plan(
            pg, plan_id="starter", name="Starter", tier="starter",
            monthly_task_limit=1000, monthly_permit_limit=500,
            monthly_effect_limit=2000, price_cents=2900,
        )
        assert plan["plan_id"] == "starter"
        assert plan["tier"] == "starter"
        assert plan["price_cents"] == 2900

        fetched = get_plan(pg, plan_id="starter")
        assert fetched["monthly_task_limit"] == 1000

    def test_create_plan_idempotent(self, pg):
        from hermes_cli.postgres_authority import create_plan

        create_plan(pg, plan_id="p1", name="P1", tier="free")
        plan2 = create_plan(pg, plan_id="p1", name="P1 Updated", tier="free")
        assert plan2["name"] == "P1"  # ON CONFLICT DO NOTHING keeps original

    def test_list_plans_includes_seeded_free(self, pg):
        from hermes_cli.postgres_authority import list_plans

        plans = list_plans(pg)
        assert any(p["plan_id"] == "free" for p in plans)

    def test_create_plan_invalid_tier_rejected(self, pg):
        from hermes_cli.postgres_authority import create_plan
        import psycopg

        with pytest.raises(psycopg.errors.CheckViolation):
            create_plan(pg, plan_id="bad", name="Bad", tier="invalid")
        pg.rollback()


class TestSubscriptions:
    def test_subscribe_tenant(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, get_subscription, create_plan, DEFAULT_TENANT_ID,
        )

        create_plan(pg, plan_id="pro", name="Pro", tier="pro", price_cents=9900)
        sub = subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="pro")
        assert sub["plan_id"] == "pro"
        assert sub["status"] == "active"

        fetched = get_subscription(pg, tenant_id=DEFAULT_TENANT_ID)
        assert fetched["plan_id"] == "pro"

    def test_subscribe_upserts(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, get_subscription, create_plan, DEFAULT_TENANT_ID,
        )

        create_plan(pg, plan_id="starter", name="Starter", tier="starter")
        create_plan(pg, plan_id="pro", name="Pro", tier="pro")

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="starter")
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="pro")

        sub = get_subscription(pg, tenant_id=DEFAULT_TENANT_ID)
        assert sub["plan_id"] == "pro"

    def test_cancel_subscription(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, cancel_subscription, get_subscription, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")
        result = cancel_subscription(pg, tenant_id=DEFAULT_TENANT_ID)
        assert result is True

        sub = get_subscription(pg, tenant_id=DEFAULT_TENANT_ID)
        assert sub["status"] == "canceled"

    def test_update_subscription_status(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, update_subscription_status,
            get_subscription, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")
        update_subscription_status(pg, tenant_id=DEFAULT_TENANT_ID, status="past_due")

        sub = get_subscription(pg, tenant_id=DEFAULT_TENANT_ID)
        assert sub["status"] == "past_due"


class TestUsageMetering:
    def test_record_usage(self, pg):
        from hermes_cli.postgres_authority import record_usage, DEFAULT_TENANT_ID

        result = record_usage(
            pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
            meter_type="task_claim", reference_id="claim:task-1:gen1",
            billing_period="2026-07",
        )
        assert result is True

    def test_record_usage_idempotent(self, pg):
        from hermes_cli.postgres_authority import record_usage, DEFAULT_TENANT_ID

        record_usage(
            pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
            meter_type="task_claim", reference_id="claim:dup-1",
            billing_period="2026-07",
        )
        dup = record_usage(
            pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
            meter_type="task_claim", reference_id="claim:dup-1",
            billing_period="2026-07",
        )
        assert dup is False

    def test_usage_summary(self, pg):
        from hermes_cli.postgres_authority import (
            record_usage, get_usage_summary, DEFAULT_TENANT_ID,
        )

        for i in range(3):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
                meter_type="task_claim", reference_id=f"claim:sum-{i}",
                billing_period="2026-07",
            )
        record_usage(
            pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
            meter_type="permit_consume", reference_id="permit:sum-1",
            billing_period="2026-07",
        )

        summary = get_usage_summary(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        assert summary["task_claim"] == 3
        assert summary["permit_consume"] == 1
        assert summary["effect_record"] == 0

    def test_usage_isolated_by_tenant(self, pg):
        from hermes_cli.postgres_authority import (
            record_usage, get_usage_summary,
        )

        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())

        record_usage(
            pg, tenant_id=tenant_a, organization_id="org-a",
            meter_type="task_claim", reference_id="claim:iso-a",
            billing_period="2026-07",
        )
        record_usage(
            pg, tenant_id=tenant_b, organization_id="org-b",
            meter_type="task_claim", reference_id="claim:iso-b",
            billing_period="2026-07",
        )

        summary_a = get_usage_summary(pg, tenant_id=tenant_a, billing_period="2026-07")
        summary_b = get_usage_summary(pg, tenant_id=tenant_b, billing_period="2026-07")
        assert summary_a["task_claim"] == 1
        assert summary_b["task_claim"] == 1


class TestQuotaEnforcement:
    def test_free_tier_enforces_hard_limit(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, record_usage, check_quota, DEFAULT_TENANT_ID,
        )

        # Free plan: 100 task claims
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")

        # Use up the quota
        for i in range(100):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
                meter_type="task_claim", reference_id=f"quota:free:{i}",
                billing_period="2026-07",
            )

        allowed, used, limit = check_quota(pg, tenant_id=DEFAULT_TENANT_ID, meter_type="task_claim")
        assert allowed is False
        assert used == 100
        assert limit == 100

    def test_paid_tier_allows_overage(self, pg):
        from hermes_cli.postgres_authority import (
            create_plan, subscribe_tenant, record_usage, check_quota, DEFAULT_TENANT_ID,
        )

        create_plan(
            pg, plan_id="pro", name="Pro", tier="pro",
            monthly_task_limit=5, price_cents=9900,
        )
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="pro")

        for i in range(10):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
                meter_type="task_claim", reference_id=f"quota:pro:{i}",
                billing_period="2026-07",
            )

        allowed, used, limit = check_quota(pg, tenant_id=DEFAULT_TENANT_ID, meter_type="task_claim")
        assert allowed is True  # Soft limit — overage billed
        assert used == 10
        assert limit == 5

    def test_enterprise_unlimited(self, pg):
        from hermes_cli.postgres_authority import (
            create_plan, subscribe_tenant, record_usage, check_quota, DEFAULT_TENANT_ID,
        )

        create_plan(
            pg, plan_id="enterprise", name="Enterprise", tier="enterprise",
            monthly_task_limit=0, price_cents=49900,
        )
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="enterprise")

        for i in range(50):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
                meter_type="task_claim", reference_id=f"quota:ent:{i}",
                billing_period="2026-07",
            )

        allowed, used, limit = check_quota(pg, tenant_id=DEFAULT_TENANT_ID, meter_type="task_claim")
        assert allowed is True
        assert used == 50
        assert limit == 0  # No limit for enterprise

    def test_canceled_subscription_denies(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, cancel_subscription, check_quota, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")
        cancel_subscription(pg, tenant_id=DEFAULT_TENANT_ID)

        allowed, used, limit = check_quota(pg, tenant_id=DEFAULT_TENANT_ID, meter_type="task_claim")
        assert allowed is False


class TestInvoiceGeneration:
    def test_generate_invoice_empty_usage(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, generate_invoice, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")
        invoice = generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        assert invoice["total_cents"] == 0
        assert invoice["status"] == "draft"

    def test_generate_invoice_with_base_fee(self, pg):
        from hermes_cli.postgres_authority import (
            create_plan, subscribe_tenant, generate_invoice, DEFAULT_TENANT_ID,
        )

        create_plan(pg, plan_id="starter", name="Starter", tier="starter", price_cents=2900)
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="starter")

        invoice = generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        assert invoice["total_cents"] == 2900

    def test_generate_invoice_with_overage(self, pg):
        from hermes_cli.postgres_authority import (
            create_plan, subscribe_tenant, record_usage,
            generate_invoice, DEFAULT_TENANT_ID,
        )

        create_plan(
            pg, plan_id="starter", name="Starter", tier="starter",
            monthly_task_limit=5, price_cents=2900,
        )
        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="starter")

        # Record 8 claims (5 included + 3 overage @ 10 cents each = 30 cents)
        for i in range(8):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org1",
                meter_type="task_claim", reference_id=f"inv:over:{i}",
                billing_period="2026-07",
            )

        invoice = generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        assert invoice["total_cents"] == 2900 + 30  # base + 3 * $0.10

    def test_generate_invoice_idempotent(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, generate_invoice, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")
        inv1 = generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        inv2 = generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        assert inv1["invoice_id"] == inv2["invoice_id"]

    def test_mark_invoice_paid(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, generate_invoice, mark_invoice_paid,
            get_invoice, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")
        invoice = generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")

        result = mark_invoice_paid(
            pg, invoice_id=str(invoice["invoice_id"]),
            stripe_invoice_id="in_test_123",
        )
        assert result is True

        paid = get_invoice(pg, invoice_id=str(invoice["invoice_id"]))
        assert paid["status"] == "paid"
        assert paid["stripe_invoice_id"] == "in_test_123"

    def test_list_invoices(self, pg):
        from hermes_cli.postgres_authority import (
            subscribe_tenant, generate_invoice, list_invoices, DEFAULT_TENANT_ID,
        )

        subscribe_tenant(pg, tenant_id=DEFAULT_TENANT_ID, plan_id="free")
        generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-06")
        generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")

        invoices = list_invoices(pg, tenant_id=DEFAULT_TENANT_ID)
        assert len(invoices) == 2
        assert invoices[0]["billing_period"] == "2026-07"  # DESC order


class TestFullBillingCycleAcceptance:
    """Acceptance test: subscribe → use → invoice → pay."""

    def test_full_cycle(self, pg):
        from hermes_cli.postgres_authority import (
            create_plan, subscribe_tenant, record_usage,
            check_quota, generate_invoice, mark_invoice_paid,
            get_invoice, get_usage_summary, DEFAULT_TENANT_ID,
        )

        # 1. Create a starter plan
        create_plan(
            pg, plan_id="starter", name="Starter", tier="starter",
            monthly_task_limit=10, monthly_permit_limit=10,
            monthly_effect_limit=50, price_cents=2900,
            stripe_price_id="price_test_starter",
        )

        # 2. Subscribe tenant
        sub = subscribe_tenant(
            pg, tenant_id=DEFAULT_TENANT_ID, plan_id="starter",
            stripe_customer_id="cus_test_001",
        )
        assert sub["status"] == "active"

        # 3. Record usage (12 claims = 10 included + 2 overage)
        for i in range(12):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org-cycle",
                meter_type="task_claim", reference_id=f"cycle:claim:{i}",
                billing_period="2026-07",
            )

        # Also record 3 permits and 5 effects (within limits)
        for i in range(3):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org-cycle",
                meter_type="permit_consume", reference_id=f"cycle:permit:{i}",
                billing_period="2026-07",
            )
        for i in range(5):
            record_usage(
                pg, tenant_id=DEFAULT_TENANT_ID, organization_id="org-cycle",
                meter_type="effect_record", reference_id=f"cycle:effect:{i}",
                billing_period="2026-07",
            )

        # 4. Verify usage summary
        summary = get_usage_summary(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        assert summary["task_claim"] == 12
        assert summary["permit_consume"] == 3
        assert summary["effect_record"] == 5

        # 5. Check quota (still allowed - soft limit)
        allowed, used, limit = check_quota(
            pg, tenant_id=DEFAULT_TENANT_ID, meter_type="task_claim"
        )
        assert allowed is True
        assert used == 12
        assert limit == 10

        # 6. Generate invoice
        invoice = generate_invoice(pg, tenant_id=DEFAULT_TENANT_ID, billing_period="2026-07")
        assert invoice["status"] == "draft"
        # Base $29.00 + 2 task overage × $0.10 = $29.20
        assert invoice["total_cents"] == 2920

        # 7. Simulate Stripe webhook: payment received
        paid = mark_invoice_paid(
            pg, invoice_id=str(invoice["invoice_id"]),
            stripe_invoice_id="in_stripe_001",
        )
        assert paid is True

        # 8. Verify final state
        final = get_invoice(pg, invoice_id=str(invoice["invoice_id"]))
        assert final["status"] == "paid"
        assert final["paid_at"] is not None
