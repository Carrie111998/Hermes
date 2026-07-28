# RFC-0.28.0: Disaster Recovery

**Status:** Accepted  
**Author:** CharterForge Agent  
**Date:** 2026-07-28

## Summary

Add backup/restore, point-in-time recovery metadata, and failover drill capabilities. The system tracks backup state, provides restore-point management, and allows tenants to verify their disaster recovery posture.

## Schema (v10 → v11 migration)

```sql
CREATE TABLE IF NOT EXISTS backup_records (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    backup_type     TEXT        NOT NULL CHECK (backup_type IN ('full', 'incremental', 'snapshot')),
    status          TEXT        NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    storage_path    TEXT        NOT NULL DEFAULT '',
    size_bytes      BIGINT      NOT NULL DEFAULT 0,
    schema_version  INTEGER     NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    metadata        JSONB       NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS restore_points (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    name            TEXT        NOT NULL,
    backup_id       BIGINT      REFERENCES backup_records(id),
    schema_version  INTEGER     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_restore_point_name UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS failover_drills (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    drill_type      TEXT        NOT NULL,
    status          TEXT        NOT NULL CHECK (status IN ('scheduled', 'running', 'passed', 'failed')),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    results         JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backups_tenant ON backup_records(tenant_id);
CREATE INDEX IF NOT EXISTS idx_restore_points_tenant ON restore_points(tenant_id);
CREATE INDEX IF NOT EXISTS idx_drills_tenant ON failover_drills(tenant_id);
```
