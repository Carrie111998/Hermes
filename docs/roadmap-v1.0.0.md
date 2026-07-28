# Charterforge Roadmap to v1.0.0

**Current Version:** v0.20.0 (2026-07-27)  
**Release Type:** Defensible — working as designed, crash recovery proven  
**Gap:** Feature-complete autonomous business OS

---

## Version Target Matrix

| Version | Theme | Key Deliverable | State |
|---------|-------|-----------------|-------|
| **0.20.0** | Crash Recovery | Proven supervised worker lifecycle | ✅ Released |
| **0.21.0** | Payment Rails | ≥3 payment rail plugins working | 🔲 Next |
| **0.22.0** | Postgres Authority Store | HA-capable authority database | 🔲 |
| **0.23.0** | Multi-Tenant Isolation | Organization/workspace boundaries | 🔲 |
| **0.24.0** | Billing Engine | Usage metering + invoicing | 🔲 |
| **0.25.0** | Agent Marketplace | Tool/skill discovery + installation | 🔲 |
| **0.26.0** | External Integrations | Email, calendar, CRM, project tools | 🔲 |
| **0.27.0** | Monitoring & Observability | Metrics, traces, health dashboards | 🔲 |
| **0.28.0** | Disaster Recovery | Backup/restore, failover drill | 🔲 |
| **0.29.0** | Security Hardening | Audit logging, secret management | 🔲 |
| **1.0.0** | Production Release | Feature-complete, documented, stable API | 🔲 |

---

## v0.21.0 — Payment Rails (In Progress)

**Goal:** User can receive funding from ≥3 payment rail options

**Status:** 🔶 Payment rail packages built, documented, and tested. CI workflow installed.

### Deliverables

- [x] `charterforge-stripe-rail` package (card payments, webhooks)
- [x] `charterforge-nevermined-rail` package (agent-to-agent USDC)
- [x] `charterforge-circle-rail` package (native USDC, CCTP cross-chain)
- [x] Entry points registered in all packages
- [x] README documentation for each rail
- [x] Build verification (wheel + sdist)
- [x] GitHub release assets updated
- [x] Integration tests for Stripe payment validation (7 tests)
- [x] CI workflow for all payment rail packages

**Ready for v0.21.0 release candidate.**

### Deliverables

- [ ] **Nevermined Rail** (`charterforge-nevermined-rail`)
  - Agent-to-agent payments via Universal Agent ID
  - Instant settlement, non-custodial
  - Entry points: `charterforge.inbound_payment_rails`, `charterforge.outbound_payment_rails`
  - Credential: `NEVERMINED_API_KEY`

- [ ] **Circle Rail** (`charterforge-circle-rail`)
  - USDC cross-chain transfers via CCTP
  - Permissionless (no API key for basic transfers)
  - Gateway API for multi-chain balance
  - Entry points: `charterforge.inbound_payment_rails`, `charterforge.outbound_payment_rails`

- [ ] **Payment Rail Selector**
  - Charter file can declare preferred rail: `"inbound_rail": "stripe"`, `"outbound_rail": "wise"`
  - CLI: `charterforge business payment-rails --configure`
  - Webhook routing to correct rail based on event signature

### Validation

```bash
# Install all rails
pip install charterforge-stripe-rail charterforge-nevermined-rail charterforge-circle-rail

# Verify all rails discoverable
charterforge business payment-rails --check

# Test inbound webhook routing
charterforge business payment-rails --test-webhook --rail stripe
charterforge business payment-rails --test-webhook --rail nevermined
```

### Definition of Done

- [ ] 3 payment rail packages build and install
- [ ] Rails auto-discover via entry points
- [ ] At least 1 end-to-end payment flow tested per rail
- [ ] Documentation for rail configuration per geography

---

## v0.22.0 — Postgres Authority Store (In Progress)

**Goal:** Production-capable authority database with connection pooling

**Status:** 🔶 Postgres authority + DB CLI complete. Alembic migrations pending.

### Deliverables

- [x] **Postgres Authority Adapter**
  - `hermes_cli/postgres_authority.py` — claim/release/complete operations
  - Environment: `AUTHORITY_POSTGRES_URL` or `DATABASE_URL`
  - Execution permit flow (issue/consume)
  - Automatic backend detection (postgres vs sqlite)
  - Optional dependency: `charterforge[postgres]`

