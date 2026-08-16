---
title: "ARCH-003 implementation plan: runtime-state audit and replay verification"
status: IMPLEMENTATION_PLAN_REVISE_V8_PENDING
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
- one mutation connection and transaction per committed mutation, under the writer model selected in Gate 0;
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

### Gate 0 - source and mutation-path preflight

- Verify repository identity, design commit, branch, worktree, and merged
  runtime-state implementation.
- Enumerate every process, thread, connection, scheduler path, maintenance
  path, and privileged baseline/compaction path that can write runtime state.
  Record the writer matrix before source edits.
- Use SQLite WAL mode for both the single-writer and multi-writer branches.
  Record and test `PRAGMA journal_mode=WAL`, the selected busy-timeout, and
  read-only WAL preconditions: `-wal` and `-shm` accessibility. The verifier
  uses a normal read-only WAL connection with `BEGIN DEFERRED`; immutable-open
  is prohibited because it cannot provide the required concurrent WAL snapshot.
- Define one named cross-process runtime-state maintenance RW lock. Ordinary
  mutation/genesis paths acquire its shared/read side before opening the
  SQLite transaction and release it after commit or rollback. The privileged
  baseline writer acquires its exclusive/write side before opening its WAL
  transaction and releases it after commit or rollback.
- Set `LOCK_ACQUIRE_TIMEOUT = 5s` for both lock sides. Acquisition failure
  returns a closed `LOCK_TIMEOUT` reason, aborts before opening SQLite, and
  never blocks ordinary mutations indefinitely. The cross-process primitive
  must release ownership automatically when its holder dies; if safe
  stale-holder recovery cannot be proven, stop preflight. Exclusive baseline
  acquisition may fail and be retried only by its out-of-band caller.
- The single-writer branch proves one ordinary mutation connection and
  defensive abort; the multi-writer branch requires the cross-process RW lock.
  Mutation/genesis transactions use `BEGIN IMMEDIATE` or equivalent
  write-lock-first discipline before sequence allocation. Since the write lock
  is held through commit, sequence collisions are not a retry path; any
  unexpected uniqueness collision aborts defensively.
- Map every runtime-state lifecycle mutation and prove the chosen CAS
  chokepoint covers them.
- Locate the existing local secret/key-storage mechanism without printing or
  copying secrets. If durable custody is unavailable, stop.
- Define the journal migration version, event kinds, replay tuple, baseline
  fields, genesis semantics, generation marker, key-check value, and rollback
  posture before edits.
- Confirm that the prior release can start with the new journal table present
  and that rollback does not require deleting the table or losing materialized
  state.
- Stop if a direct mutation path bypasses the chokepoint, key custody is
  unavailable, WAL or read-only WAL preconditions cannot be established,
  writer semantics are unresolved, or prior-version tolerance is unproven.

### Gate 1 - journal schema and migration

Add a versioned runtime-state-owned journal table with:

- event identity and event kind: `mutation`, `genesis`, or `baseline`;
- profile/entity category;
- profile-scoped keyed entity digest;
- non-secret digest-parameter identifier;
- non-secret key-check value bound to the digest-parameter identifier;
- origin marker for migration-origin or post-migration genesis;
- per-profile/entity monotonic sequence with uniqueness;
- operation category;
- lifecycle state before/after;
- owner-version before/after;
- state schema version and journal event version;
- journal-writer generation/epoch continuity marker;
- diagnostic timestamp;
- baseline sealed lifecycle state, owner-version, state schema version, and
  sealed-through sequence when event kind is `baseline`.

Add a materialized-row journal-writer generation marker. The upgraded runtime
advances the database generation atomically on startup under the new schema
version; every mutation/genesis updates the row marker and records the current
generation in its event. A row whose marker is older than the current
generation, or whose materialized state advanced without a corresponding
current-generation event, is `UNKNOWN`, never `DRIFT`. A rollback-then-
roll-forward cycle therefore keeps affected entities `UNKNOWN` until a
subsequent journaled genesis/mutation re-establishes current-generation origin.

The replay tuple is explicitly `lifecycle_state`, `owner_version`, and
`state_schema_version`; terminality is derived from lifecycle state. The
generation marker is a provenance continuity check, not a user-visible
replayed state. Columns outside the replay tuple are outside the verifier's
`CONSISTENT` claim.

