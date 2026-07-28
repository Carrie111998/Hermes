# Charterforge Roadmap to v1.0.0

**Current Version:** v0.22.0 (2026-07-28)  
**Release Type:** Production HA-capable authority database  
**Gap:** Multi-tenant isolation and billing engine

---

## Version Target Matrix

| Version | Theme | Key Deliverable | State |
|---------|-------|-----------------|-------|
| **0.20.0** | Crash Recovery | Proven supervised worker lifecycle | ✅ Released |
| **0.21.0** | Payment Rails | ≥3 payment rail plugins working | ✅ Released |
| **0.22.0** | Postgres Authority Store | HA-capable authority database | ✅ Released |
| **0.23.0** | Multi-Tenant Isolation | Organization/workspace boundaries | 🟡 In Progress |
| **0.24.0** | Billing Engine | Usage metering + invoicing | 🔲 |
| **0.25.0** | Agent Marketplace | Tool/skill discovery + installation | 🔲 |
| **0.26.0** | External Integrations | Email, calendar, CRM, project tools | 🔲 |
| **0.27.0** | Monitoring & Observability | Metrics, traces, health dashboards | 🔲 |
| **0.28.0** | Disaster Recovery | Backup/restore, failover drill | 🔲 |
| **0.29.0** | Security Hardening | Audit logging, secret management | 🔲 |
| **1.0.0** | Production Release | Feature-complete, documented, stable API | 🔲 |

---

## v0.23.0 — Multi-Tenant Isolation (Next)

**Goal:** Secure tenant boundaries preventing cross-organization data leakage.

**Status:** 🟡 In progress — RFC accepted, schema + tests implemented.

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

### Remaining

- [ ] Gateway tenant_id extraction from authenticated credential
- [ ] Agent runtime tenant_id injection into all DB calls
- [ ] Workspace model (workspace_id scoping)
- [ ] RBAC capability grammar enforcement
- [ ] Documentation: migration guide, capability model

---

EOF