- [x] **Database CLI Commands**
  - `charterforge db init --postgres-url <url>`
  - `charterforge db upgrade`
  - `charterforge db status`
  - Backend auto-detection + forced selection

- [x] **CI Integration**
  - Postgres service container in GH Actions
  - Integration tests against real Postgres

- [ ] **Schema Migration**
  - `alembic` migrations for authority tables
  - `charterforge db downgrade` implementation

- [ ] **Health Check**
  - `charterforge business readiness --check` validates Postgres connection
  - Dashboard: `/health` endpoint returns database status

### Tests

- 16 authority operation tests (postgres_authority.py)
- 8 db command tests (db_commands.py)
- Schema isolation per test via unique search_path
- Backend detection tests

### Validation

```bash
# Initialize Postgres authority store
docker run -d --name charterforge-db -e POSTGRES_PASSWORD=secret postgres:16
charterforge db init --postgres postgres://... --authority
charterforge business readiness --check
```

### Definition of Done

- [ ] Postgres adapter passes all authority store tests
- [ ] Migration script tested on representative SQLite database
- [ ] Connection pooling handles 100 concurrent connections
- [ ] Readiness check validates database connectivity

---

## v0.23.0 — Multi-Tenant Isolation

**Goal:** Multiple organizations on single Charterforge instance with data isolation

### Deliverables

- [ ] **Organization Boundaries**
  - Organization-level authority store (separate database or schema)
  - `charterforge org create --name "Acme Corp"`
  - `charterforge org switch --org "Acme Corp"`

- [ ] **Workspace Isolation**
  - Workspace-level Kanban boards, objectives
  - Role-based access: Owner, Admin, Member, Guest
  - Cross-org queries blocked at database level

- [ ] **Tenant Context**
  - `HERMES_ORG_ID` environment variable for worker context
  - Permit derivation enforces org/workspace scope
  - Audit logs tagged with org/workspace

### Validation

```bash
charterforge org create --name "Acme Corp"
charterforge org create --name "Beta LLC"

charterforge --org "Acme Corp" business bootstrap ...
charterforge --org "Beta LLC" business bootstrap ...

# Verify isolation: Acme cannot see Beta's objectives
charterforge --org "Acme Corp" objectives list --all  # Only Acme objectives
```

### Definition of Done

- [ ] 2+ organizations can run concurrently with isolated data
- [ ] Cross-org queries return empty (not error)
- [ ] Permit derivation enforces org/workspace scope
- [ ] Audit logs include org/workspace context

---

## v0.24.0 — Billing Engine

**Goal:** Autonomous billing for services rendered

### Deliverables

- [ ] **Usage Metering**
  - Track: API calls, token usage, compute time, storage
  - Per-customer usage aggregation
  - Usage events persisted to billing ledger

- [ ] **Invoice Generation**
  - `charterforge billing invoice --customer <id> --period <month>`
  - Invoice PDF generation (WeasyPrint or ReportLab)
  - Invoice delivery via email or webhook

- [ ] **Payment Collection**
  - Automated charge via payment rail (Stripe, Circle)
  - Payment retry logic (3 attempts, exponential backoff)
  - Dunning workflow for failed payments

### Validation

```bash
# Generate invoice
charterforge billing invoice --customer acme-corp --period 2026-08

# Auto-collect payment
charterforge billing collect --invoice <invoice-id> --rail stripe
```

### Definition of Done

- [ ] Usage metering tracks >100 billable events
- [ ] Invoice PDF generates with correct line items
- [ ] Payment collection succeeds via at least 1 rail
- [ ] Dunning workflow handles payment failures gracefully

---

## v0.25.0 — Agent Marketplace

**Goal:** Discover and install tools/skills/plugins from centralized catalog

### Deliverables

- [ ] **Skill Registry**
  - Central skill catalog (GitHub repo or API)
  - Skill metadata: name, version, category, dependencies
  - `charterforge skill search <query>`

- [ ] **Skill Installation**
  - `charterforge skill install <name>` from registry
  - Dependency resolution (uv or pip)
  - Skill verification (hash/signature check)

- [ ] **Plugin Marketplace**
  - Plugin discovery via registry
  - `charterforge plugin install <name>`
  - Plugin activation/deactivation

### Validation

