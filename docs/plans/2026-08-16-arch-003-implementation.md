---
title: "ARCH-003 implementation plan: runtime-state audit and replay verification"
status: IMPLEMENTATION_PLAN_REVISE_V15_PENDING
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
  path, materialized-row deletion/purge path, and privileged baseline/compaction
  path that can write runtime state. Record the writer matrix before source
  edits.
- This ticket has no hard-delete or profile-purge event contract. If preflight
  finds a delete path reaching runtime-state rows, stop implementation and
  revise the plan before source edits; otherwise record an explicit no-delete
  assertion and test it.
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
- Set `LOCK_ACQUIRE_TIMEOUT = 5s` for both lock sides and
  `SQLITE_BUSY_TIMEOUT = 5s` for SQLite busy handling. Record and test both
  fixed values; the Gate 4 benchmark is evaluated with these values. Acquisition
  failure returns a closed `LOCK_TIMEOUT` reason, aborts before opening SQLite,
  and never blocks ordinary mutations indefinitely. The cross-process primitive
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
  fields, genesis semantics, generation record, key-check value, and rollback
  posture before edits.
- Freeze the closed diagnostic reason-code enum before Gate 3 edits. Statuses
  remain `CONSISTENT`, `DRIFT`, and `UNKNOWN`. The only permitted codes,
  partitioned by surface, are:
  - shared: `OK`, `KEY_UNAVAILABLE`;
  - mutation-side only: `LOCK_TIMEOUT`, `WRITE_ABORT`;
  - verifier-side only: `DRIFT_DETECTED`, `EMPTY_HISTORY`,
    `LEGACY_ORIGIN_MISSING`, `KEY_CHECK_MISMATCH`,
    `DIGEST_PARAMETER_MISMATCH`, `GENERATION_MISMATCH`,
    `MATERIALIZED_STATE_ASYMMETRY`, `SEQUENCE_INVALID`,
    `BASELINE_INVALID`, `HISTORY_MALFORMED`, `UNSUPPORTED_VERSION`,
    `REPLAY_LIMIT_EXCEEDED`, `SNAPSHOT_FAILURE`,
    `POST_TERMINAL_EVENT`, and `WRITE_COUNTER_GAP`.
  Gate 3 validates only the shared and verifier-side subsets; Gate 4 tests
  every member on the surface where it is legal. No implementation may add a
  code outside this partition without a plan revision.
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
- origin epoch marker and origin-genesis sequence, copied into genesis and
  baseline records so origin-epoch validity is checked from bounded row metadata;
- per-profile/entity monotonic sequence with uniqueness;
- operation category;
- lifecycle state before/after;
- owner-version before/after;
- state schema version and journal event version;
- journal-writer generation/epoch continuity marker;
- materialized-write counter before/after, incremented for every committed
  materialized-state mutation and stored in each mutation/genesis event;
- diagnostic timestamp;
- baseline sealed lifecycle state, owner-version, state schema version, and
  sealed-through sequence when event kind is `baseline`;
- sealed materialized-write counter when event kind is `baseline`.

Add a durable database-level current-generation record:
`runtime_state_journal_meta.current_generation`, owned by the runtime-state
migration/startup module. Define a durable writer-epoch transition record
alongside it. The upgraded startup path computes its immutable writer epoch from
the journal schema/code-contract version and advances `current_generation`
exactly once only when that epoch is strictly newer than the durable epoch;
restarting the same writer epoch never advances it. A startup observing a
durable epoch newer than the running writer enters downgrade-unsafe mode and
must not perform ordinary runtime-state writes. The transition record is
durable before the upgraded writer serves mutations, so a strictly newer
contract transition is represented by a distinct epoch transition rather than
by process restart count. A rollback followed by roll-forward to the same
writer epoch does not advance `current_generation`; it is detected by the
materialized-write counter gap defined below. Gate 3 reads the
current-generation record, writer epoch, transition marker, and counter
metadata inside the same `BEGIN DEFERRED` snapshot as the event stream and
materialized row.

