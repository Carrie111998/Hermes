# Changelog

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

All notable independent Charterforge changes are documented here. Upstream
Hermes Agent history remains available in Git.

## Unreleased

### Added

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