Baseline events consume the same per-profile/entity sequence space. A baseline
row has `entity_seq = sealed_through_seq + 1`; inside its transaction the
writer must observe `sealed_through_seq` equal to the current maximum event
sequence for that entity. A baseline that seals behind the current maximum is
invalid and verifies as `UNKNOWN`. The first subsequent mutation or genesis
must use `entity_seq + 1`. The verifier starts from the baseline tuple and
checks this continuity. A baseline is the sole event kind allowed to commit
without an accompanying materialized-state mutation.

The privileged baseline writer is an internal runtime-state maintenance
operation with explicit operator authorization, outside the gateway, scheduler,
and verifier surfaces. It acquires the exclusive side of the named
cross-process maintenance RW lock before opening its SQLite WAL transaction,
validates the sealed tuple against the current materialized row and current
maximum sequence, appends the baseline event on the same connection, and
releases the lock after commit or rollback. Production gateway, scheduler, and
automatic-repair paths never invoke it in this ticket; hermetic tests may invoke
it directly.

A valid baseline must be in or after a valid origin epoch under the current
digest-parameter identifier. A legacy/pre-journal or pre-rotation entity with
no valid current-epoch genesis cannot be made `CONSISTENT` by a baseline.
Successive baselines with strictly increasing `sealed_through_seq` are the
expected compaction case. Same-sequence baselines with different tuples, or a
later baseline whose sealed tuple contradicts a surviving baseline in its
sealed range, are conflicting and verify as `UNKNOWN`.

The migration must not record raw business keys, owner tokens, prompts,
messages, tool arguments, credentials, filesystem paths, provider payloads, or
arbitrary user data.

Legacy entities use the no-backfill policy: pre-journal rows remain `UNKNOWN`
until a successful post-migration mutation emits a marked post-migration
genesis event. That single genesis event both establishes the trusted origin
and records the committed mutation: its before-state tuple is the
then-observed origin tuple, its after-state tuple is the committed post-mutation
tuple, and no separate genesis-plus-mutation pair is emitted. `CONSISTENT`
for that entity covers only history from that genesis event onward. A truncated
head without the required origin marker returns `UNKNOWN`.

Digest parameter identifiers are persisted metadata; key material is never
journaled. Persist the key-check as a truncated digest of a fixed known
constant under the digest key. The verifier recomputes it before replay and
returns `UNKNOWN` if the key-check does not match the identifier or if the key
is unavailable.

A digest-parameter rotation starts a new epoch: the first committed
post-rotation replay-tuple mutation emits a marked genesis event under the new
identifier, with `CONSISTENT` covering only post-rotation history. All
pre-rotation history remains `UNKNOWN` and is not bridged by this ticket.

Migration rollback is forward-compatible: prior code must tolerate the journal
table's presence and continue serving materialized rows; rollback must not
delete the table or rewrite history. Migration compatibility tests must assert
this posture.

### Gate 2 - atomic emission at the CAS chokepoint

- Emit exactly one `mutation` or `genesis` event for every committed
  mutation that changes any member of the replay tuple, including
  owner-version-only changes with unchanged lifecycle state.
- Emit no event for a true no-op that changes no replayed column.
- Ordinary mutation/genesis paths acquire the shared side of the named
  cross-process maintenance RW lock before opening the SQLite WAL transaction;
  use `BEGIN IMMEDIATE` or equivalent before sequence allocation; release the
  shared lock only after commit or rollback.
- Use the caller's existing SQLite WAL connection and transaction for mutation
  and genesis events, update the materialized generation marker, and record the
  current generation in the event.
- Any mutation/genesis journal constraint or write failure aborts the complete
  enclosing materialized-state mutation; errors must not be swallowed.
- The privileged baseline writer is the sole event-only exception and uses the
  exclusive lock ordering defined in Gate 1.
- There is no sequence retry path under the write-lock-first discipline. An
  unexpected uniqueness collision or busy-timeout exhaustion aborts the whole
  transaction and surfaces a bounded failure; it is not retried inside the
  invalid transaction.
- Neither abort may create a committed mutation/genesis event without its state
  mutation or a replay-visible sequence gap. Baseline sequence continuity is
  governed by Gate 1.
- Add a negative test proving lifecycle writes cannot bypass the mutation/genesis
  emission chokepoint; the test must not reject the explicitly named baseline
  writer.

### Gate 3 - read-only verifier

Implement a bounded verifier that:

- opens a read-only SQLite WAL connection and explicitly executes
  `BEGIN DEFERRED`; the first event-stream read establishes the snapshot,
  and both the bounded event stream and materialized row are read after that
  snapshot establishment and before the transaction ends. Immutable-open is
  prohibited for this verifier;