Add a materialized-row journal-writer generation marker and a monotonic
`materialized_write_counter`. The counter increments only when a committed
mutation changes a member of the replay tuple, independently of whether
journal emission succeeds. Non-replay-tuple-only updates do not increment it
and remain outside the verifier's `CONSISTENT` claim. A database-level
trigger restricted to the replay-tuple columns, with a value-change predicate,
or an equivalent write guard must preserve this increment for any prior writer
that can mutate those columns. The mutation/genesis transaction records the
counter before/after values in its event and updates the row marker. A row whose marker is older
than the current generation, or whose materialized counter is greater than the
latest journaled counter for that entity, is `UNKNOWN`, never `DRIFT`. Every
event's before-counter must equal the immediately preceding event's
after-counter, or the baseline's sealed counter; any discontinuity is a
permanent history gap and returns `UNKNOWN` with `WRITE_COUNTER_GAP`. If the
counters agree, the history is complete, and the replay tuple differs, the
result may be `DRIFT` with `DRIFT_DETECTED`. This is the concrete
discriminator between an unjournaled state advance and genuine drift. A rollback-then-roll-forward cycle therefore keeps affected entities
`UNKNOWN`. A strictly newer writer epoch can re-originate on its first
journaled mutation as defined above; a same-epoch counter-gap entity has no
automatic or plain-mutation recovery path in this ticket. Its next ordinary
mutation is rejected with `WRITE_ABORT` until a separately authorized
re-originating operation exists. A same-epoch process restart does not advance
`current_generation`; an unjournaled write is detected by the counter gap.

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
verifies the target row's generation marker equals the current-generation
record, validates the sealed tuple against the current materialized row and
current maximum sequence, appends the baseline event on the same connection,
and releases the lock after commit or rollback. Production gateway, scheduler,
and automatic-repair paths never invoke it in this ticket; hermetic tests may
invoke it directly.

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
genesis event. Any entity with no prior current-epoch journal history, including
an entity first created after migration, likewise emits a marked genesis for its
first journaled replay-tuple mutation. After a current-generation advance, the
first journaled replay-tuple mutation also emits a marked genesis; a plain
mutation never establishes a new origin epoch. That single genesis event both
establishes the trusted origin and records the committed mutation: its before-state
tuple is the then-observed origin tuple, its after-state tuple is the committed
post-mutation tuple, and no separate genesis-plus-mutation pair is emitted.
`CONSISTENT` for that entity covers only history from that genesis event onward. A
truncated head without the required origin marker returns `UNKNOWN`.

Digest parameter identifiers are persisted metadata; key material is never
journaled. Use HMAC-SHA-256 for the entity digest and for the key-check over a
fixed known constant; retain at least 128 bits of key-check output. Record the
algorithm, truncation length, and key generation under the non-secret
digest-parameter identifier. The verifier recomputes and compares the key-check
before replay and returns `UNKNOWN` on mismatch or key unavailability.

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
  owner-version-only changes with unchanged lifecycle state. The first
  journaled replay-tuple mutation for an entity with no current-epoch history
  (including a post-migration entity) is `genesis`; the first such mutation
  after a current-generation advance is also `genesis`, while subsequent
  mutations use `mutation`.
- Emit no event for a true no-op that changes no replayed column.
- Ordinary mutation/genesis paths acquire the shared side of the named
  cross-process maintenance RW lock before opening the SQLite WAL transaction;
  use `BEGIN IMMEDIATE` or equivalent before sequence allocation; release the
  shared lock only after commit or rollback.
- Inside that write transaction, compare the materialized row's counter with
  the latest journaled after-counter (or baseline sealed counter). For a
  same-epoch ordinary mutation, a mismatch is a persisted counter gap:
  abort with mutation-side `WRITE_ABORT` before changing replay state or
  emitting an event. A strictly newer writer-epoch re-originating genesis is
  the explicit exception; it starts a new selected replay epoch and supersedes
  the prior gap as defined by Gate 3.
- Use the caller's existing SQLite WAL connection and transaction for mutation
  and genesis events, update the materialized generation marker, and record the
  current generation in the event.
- Any mutation/genesis journal constraint or write failure aborts the complete
  enclosing materialized-state mutation; errors must not be swallowed.
- If the digest key is unavailable while preparing a mutation/genesis, fail
  closed before opening the SQLite transaction, leave materialized state
  unchanged, emit no event, and surface the shared `KEY_UNAVAILABLE` reason.
  This is distinct from `WRITE_ABORT`, which covers a transaction or
  continuity refusal after the mutation path has entered its write contract.
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
  and the current-generation record, bounded event stream, and materialized row
  are all read after that snapshot establishment and before the transaction
  ends. Immutable-open is prohibited for this verifier;
- validates the HMAC-SHA-256 key-check, algorithm/truncation parameters, and
  digest identifier before replay;
