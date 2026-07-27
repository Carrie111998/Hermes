# Implementation Status and Known Limitations

**Current readiness determination:** Controlled Founder/CEO runtime acceptance
**PASS** at [READINESS.md](../READINESS.md); production autonomous business
operation **NOT READY**. This inventory is broader than the current release
gate, so an implemented capability is not automatically covered by the pinned
acceptance evidence.

## Implemented

- Persistent governed objective/event runtime and explicit lifecycle state.
- Permit issuance is bound to the current immutable plan version; superseded
  actions cannot regain execution authority.
- Permit consumption rejects permits whose policy version is stale for the
  active runtime.
- Permit consumption rechecks objective lifecycle state and rejects permits
  after cancellation, expiry, or other terminal transitions.
- Registered action payload schemas and temporal observation windows are
  validated before permit admission and again at the final execution boundary.
- Runtime construction rejects self-supervising verifier identities; verifier
  evidence must come from an identity distinct from the planner and executor.
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
- Spend-hold intervention resolution is evidence-bound: succeeded durable
  provider read-back settles a hold, while failed/cancelled settlement evidence
  is required to release it.
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
- Circuit-breaker recovery probes are serialized as a durable half-open lease;
  concurrent probes fail closed and an expired probe can be reclaimed safely.
- Portfolio child/successor admission and employee grant/revocation admission
  serialize budget and authority checks across concurrent local workers.
- External action handlers recheck the autonomy kill-switch immediately before
  invoking provider side effects.
- Standalone objective workers fail closed and stop durably when autonomy is
  disabled or runtime/security/integrity gates block execution.
- The worker supervisor checks the durable autonomy mode before invoking any
  tick callback, including alternate or injected integrations.
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
- Proposed objectives created by the active Founder/CEO using the canonical
  employee identity are accepted during the runtime cycle under standing
  organizational authority, so routine CEO work does not wait for an advisor
  dispatch. Proposed objectives from outside that authority scope create a
  durable acceptance handoff; evidence-bearing advisor acceptance transitions
  them to `accepted` and emits a fresh wake event.
- Explicit organization-bound objective admission rejects unknown tenant IDs
  when the enterprise organization authority schema is present.
- Provider payment read-back rejects missing references, statuses, invalid
  amounts, and malformed currencies before settlement is recorded.
- Payment idempotency retries are bound to the original intent's tenant,
  direction, provider, party, amount, currency, purpose, and action fields.
- Treasury ledger retries are bound to the original entry parameters before a
  duplicate idempotency key is accepted.
- Accounting journal retries are bound to the original description, currency,
  and complete line set before a duplicate source key is accepted.
- Procurement decision retries are bound to the original tenant, objective,
  sourcing case, and source evidence before a duplicate key is accepted.
- Metered usage-event retries are bound to the original meter, customer,
  quantity, supplied timestamp, and evidence before a duplicate key is accepted.
- Child and successor objective relationship retries carry immutable request
  fingerprints; unbound legacy relationships fail closed on replay.
- FTE/contractor hiring decision retries carry an immutable request fingerprint
  over the organization, staffing case, policy, and evaluator identity.
- Budget reservation retries are bound to the original account, objective,
  action, amount, and currency before an existing spend authorization is reused.
- Advisor-intervention action and dedupe replays are organization-scoped;
  cross-tenant intervention collisions fail closed.
- Approval-artifact issuance requires an explicit expected organization and
  filters the intervention lookup by that organization.
- Company-email operation retries are bound to the original organization,
  objective, action, inbox, recipients, subject, and body hashes.
- Compute-cost reconciliation retries are bound to the original provider,
  model, reference, amount, status, and exact evidence.
- Identical payment-provider readbacks converge to one immutable observation;
  changed provider states remain separately recorded.
- Objective intent freshness is durable: `reaffirmed_at` and the configured
  reaffirmation TTL block stale objectives and require evidence-bearing advisor
  reaffirmation before planning resumes.
- Reaffirmation resolution requires a substantive decision basis; an arbitrary
  non-empty JSON payload cannot refresh stale intent.
- Payment-provider assessments reject future-dated verification evidence before
  a rail can be authorized.
- Payment-provider assessments also reject evidence already expired at
  admission; only currently valid assessments can enter the authorization set.
- Provider assessments require non-empty auditable provider, jurisdiction, and
  registry-reference fields before entering the authorization set.