- validates the key-check value before replay and returns `UNKNOWN` on
  identifier/key mismatch or key unavailability;
- returns per-entity `CONSISTENT`, `DRIFT`, or `UNKNOWN`;
- treats `UNKNOWN` as absorbing;
- requires complete verified history for `DRIFT`;
- validates event kind, origin/genesis markers, journal generation/epoch
  continuity, baseline contents and baseline-to-next-event sequence continuity,
  duplicate or missing predecessors, digest-parameter identity, schemas,
  reason codes, and terminal state;
- uses a code-level `REPLAY_EVENT_LIMIT = 10000` counted from the effective
  replay start point: the latest valid baseline under the current digest
  identifier if one exists, otherwise the latest valid marked genesis under the
  current digest identifier; otherwise `UNKNOWN`. Events preceding that start
  are ignored;
- selects the valid baseline with the highest `sealed_through_seq`, ignores
  events before it, and returns `UNKNOWN` for same-sequence tuple conflicts,
  overlapping contradictory baselines, a baseline without a valid current
  origin epoch, a stale generation marker, or a materialized state advanced
  during a generation with no current-generation event;
- returns `UNKNOWN` on snapshot failure, unsupported/newer versions, malformed
  history, unknown digest regime, key-check mismatch, verify-time key
  unavailability, exceeded bound, missing legacy origin, or a
  materialized-row/history asymmetry;
- treats a materialized row missing while history exists, or history existing
  without a materialized row, as `UNKNOWN`, never `DRIFT`;
- treats any event observed after a terminal lifecycle state as malformed
  history and returns `UNKNOWN`, never `DRIFT`;
- may read digest key material through the approved local key store, but never
  logs, persists, echoes, or includes key material in diagnostics or results;
- performs no writes, ownership claims, external calls, gateway replay, or
  prompt construction.

The verifier must never run inside the gateway conversation loop. A verifier
result covers only the replay tuple; non-replayed materialized columns are
outside the `CONSISTENT` claim.

### Gate 4 - focused hermetic tests

Add deterministic tests for:

- empty database and journal migration;
- legacy populated rows returning `UNKNOWN`;
- marked post-migration genesis adopting the before-state origin tuple and
  limiting `CONSISTENT` to post-genesis history;
- post-rotation genesis under a new digest identifier, with pre-rotation
  history remaining `UNKNOWN`;
- downgrade-window simulation: mutate materialized state with journaling
  disabled, roll forward, and assert verifier returns `UNKNOWN`, never
  `DRIFT`;
- prior-version startup with the journal table present and rollback posture;
- one event per committed replay-tuple mutation;
- true no-op without an event;
- owner-version-only mutation;
- mutation/genesis journal failure rolling back materialized state;
- SQLite WAL, selected busy-timeout, `BEGIN IMMEDIATE`, and read-only WAL
  preconditions;
- `LOCK_ACQUIRE_TIMEOUT = 5s`, lock-timeout reason, stale-holder recovery,
  and shared/exclusive cross-process RW-lock exclusion/release ordering;
- single-writer invariant and defensive abort; no sequence-collision retry is
  expected under write-lock-first discipline;
- bypass-path rejection for mutation/genesis while permitting the named baseline
  writer;
- keyed digest/profile isolation, identifier/key-check mismatch, digest
  rotation without bridging, and verify-time key unavailability;
- duplicate, missing, malformed, non-contiguous, and unsupported events;
- truncated-head history without an origin marker returning `UNKNOWN`;
- sealed-baseline tuple mismatch against the materialized row returning
  `UNKNOWN`;
- baseline behind the current maximum sequence returning `UNKNOWN`;
- baseline with no valid current origin returning `UNKNOWN`;
- baseline with a non-current digest-parameter identifier returning `UNKNOWN`;
- same-sequence conflicting baselines and contradictory overlapping baselines
  returning `UNKNOWN`, while strictly increasing successive baselines select
  the highest valid one;
- sealed baseline contents, baseline sequence consumption, and post-baseline
  continuity;
- snapshot race using explicit `BEGIN DEFERRED` and the selected WAL
  read-transaction mechanism, returning `CONSISTENT` or `UNKNOWN`, never
  false `DRIFT`;
- replay bound counted from the effective replay start point, current-epoch
  genesis selection, baseline selection, conflicting/overlapping baselines,
  generation mismatch, and closed reason codes;
