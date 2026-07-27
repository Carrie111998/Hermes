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
  commands and results recorded in [READINESS.md](READINESS.md). The evidence
  documentation itself was recorded in commit
  `757408d82884afd60651762715c3ef00446bc0c0`.
- Immutable annotated release tag: `v0.19.0-agentic-foundation` points at the
  evidence commit above.
- This SHA is the designated `0.19.0-agentic-foundation` release boundary.
  `main` is ahead of it: the stale-spend-hold escalation
  (`172a515c6b5d7efeef1a4e222c5c35ca46246a0b`) and evidence-bound spend-hold
  resolution (`5c92744e42878929ed981c81f34b1239c00d992a`) are post-boundary
  commits. Those changes have focused regression evidence, but they are not
  included in this release acceptance determination and remain under
  Unreleased.

### Capability inventory present at the designated boundary

The immutable release tree at tag `v0.19.0-agentic-foundation` is the
authoritative source for this included capability inventory. The entries below
describe capabilities present in that tagged tree and are part of
`0.19.0-agentic-foundation`, not Unreleased. The commit comparison is used
only to classify changes introduced after the tagged boundary; it is not the
source of the complete inventory.

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
- Hardened durable runtime ledgers so schema checks preserve active authority
  transactions across finance, accounting, payments, compliance, approvals,
  event ingress, billing, commitments, metrics, worker state, and audit
  lineage; focused regressions cover rollback preservation.

### Additional capability inventory at the designated boundary

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

#### Changed

- Product-specific development is independent and is not intended for
  submission to the upstream Hermes Agent repository.
- Legacy Hermes commands, environment variables, paths, and Python modules are
  migration compatibility surfaces rather than project branding.

#### Security

- Governed worker launch and result handoff fail closed when task, mandate,
  profile, authority, toolset, skill, budget, or expiry evidence differs from
  the immutable grant.
- Housekeeping repairs lost wake events for active accepted/planned objectives
  using versioned idempotency fences, without reviving blocked objectives.

All notable independent Charterforge changes are documented here. Upstream
Hermes Agent history remains available in Git.

## Unreleased (commits after 0.19.0-agentic-foundation)

This section is derived from `4f7d585b8d0eda6f0d7646c843f9ddd43c4d7afe..HEAD`.
The current branch is ahead of the validated boundary; these changes do not
inherit the boundary's PASS determination until a newer evidence commit is
recorded.

- `757408d82884afd60651762715c3ef00446bc0c0` — recorded the release
  readiness evidence documentation.
- `172a515c6b5d7efeef1a4e222c5c35ca46246a0b` — escalated stale outbound spend
  holds without automatically releasing uncertain provider commitments.
- `5c92744e42878929ed981c81f34b1239c00d992a` — bound spend-hold resolutions to
  durable provider read-back or failed/cancelled settlement evidence.
- `e5ee81ded0b18b53453fc15570fafce231bdef75` — clarified release evidence,
  coverage scope, and the test-provider boundary.
- `1bf543e12f7b14bd4d808fbb1438690a594f53e9` — stopped the supervised
  objective worker on global `recovery_blocked` results so unavailable
  authority recovery cannot leave the runtime polling for new work.
- `a4ae9be834e76548c96556fd2ecf41cbd2b1c4e1` — shared the fail-closed
  supervisor status contract with the gateway-hosted objective loop.
- `8e415f4ed85339b6bb3dc83e39b93b938028f6c3` — made proposed-objective
  acceptance an explicit evidence-bearing advisor handoff that wakes the
  governed runtime after acceptance.
- `62866e01cb3ec1bf672038653fcd413a5e3a3f21` — blocked stale objective intent
  until evidence-bearing reaffirmation refreshes the standing objective.
- `1720b01ade8543326029676e4d142d914ee4bc9f` — required a substantive
  decision basis when resolving stale-intent reaffirmation.
- `33ca4eae20cd4291381e053de7838b2a107138a3` — rejected future-dated payment
  provider assessments before rail authorization.
- `d38f119ee35f9f0998f220f2883720f32a1d006c` — rejected provider assessments
  that were already expired at admission.
- `a498499a0273adb8d849474e5c461bdf0dd27443` — enforced standalone worker
  deployment-role and live-host checks even for injected callbacks.
- `2caf2de7ed8941d6a542cb38bfbee1afee338bb4` — required auditable provider,
  jurisdiction, and registry-reference fields for payment assessments.
- `4a129bcbb3d09606ce83b577e63a6e601303331b` — revalidated standalone worker
  deployment authority before every cycle to fence dynamic host changes.
- Current-main focused acceptance rerun at baseline
  `c4662689660f1e2447a2479f1844fae28bd2a57c`: 6 Founder/CEO E2E tests, 48
  objective service/runtime/worker tests, and 21 finance/attribution tests
  passed; compilation and diff checks passed. This is post-boundary evidence,
  not a release-tag move.
- The containing documentation/control-boundary commit also adds proposal-time
  validation of registered action payload schemas and temporal preconditions;
  its SHA is the final commit that contains this changelog entry.
- Runtime construction now rejects a verifier that shares the planner or
  executor identity, preserving an explicit independent-verification boundary.
- The supervised worker now checks the durable autonomy kill switch before
  invoking any tick callback, including alternate worker integrations.
- `29df862970a7caa3e34e3b4f27c98218ab0efce2` — rejected malformed,
  future-dated, and stale authenticated external-event evidence before
  objective wake-up; the focused ingress regression passed 21 tests.
- `c4662689660f1e2447a2479f1844fae28bd2a57c` — provider-authenticated ingress
  now requires signed freshness evidence for Stripe and Svix schemes; the
  focused ingress regression passed 23 tests.
- `247617ca47102af87c968d303897fff45dcaa2ee` — Founder/CEO-originated
  objectives now enter the operating portfolio under standing authority
  without requiring a routine advisor dispatch; externally originated
  proposals still require evidence-bearing acceptance.
- `a497763a54767bc5c577e535e4bfafd531981cd7` — bound automatic objective
  acceptance to the active CEO's canonical employee identity and organization,
  preventing a forgeable `employee:ceo` label from granting standing authority.
- `71330197889f03c0ae67ba8424038aaed4bda54c` — rejected explicit objective
  organization IDs that are not present in the enterprise tenant authority
  store.
- `dee2818df7f12949911e87419b33545c350080ce` — required provider payment
  read-backs to carry a non-empty reference/status and valid amount/currency
  fields before settlement or receipt recording.