- returns per-entity `CONSISTENT`, `DRIFT`, or `UNKNOWN`;
- treats `UNKNOWN` as absorbing;
- requires complete verified history for `DRIFT`;
- validates event kind, origin/genesis markers, journal generation/epoch
  continuity, baseline contents and baseline-to-next-event sequence continuity,
  duplicate or missing predecessors, digest-parameter identity, schemas, the
  shared and verifier-side closed reason-code subsets, and terminal state;
- uses a code-level `REPLAY_EVENT_LIMIT = 10000` counted from the effective
  replay start point. Compute the start candidates as the highest valid baseline
  under the current digest identifier and the latest valid marked genesis under
  the current digest identifier, when present; select the later candidate by
  `entity_seq`. If neither candidate exists, return `UNKNOWN`. Baseline and
  genesis rows carry the origin epoch marker and origin-genesis sequence, so
  current-origin validity is checked from bounded row metadata without scanning
  an unbounded pre-start prefix. Events preceding the selected start are
  ignored after that O(1) metadata check, and counter continuity is enforced
  only from the selected start forward. This permits a newer-writer-epoch
  genesis after a baseline to re-originate the entity.
- validates the selected baseline when one is the later replay-start candidate,
  including highest `sealed_through_seq` selection, same-sequence tuple
  conflicts, overlapping contradictory baselines, a baseline without a valid
  current origin epoch, a stale generation marker, a materialized-write counter
  greater than the latest journaled counter, or a materialized state advanced
  during a generation with no current-generation event;
- returns `UNKNOWN` with the corresponding closed reason code on empty history
  (`EMPTY_HISTORY`), snapshot failure, unsupported/newer versions, malformed
  history, unknown digest regime, key-check mismatch, verify-time key
  unavailability, exceeded bound, missing legacy origin, counter gap
  (`WRITE_COUNTER_GAP`), or a materialized-row/history asymmetry;
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
- explicit no-delete assertion: no delete/purge path reaches runtime-state rows;
- legacy populated rows returning `UNKNOWN`;
- marked post-migration genesis adopting the before-state origin tuple and
  limiting `CONSISTENT` to post-genesis history;
- post-rotation genesis under a new digest identifier, with pre-rotation
  history remaining `UNKNOWN`;
- downgrade-window simulation: mutate materialized state with journaling
  disabled, roll forward, and assert verifier returns `UNKNOWN`, never
  `DRIFT`;
- prior-version startup with the journal table present and rollback posture;
- current-generation record advancement only on a strictly newer writer epoch,
  same-epoch restart without advancement, same-epoch rollback/roll-forward
  detected by the materialized-write counter gap, downgrade-unsafe startup
  returning mutation-side `WRITE_ABORT`, and concurrent generation advance
  during verify returning `CONSISTENT` or `UNKNOWN`, never false `DRIFT`;
- a newly created post-migration entity whose first journaled mutation is a
  marked genesis and reaches `CONSISTENT` after a valid follow-up mutation;
- first post-generation-advance mutation emitting a marked genesis, with a
  plain mutation alone unable to re-establish the epoch;
- one event per committed replay-tuple mutation;
- true no-op without an event;
- owner-version-only mutation;
- mutation/genesis journal failure rolling back materialized state with
  mutation-side `WRITE_ABORT`;
- any same-epoch ordinary mutation attempted in downgrade-unsafe mode or
  against a persisted counter-gap entity returns mutation-side `WRITE_ABORT`;
  a strictly newer writer-epoch re-originating genesis is exempt and
  supersedes the gap at the later replay start;
- SQLite WAL, fixed `SQLITE_BUSY_TIMEOUT = 5s`, `BEGIN IMMEDIATE`, and
  read-only WAL preconditions;
- `LOCK_ACQUIRE_TIMEOUT = 5s`, `SQLITE_BUSY_TIMEOUT = 5s`,
  `LOCK_TIMEOUT` reason, stale-holder recovery, and shared/exclusive
  cross-process RW-lock exclusion/release ordering;
- single-writer invariant and defensive abort; no sequence-collision retry is
  expected under write-lock-first discipline;
- baseline generation-marker mismatch returning `UNKNOWN`;
- bypass-path rejection for mutation/genesis while permitting the named baseline
  writer;
- keyed digest/profile isolation, identifier/key-check mismatch, digest
  rotation without bridging, verify-time key unavailability, and mutation-time
  key unavailability returning shared `KEY_UNAVAILABLE` without changing
  materialized state;
- duplicate, missing, malformed, non-contiguous, and unsupported events;
- empty history returning `UNKNOWN` with `EMPTY_HISTORY`;
- the same fixture with a materialized-write counter gap returning `UNKNOWN`
  with `WRITE_COUNTER_GAP`, and a subsequent ordinary mutation rejected with
  `WRITE_ABORT`;
