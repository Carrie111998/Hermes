# RFC-0.27.0: Monitoring & Observability

**Status:** Accepted  
**Author:** CharterForge Agent  
**Date:** 2026-07-28

## Summary

Add metrics collection, distributed tracing, and health dashboard capabilities. The system records operational metrics (task throughput, permit latency, effect counts) and exposes them via standard interfaces (Prometheus, OpenTelemetry) with per-tenant scoping.

## Schema (v9 → v10 migration)

```sql
-- Metric data points (time-series append-only)
CREATE TABLE IF NOT EXISTS metrics (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    metric_name     TEXT        NOT NULL,
    metric_type     TEXT        NOT NULL CHECK (metric_type IN ('counter', 'gauge', 'histogram')),
    value           DOUBLE PRECISION NOT NULL,
    labels          JSONB       NOT NULL DEFAULT '{}',
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Health check records
CREATE TABLE IF NOT EXISTS health_checks (
    id              BIGSERIAL   PRIMARY KEY,
    service_name    TEXT        NOT NULL,
    status          TEXT        NOT NULL CHECK (status IN ('healthy', 'degraded', 'unhealthy')),
    details         JSONB       NOT NULL DEFAULT '{}',
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Alert rules
CREATE TABLE IF NOT EXISTS alert_rules (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    name            TEXT        NOT NULL,
    metric_name     TEXT        NOT NULL,
    condition       TEXT        NOT NULL,
    threshold       DOUBLE PRECISION NOT NULL,
    window_seconds  INTEGER     NOT NULL DEFAULT 300,
    notify_channel  TEXT        NOT NULL DEFAULT '',
    enabled         BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_alert_rule_name UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS idx_metrics_tenant_name_time
    ON metrics(tenant_id, metric_name, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_checks_service
    ON health_checks(service_name, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_rules_tenant
    ON alert_rules(tenant_id);
```

## Definition of Done

- [ ] v9→v10 migration
- [ ] Metric recording (record, query by time range)
- [ ] Health check recording and retrieval
- [ ] Alert rule CRUD
- [ ] Tests
