# RFC-0.24.0: Billing Engine

**Status:** Accepted  
**Author:** CharterForge Agent  
**Date:** 2026-07-28

## Summary

Add usage metering, plan management, invoice generation, and Stripe-based payment collection to the multi-tenant authority platform. Every billable action (task claim, permit consumption, effect recording) is counted per-tenant and rolled up into invoices on configurable billing cycles.

## Motivation

v0.23.0 delivered tenant isolation with RBAC and quota fields on the tenant registry. Without a billing engine, quota limits are advisory-only and there is no path to revenue. The billing engine makes quotas enforceable and ties tenant usage to payment.

## Design Principles

1. **Metering at the authority layer** — count what the authority store already tracks (claims, permits, effects), not application-level abstractions
2. **Idempotent recording** — same event cannot be double-counted; relies on existing effect_key uniqueness
3. **Stripe as the payment backend** — subscriptions for base plans, usage records for overage
4. **Fail-open on billing errors** — metering failures log and alert but do not block task execution
5. **Quota enforcement at claim time** — hard limit checks happen in claim_task, before work begins

## Schema (v6 → v7 migration)

```sql
-- Billing plans
CREATE TABLE IF NOT EXISTS billing_plans (
    plan_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('free', 'starter', 'pro', 'enterprise')),
    monthly_task_limit INTEGER NOT NULL DEFAULT 0,
    monthly_permit_limit INTEGER NOT NULL DEFAULT 0,
    monthly_effect_limit INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER NOT NULL DEFAULT 0,
    stripe_price_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tenant subscription (1:1 with tenant)
CREATE TABLE IF NOT EXISTS tenant_subscriptions (
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    plan_id TEXT NOT NULL REFERENCES billing_plans(plan_id),
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    billing_cycle_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    billing_cycle_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'past_due', 'canceled', 'trialing')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id)
);

-- Usage meters (append-only, one row per billable event)
CREATE TABLE IF NOT EXISTS usage_meters (
    meter_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    organization_id TEXT NOT NULL,
    meter_type TEXT NOT NULL CHECK (meter_type IN ('task_claim', 'permit_consume', 'effect_record')),
    reference_id TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    billing_period TEXT NOT NULL,
    UNIQUE (meter_type, reference_id)
);

-- Invoice records
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    billing_period TEXT NOT NULL,
    line_items JSONB NOT NULL DEFAULT '[]',
    total_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'issued', 'paid', 'void')),
    stripe_invoice_id TEXT,
    issued_at TIMESTAMPTZ,
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, billing_period)
);

CREATE INDEX IF NOT EXISTS idx_usage_meters_tenant_period
    ON usage_meters(tenant_id, billing_period);
CREATE INDEX IF NOT EXISTS idx_usage_meters_type_ref
    ON usage_meters(meter_type, reference_id);
```

## API Surface

### Metering (internal, called by authority functions)

```python
def record_usage(conn, *, tenant_id, organization_id, meter_type, reference_id, billing_period) -> bool
def get_usage_summary(conn, *, tenant_id, billing_period) -> dict
def check_quota(conn, *, tenant_id, meter_type) -> tuple[bool, int, int]  # (allowed, used, limit)
```

### Plan Management

```python
def create_plan(conn, *, plan_id, name, tier, limits, price_cents, stripe_price_id=None) -> dict
def get_plan(conn, *, plan_id) -> dict | None
def list_plans(conn) -> list[dict]
```

### Subscription Management

```python
def subscribe_tenant(conn, *, tenant_id, plan_id, stripe_customer_id=None) -> dict
def get_subscription(conn, *, tenant_id) -> dict | None
def cancel_subscription(conn, *, tenant_id) -> bool
def update_subscription_status(conn, *, tenant_id, status) -> bool
```

### Invoice Management

```python
def generate_invoice(conn, *, tenant_id, billing_period) -> dict
def mark_invoice_paid(conn, *, invoice_id, stripe_invoice_id=None) -> bool
def get_invoice(conn, *, invoice_id) -> dict | None
def list_invoices(conn, *, tenant_id) -> list[dict]
```

## Integration Points

1. **claim_task** — after successful claim, call `record_usage(meter_type='task_claim')`. Before claim, call `check_quota()` and reject if over hard limit.
2. **consume_permit** — after successful consume, call `record_usage(meter_type='permit_consume')`
3. **record_effect** — after successful effect, call `record_usage(meter_type='effect_record')`
4. **Stripe webhooks** — `invoice.paid` → `mark_invoice_paid()`, `customer.subscription.updated` → `update_subscription_status()`

## Quota Enforcement

- Free tier: hard limit (claim_task returns None when over quota)
- Paid tiers: soft limit (warning at 80%, overage billed at rate)
- Enterprise: no limit (usage-based billing only)
- Quota checks are per billing period (calendar month by default)

## Billing Period

Format: `YYYY-MM` (e.g., `2026-07`). Determined from `recorded_at` timestamp. Rollup queries aggregate by this field.

## Definition of Done

- [ ] v6→v7 migration with billing tables
- [ ] record_usage + check_quota functions
- [ ] Plan CRUD operations
- [ ] Subscription management
- [ ] Invoice generation from usage meters
- [ ] Quota enforcement wired into claim_task
- [ ] Stripe subscription integration (create/cancel/webhook)
- [ ] Usage metering wired into consume_permit and record_effect
- [ ] Tests: quota enforcement, metering idempotency, invoice generation
- [ ] Acceptance test: full billing cycle (subscribe → use → invoice → pay)
