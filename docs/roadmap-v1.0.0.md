# Charterforge Roadmap to v1.0.0

**Current Version:** v1.0.0 (2026-07-28)  
**Release Type:** Production release — feature-complete, documented, stable API  
**Gap:** None — all milestones delivered

---

## Version Target Matrix

| Version | Theme | Key Deliverable | State |
|---------|-------|-----------------|-------|
| **0.20.0** | Crash Recovery | Proven supervised worker lifecycle | ✅ Released |
| **0.21.0** | Payment Rails | ≥3 payment rail plugins working | ✅ Released |
| **0.22.0** | Postgres Authority Store | HA-capable authority database | ✅ Released |
| **0.23.0** | Multi-Tenant Isolation | Organization/workspace boundaries | ✅ Released |
| **0.24.0** | Billing Engine | Usage metering + invoicing | ✅ Released |
| **0.25.0** | Agent Marketplace | Tool/skill discovery + installation | ✅ Released |
| **0.26.0** | External Integrations | Email, calendar, CRM, project tools | ✅ Released |
| **0.27.0** | Monitoring & Observability | Metrics, traces, health dashboards | ✅ Released |
| **0.28.0** | Disaster Recovery | Backup/restore, failover drill | ✅ Released |
| **0.29.0** | Security Hardening | Audit logging, secret management | ✅ Released |
| **1.0.0** | Production Release | Feature-complete, documented, stable API | ✅ Released |

---

## v0.23.0 — Multi-Tenant Isolation (Released)

**Goal:** Secure tenant boundaries preventing cross-organization data leakage.

**Status:** ✅ Complete — schema, runtime, bridge, and acceptance tests all passing.

### Scope

- Organization-scoped authority store queries
- Tenant ID propagation through all agent runtime paths
- Workspace isolation for solo-founder deployments
- RBAC model with capability-based access control

### Completed

- [x] RFC accepted (docs/rfc-0.23.0-multi-tenant-isolation.md)
- [x] tenant_id column on all authority tables (schema v4)
- [x] tenants registry with per-tenant quota + suspension
- [x] v2→v3 + v3→v4 migrations (idempotent, fail-closed)
- [x] Cross-tenant attack vector tests (100% pass, 4 vectors)
- [x] Claim exclusivity invariant preserved (UNIQUE task_id, org_id only)
- [x] Runtime tenant context propagation (ContextVar + env fallback)
- [x] Objective worker binds tenant at registration
- [x] Workspace model (v4→v5 migration, tenant-scoped CRUD)
- [x] RBAC capability grants (v5→v6 migration, grant/revoke/check/list)
- [x] Gateway tenant_id extraction (env-based, session context propagation)
- [x] RBAC enforcement (enforce_capability with opt-in fail-open)
- [x] Documentation: migration guide and capability model

### Remaining

- [x] Legacy v1 schema detection (fail-closed on incompatible schema)
- [x] Decisive two-tenant, two-worker acceptance test (12-point scenario)
- [x] Backend contract invariant tests (7 invariants, 10 tests)
- [x] AuthorityBridge for runtime-to-Postgres integration
- [x] Wire AuthorityBridge into objective_service tick cycle
- [x] Production worker runtime test with Postgres backend (3 scenarios: full lifecycle, race exclusivity, crash recovery)

---

## v0.24.0 — Billing Engine (Next)

**Goal:** Usage metering, invoicing, and payment collection for multi-tenant SaaS.

**Status:** ✅ Complete — all scope items delivered and tested.

### Scope

- Usage metering: track task executions, permit consumption, effect recordings per tenant
- Meter aggregation: rollup hourly/daily usage into billable units
- Plan/tier management: free, starter, pro, enterprise with quota limits
- Invoice generation: periodic billing cycles with line items
- Payment integration: Stripe subscription + usage-based billing
- Quota enforcement: hard/soft limits on task claims per billing period
- Tenant billing dashboard: current usage, invoices, payment methods
- Webhook handlers: Stripe payment events → authority store state

### Completed

- [x] RFC accepted (docs/rfc-0.24.0-billing-engine.md)
- [x] v6→v7 migration with billing tables (plans, subscriptions, meters, invoices)
- [x] Plan CRUD operations (create, get, list)
- [x] Subscription management (subscribe, cancel, status updates)
- [x] Usage metering with idempotent recording (UNIQUE meter_type, reference_id)
- [x] Quota enforcement: free=hard limit, paid=soft limit, enterprise=unlimited
- [x] Quota enforcement wired into claim_task (blocks at authority layer)
- [x] Usage metering wired into claim_task, consume_permit, record_effect
- [x] Invoice generation from usage meters (base fee + overage line items)
- [x] Stripe integration (customer, subscription, usage reporting, webhook verify)
- [x] Webhook handlers: invoice.paid, payment_failed, subscription.updated/deleted
- [x] Billing API endpoints (REST: plans, subscription, usage, invoices, stripe webhook)
- [x] Tests: 23 billing + 4 quota enforcement + 5 Stripe webhook = 32 tests
- [x] Full billing cycle acceptance test (subscribe → use → invoice → pay)

### Prerequisites (from v0.23.0)

- [x] Tenant registry with quota fields
- [x] Organization-scoped task claims (metering anchor point)
- [x] RBAC capability model (billing:manage capability)
- [x] AuthorityBridge lifecycle tracking (countable events)

---

EOF