```bash
charterforge skill search seo
charterforge skill install seo-audit
charterforge skill view seo-audit
```

### Definition of Done

- [ ] Registry contains ≥10 skills
- [ ] Skill installation works end-to-end
- [ ] Installed skills are usable in autonomous operation

---

## v0.26.0 — External Integrations

**Goal:** Connect to external tools humans use

### Deliverables

- [ ] **Email Integration**
  - IMAP/SMTP integration (via `himalaya` skill)
  - Email-to-task conversion
  - Email notification triggers

- [ ] **Calendar Integration**
  - Google Calendar, Outlook, iCloud CalDAV
  - Meeting scheduling from objectives
  - Calendar-aware scheduling (avoid conflicts)

- [ ] **CRM Integration**
  - HubSpot, Salesforce, Pipedrive
  - Lead creation from autonomous outreach
  - Pipeline sync (objective ↔ CRM deal)

- [ ] **Project Tools**
  - Linear, Jira, Asana, Notion
  - Task sync (Kanban ↔ external project)
  - Comment sync (evidence ↔ external task)

### Definition of Done

- [ ] At least 1 integration tested end-to-end per category
- [ ] Data flows bidirectionally (Charterforge ↔ external)
- [ ] Error handling for rate limits, auth failures

---

## v0.27.0 — Monitoring & Observability

**Goal:** Production-grade visibility into autonomous operation

### Deliverables

- [ ] **Metrics Collection**
  - Prometheus metrics endpoint
  - Metrics: objectives created/completed, tasks executed, payments received/sent, errors
  - Grafana dashboard template

- [ ] **Distributed Tracing**
  - OpenTelemetry integration
  - Trace spans: objective lifecycle, task execution, payment processing
  - Jaeger or Tempo backend

- [ ] **Health Dashboard**
  - Real-time health: database, payment rails, workers
  - Alert thresholds (error rate, latency)
  - SLO/SLI tracking

### Definition of Done

- [ ] Prometheus scrapes metrics successfully
- [ ] Grafana dashboard displays key metrics
- [ ] Traces link objective → task → payment

---

## v0.28.0 — Disaster Recovery

**Goal:** Business continuity with tested backup/restore

### Deliverables

- [ ] **Automated Backups**
  - Daily PostgreSQL backups (pg_dump)
  - Backup retention: 30 days
  - Encrypted backup storage (S3, GCS, or local)

- [ ] **Restore Procedures**
  - `charterforge db restore --backup <path>`
  - Point-in-time recovery support
  - Tested restore into fresh environment

- [ ] **Failover Drill**
  - Documented failover procedure
  - Automated health check promotion
  - RTO < 15 minutes, RPO < 1 hour

### Definition of Done

- [ ] Backup runs automatically and verifies integrity
- [ ] Restore tested on fresh environment
- [ ] Failover drill documented and executed

---

## v0.29.0 — Security Hardening

**Goal:** Production-ready security posture

### Deliverables

- [ ] **Audit Logging**
  - Comprehensive audit log for all authority-bound actions
  - Audit log integrity (append-only, tamper detection)
  - Audit log retention: 1 year

- [ ] **Secret Management**
  - Secrets stored in HashiCorp Vault or AWS Secrets Manager
  - Runtime secret injection (no plaintext in config)
  - Secret rotation support

- [ ] **Access Control**
  - Role-based access control (RBAC)
  - Principle of least privilege enforcement
  - Session timeout and revocation

### Definition of Done

- [ ] Audit logs capture all critical actions
- [ ] Secrets never appear in plaintext
- [ ] RBAC tested with multiple roles

---

## v1.0.0 — Production Release

**Goal:** Feature-complete, documented, stable API

### Deliverables

- [ ] **API Stability**
  - Public API versioned (e.g., `/api/v1/`)
  - Breaking changes require major version bump
  - API documentation (OpenAPI spec)

- [ ] **Documentation**
  - User guide (installation, configuration, operation)
  - Developer guide (architecture, extension, contribution)
  - API reference

- [ ] **Release Artifacts**
  - Wheel and sdist published to PyPI
  - Docker image published to registry
  - Helm chart or Kubernetes manifests

- [ ] **Support**
  - Issue template and bug report process
  - Slack/Discord channel for community
  - FAQ and troubleshooting guide

### Definition of Done