- verifier-performs-no-writes using a direct database-change assertion;
- materialized-row/history asymmetry returning `UNKNOWN`;
- post-terminal events returning `UNKNOWN`;
- prompt-cache/gateway no-change check: runtime-state modules must not import
  or invoke gateway conversation or prompt-construction modules;
- mutation benchmark: 20 repetitions, each with 200 warmup mutations followed
  by 1000 measured mutations distributed round-robin across 100 entities, on
  the same Python/SQLite WAL test environment and temp database shape. Pool all
  measured per-mutation latencies across the 20 repetitions and compute one
  p95 per arm; report the journal-enabled minus journal-disabled p95. The
  5 ms p95 threshold is a local-preflight blocker; CI records the result but
  does not gate on host-noise variance. The control is test-local and has no
  user-facing disable switch;
- `DRIFT` only for complete verified history;
- non-replayed columns remain outside the verifier claim.

Run focused tests first, then the repository's relevant full test suite.

### Gate 5 - implementation cross-review

Create a metadata-only implementation packet containing only:

- changed file paths and symbols;
- schema/event field contract;
- mutation and verifier invariants;
- test matrix and results;
- redacted diff statistics and hashes.

Use exactly one authenticated Claude reviewer and one authenticated AGY
reviewer on the same packet. Reconcile any correction set and re-review the
same reviewer family before CI or merge.

### Gate 6 - CI and delivery

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

## Implementation-plan review reconciliation v7

The v7 review required downgrade-window detection, current-epoch genesis
selection, key-check binding, bounded lock acquisition and stale-holder
recovery, removal of unreachable sequence retry, distributed benchmark shape,
aligned benchmark gating, and a continuous reconciliation index. These are
incorporated in Gates 0-4 above. Earlier v1-v6 correction summaries are
retained in the reconciliation index below.

## Acceptance criteria

ARCH-003 implementation is ready for its delivery gates only when:

- every committed replay-tuple mutation emits exactly one metadata-only event;
- journal failure cannot commit a materialized mutation;
- the writer model, snapshot mechanism, sequence retry cap, baseline/genesis
  schema, digest rotation behavior, rollback posture, and key handling are
  explicit in the Gates 0-4 bodies;
- profile/entity sequences are contiguous and race-safe;
- legacy rows have explicit `UNKNOWN` behavior;
- baselines are verifiable; digest rotation starts a marked post-rotation
  genesis epoch, while all pre-rotation history remains `UNKNOWN` with no
  in-ticket bridge or recovery path; rollback downgrade windows are detected
  by the generation marker and remain `UNKNOWN` until re-originated;
- verifier reads are snapshot-consistent and read-only;
- `DRIFT` is never produced from incomplete or ambiguous history;
- verifier mutation and prompt-cache/gateway isolation tests pass;
- incremental mutation-path overhead target is at most 5 ms p95 in the focused
  benchmark; exceeding it blocks delivery and requires plan revision;
- journal growth is one bounded metadata row per committed replay-tuple
  mutation, with no automatic retention job; entities beyond the replay limit
  remain UNKNOWN in this ticket, and the 100,000-event/100 MiB per-profile
  threshold is a documented follow-up operational review, not an in-ticket
  operator gate;
- reconciliation v1 items are incorporated into the normative Gates 0-4 text and
  this merged plan revision receives the same-family re-review;
- all focused and relevant tests pass;
- authenticated Claude + AGY implementation review reaches consensus;
- no unrelated dirty worktree or DGX runtime state was changed.

## Reconciliation changelog index

- v1: writer model, snapshot mechanism, baseline/genesis fields, rotation and
  rollback semantics, overhead/growth expectations, and direct safety tests.
- v2-v4: folded into v1 and v4 normative Gate 0-4 text; revision commits are
  retained in GitHub history.
- v5: post-rotation genesis, current-sequence baseline checks, write-lock-first
  discipline, fixed lock ordering, benchmark protocol, and WAL preconditions.
- v6: cross-process maintenance-RW-lock granularity, immutable-open removal,
  current-origin baseline requirement, successive-baseline conflict semantics.
- v7: generation continuity for downgrade windows, current-epoch replay start,
  key-check binding, lock timeout/stale-holder behavior, retry removal, and
  distributed benchmark shape.


## Non-goals

- No automatic drift repair.
- No full event-sourcing rewrite.
- No migration of legacy conversation/session storage.
- No Telegram, gateway, provider, UI, or prompt-loop changes.
- No new user-facing `HERMES_*` configuration variables.
- No cloud storage, inference, OCR, embedding, telemetry, or external service.
- No DGX migration or deployment as part of plan creation.
