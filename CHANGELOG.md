# Changelog

## 0.19.0-agentic-foundation — 2026-07-27

This is the consolidated release-evidence boundary for the independent
Charterforge agentic runtime. It is a controlled-runtime foundation boundary,
not a claim of production autonomous-business readiness.

### Included

- Founder/CEO solo-founder bootstrap with advisor-by-default human posture.
- Durable objectives, immutable plans, admissible permits, event-driven
  replanning, independent verification, audit lineage, and bounded worker
  coordination.
- Fail-closed authority, budget, payment, accounting, compliance, recovery,
  lease, and advisor-intervention controls.
- Explicit end-to-end acceptance evidence in
  `tests/hermes_cli/test_agentic_business_e2e.py::test_founder_ceo_operating_loop_acceptance`.

### Evidence boundary

- Controlled Founder/CEO runtime acceptance: **PASS** when the exact commands
  in [READINESS.md](READINESS.md) pass at the recorded evidence commit.
- Production autonomous business operation: **NOT READY**.
- Universal legal, tax, payment, and compliance operation: **NOT PROVEN**.
- Evidence commit SHA: `4f7d585b8d0eda6f0d7646c843f9ddd43c4d7afe`, with exact
  commands and results recorded in [READINESS.md](READINESS.md).

## Unreleased

- Added the optional standalone `charterforge-stripe-rail` package for
  idempotent inbound Checkout Sessions, provider read-back verification, and
  narrowly scoped Connected Account outbound payments. The core runtime does
  not install or enable it automatically.
- Enforced objective-level cumulative spend ceilings inside the atomic treasury
  reservation transaction, including concurrent workers and released-budget
  reuse.
- Paused/manual autonomy now causes the supervised objective worker to exit with
  a durable `autonomy_paused` stop reason instead of polling indefinitely.
- Worker exception handling now checks the persisted autonomy mode, so a
  provider failure caused by an in-flight emergency stop cannot trigger retry
  loops.
- Durable audit and planner-lineage records now redact credential-like fields
  before persistence while preserving ordinary response evidence unchanged.
- External event receipts now redact credential-like payload and authentication
  fields before durable storage and CEO-planner routing.
- Added an optional immutable runtime baseline that pauses autonomous cycles on
  charter, schema, package, or Python-runtime drift until a human rebaselines
  explicitly.
- External-content ingestion and authenticated event fan-out now collapse
  concurrent duplicate deliveries into one durable receipt and wakeup.
- Portfolio and workforce authority admission now serialize local concurrent
  budget checks; external handlers perform a final autonomy-state check before
  side effects.
- Stripe webhook ingress now rejects missing or malformed positive
  amount/currency evidence before routing.
- Worker supervision now persists expired heartbeat leases as `stale` workers
  with an explicit stop reason during supervisor startup and emits a
  deduplicated advisor intervention for each stale worker.
- Gateway-hosted objective supervision now reconciles stale gateway workers
  before registering a replacement worker.
- Supervised workers now fence each cycle on their durable heartbeat lease and
  stop when that lease is revoked.
- Autonomous readiness stops now persist advisor handoffs for missing CEO
  authority, unavailable governed capabilities, and unreachable objectives;
  objectives without admissible verifiers are blocked before execution.
- Outbound payment velocity controls now reserve daily spend atomically per
  tokenized instrument until provider read-back settles or releases the hold.
- Payment and metered-billing schema checks no longer release active authority
  transactions on already-initialized stores.
- Outcome attribution reconciliation now uses the same guarded payment schema
  initialization path.
- Runtime-host mismatches and empty action-contract charters now create durable
  advisor handoffs before autonomous operation stops.
- Housekeeping now escalates stale outbound spend holds without automatically
  releasing uncertain provider commitments.
- Spend-hold advisor resolutions are now evidence-bound to durable provider
  read-back or failed/cancelled settlement evidence.
- Hardened durable runtime ledgers so schema checks preserve active authority
  transactions across finance, accounting, payments, compliance, approvals,
  event ingress, billing, commitments, metrics, worker state, and audit
  lineage; focused regressions cover rollback preservation.

All notable independent Charterforge changes are documented here. Upstream
Hermes Agent history remains available in Git.

## Unreleased

### Added

- Immutable usage metering and governed metered-invoice actions. Prices are
  captured when usage occurs; invoice actions reference exact event IDs and
  immutable allocations prevent duplicate billing.
- Metered-invoice recovery now permits only same-intent allocation replay and
  rejects idempotency-key amount drift.
- Standalone objective workers now stop durably on disabled autonomy and
  fail-closed runtime, security, configuration, integrity, or drift gates.
- Metered-invoice verification now requires independent allocation-ledger
  read-back of the exact event set and total amount.
- External objective-event routing now rejects evidence without an adapter
  validation marker.
- Permit issuance now rejects actions from superseded plan versions.
- Permit consumption now rejects unexpired permits issued under a stale policy
  version.
- Hiring materialization now rejects stale positive decisions when intervening
  headcount or payroll use exhausts the current organization limits.
- Permit consumption now rechecks objective lifecycle state and rejects stale
  permits after cancellation or expiry.
- Metered invoices now calculate optional tax only from an active,
  organization-owned jurisdiction-matched tax rule and record the gross intent.
- External subscriptions now skip terminal objectives, preventing stale goals
  from being reactivated by late events.
- Durable inbox claims now apply the same terminal-objective fence to internal
  worker, compliance, and maintenance events.
- Authority-store connections now explicitly use full synchronous durability
  and a bounded busy timeout alongside WAL and foreign-key enforcement.
- Charterforge independent identity, canonical package/CLI/namespace, state
  root, environment prefix, container/service naming, attribution, and
  migration documentation.
- Governed autonomous-company runtime with durable objectives, event-driven
  progression, deterministic permits, independent verification, recovery, and
  audit evidence.
- Founder/CEO organization model with advisor-by-default human role.
- Solo-founder self-dispatch bound to exact toolset and skill grants.
- Evidence-based contractor-versus-FTE staffing and enterprise hierarchy.
- Treasury, accounting, tax-record, compliance, commitment, procurement, and
  non-custodial payment-rail control surfaces.

### Changed

- Product-specific development is independent and is not intended for
  submission to the upstream Hermes Agent repository.
- Legacy Hermes commands, environment variables, paths, and Python modules are
  migration compatibility surfaces rather than project branding.

### Security

- Governed worker launch and result handoff fail closed when task, mandate,
  profile, authority, toolset, skill, budget, or expiry evidence differs from
  the immutable grant.
- Housekeeping repairs lost wake events for active accepted/planned objectives
  using versioned idempotency fences, without reviving blocked objectives.