- the same fixture with equal counters but a tuple mismatch returning
  `DRIFT` with `DRIFT_DETECTED`;
- truncated-head history without an origin marker returning `UNKNOWN`;
- sealed-baseline tuple mismatch against the materialized row returning
  `UNKNOWN`;
- baseline behind the current maximum sequence returning `UNKNOWN`;
- baseline with no valid current origin returning `UNKNOWN`;
- baseline with a non-current digest-parameter identifier returning `UNKNOWN`;
- same-sequence conflicting baselines and contradictory overlapping baselines
  returning `UNKNOWN`, while strictly increasing successive baselines select
  the highest valid one;
- a baseline followed by a newer-writer-epoch marked genesis selecting the
  later genesis start and reaching `CONSISTENT`;
- sealed baseline contents, baseline sequence consumption, and post-baseline
  continuity;
- snapshot race using explicit `BEGIN DEFERRED` and the selected WAL
  read-transaction mechanism, returning `CONSISTENT` or `UNKNOWN`, never
  false `DRIFT`;
- replay bound counted from the effective replay start point, bounded
  origin-epoch metadata validation, current-epoch genesis selection, baseline
  selection, generation mismatch, and every member of each surface's closed
  reason-code subset;
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

## Implementation-plan review reconciliation v8

The v8 review required a durable current-generation record in the verifier
snapshot, explicit no-delete policy, acceptance wording aligned with the
no-retry discipline, baseline generation checks, HMAC-SHA-256 key-check
parameters, and one authoritative benchmark gate. These are incorporated in
Gates 0-4 above. This section is changelog only.

## Implementation-plan review reconciliation v9

The v9 authenticated Claude Opus review returned `REVISE` with seven bounded
plan-text findings, grouped into six correction areas. The corrections are
incorporated above:

1. Any entity without current-epoch history, including post-migration-created
   entities, begins with a marked genesis; generation recovery is genesis-only.
2. The acceptance criteria use the write-lock-first/no-retry defensive-abort
   discipline rather than a nonexistent sequence retry cap.
3. Local preflight is the authoritative 5 ms p95 benchmark gate; CI is
   record-only for host-noise variance.
4. Gate 0 freezes the closed diagnostic reason-code enum.
5. Growth accounting includes out-of-band baseline rows.
6. `SQLITE_BUSY_TIMEOUT = 5s` is fixed and included in the benchmark contract.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v10

The v10 authenticated Claude Opus review returned `REVISE` with four bounded
plan-text corrections. The corrections are incorporated above:

1. Current-generation advancement is tied to a strictly newer durable writer
   epoch, not ordinary process restart; downgrade-unsafe startup is explicit.
2. Genesis and baseline records carry bounded origin-epoch metadata, so
   pre-start origin validation does not create an unbounded verifier scan.
3. The closed reason-code enum is partitioned into shared, mutation-side, and
   verifier-side surfaces.
4. The v9 changelog now states seven findings grouped into six correction areas.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v11

The v11 authenticated Claude Opus review returned `REVISE` with four bounded
plan-text corrections. The corrections are incorporated above:

1. Same-epoch rollback/roll-forward is detected by a durable materialized-write
   counter gap; strictly newer writer epochs alone advance `current_generation`.
2. Equal counters with a complete history and tuple mismatch are the only
   `DRIFT` path; counter gaps are `UNKNOWN`.
3. Acceptance reconciliation now enumerates v1 and v5-v10, with v2-v4 folded
   explicitly into the earlier normative text.
4. `WRITE_ABORT`, `EMPTY_HISTORY`, and `DRIFT_DETECTED` are bound to
   concrete surfaces and tests.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v12

The v12 authenticated Claude Opus review returned `REVISE` with five bounded
plan-text corrections. The corrections are incorporated above:

1. Every event's materialized-write counter must equal the preceding event's
   after-counter (or the baseline sealed counter); any gap is permanently
   `UNKNOWN`.
2. Same-epoch counter-gap entities have no in-ticket automatic recovery; the
   next ordinary mutation is rejected with `WRITE_ABORT`.
3. `WRITE_COUNTER_GAP` is a verifier-side closed reason code.
4. Downgrade-unsafe startup is bound to mutation-side `WRITE_ABORT`.
5. Duplicate schema bullets were removed and acceptance/changelog coverage now
   includes v1 and v5-v12.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v13

