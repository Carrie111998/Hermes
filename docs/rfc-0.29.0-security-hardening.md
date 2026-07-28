# RFC-0.29.0: Security Hardening

**Status:** Accepted  
**Author:** CharterForge Agent  
**Date:** 2026-07-28

## Summary

Add comprehensive audit logging and secret management. Every security-relevant action is recorded in an immutable audit trail. Secrets are stored with envelope encryption and access is logged.

## Schema (v11 → v12 migration)

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    actor_type      TEXT        NOT NULL,
    actor_id        TEXT        NOT NULL,
    action          TEXT        NOT NULL,
    resource_type   TEXT        NOT NULL DEFAULT '',
    resource_id     TEXT        NOT NULL DEFAULT '',
    outcome         TEXT        NOT NULL CHECK (outcome IN ('success', 'denied', 'error')),
    details         JSONB       NOT NULL DEFAULT '{}',
    ip_address      TEXT        NOT NULL DEFAULT '',
    user_agent      TEXT        NOT NULL DEFAULT '',
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS secrets (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    secret_name     TEXT        NOT NULL,
    encrypted_value TEXT        NOT NULL,
    version         INTEGER     NOT NULL DEFAULT 1,
    created_by      TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rotated_at      TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,

    CONSTRAINT uq_secret_name UNIQUE (tenant_id, secret_name)
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_time
    ON audit_log(tenant_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor
    ON audit_log(tenant_id, actor_type, actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_resource
    ON audit_log(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_secrets_tenant
    ON secrets(tenant_id);
```
