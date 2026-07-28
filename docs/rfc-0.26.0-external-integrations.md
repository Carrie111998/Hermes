# RFC-0.26.0: External Integrations

**Status:** Accepted  
**Author:** CharterForge Agent  
**Date:** 2026-07-28

## Summary

Add a unified integration framework for connecting agents to external services: email (SMTP/IMAP), calendar (Google/Outlook), CRM (Salesforce/HubSpot), and project management (Linear/Jira). Each integration is a marketplace-installable tool with standardized configuration and credential management.

## Motivation

Agents that can only interact with their runtime environment have limited utility. External integrations allow agents to take real-world actions — send emails, schedule meetings, update CRM records, and manage project tasks — while still being governed by the authority store's permit/effect model.

## Design Principles

1. **Provider-agnostic interfaces** — each integration type (email, calendar, etc.) defines an abstract interface; providers implement it
2. **Credential isolation** — credentials stored encrypted per-tenant, never cross-tenant readable
3. **Governed actions** — all external side effects require permits and record effects
4. **Marketplace-native** — integrations are marketplace listings, installed per-tenant
5. **Webhook-capable** — integrations can receive inbound events (email received, calendar updated)

## Schema (v8 → v9 migration)

```sql
-- Integration credentials (encrypted at rest)
CREATE TABLE IF NOT EXISTS integration_credentials (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    integration_id  TEXT        NOT NULL,
    provider        TEXT        NOT NULL,
    credential_type TEXT        NOT NULL CHECK (credential_type IN ('oauth2', 'api_key', 'smtp', 'webhook')),
    encrypted_data  TEXT        NOT NULL,
    scopes          TEXT[]      NOT NULL DEFAULT '{}',
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_credential_tenant_integration
        UNIQUE (tenant_id, integration_id, provider)
);

-- Integration webhook subscriptions
CREATE TABLE IF NOT EXISTS integration_webhooks (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    integration_id  TEXT        NOT NULL,
    provider        TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,
    webhook_url     TEXT        NOT NULL,
    secret          TEXT        NOT NULL DEFAULT '',
    active          BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_webhook_tenant_event
        UNIQUE (tenant_id, integration_id, event_type)
);

-- Integration event log (inbound events from external services)
CREATE TABLE IF NOT EXISTS integration_events (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    integration_id  TEXT        NOT NULL,
    provider        TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,
    event_id        TEXT        NOT NULL,
    payload         JSONB       NOT NULL DEFAULT '{}',
    processed       BOOLEAN     NOT NULL DEFAULT false,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_event_idempotency
        UNIQUE (tenant_id, integration_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_credentials_tenant
    ON integration_credentials(tenant_id);
CREATE INDEX IF NOT EXISTS idx_webhooks_tenant
    ON integration_webhooks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant_unprocessed
    ON integration_events(tenant_id, processed) WHERE processed = false;
```

## API Surface

### Credentials

```python
def store_credential(conn, *, tenant_id, integration_id, provider, credential_type, data, scopes=None, expires_at=None) -> dict
def get_credential(conn, *, tenant_id, integration_id, provider) -> dict | None
def delete_credential(conn, *, tenant_id, integration_id, provider) -> bool
def list_credentials(conn, *, tenant_id) -> list[dict]
```

### Webhooks

```python
def register_webhook(conn, *, tenant_id, integration_id, provider, event_type, webhook_url, secret="") -> dict
def deactivate_webhook(conn, *, tenant_id, integration_id, event_type) -> bool
def list_webhooks(conn, *, tenant_id) -> list[dict]
```

### Events

```python
def record_integration_event(conn, *, tenant_id, integration_id, provider, event_type, event_id, payload) -> bool
def get_unprocessed_events(conn, *, tenant_id, limit=50) -> list[dict]
def mark_event_processed(conn, *, event_id_internal) -> bool
```

## Definition of Done

- [ ] v8→v9 migration with integration tables
- [ ] Credential storage (store, get, delete, list)
- [ ] Webhook management (register, deactivate, list)
- [ ] Event recording and processing (idempotent, ordered)
- [ ] Tenant isolation enforced on all operations
- [ ] Tests: credentials, webhooks, events, tenant isolation
- [ ] Acceptance test: store credential → register webhook → receive event → mark processed