The v13 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. Replay start selects the later of the highest valid baseline and the latest
   valid current-epoch genesis, with counter continuity enforced only from that
   selected start.
2. Mutation-time digest-key unavailability is a shared `KEY_UNAVAILABLE`
   fail-closed outcome with no state or event mutation.
3. Acceptance coverage now explicitly includes v12.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v14

The v14 authenticated Claude Opus review returned `REVISE` with five bounded
plan-text corrections. The corrections are incorporated above:

1. The materialized-write counter is limited to replay-tuple changes; non-replay
   updates remain outside the verifier claim.
2. Same-epoch mutation counter-gap detection is an in-transaction comparison
   before replay-state mutation and returns `WRITE_ABORT`.
3. A strictly newer writer-epoch genesis is explicitly exempt from that abort
   and supersedes the prior gap at the later replay start.
4. Acceptance coverage now includes v13.
5. The replay-start baseline validation is merged with the later-of-baseline-or-
   genesis selection rule.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Acceptance criteria

ARCH-003 implementation is ready for its delivery gates only when:

- every committed replay-tuple mutation emits exactly one metadata-only event;
- journal failure cannot commit a materialized mutation;
- the writer model, snapshot mechanism, write-lock-first/no-retry defensive
  abort discipline, baseline/genesis schema, digest rotation behavior, rollback
  posture, and key handling are explicit in the Gates 0-4 bodies;
- profile/entity sequences are contiguous and race-safe;
- legacy rows have explicit `UNKNOWN` behavior;
- baselines are verifiable; digest rotation starts a marked post-rotation
  genesis epoch, while all pre-rotation history remains `UNKNOWN` with no
  in-ticket bridge or recovery path; rollback downgrade windows are detected
  by the generation marker and remain `UNKNOWN` until the first subsequent
  marked genesis re-originates the entity; a plain mutation cannot recover it;
- verifier reads are snapshot-consistent and read-only;
- `DRIFT` is never produced from incomplete or ambiguous history;
- verifier mutation and prompt-cache/gateway isolation tests pass;
- incremental mutation-path overhead target is at most 5 ms p95 in the focused
  benchmark; the authoritative enforcement point is the local-preflight
  benchmark arm, where exceeding 5 ms p95 blocks delivery and requires plan
  revision; CI records the result but does not gate on host-noise variance;
- journal growth is one bounded metadata row per committed replay-tuple
  mutation, plus one row per out-of-band baseline event, with no automatic
  retention job; entities beyond the replay limit remain UNKNOWN in this ticket,
  and the 100,000-event/100 MiB per-profile threshold is a documented follow-up
  operational review, not an in-ticket operator gate;
- reconciliation v1 and v5-v13 items are incorporated into the normative
  Gates 0-4 text (with v2-v4 explicitly folded into the v1/v4 text), and this
  merged plan revision receives the same-family re-review;
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
- v8: durable current-generation record, explicit no-delete policy, baseline
  generation checks, HMAC key-check parameters, and authoritative benchmark
  wording.
- v9: post-migration creation genesis, genesis-only generation recovery,
  closed reason-code enumeration, fixed SQLite busy timeout, no-retry acceptance
  wording, benchmark enforcement point, and baseline-inclusive growth accounting.
- v10: durable writer-epoch advancement trigger, bounded origin-epoch metadata
  for verifier start selection, reason-code surface partition, and corrected
  reconciliation count.
- v11: same-epoch continuity counter, UNKNOWN-versus-DRIFT discriminator,
  complete reconciliation coverage, and explicit reason-code test bindings.
- v12: event-to-event counter continuity, permanent counter-gap UNKNOWN,
  explicit WRITE_ABORT recovery boundary, and deduplicated schema contract.
- v13: later-start selection for baseline/genesis re-origination, shared
  mutation-time KEY_UNAVAILABLE semantics, and complete acceptance coverage.
- v14: replay-tuple-only counter scope, in-transaction gap refusal, explicit
  newer-epoch genesis exception, and merged replay-start validation.
- v14: replay-tuple-only counter scope, in-transaction gap refusal, explicit
  newer-epoch genesis exception, and merged replay-start validation.


## Non-goals

- No automatic drift repair.
- No full event-sourcing rewrite.
- No migration of legacy conversation/session storage.
- No Telegram, gateway, provider, UI, or prompt-loop changes.
- No new user-facing `HERMES_*` configuration variables.
- No cloud storage, inference, OCR, embedding, telemetry, or external service.
- No DGX migration or deployment as part of plan creation.
