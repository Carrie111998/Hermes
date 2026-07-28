# RFC-0.23.0: Multi-Tenant Isolation

**Author:** Hermes  
**Date:** 2026-07-28  
**Status:** Draft  
**Version:** v0.23.0  
**Blocked on:** v0.22.0 (Postgres Authority Store)

---

## Abstract

v0.22.0 introduced a production HA-capable authority database with exclusive task claims, lease-generation fencing, and schema migrations. v0.23.0 extends this with **tenant-scoped isolation** to prevent cross-organization data leakage, resource quota abuse, and identity confusion across workers executing on shared infrastructure.

---

## Goal

Secure tenant boundaries preventing:

1. **Identity leakage** — Worker must never mistake org context
2. **Resource quota abuse** — Per-org limits on concurrent claims
3. **Audit trail integrity** — All operations logged with tenant context

---

## Scope

- Organization-scoped authority store queries
- Tenant ID propagation through all agent runtime paths
- Workspace isolation for solo-founder deployments
- RBAC model with capability-based access control

---

## Non-Goals (v0.23.0)

- Multi-org billing (v0.24.0 Billing Engine)
- Org self-service provisioning (v0.25.0)
- Cross-org resource sharing (v0.28.0 DR/failover)

---

## Backed Model: Postgres Authority Store

**Current (v0.22.0):** Single-schema Postgres with `claims`, `leases`, `effects`, `perms` tables.

**Problem:** No enforcement of tenant isolation. A worker connected to one schema could theoretically access tables in another schema if `search_path` manipulation succeeds.

**Requirement:** Tenant ID **MUST** be explicit in every query, never implicit via schema inference.

### Schema Design

```
-- v0.22.0: single-tenant (insecure on shared DB)
claims (id, task_id, organization_id, state, lease_generation, ...

-- v0.23.0: multi-tenant explicit
tenants (id, slug, name, created_at, active_at, suspended_at)
schema_versions (tenant_id references tenants, version, applied_at)
claims (id, task_id, organization_id, tenant_id references tenants, state, lease_generation, ...
```

**Migration path:**  
- v0.22.0 → v0.23.0: Add `tenant_id` column to all tables, backfill from singleton `default` tenant, make `NOT NULL`.

---

## Tenant Identity Model

### Single-Tenant (v0.20.0–v0.22.0)

- One instance = one org
- `organization_id` is implicit via DB connection (schema name or default)
- No explicit tenant routing in runtime

### Multi-Tenant (v0.23.0+)

- One DB cluster = many orgs
- Each request **MUST** carry `tenant_id` as a first-class header
- Workers derive `tenant_id` from authenticated credential → org mapping

### Identity Propagation Chain

```
Incoming Request
       ↓
Gateway (extract tenant_id from credential)
       ↓
Agent Runtime (inject tenant_id into all DB calls)
       ↓
Postgres Authority Store (WHERE tenant_id = ?)
```

**Invariant:** `tenant_id` **MUST NOT** be inferred from context; it is explicitly injected at entry and propagated through all internal calls.

---

## Workspace Isolation Model

Solo-founder deployments currently use a single "workspace" per organization. v0.23.0 formalizes this into a first-class resource.

### Workspace Schema

```
workspace_id UUID PRIMARY KEY
tenant_id   UUID NOT NULL REFERENCES tenants(id)
name        TEXT NOT NULL
slug        TEXT NOT NULL UNIQUE
owner_id    UUID NOT NULL
created_at  TIMESTAMP NOT NULL
updated_at  TIMESTAMP NOT NULL
active      BOOLEAN NOT NULL DEFAULT true
```

### Scope Resolution

```
Request → tenant_id + workspace_id → authority scope
```

**Rule:** All queries inside a tenant must be scoped to **both** `tenant_id` and `workspace_id`.

**Exception:** Global lookup tables (`schemas`, `migrations`, `roles`) are not scoped.

---

## RBAC Capability Model

Permissions are capability-based, not role-based. Each task execution carries a set of capabilities.

### Capability Grammar

```
capability := <resource>:<action>:<scope>

examples:
  task:claim:workspace={workspace_id}
  task:complete:workspace={workspace_id}
  audit:read:tenant={tenant_id}
  payment:write:workspace={workspace_id}
  user:read:org={org_id}
```

### Enforcement Points

1. **Claim request** — Worker must hold `task:claim:workspace=X`
2. **Complete request** — Worker must hold `task:complete:workspace=X`
3. **Audit execution** — Agent must hold `audit:execute:workspace=X`

### Capability Derivation

```
Worker credential
     ↓
[JWT claim or DB lookup]
     ↓
{tenant_id, workspace_id, capabilities[]}
```

**Policy:** Capabilities are **never** amplified. A worker cannot gain more permission than its credential grants.

---

## Migration Plan

### Phase 1: Schema Additive Changes

1. Add `tenant_id` column (nullable) to all existing tables
2. Backfill `tenant_id = 'default'` for existing records
3. Add foreign key constraint `REFERENCES tenants(id)`
4. Make `tenant_id` NOT NULL after data integrity verified

### Phase 2: Runtime Injection

1. Gateway extracts `tenant_id` from authenticated request
2. Agent runtime injects `tenant_id` into all authority store calls
3. Postgres queries updated: `WHERE tenant_id = ? AND ...`

### Phase 3: Test Coverage

1. Unit tests: 95% coverage of tenant-scoped queries
2. Integration test: Simulate cross-tenant request leakage (must fail)
3. Adversarial test: Malicious `search_path` manipulation (must be rejected)

### Phase 4: Production Rollout

1. Deploy with dual-mode support (`tenant_id` optional → later required)
2. Monitor for queries without `tenant_id` (log warning)
3. Enforce `tenant_id` on v0.24.0+

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Identity leakage via schema name | Enforce `search_path` is read-only, controlled by `OPTIONS=-c search_path=...` in connection string |
| Worker confusion via stale context | Lease generation fencing token fences stale workers; `tenant_id` checked on every request |
| Resource quota abuse | Per-org claim limits enforced at claim time, not DB level |
| Audit trail corruption | All operations log `tenant_id` in `audit_logs` table |

---

## Definition of Done

- [ ] RFC accepted
- [ ] Migration plan implemented
- [ ] Unit tests for tenant-scoped queries (95% coverage target)
- [ ] Integration test simulating cross-tenant attack vector (100% pass)
- [ ] Documentation: Migration guide, capability model, workspace management

---

## Timeline Estimate

- Design: 2 days
- Implementation: 5 days
- Testing: 2 days
- Documentation: 1 day

**Total:** ~10 business days

---

## References

- v0.22.0 Postgres Authority Store spec
- RFC-0.24.0 Billing Engine (planned dependency)
- v1.0.0 production readiness checklist

---

**Status:** Draft — awaiting acceptance before implementation.