- [ ] All previous milestones complete
- [ ] Documentation covers 100% of user-facing features
- [ ] PyPI package installs and runs on fresh environment
- [ ] Docker image passes health checks
- [ ] ≥10 beta users complete onboarding successfully

---

## Milestone Dependencies

```
v0.21.0 Payment Rails
    ↓
v0.22.0 Postgres Authority Store ← required for multi-tenant
    ↓
v0.23.0 Multi-Tenant Isolation ← required for billing
    ↓
v0.24.0 Billing Engine ← requires payment rails + multi-tenant
    ↓
v0.25.0 Agent Marketplace ← independent
    ↓
v0.26.0 External Integrations ← independent
    ↓
v0.27.0 Monitoring ← required for production
    ↓
v0.28.0 Disaster Recovery ← requires Postgres
    ↓
v0.29.0 Security Hardening ← required for production
    ↓
v1.0.0 Production Release ← all previous complete
```

---

## Parallel Workstreams

Some milestones can be developed in parallel:

| Workstream A | Workstream B | Workstream C |
|--------------|--------------|--------------|
| v0.21.0 Payment Rails | v0.25.0 Agent Marketplace | v0.26.0 External Integrations |
| v0.22.0 Postgres | v0.27.0 Monitoring | |
| v0.23.0 Multi-Tenant | v0.28.0 DR | |
| v0.24.0 Billing | v0.29.0 Security | |

---

## Resource Allocation

Assuming 1-2 full-time developers:

| Milestone | Estimated Effort | Dependencies |
|-----------|------------------|--------------|
| v0.21.0 Payment Rails | 2-3 weeks | None |
| v0.22.0 Postgres | 2-3 weeks | None |
| v0.23.0 Multi-Tenant | 1-2 weeks | v0.22.0 |
| v0.24.0 Billing | 2-3 weeks | v0.21.0, v0.23.0 |
| v0.25.0 Marketplace | 1-2 weeks | None (parallel) |
| v0.26.0 Integrations | 2-3 weeks | None (parallel) |
| v0.27.0 Monitoring | 1-2 weeks | v0.22.0 |
| v0.28.0 DR | 1-2 weeks | v0.22.0 |
| v0.29.0 Security | 2-3 weeks | v0.22.0 |
| v1.0.0 Production | 2-3 weeks | All |

**Total:** ~16-24 weeks (4-6 months) to v1.0.0

---

## Success Criteria for v1.0.0

**Minimal Viable v1.0.0 (Tranche 1):**
- [ ] Payment Rails: ≥2 rails working (Stripe + 1 other)
- [ ] Postgres Authority Store: production-capable
- [ ] Multi-Tenant: ≥2 orgs isolated
- [ ] Billing: usage metering + invoice generation
- [ ] Monitoring: metrics + health dashboard
- [ ] Disaster Recovery: backup/restore tested
- [ ] Security: audit logging + secret injection

**Complete v1.0.0 (Tranche 2):**
- [ ] All "Minimal" criteria
- [ ] Agent Marketplace: skill registry + installation
- [ ] External Integrations: ≥1 integration per category
- [ ] Documentation: 100% coverage
- [ ] Beta users: ≥10 successful onboards

---

## Out of Scope (v1.0.0)

These remain deployment/legal work outside v1.0.0:

- Legal formation, corporate registration
- Banking relationship setup
- Compliance certifications (PCI DSS, SOC 2, GDPR)
- Tax filing automation
- Trademark/brand protection
- Insurance/liability coverage

The human operator retains authority over:
- Legal entity creation
- Banking and financial accounts
- Regulatory compliance decisions
- Strategic business decisions (pricing, markets, partnerships)
- Brand and public communication

---

## Changelog vs. Roadmap

| Changelog | Roadmap |
|-----------|---------|
| What was released | What will be released |
| Past tense, evidence-based | Future tense, goal-based |
| Immutable after release | Adjustable based on feedback |
| Per-commit granularity | Per-version granularity |

---

## References

- [READINESS.md](../READINESS.md) — Release gates and validation evidence
- [CHANGELOG.md](../CHANGELOG.md) — Version history
- [docs/payment-rails-research.md](payment-rails-research.md) — Payment rail options
- [examples/autonomous-business-charter.json](../examples/autonomous-business-charter.json) — Charter template
