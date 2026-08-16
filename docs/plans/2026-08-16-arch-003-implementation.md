---
title: "ARCH-003 implementation plan: runtime-state audit and replay verification"
status: IMPLEMENTATION_PLAN_REVISE_PENDING
date: 2026-08-16
type: implementation-plan
ticket: ARCH-003
design_commit: 18661d7261215d0a6636ae82870f7d433eb708df
target_repo: hermes-agent
---

# ARCH-003 implementation plan

## Status and gate

This is a source-implementation plan only. It does not authorize source edits,
database migration, live-state inspection, DGX mutation, deployment, repair, or
event-sourcing.

The design gate passed with one authenticated Claude Opus reviewer and one
authenticated AGY reviewer on the identical final packet:

- final design packet SHA-256:
  `d3789a8daefa1a3f903692b829a2aa4030a6469b6d5089cd5efc121329601657`;
- design plan commit:
  `18661d7261215d0a6636ae82870f7d433eb708df`.

Implementation starts only after this plan receives its own implementation
review and the user separately authorizes implementation.

## Objective

Add a local, metadata-only runtime-state audit journal and a read-only replay
verifier while keeping materialized runtime-state tables as the serving
boundary.

The implementation must preserve:

- the existing runtime-state CAS transition contract;
- one mutation connection and transaction;
- profile isolation;
- prompt-cache stability;
- the gateway and platform message loops;
- the local-only data boundary.

## Explicit implementation decisions

1. **Legacy rows:** Do not synthesize historical events during the first
   migration. Entities that predate the journal remain `UNKNOWN` until a
   successful post-migration mutation establishes a verifiable origin. Add an
   explicit migration/compatibility test for this behavior.
2. **Digest custody:** Use an existing local Hermes secret/key-storage
   mechanism discovered during preflight. The digest key is never written to
   the journal, `config.yaml`, or user-facing non-secret configuration. If no
   supported durable local key store exists, stop at the implementation
   preflight gate and raise a bounded key-custody correction; do not invent an
   environment variable or silently regenerate the key.
3. **Compaction:** Implement the sealed baseline representation and verifier
   rules. Compaction is a privileged, out-of-band operation and is not wired
   into the gateway, scheduler, or automatic repair path in this ticket.
4. **Verifier surface:** Keep the verifier library-local plus hermetic tests.
   Any operator CLI, scheduler, or dashboard surface is a separate follow-up.

## Expected code surfaces

Confirm exact paths from the merged source before editing. Expected touchpoints
are:

- the existing `runtime_state` schema/migration module;
- the existing CAS transition/chokepoint, including
  `runtime_state/contract.py`;
- a focused runtime-state journal/replay module;
- runtime-state package exports only where required;
- focused runtime-state tests and migration compatibility tests.

Do not modify Telegram, gateway conversation handling, provider adapters,
prompt construction, legacy `state.db`, or unrelated dirty worktree files.

## Implementation sequence

### Gate 0 — source and mutation-path preflight

- Verify repository identity, design commit, branch, worktree, and merged
  runtime-state implementation.
- Map every runtime-state lifecycle mutation and prove the chosen CAS
  chokepoint covers them.
- Locate the existing local secret/key-storage mechanism without printing or
  copying secrets.
- Define the journal migration version and event field names before edits.
- Stop if a direct mutation path bypasses the chokepoint or durable key custody
  is unavailable.

### Gate 1 — journal schema and migration

Add a versioned runtime-state-owned journal table with:

- event identity;
- profile/entity category;
- profile-scoped keyed entity digest;
- non-secret digest-parameter identifier;
- per-profile/entity monotonic sequence with uniqueness;
- operation category;
- lifecycle state before/after;
- owner-version before/after;
- state schema version and journal event version;
- bounded closed reason code;
- diagnostic timestamp.

The migration must not record raw business keys, owner tokens, prompts,
messages, tool arguments, credentials, filesystem paths, provider payloads, or
arbitrary user data.

Legacy entities must follow the explicit `UNKNOWN` policy above.

### Gate 2 — atomic emission at the CAS chokepoint

- Emit exactly one event for every committed mutation that changes a replayed
  column, including owner-version changes with unchanged lifecycle state.
- Emit no event for a true no-op that changes no replayed column.
- Use the caller's existing SQLite connection and transaction.
- Any journal constraint/write failure aborts the complete enclosing mutation;
  errors must not be swallowed.
- Resolve sequence contention by retrying with a fresh in-transaction sequence
  or aborting; neither path may create a committed event without its state
  mutation or a replay-visible sequence gap.
- Add a negative test proving lifecycle writes cannot bypass the emission
  chokepoint.

### Gate 3 — read-only verifier

Implement a bounded verifier that:

- reads the event stream and materialized row in one read-only snapshot;
- returns per-entity `CONSISTENT`, `DRIFT`, or `UNKNOWN`;
- treats `UNKNOWN` as absorbing;
- requires complete verified history for `DRIFT`;
- validates sequence continuity, duplicate/missing predecessors, baseline
  contents, digest-parameter identity, schema versions, reason codes, and
  terminal state;
- returns `UNKNOWN` on snapshot failure, unsupported/newer versions, malformed
  history, unknown digest regime, exceeded bound, or missing legacy origin;
- performs no writes, ownership claims, external calls, gateway replay, or
  prompt construction.

The verifier must never run inside the gateway conversation loop.

### Gate 4 — focused hermetic tests

Add deterministic tests for:

- empty database and journal migration;
- legacy populated rows returning `UNKNOWN`;
- one event per committed mutation;
- true no-op without an event;
- owner-version-only mutation;
- journal failure rolling back materialized state;
- sequence uniqueness and concurrent contention;
- bypass-path rejection;
- keyed digest/profile isolation and digest-parameter rotation;
- duplicate, missing, malformed, non-contiguous, and unsupported events;
- sealed baseline contents and post-baseline continuity;
- snapshot race returning `CONSISTENT` or `UNKNOWN`, never false `DRIFT`;
- replay bound and closed reason-code enforcement;
- `DRIFT` only for complete verified history;
- no gateway/prompt-cache surface changes.

Run focused tests first, then the repository's relevant full test suite.

### Gate 5 — implementation cross-review

Create a metadata-only implementation packet containing only:

- changed file paths and symbols;
- schema/event field contract;
- mutation and verifier invariants;
- test matrix and results;
- redacted diff statistics and hashes.

Use exactly one authenticated Claude reviewer and one authenticated AGY
reviewer on the same packet. Reconcile any correction set and re-review the
same reviewer family before CI or merge.

### Gate 6 — CI and delivery

Keep these gates separate and record evidence independently:

1. focused tests;
2. full relevant tests;
3. implementation review consensus;
4. commit and push;
5. CI;
6. independent merge authorization;
7. DGX staging/hash/rollback deployment;
8. runtime health;
9. cleanup and handover refresh.

No automatic repair or full event-sourcing work may be inferred from a green
implementation or deployment result.

## Implementation-plan review reconciliation v1

The identical implementation-plan packet received authenticated AGY `PASS`
and authenticated Claude Opus `REVISE`. The following bounded plan corrections
must be incorporated before implementation authorization:

1. Gate 0 must enumerate all runtime-state writer processes/connections and
   explicitly choose the single-writer model or a multi-writer model. If
   multi-writer behavior exists, specify SQLite journal mode, busy timeout, and
   retry policy; align Gate 2's contention rule and Gate 4's test accordingly.
2. Gate 3 must name the concrete read-snapshot mechanism and isolation guarantee;
   Gate 4 must test that mechanism rather than only asserting a race outcome.
3. Gate 1 must add sealed-baseline fields to the journal schema and identify the
   privileged out-of-band baseline writer, which is not invoked by this ticket.
4. Gate 1 must add an explicit genesis/origin marker for the first post-migration
   event of a legacy entity; Gate 4 must cover truncated-head history returning
   `UNKNOWN`.
5. Define digest rotation semantics for pre-rotation events and state that
   verify-time key unavailability returns `UNKNOWN`.
6. Define migration rollback posture and whether prior code tolerates the new
   journal table; assert this in migration compatibility tests.
7. Add an accepted mutation-path overhead budget and journal growth/retention
   expectation to the acceptance criteria.

These are plan-only corrections. They do not authorize source edits, migration,
tests, DGX changes, deployment, repair, or event-sourcing.

## Acceptance criteria

ARCH-003 implementation is ready for its delivery gates only when:

- every committed replayed mutation emits exactly one metadata-only event;
- journal failure cannot commit a materialized mutation;
- profile/entity sequences are contiguous and race-safe;
- legacy rows have explicit `UNKNOWN` behavior;
- baselines and digest regimes are verifiable;
- verifier reads are snapshot-consistent and read-only;
- `DRIFT` is never produced from incomplete or ambiguous history;
- all focused and relevant tests pass;
- authenticated Claude + AGY implementation review reaches consensus;
- no unrelated dirty worktree or DGX runtime state was changed.

## Non-goals

- No automatic drift repair.
- No full event-sourcing rewrite.
- No migration of legacy conversation/session storage.
- No Telegram, gateway, provider, UI, or prompt-loop changes.
- No new user-facing `HERMES_*` configuration variables.
- No cloud storage, inference, OCR, embedding, telemetry, or external service.
- No DGX migration or deployment as part of plan creation.
