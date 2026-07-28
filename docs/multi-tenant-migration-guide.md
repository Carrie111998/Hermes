# Multi-Tenant Migration Guide (v0.23.0)

This guide covers upgrading from a single-tenant Charterforge deployment to
the multi-tenant authority store introduced in v0.23.0.

## Prerequisites

- Charterforge v0.22.0+ with Postgres authority store active
- `AUTHORITY_POSTGRES_URL` or `DATABASE_URL` pointing to your authority database

## Schema Migration

Schema migrations are applied automatically on startup via `init_schema()`.
The v0.23.0 release adds migrations v2→v3 through v5→v6:

| Migration | Change |
|-----------|--------|
| v2→v3 | Adds `tenant_id` column to `task_claims`, `task_runs`, `permits`, `execution_effects` |
| v3→v4 | Creates `tenants` table with quota and suspension support |
| v4→v5 | Creates `workspaces` table for sub-tenant isolation |
| v5→v6 | Creates `capability_grants` table for RBAC |

All migrations are idempotent and transactional. A failed migration rolls
back only the failing step; prior successful migrations are preserved.

### Existing Data

Existing rows receive `DEFAULT_TENANT_ID` (`00000000-0000-0000-0000-000000000000`)
for the `tenant_id` column. No manual data migration is required.

## Tenant Configuration

### Single-Tenant Deployment (Default)

No configuration change required. All operations use `DEFAULT_TENANT_ID`
automatically when no tenant is specified.

### Environment-Based Tenant Binding

Set these environment variables to bind a deployment to a specific tenant:

```bash
export HERMES_TENANT_ID="<uuid>"
export HERMES_ORGANIZATION_ID="<organization-slug-or-id>"
```

The gateway and objective worker read these at session entry and propagate
them via ContextVars to all downstream authority store calls.

### Creating a Tenant

```python
from uuid import uuid4
from hermes_cli.postgres_authority import connect, init_schema, create_tenant

conn = connect()
init_schema(conn)

tenant_id = uuid4()
create_tenant(conn, tenant_id=tenant_id, slug="acme-corp")
```

### Tenant Suspension

Suspended tenants cannot create new claims:

```python
from hermes_cli.postgres_authority import suspend_tenant, activate_tenant

suspend_tenant(conn, tenant_id=tenant_id)   # blocks new claims
activate_tenant(conn, tenant_id=tenant_id)  # restores access
```

## Workspace Model

Workspaces provide sub-tenant isolation for teams or projects:

```python
from uuid import uuid4
from hermes_cli.postgres_authority import create_workspace

workspace_id = uuid4()
create_workspace(
    conn,
    workspace_id=workspace_id,
    tenant_id=tenant_id,
    slug="engineering",
    display_name="Engineering Team",
)
```

Workspaces are tenant-scoped: listing or deactivating workspaces requires
the owning `tenant_id`.

## RBAC Capability Model

### Grammar

Capabilities follow the grammar: `resource:action:scope`

- **resource**: The entity type (e.g., `task`, `payment`, `objective`, `audit`)
- **action**: The operation (e.g., `claim`, `complete`, `write`, `read`, `approve`)
- **scope**: Contextual restriction (e.g., `workspace=engineering`, `*` for all)

### Granting Capabilities

```python
from hermes_cli.postgres_authority import grant_capability

grant_capability(
    conn,
    tenant_id=tenant_id,
    principal_type="worker",
    principal_id="worker-001",
    resource="task",
    action="claim",
    scope="workspace=engineering",
)
```

### Enforcement Semantics

`enforce_capability()` uses **opt-in fail-open** semantics:

- **No grants configured for tenant**: All operations pass (backward compatible)
- **Any grant exists for tenant**: Principals must hold explicit grants

This enables gradual rollout — add grants for privileged operations first,
then gate additional operations as the RBAC model matures.

```python
from hermes_cli.postgres_authority import enforce_capability

# Passes silently if no grants exist for the tenant (fail-open)
# Raises PermissionError if grants exist but principal lacks this one
enforce_capability(
    conn,
    tenant_id=tenant_id,
    principal_type="worker",
    principal_id="worker-001",
    resource="task",
    action="claim",
    scope="workspace=engineering",
)
```

### Wildcard Scope

A grant with `scope="*"` matches any scope check:

```python
grant_capability(
    conn, tenant_id=tenant_id,
    principal_type="role", principal_id="admin",
    resource="task", action="claim", scope="*",
)
```

### TTL and Revocation

Grants can be time-bounded or explicitly revoked:

```python
# Grant expires in 1 hour
grant_capability(conn, ..., ttl_seconds=3600)

# Explicit revocation
revoke_capability(
    conn, tenant_id=tenant_id,
    principal_type="worker", principal_id="worker-001",
    resource="task", action="claim", scope="*",
)
```

## Security Invariants

1. **Claim exclusivity**: UNIQUE constraint on `(task_id, organization_id)` only — tenant_id is excluded to prevent cross-tenant double-claiming of the same task
2. **Non-amplifiable**: A capability in tenant A is invisible to tenant B
3. **Non-transferable**: Grants are bound to a specific principal identity
4. **Fail-closed on unknown schema**: If the database is on a version ahead of the running code, startup refuses to proceed
5. **Fencing tokens**: Every claim carries a monotonic `lease_generation`; stale workers cannot complete tasks they no longer own