- Standalone workers enforce the configured deployment role and live worker
  registration even when an embedding supplies a callback; callback injection
  cannot bypass supervised-host boundaries.
- Standalone workers re-read and revalidate deployment authority before every
  cycle, so runtime-host changes cannot leave a process operating under stale
  permissions.
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

The current release acceptance gate is deliberately narrower than this full
implementation inventory. Its pinned evidence consists of the Founder/CEO
end-to-end test, objective service/runtime/worker tests, finance and
outcome-attribution tests, compilation of selected runtime modules, and
`git diff --check`, all recorded against commit
`4f7d585b8d0eda6f0d7646c843f9ddd43c4d7afe` in [READINESS.md](../READINESS.md).
It does not claim current release-gate coverage for compliance, procurement,
external ingress and scheduling, billing, tax/accounting, staffing/workforce,
runtime-drift enforcement, live payment rails, or every other item in the
inventory below.

A separate post-boundary rerun at baseline commit
`7fabe92dd94c0aeedfb5193cad505c2861ed4ed9` passed 6 Founder/CEO E2E tests, 48
objective service/runtime/worker tests, and 21 finance/attribution tests,
plus selected module compilation and `git diff --check`. This supports the
bounded tested runtime on current `main`; it does not change the tagged release
boundary or broaden the inventory coverage claim.

The same baseline passed the accounting replay/idempotency regression: 7 tests.
It also passed the procurement decision replay regression: 8 tests.
It also passed the metered usage and billing regression: 8 tests.
It also passed the objective portfolio relationship regression: 7 tests.
It also passed the hiring policy regression: 13 tests.
It also passed the finance reservation regression: 21 tests.
It also passed the intervention-control regression: 10 tests.
It also passed the approval-artifact regression: 9 tests.
It also passed the company-email regression: 4 tests.
It also passed the compute-reconciliation regression: 9 tests.
The finance suite also passed the payment readback convergence regression: 21 tests.
The circuit-breaker recovery-probe regression passed 3 tests, including
single-probe admission and expired-probe reclamation.
The broader current-main regression command across resource budgets, company
email, approvals, operational control, hiring, portfolio, accounting,
procurement, metered billing, and usage billing passed **76 tests, 0 failed**.
Compliance registry admission regressions passed 8 tests, including rejection
of unknown and retired regimes.
Control-evidence admission now rejects stale expiry and passed the compliance
regression suite.
Compliance evidence records are append-only and passed immutable update/delete
regressions.
Business-commitment fulfillment is organization-bound and passed its focused
regression, including cross-tenant rejection.
Tax filing and payment mutations are organization-bound and passed the
accounting regression, including cross-tenant filing rejection.
Fiscal-period closure is organization-bound and passed the accounting
regression, including cross-tenant closure rejection.
Approval-artifact validation is organization-bound and passed its focused
regression, including cross-tenant validation rejection.
Permit consumption is organization-bound and passed its focused regression,
including cross-tenant consumption rejection.
Execution-result recording is organization-bound and passed the outcome
attribution regression.
Durable verification recording is organization-bound in governed runtime and
CLI paths and passed the runtime and attribution regressions.
The verification API now requires organization scope for all direct callers.
External and scheduled objective wakeups now pass organization scope into the
event inbox boundary.
Employee identities are organization-scoped for objective reaffirmation and
transitions when present in the employee directory.
The same employee scope applies to plan creation and action proposals.
Permit issuance also rejects employee identities outside the objective
organization before minting execution authority.

Authenticated external-event ingress has a separate focused regression at the
same current-main baseline: 23 tests passed, including rejection of malformed,
future-dated, and stale provider authentication timestamps. Provider adapters
without a timestamp remain compatible; when a timestamp is supplied, the
router enforces a five-minute age limit and thirty-second future-skew limit.

The following historical focused regression results are contextual evidence,
not additional claims that the current release boundary covered every listed
surface. Their commit pins and reconstructability status are recorded in
[historical validation evidence](evidence/historical-validation.md): the
exact-grant increment was reported as 281 focused tests; rebrand and migration
validation was reported as 361 focused Python tests with desktop/TUI/web
TypeScript typechecks; and a governed-runtime sweep was reported as 161 tests
across 20 focused files. The independent documentation site prebuild
completed, but its Docusaurus build was unavailable because the local
`docusaurus` executable is not installed; no build pass is claimed.
