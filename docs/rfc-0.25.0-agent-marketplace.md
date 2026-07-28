# RFC-0.25.0: Agent Marketplace

**Status:** Accepted  
**Author:** CharterForge Agent  
**Date:** 2026-07-28

## Summary

Add a tool/skill discovery and installation system that allows tenants to browse, install, and manage agent capabilities (tools, skills, plugins) from a registry. The marketplace enables composable agent functionality without requiring code changes to the core runtime.

## Motivation

Agents become more useful as their tool repertoire grows, but hard-coding tools into the runtime couples deployment to capability expansion. A marketplace decouples these concerns: the registry holds metadata, the runtime resolves installed tools at execution time.

## Design Principles

1. **Registry is metadata** — the marketplace stores manifests, not code. Code lives in packages (pip, git, or bundled).
2. **Tenant-scoped installation** — each tenant sees only their installed tools; no cross-tenant leakage.
3. **Capability-gated** — installing a tool requires the `marketplace:install` RBAC capability.
4. **Version-pinned** — installations pin to a specific version; upgrades are explicit.
5. **Dependency-aware** — tools can declare dependencies on other tools or minimum runtime version.

## Schema (v7 → v8 migration)

```sql
-- Marketplace listings (registry of available tools/skills)
CREATE TABLE IF NOT EXISTS marketplace_listings (
    listing_id      TEXT        PRIMARY KEY,
    name            TEXT        NOT NULL,
    description     TEXT        NOT NULL DEFAULT '',
    category        TEXT        NOT NULL DEFAULT 'general',
    author          TEXT        NOT NULL DEFAULT '',
    version         TEXT        NOT NULL,
    package_type    TEXT        NOT NULL CHECK (package_type IN ('builtin', 'pip', 'git', 'bundle')),
    package_ref     TEXT        NOT NULL DEFAULT '',
    entry_point     TEXT        NOT NULL DEFAULT '',
    capabilities_required TEXT[] NOT NULL DEFAULT '{}',
    dependencies    JSONB       NOT NULL DEFAULT '[]',
    metadata        JSONB       NOT NULL DEFAULT '{}',
    published_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active          BOOLEAN     NOT NULL DEFAULT true
);

-- Tenant installations (which tools each tenant has)
CREATE TABLE IF NOT EXISTS marketplace_installations (
    id              BIGSERIAL   PRIMARY KEY,
    tenant_id       UUID        NOT NULL,
    listing_id      TEXT        NOT NULL REFERENCES marketplace_listings(listing_id),
    version         TEXT        NOT NULL,
    installed_by    TEXT        NOT NULL DEFAULT '',
    installed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enabled         BOOLEAN     NOT NULL DEFAULT true,
    config          JSONB       NOT NULL DEFAULT '{}',

    CONSTRAINT uq_installation_tenant_listing UNIQUE (tenant_id, listing_id)
);

CREATE INDEX IF NOT EXISTS idx_installations_tenant
    ON marketplace_installations(tenant_id);
CREATE INDEX IF NOT EXISTS idx_installations_listing
    ON marketplace_installations(listing_id);

-- Marketplace reviews/ratings
CREATE TABLE IF NOT EXISTS marketplace_reviews (
    id              BIGSERIAL   PRIMARY KEY,
    listing_id      TEXT        NOT NULL REFERENCES marketplace_listings(listing_id),
    tenant_id       UUID        NOT NULL,
    rating          INTEGER     NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_text     TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_review_tenant_listing UNIQUE (tenant_id, listing_id)
);
```

## API Surface

### Listings (public catalog)

```python
def publish_listing(conn, *, listing_id, name, description, category, author, version, package_type, package_ref, entry_point, capabilities_required=None, dependencies=None, metadata=None) -> dict
def get_listing(conn, *, listing_id) -> dict | None
def search_listings(conn, *, query="", category="", limit=50) -> list[dict]
def update_listing_version(conn, *, listing_id, version, package_ref="") -> bool
```

### Installations (tenant-scoped)

```python
def install_tool(conn, *, tenant_id, listing_id, version, installed_by="", config=None) -> dict
def uninstall_tool(conn, *, tenant_id, listing_id) -> bool
def get_installed_tools(conn, *, tenant_id) -> list[dict]
def enable_tool(conn, *, tenant_id, listing_id) -> bool
def disable_tool(conn, *, tenant_id, listing_id) -> bool
def get_tool_config(conn, *, tenant_id, listing_id) -> dict | None
def update_tool_config(conn, *, tenant_id, listing_id, config) -> bool
```

### Reviews

```python
def submit_review(conn, *, listing_id, tenant_id, rating, review_text="") -> dict
def get_reviews(conn, *, listing_id, limit=20) -> list[dict]
```

## Built-in Seed Listings

The migration seeds core tools that ship with the runtime:
- `hermes.web-search` — Web search via configured provider
- `hermes.code-exec` — Sandboxed code execution
- `hermes.file-ops` — File system operations
- `hermes.calendar` — Calendar integration
- `hermes.email` — Email send/receive

## Definition of Done

- [ ] v7→v8 migration with marketplace tables
- [ ] Listing CRUD (publish, get, search, update version)
- [ ] Installation management (install, uninstall, enable, disable, configure)
- [ ] Reviews system
- [ ] RBAC enforcement on install (marketplace:install capability)
- [ ] Seed built-in listings
- [ ] Tests: listings, installations, reviews, RBAC gating
- [ ] Acceptance test: publish → install → resolve at runtime
