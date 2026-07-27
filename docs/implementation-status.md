# Implementation Status and Known Limitations

## Implemented

- Persistent governed objective/event runtime and explicit lifecycle state.
- Permit issuance is bound to the current immutable plan version; superseded
  actions cannot regain execution authority.
- Permit consumption rejects permits whose policy version is stale for the
  active runtime.
- Permit consumption rechecks objective lifecycle state and rejects permits
  after cancellation, expiry, or other terminal transitions.
- Hiring materialization rechecks current headcount and payroll limits against
  the immutable decision before adding an employee.
- Founder/CEO organization bootstrap and versioned mandate.
- Advisor-by-default authority policy.
- Exact solo-founder and employee delegation grants.
- Evidence-based contractor/FTE decision and hierarchical profiles.
- Treasury, budgets, reservations, double-entry accounting, fiscal periods,
  tax records, payment intents, and verification.
- Outbound spend controls atomically reserve per-instrument daily velocity
  before provider calls and retain pending holds until read-back settles or
  releases them.
- Housekeeping escalates stale outbound spend holds with a deduplicated advisor
  intervention and never releases an uncertain provider hold automatically.
- Payment and metered-billing schema initialization preserves active authority
  transactions when the durable tables already exist.
- Outcome attribution synchronization uses the guarded payment schema path and
  preserves active authority transactions during cross-ledger reconciliation.
- Immutable usage-event metering and exact-event metered invoicing with
  idempotent allocation guards against duplicate billing.
- Metered-invoice verification independently reconciles the exact usage-event
  set and aggregate amount against the payment intent.
- Metered invoices can calculate jurisdiction-matched tax from verified active
  accounting registrations; missing or mismatched rules fail closed.
- Compliance inventory, obligations, deadlines, evidence, and audit export.
- Authenticated external-event receipts with idempotent provenance and
  credential-redacted ingress envelopes.
- Concurrent authenticated deliveries converge to one immutable receipt and
  one objective inbox wakeup; duplicate external-content inserts collapse to
  the existing immutable item.
- External event routing now requires an explicit adapter-validated
  authentication marker before waking objectives.
- External subscriptions and schedules no longer wake terminal objectives.
- Durable inbox claims also skip internally emitted events for terminal
  objectives.
- Runtime schema checks across authority, objectives, worker liveness, finance,
  accounting, payments, compliance, approvals, event triggers, external
  ingress, billing, commitments, metrics, and audit lineage preserve active
  SQLite transactions instead of allowing `executescript` to release a lock or
  commit a partial state transition.
- Recovery snapshots, leases, retries, circuit breakers, stop reasons, and
  interventions.
- Portfolio child/successor admission and employee grant/revocation admission
  serialize budget and authority checks across concurrent local workers.
- External action handlers recheck the autonomy kill-switch immediately before
  invoking provider side effects.
- Standalone objective workers fail closed and stop durably when autonomy is
  disabled or runtime/security/integrity gates block execution.
- Supervisor startup reconciles expired worker heartbeats into durable `stale`
  stop states instead of leaving dead workers marked `running`, and emits a
  deduplicated advisor intervention with restart/diagnose/manual options.
- Gateway-hosted objective supervision applies the same stale-worker
  reconciliation before registering its replacement worker.
- Supervised workers assert their heartbeat lease before and after each tick;
  a revoked lease stops the process before it can begin another cycle.
- Readiness stops now persist deduplicated advisor handoffs for missing CEO
  authority, unavailable governed capabilities, and objectives without an
  admissible success verifier; unreachable objectives are blocked before any
  plan or external action is attempted.
- Runtime-host mismatches and charters with no registered action contracts also
  persist bounded advisor handoffs before stopping.
- Optional fail-closed runtime drift detection with immutable human-accepted
  baselines for the charter, authority schema, Python runtime, and dependency
  lock/package identity.
- Canonical Charterforge Python distribution/namespace/CLI plus migration
  aliases.

## Partial

- The authority store is SQLite, not Postgres.
- Authority SQLite connections enforce foreign keys, WAL, full synchronous
  durability, and a bounded busy timeout.
- Event scheduling is implemented in-process; Kafka/RabbitMQ are not bundled.
- The inherited UI and internal code still contain legacy identifiers where
  changing them would break migration or require a separate subsystem rewrite.
- The optional `packages/charterforge-stripe-rail` package provides a concrete
  HTTP Stripe Checkout/read-back rail and a narrowly scoped Connected Account
  outbound path. It is not installed by the core runtime; live settlement
  still requires separate installation, credentials, provider assessment, and
  jurisdictional compliance evidence. Webhook admission rejects missing or
  malformed positive amount/currency facts.
- AgentMail configuration exists; provider availability and plan terms are
  external facts.
- Compliance tracking exists; legal applicability is not autonomously proven.
- Local terminal execution is available but is not isolation.

## Not implemented or not proven

- Corporate formation, beneficial-owner filings, bank-account opening, or
  legal personhood for the agent.
- Autonomous signing of contracts where law requires a human/legal principal.
- Universal tax filing in every jurisdiction.
- Automatic tax determination for metered usage without verified jurisdictional
  rules (tax-bearing metered invoices remain blocked until configured).
- PCI DSS, SOC 2, SOX, GDPR, EU AI Act, CASL, CAN-SPAM, or other certification.
- A published Charterforge installer, package release, container registry
  image, production deployment, or repository rename.
- Trademark clearance for Charterforge.
- High-availability database replication or disaster-recovery drill evidence.
- Runtime drift enforcement remains opt-in for migrated installations; fresh
  agentic setup enables the strict gate and records a baseline. Migrated
  installations should enable it after reviewing deployment policy.

## Validation evidence

The exact-grant increment passed 281 focused tests. The rebrand and migration
validation passed 361 focused Python tests, desktop/TUI/web TypeScript
typechecks, Python compilation, and the implemented `charterforge` help and
version commands. The independent documentation site prebuild completed, but
its Docusaurus build was unavailable because the local `docusaurus` executable
is not installed; no build pass is claimed. The current governed-runtime
regression sweep covers 20 focused test files and passed 161 tests, including
objectives, workers, authority, finance, accounting, payments, compliance,
approvals, workforce, procurement, metrics, commitments, event ingress,
billing, and audit lineage.
