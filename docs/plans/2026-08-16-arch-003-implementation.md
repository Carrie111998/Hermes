---
title: "ARCH-003 implementation plan: runtime-state audit and replay verification"
status: IMPLEMENTATION_PLAN_REVISE_V33_PENDING
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
4. **Verifier and maintenance surface:** Keep the verifier library-local plus
   hermetic tests. The authorized re-originating genesis is an internal,
   code-level maintenance invocation requiring explicit operator authorization;
   it is not exposed as a user-facing CLI, scheduler, dashboard, gateway, or
   automatic-repair path in this ticket. If that maintenance invocation is not
   available in a deployment, affected entities remain write-blocked with
   bounded `WRITE_ABORT` while unaffected conversations and platform loops
   continue; delivery must record this operational consequence.

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
- The named cross-process maintenance RW lock is required in both the
  single-writer and multi-writer branches. The single-writer branch additionally
  proves one ordinary mutation connection and defensive abort; the multi-writer
  branch additionally proves cross-process writer coordination. Ordinary
  shared-side paths and privileged/startup exclusive-side paths use this lock
  in both branches.
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
- Freeze the origin-marker enum before Gate 1 edits:
  `POST_MIGRATION_GENESIS`, `NEW_ENTITY_GENESIS`,
  `POST_ROTATION_GENESIS`, `GENERATION_REORIGIN_GENESIS`, and
  `MANUAL_REORIGIN_GENESIS`. Assign markers by this deterministic
  precedence: explicit `MANUAL_REORIGIN_GENESIS` first; then
  `NEW_ENTITY_GENESIS` when the row is created by the mutation; then
  `GENERATION_REORIGIN_GENESIS` for a pre-existing row after a newer writer
  epoch; then `POST_ROTATION_GENESIS` for a pre-existing row after digest
  rotation; then `POST_MIGRATION_GENESIS` for a legacy/pre-journal row.
  A baseline copies the origin marker of the epoch it seals; no other origin
  marker is valid.
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
- origin marker from the closed set `POST_MIGRATION_GENESIS`,
  `NEW_ENTITY_GENESIS`, `POST_ROTATION_GENESIS`,
  `GENERATION_REORIGIN_GENESIS`, or `MANUAL_REORIGIN_GENESIS`;
- origin epoch marker and origin-genesis sequence, copied into genesis and
  baseline records so origin-epoch validity is checked from bounded row metadata;
- per-profile/entity monotonic sequence with uniqueness;
- operation category;
- lifecycle state before/after;
- owner-version before/after;
- state schema version and journal event version;
- journal-writer generation/epoch continuity marker;
- materialized-write counter before/after, incremented only for every committed
  mutation that changes a replay-tuple member and stored in each mutation/genesis
  event;
- diagnostic timestamp;
- baseline sealed lifecycle state, owner-version, state schema version, and
  sealed-through sequence when event kind is `baseline`;
- sealed materialized-write counter when event kind is `baseline`.

Within the same migration transaction, add the non-null
`materialized_write_counter` column to every runtime-state materialized row
with deterministic default `0`, backfill all pre-existing rows to `0`, and
install the database-level replay-tuple trigger/write-guard for INSERT and
replay-tuple-changing UPDATE before the migration commits. The INSERT guard
treats row creation as a replay-tuple change: it starts from the deterministic
default counter `0` and persists an after-counter of `1`; the UPDATE guard
advances the counter only when a replay-tuple member changes. No row may be
observable with a missing counter or without the guard. Migration compatibility
tests must verify the column, default, backfill, INSERT/UPDATE guard, and guard
survival through prior-version startup and rollback.

Add a durable database-level current-generation record:
`runtime_state_journal_meta.current_generation`, owned by the runtime-state
migration/startup module. Define a durable writer-epoch transition record
alongside it. The upgraded startup path computes its immutable writer epoch from
the journal schema/code-contract version and, under the exclusive side of the
named maintenance RW lock and one `BEGIN IMMEDIATE` transaction, performs an
atomic compare-and-set: it advances `current_generation` exactly once only
when that epoch is strictly newer than the durable epoch. If lock acquisition or the startup write transaction reaches its fixed timeout,
startup returns `LOCK_TIMEOUT` before opening or committing SQLite, enters
process-local `STARTUP_LOCKED` read-only state, writes no durable transition,
and serves no ordinary or event-only writes until a later successful startup
transition attempt in that process. While `STARTUP_LOCKED`, every ordinary or
event-only write entry returns mutation-side `WRITE_ABORT` without opening or
committing SQLite; Gate 4 asserts that reason code and the absence of a durable
transition or mode. It must not serve mutations under an uncleared transition.
A same-epoch restart never advances `current_generation`. A startup observing a durable epoch newer than the running writer acquires the
exclusive side of the named maintenance RW lock and uses one `BEGIN IMMEDIATE`
transaction to set the durable global `DOWNGRADE_UNSAFE` mode for the
database: all ordinary runtime-state writes across all profiles and entities,
including both event-only baseline and re-originating genesis writers, return
mutation-side `WRITE_ABORT`; only read-only verification may continue. If
that mode-set lock or transaction reaches either fixed timeout, no durable mode
is written and the process falls back to process-local `STARTUP_LOCKED` with
`LOCK_TIMEOUT`. The durable mode exits only when a writer at least as new as
the durable epoch completes the startup transition and atomically clears it.
An equal-epoch startup performs this durable mode-clear write under the
exclusive lock and `BEGIN IMMEDIATE` but does not advance
`current_generation`; only a strictly newer epoch advances the generation.
A startup lock/busy timeout remains process-local `STARTUP_LOCKED` read-only,
writes no durable mode or transition record, and remains read-only until a
later explicit startup transition attempt (supervisor restart/re-exec or
explicitly authorized maintenance retry) succeeds. Mutation entry does not
retry or clear it. The transition record and durable mode are written before
the upgraded writer serves mutations, so a strictly newer contract transition
is represented by a distinct epoch transition rather than by process restart
count. Delivery must record intervals spent in either write-blocked state. A rollback followed by roll-forward to the same
writer epoch does not advance `current_generation`; it is detected by the
materialized-write counter gap defined below. Gate 3 reads the
current-generation record, writer epoch, transition marker, and counter
metadata inside the same `BEGIN DEFERRED` snapshot as the event stream and
materialized row.

Add a materialized-row journal-writer generation marker and a monotonic
`materialized_write_counter`. The counter increments only for a committed mutation that changes a member of
the replay tuple. In the new writer transaction, the trigger increment/read-back
and journal emission share atomicity: if that transaction rolls back, neither is
committed. The database-level trigger still records a replay-tuple change made
by a prior or unaware writer that emits no journal event, so that rollback or
legacy path remains observable. Non-replay-tuple-only updates do not increment
it and remain outside the verifier's `CONSISTENT` claim. A database-level
trigger restricted to the replay-tuple columns, with a value-change predicate,
or an equivalent write guard must preserve this increment for any prior writer
that can mutate those columns. The increment mechanism must be the database-level trigger/write-guard
restricted to replay-tuple columns with a value-change predicate; an
application-code-only counter is prohibited because prior writers must remain
observable during rollback. The new writer must not increment the counter both
in application code and in the trigger/write-guard. The mutation/genesis
transaction records the counter before/after values in its event and updates
the row marker. A row whose marker is older than the current generation is `UNKNOWN` with
`GENERATION_MISMATCH`. If the marker is current but the materialized counter
is not equal to the latest journaled counter for that entity, it is `UNKNOWN`
with `WRITE_COUNTER_GAP`, never `DRIFT`. When both conditions hold,
`GENERATION_MISMATCH` has precedence over `WRITE_COUNTER_GAP`. Every event's before-counter must equal
the immediately preceding event's after-counter, or the baseline's sealed
counter. A marked genesis for an
entity with no current-epoch history is the other valid starting point: its
before-counter adopts the observed materialized counter and its after-counter
becomes the new epoch's starting counter. Any discontinuity after the selected
start is a permanent history gap and returns `UNKNOWN` with
`WRITE_COUNTER_GAP`. If the
counters agree, the history is complete, and the replay tuple differs, the
result may be `DRIFT` with `DRIFT_DETECTED`. This is the concrete
discriminator between an unjournaled state advance and genuine drift. A rollback-then-roll-forward cycle therefore keeps affected entities
`UNKNOWN`. A strictly newer writer epoch can re-originate on its first
journaled mutation as defined above; a same-epoch counter-gap entity has no
automatic or plain-mutation recovery path. Its next ordinary mutation is
rejected with `WRITE_ABORT`. The only in-ticket exit is a separately
authorized, out-of-band re-originating genesis operation, which records a new
origin marker using the observed materialized counter as its before-counter;
it is not callable by the gateway, scheduler, verifier, or automatic-repair
path. A same-epoch process restart does not advance `current_generation`;
an unjournaled write is detected by the counter gap.

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
checks this continuity. A baseline or an explicitly authorized re-originating `genesis` is an
event-only exception allowed to commit without an accompanying
materialized-state mutation.

The privileged baseline writer is an internal runtime-state maintenance
operation with explicit operator authorization, outside the gateway, scheduler,
and verifier surfaces. It acquires the exclusive side of the named
cross-process maintenance RW lock before opening its SQLite WAL transaction,
verifies the target row's generation marker equals the current-generation
record, validates the sealed tuple against the current materialized row and
current maximum sequence, and also requires the materialized counter to equal
the latest journaled after-counter (or prior baseline sealed counter). A
counter-gap entity cannot be sealed across by this baseline writer: it refuses
with mutation-side `WRITE_ABORT`, appends no baseline, and leaves the entity
for the separately authorized re-originating genesis operation. Otherwise it
appends the baseline event on the same connection and releases the lock after
commit or rollback.

The separately authorized re-originating genesis writer is the second
event-only exception. It is an internal maintenance operation outside the
gateway, scheduler, verifier, and automatic-repair paths; it requires explicit
operator authorization, acquires the exclusive side of the same maintenance
RW lock before its WAL transaction, and verifies the current materialized row
and observed counter. It appends exactly one `genesis` event with a new
manual-reorigin origin marker, before- and after-replay tuples equal to the
observed materialized replay tuple, and before- and after-counter equal to the
observed materialized counter. It changes no replay-tuple column, updates only
the journal-writer provenance marker, and releases the lock after commit or
rollback. The next ordinary mutation is then allowed from that genesis
starting point. Production gateway, scheduler,
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
delete
the table, counter column, or counter trigger/write-guard, and must not rewrite
history. Any prior-version path that drops or bypasses the counter mechanism
fails the compatibility preflight and leaves affected verification `UNKNOWN`.
Migration compatibility tests must assert this posture.

### Gate 2 - atomic emission at the CAS chokepoint

- Emit exactly one `mutation` or `genesis` event for every committed
  mutation that changes any member of the replay tuple, including
  owner-version-only changes with unchanged lifecycle state. The first
  journaled replay-tuple mutation for an entity with no current-epoch history
  (including a post-migration entity) is `genesis`; the first such mutation
  after a current-generation advance is also `genesis`, while subsequent
  mutations use `mutation`.
- Determine a no-op by comparing the requested before/after replay tuple
  before issuing any replay-tuple UPDATE. A true no-op issues no replay-tuple
  UPDATE, therefore does not fire the counter trigger and emits no event.
  Do not implement a write-then-revert shape.

- Ordinary mutation/genesis paths acquire the shared side of the named
  cross-process maintenance RW lock before opening the SQLite WAL transaction;
  use `BEGIN IMMEDIATE` or equivalent before sequence allocation; release the
  shared lock only after commit or rollback.
- Inside that write transaction, compare an existing materialized row's counter
  with the latest journaled after-counter (or baseline sealed counter). For a
  row-creating mutation, no prior materialized counter or journaled
  after-counter exists: the INSERT trigger/write-guard starts at counter `0`
  and persists `1`, the mutation emits `NEW_ENTITY_GENESIS` with
  before-counter `0` and after-counter `1`, and the in-transaction gap
  comparison is explicitly exempt. A prior or unaware writer's row INSERT
  receives the same guard treatment and is therefore counter-observable even
  when it emits no event. For a same-epoch ordinary mutation on an existing
  row, a mismatch is a persisted counter gap: abort with mutation-side
  `WRITE_ABORT` before changing replay state or emitting an event. A genesis
  for an existing entity with no current-epoch history
  (legacy/pre-journal, post-rotation, or first post-migration entity) is also
  exempt: it adopts the observed materialized counter as its before-counter
  and starts a new counter-continuity epoch. A strictly newer writer-epoch
  re-originating genesis is the other explicit exception; it starts a new
  selected replay epoch and supersedes the prior gap as defined by Gate 3.
  The separately authorized same-epoch re-originating genesis is the third
  explicit exception and uses the same observed-counter starting rule.
- Use the caller's existing SQLite WAL connection and transaction for mutation
  and genesis events, update the materialized generation marker, and record the
  current generation in the event.
- After all replay-tuple-changing updates in the transaction, read the
  trigger-produced materialized counter back from the row before inserting the
  event; record the counter observed before the first update and the actual
  persisted after-counter, never a predicted `+1`. Intermediate updates are
  not externally visible under the transaction. Gate 4 must exercise a
  multi-statement replay-tuple mutation and assert that the event after-counter
  equals the materialized counter at commit.
- Any mutation/genesis journal constraint or write failure aborts the complete
  enclosing materialized-state mutation; errors must not be swallowed.
- The CAS caller receives a typed bounded mutation refusal for
  `LOCK_TIMEOUT`, `KEY_UNAVAILABLE`, or `WRITE_ABORT` (covering
  continuity, downgrade-unsafe, journal-transaction, lock-acquisition, and
  key-custody failure); it performs no automatic retry, does not drop or
  corrupt the session loop, and leaves unaffected conversations/platform loops
  running. The gateway-facing error boundary returns the existing safe
  operation-failure response without entering prompt construction; Gate 4
  exercises this caller contract with a real CAS caller adapter.
- Prepare and validate the digest key before acquiring the shared maintenance
  lock. If it is unavailable, fail closed before opening SQLite or acquiring
  any lock, leave materialized state unchanged, emit no event, and surface the
  shared `KEY_UNAVAILABLE` reason. If a future implementation checks the key
  after acquiring the lock, it must release that lock on the pre-transaction
  abort; the primary contract is the pre-lock check.
  This is distinct from `WRITE_ABORT`, which covers a transaction or
  continuity refusal after the mutation path has entered its write contract.
- The privileged baseline writer and the explicitly authorized re-originating
  genesis writer are the only event-only exceptions and use the exclusive lock
  ordering defined in Gate 1.
- There is no sequence retry path under the write-lock-first discipline. An
  unexpected uniqueness collision or busy-timeout exhaustion aborts the whole
  transaction and surfaces a bounded failure; it is not retried inside the
  invalid transaction.
- Neither abort may create a committed mutation/genesis event without its state
  mutation or a replay-visible sequence gap. The two named event-only writers
  are explicit exceptions; baseline and re-origin sequence continuity is
  governed by Gate 1.
- Add a negative test proving lifecycle writes cannot bypass the mutation/genesis
  emission chokepoint; the test must not reject the explicitly named baseline
  writer or the explicitly authorized re-originating genesis writer.

### Gate 3 - read-only verifier

Implement a bounded verifier that:

- opens a read-only SQLite WAL connection and explicitly executes
  `BEGIN DEFERRED`; the first event-stream read establishes the snapshot,
  and the current-generation record, bounded event stream, and materialized row
  are all read after that snapshot establishment and before the transaction
  ends. Immutable-open is prohibited for this verifier;
- if snapshot establishment or the first snapshot read fails, returns
  `UNKNOWN` with `SNAPSHOT_FAILURE` before evaluating any
  snapshot-dependent key-check or digest-parameter comparison;
- after the snapshot is established, validates local key availability first,
  then validates the HMAC-SHA-256 algorithm/truncation parameters and digest
  identifier, and finally validates the key-check bound to that identifier.
  The pre-replay reason order is `KEY_UNAVAILABLE` ->
  `DIGEST_PARAMETER_MISMATCH` -> `KEY_CHECK_MISMATCH`;
- returns per-entity `CONSISTENT`, `DRIFT`, or `UNKNOWN`;
- treats `UNKNOWN` as absorbing;
- requires complete verified history for `DRIFT`;
- validates event kind, origin/genesis markers, journal generation/epoch
  continuity, baseline contents and baseline-to-next-event sequence continuity,
  duplicate or missing predecessors (`SEQUENCE_INVALID`), malformed history
  (`HISTORY_MALFORMED`), digest-parameter identity, schemas, the shared and
  verifier-side closed reason-code subsets, and terminal state;
- uses a code-level `REPLAY_EVENT_LIMIT = 10000` counted from the effective
  replay start point. Snapshot failure is the highest-precedence result and
  returns `SNAPSHOT_FAILURE` before any snapshot-dependent precondition can
  be evaluated. After the pre-replay reason order above passes, apply this
  structural verifier diagnostic precedence from highest to lowest:
  `UNSUPPORTED_VERSION`, `POST_TERMINAL_EVENT`,
  `HISTORY_MALFORMED`, `EMPTY_HISTORY`,
  `MATERIALIZED_STATE_ASYMMETRY`, `GENERATION_MISMATCH`,
  `LEGACY_ORIGIN_MISSING`, `SEQUENCE_INVALID`, `BASELINE_INVALID`,
  `WRITE_COUNTER_GAP`, `REPLAY_LIMIT_EXCEEDED`, `DRIFT_DETECTED`,
  `OK`. The first applicable
  code wins; `EMPTY_HISTORY` therefore wins over row-marker mismatch when
  the entity has zero journal rows, while `MATERIALIZED_STATE_ASYMMETRY`
  applies when one side exists and the other does not. Compute the start candidates as the highest valid baseline
  under the current digest identifier and the latest valid marked genesis under
  the current digest identifier, when present; select the later candidate by
  `entity_seq`. If neither candidate exists, return `UNKNOWN` with
  `LEGACY_ORIGIN_MISSING`. Baseline and genesis rows carry the origin epoch
  marker and origin-genesis sequence, so
  current-origin validity is checked from bounded row metadata without scanning
  an unbounded pre-start prefix. Events preceding the selected start are
  ignored after that O(1) metadata check, and counter continuity is enforced
  only from the selected start forward. If the entity has zero journal rows,
  the result is `UNKNOWN` with `EMPTY_HISTORY`; if journal rows exist but
  neither candidate is valid, the result is `UNKNOWN` with
  `LEGACY_ORIGIN_MISSING`. This permits a newer-writer-epoch genesis after a
  baseline to re-originate the entity.
- validates the selected baseline when one is the later replay-start candidate,
  including highest `sealed_through_seq` selection, same-sequence tuple
  conflicts, overlapping contradictory baselines, a baseline without a valid
  current origin epoch, a stale generation marker returning
  `GENERATION_MISMATCH`, a materialized-write counter not equal to the latest
  journaled counter returning `WRITE_COUNTER_GAP` only when the generation
  marker is current, or a materialized state advanced during a generation with
  no current-generation event;
- returns `UNKNOWN` with the corresponding closed reason code on empty history
  (`EMPTY_HISTORY`), snapshot failure, unsupported/newer versions, malformed
  history, unknown digest regime, key-check mismatch, verify-time key
  unavailability, exceeded bound, missing legacy origin, counter gap
  (`WRITE_COUNTER_GAP`), or a materialized-row/history asymmetry;
- treats a materialized row missing while history exists, or history existing
  without a materialized row, as `UNKNOWN`, never `DRIFT`;
- treats any event observed after a terminal lifecycle state as
  `POST_TERMINAL_EVENT` and returns `UNKNOWN`, never `DRIFT`;
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
- legacy populated rows with zero journal rows returning `UNKNOWN` with
  `EMPTY_HISTORY`;
- journal history present without a valid current-epoch origin returning
  `UNKNOWN` with `LEGACY_ORIGIN_MISSING`;
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
  detected by the materialized-write counter gap, global downgrade-unsafe
  startup blocking all profiles/entities and both event-only writers with
  mutation-side `WRITE_ABORT`, newer-epoch/equal-epoch roll-forward clearing
  that mode, startup lock/busy timeout returning `LOCK_TIMEOUT`, asserting no durable
  mode/transition was written, every ordinary or event-only write while
  `STARTUP_LOCKED` returning `WRITE_ABORT` without mutation retry, remaining
  read-only, and clearing process-local `STARTUP_LOCKED` only after a later
  explicit startup transition attempt succeeds, baseline refusal across a counter gap returning
  `WRITE_ABORT`, separately authorized same-epoch re-originating genesis
  restoring mutation ability, atomic concurrent startup advancing exactly once,
  delivery recording of the blocked interval, and concurrent generation
  advance during verify returning `CONSISTENT` or `UNKNOWN`, never false
  `DRIFT`;
- a newly created post-migration entity whose first journaled mutation is a
  marked genesis and reaches `CONSISTENT` after a valid follow-up mutation;
- first post-generation-advance mutation emitting a marked genesis, with a
  plain mutation alone unable to re-establish the epoch;
- one event per committed replay-tuple mutation;
- multi-statement replay-tuple mutation reads back the trigger-produced
  after-counter and matches the event at commit;
- true no-op without an event;
- owner-version-only mutation;
- mutation/genesis journal failure rolling back materialized state with
  mutation-side `WRITE_ABORT`;
- any same-epoch ordinary mutation attempted in downgrade-unsafe mode or
  against a persisted counter-gap entity returns mutation-side `WRITE_ABORT`;
  both event-only writers are also rejected while global downgrade-unsafe mode
  is set;
  a strictly newer writer-epoch or separately authorized same-epoch
  re-originating genesis is exempt and supersedes the gap at the later replay
  start;
- SQLite WAL, fixed `SQLITE_BUSY_TIMEOUT = 5s`, `BEGIN IMMEDIATE`, and
  read-only WAL preconditions;
- `LOCK_ACQUIRE_TIMEOUT = 5s`, `SQLITE_BUSY_TIMEOUT = 5s`,
  `LOCK_TIMEOUT` reason, stale-holder recovery, shared/exclusive
  cross-process RW-lock exclusion/release ordering, and pre-lock key
  unavailability without lock leakage;
- single-writer invariant and defensive abort; no sequence-collision retry is
  expected under write-lock-first discipline;
- baseline generation-marker mismatch returning `UNKNOWN` with
  `GENERATION_MISMATCH`;
- bypass-path rejection for mutation/genesis while permitting only the named
  baseline writer and the explicitly authorized re-originating genesis writer;
- caller contract for mutation-surface `LOCK_TIMEOUT`, `KEY_UNAVAILABLE`,
  and `WRITE_ABORT`: no automatic retry, each affected operation receives a
  bounded failure, and unaffected loops continue;
- keyed digest/profile isolation, identifier/key-check mismatch, digest
  rotation without bridging, verify-time key unavailability, and mutation-time
  key unavailability returning shared `KEY_UNAVAILABLE` without changing
  materialized state;
- duplicate or missing predecessors returning `UNKNOWN` with
  `SEQUENCE_INVALID`; malformed/non-contiguous/unsupported events returning
  `UNKNOWN` with `HISTORY_MALFORMED` or their more specific closed code;
- the same fixture with a materialized-write counter gap in either direction
  (`>` or `<`) returning `UNKNOWN` with `WRITE_COUNTER_GAP`, and a baseline
  attempt refused with `WRITE_ABORT`;
- the same fixture with an explicitly authorized out-of-band same-epoch
  re-originating genesis adopting the observed counter, followed by a valid
  mutation reaching `CONSISTENT`;
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
  selection, and every member of each surface's closed reason-code subset;
- verifier-performs-no-writes using a direct database-change assertion;
- materialized-row/history asymmetry returning `UNKNOWN`;
- post-terminal events returning `UNKNOWN` with
  `POST_TERMINAL_EVENT`;
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

## Implementation-plan review reconciliation v15

The v15 authenticated Claude Opus review returned `REVISE` with five bounded
plan-text corrections. The corrections are incorporated above:

1. Legacy, post-migration, and post-rotation genesis events explicitly adopt
   the observed materialized counter as their new continuity starting point.
2. Rollback compatibility protects the counter column and trigger/write-guard
   alongside the journal table.
3. A missing valid baseline/genesis candidate returns
   `LEGACY_ORIGIN_MISSING`.
4. Acceptance coverage now includes v14.
5. The duplicate v14 changelog entry was removed.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v16

The v16 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. Counter-gap entities have a bounded, explicitly authorized out-of-band
   same-epoch re-originating genesis remediation; ordinary mutation remains
   fail-closed.
2. The privileged baseline writer refuses to seal across a counter gap with
   `WRITE_ABORT`; it does not clear the gap.
3. Acceptance coverage now includes v15, and the counter increment mechanism is
   singular to prevent double increments.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v17

The v17 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. The authorized same-epoch re-originating genesis is a fully defined,
   exclusive-lock, event-only writer with equal observed before/after tuple and
   counter, and it is included in the allowed-writer/bypass contract.
2. Counter increment is forced to the database-level trigger/write-guard;
   application-only increment is prohibited for rollback observability.
3. Acceptance coverage now includes v16.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v18

The v18 authenticated Claude Opus review returned `REVISE` with four bounded
plan-text corrections. The corrections are incorporated above:

1. The authorized re-originating genesis remains an internal code-level
   maintenance invocation; unavailable invocation leaves affected entities
   write-blocked, while unaffected loops continue and the consequence is
   recorded.
2. `WRITE_ABORT` has a bounded caller contract with no automatic retry and no
   session-loop corruption.
3. Acceptance coverage now includes v17.
4. Migration initializes the non-null counter to zero and installs the
   replay-tuple trigger/write-guard in the same migration transaction.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v19

The v19 authenticated Claude Opus review returned `REVISE` with four bounded
plan-text corrections. The corrections are incorporated above:

1. Downgrade-unsafe mode is a global database write block with a newer-epoch
   roll-forward exit and delivery-recorded consequence.
2. Current-generation advancement uses the exclusive lock and atomic
   `BEGIN IMMEDIATE` compare-and-set, with a concurrent-startup test.
3. Digest-key availability is checked before shared-lock acquisition, with
   explicit no-leak fallback behavior.
4. Acceptance coverage now includes v18.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v20

The v20 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. Mutation/genesis records the database trigger's actual after-counter by
   in-transaction read-back, including multi-statement mutation coverage.
2. Acceptance coverage now includes v19.
3. Duplicate Gate 4 startup/downgrade tests are merged into one matrix entry.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v21

The v21 authenticated Claude Opus review returned `REVISE` with five bounded
plan-text corrections. The corrections are incorporated above:

1. Counter mismatch is bidirectional: either materialized counter greater than
   or less than the latest journaled counter returns `UNKNOWN` with
   `WRITE_COUNTER_GAP`.
2. Both event-only writers are blocked during global downgrade-unsafe mode.
3. The residual cross-reference bullet was removed from Gate 4.
4. Equal-epoch restart clearing downgrade-unsafe mode is included in the test
   matrix.
5. Acceptance coverage now includes v20.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v22

The v22 authenticated Claude Opus review returned `REVISE` with four bounded
plan-text corrections. The corrections are incorporated above:

1. Acceptance coverage now includes v21.
2. Gate 3 treats either counter inequality as `WRITE_COUNTER_GAP`.
3. The origin-marker value set is closed and enumerated.
4. Diagnostic precedence is deterministic: zero events maps to
   `EMPTY_HISTORY`; existing rows without a valid current origin map to
   `LEGACY_ORIGIN_MISSING`.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v23

The v23 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. Acceptance coverage now includes v22.
2. Stale generation marker has deterministic `GENERATION_MISMATCH`
   precedence; current-marker counter inequality maps to
   `WRITE_COUNTER_GAP`.
3. Startup lock or busy timeout fails closed with `LOCK_TIMEOUT`; the writer
   remains global write-blocked/read-only until a successful transition.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v24

The v24 authenticated Claude Opus review returned `REVISE` with six bounded
plan-text corrections. The corrections are incorporated above:

1. Acceptance coverage now includes v23.
2. The startup same-epoch non-advancement sentence is repaired.
3. `NEW_ENTITY_GENESIS` and deterministic origin-marker precedence are
   defined.
4. Total verifier reason-code precedence is defined across all diagnostic
   groups.
5. Duplicate Gate 4 entries are removed.
6. No-op determination occurs before any replay-tuple update.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v25

The v25 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. The frozen replay-limit reason code is consistently named
   `REPLAY_LIMIT_EXCEEDED`.
2. Acceptance coverage now includes v24.
3. Gate 2 and Gate 4 both permit the explicitly authorized re-originating
   genesis writer in the bypass contract; redundant counter-gap assertions
   are consolidated.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v26

The v26 authenticated Claude Opus review returned `REVISE` with four bounded
plan-text corrections. The corrections are incorporated above:

1. Acceptance coverage now includes v25.
2. `POST_TERMINAL_EVENT` is a distinct higher-precedence classification than
   `HISTORY_MALFORMED`.
3. Materialized-state asymmetry directions and `EMPTY_HISTORY` mapping are
   explicit.
4. Process-local `STARTUP_LOCKED` is separated from durable
   `DOWNGRADE_UNSAFE`, with distinct exit and delivery-recording behavior.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v27

The v27 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. `POST_TERMINAL_EVENT` is above `HISTORY_MALFORMED` in total precedence,
   and post-terminal Gate 4 coverage names the code.
2. The changelog index includes v26.
3. Acceptance coverage now includes v26.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v28

The v28 authenticated Claude Opus review returned `REVISE` with five bounded
plan-text corrections. The corrections are incorporated above:

1. The schema counter field is limited to replay-tuple-changing mutations.
2. Growth accounting includes authorized re-originating genesis rows.
3. Sequence and malformed-history conditions bind to explicit reason codes.
4. Equal-epoch startup durably clears downgrade mode without advancing the
   generation.
5. The redundant Gate 2 no-op bullet is removed.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v29

The v29 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. Durable `DOWNGRADE_UNSAFE` mode-setting uses the exclusive maintenance lock
   and one `BEGIN IMMEDIATE` transaction; timeout falls back to process-local
   `STARTUP_LOCKED` without writing durable mode.
2. `STARTUP_LOCKED` exits only on a later explicit startup transition attempt;
   mutation entry does not retry or clear it.
3. The changelog index includes v28 and acceptance coverage includes v28.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v30

The v30 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. `STARTUP_LOCKED` ordinary and event-only write entry has an explicit
   mutation-side `WRITE_ABORT` contract, with Gate 4 coverage.
2. The new-writer counter trigger/read-back and journal emission share
   transaction atomicity, while prior/unaware writers remain observable when
   they emit no event.
3. Gate 3 validates key/digest preconditions before replay; only then does the
   structural diagnostic precedence apply.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v31

The v31 authenticated Claude Opus review returned `REVISE` with three bounded
plan-text corrections. The corrections are incorporated above:

1. Snapshot establishment failure is the highest-precedence verifier result;
   snapshot-dependent digest/key checks are not attempted when it fails.
2. The pre-replay mutation-independent reason order is explicit:
   `KEY_UNAVAILABLE` -> `DIGEST_PARAMETER_MISMATCH` ->
   `KEY_CHECK_MISMATCH`, followed only after success by structural precedence.
3. Mutation-surface `LOCK_TIMEOUT`, `KEY_UNAVAILABLE`, and `WRITE_ABORT`
   share the bounded no-retry and unaffected-loop caller contract, with Gate 4
   coverage.

These remain plan-only corrections and do not authorize source edits, tests,
DGX changes, deployment, repair, or event-sourcing. The corrected plan must
return to the same authenticated Claude reviewer family and then to AGY on the
identical packet.

## Implementation-plan review reconciliation v32

The v32 authenticated Claude Opus review returned `REVISE` with two bounded
plan-text corrections. The corrections are incorporated above:

1. The named maintenance RW lock is mandatory in both single-writer and
   multi-writer branches; the branch-specific difference is the ordinary
   connection/coordination invariant, not lock applicability.
2. The INSERT trigger/write-guard behavior and `NEW_ENTITY_GENESIS`
   counter semantics are explicit: before-counter `0`, after-counter `1`,
   with a row-creation exemption from existing-row gap comparison and
   prior/unaware INSERT observability.

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
  mutation, plus one row per out-of-band baseline event and one row per
  explicitly authorized out-of-band re-originating genesis, with no automatic
  retention job; entities beyond the replay limit remain UNKNOWN in this ticket,
  and the 100,000-event/100 MiB per-profile threshold is a documented follow-up
  operational review, not an in-ticket operator gate;
- reconciliation v1 and v5-v32 items are incorporated into the normative
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
- v15: genesis continuity starting counters, rollback-preserved counter
  mechanism, LEGACY_ORIGIN_MISSING binding, and corrected coverage.
- v16: authorized same-epoch re-origin remediation, baseline gap refusal,
  and singular counter increment mechanism.
- v17: complete re-originating genesis event-only contract, exclusive-lock
  bypass allowance, and mandatory database-level counter guard.
- v18: bounded WRITE_ABORT caller behavior, explicit no-CLI operational
  consequence, and atomic counter migration initialization.
- v19: global downgrade-unsafe scope and exit, startup compare-and-set,
  pre-lock key check, and complete coverage.
- v20: trigger counter read-back, multi-statement mutation coverage, and
  deduplicated startup/downgrade tests.
- v21: bidirectional counter-gap detection, event-only writer blocking during
  downgrade-unsafe mode, and equal-epoch exit coverage.
- v22: origin-marker closed enum, bidirectional Gate 3 wording, deterministic
  EMPTY_HISTORY versus LEGACY_ORIGIN_MISSING precedence, and complete coverage.
- v23: generation-mismatch reason precedence, startup timeout fail-closed
  posture, and complete coverage.
- v24: startup sentence repair, NEW_ENTITY_GENESIS and marker precedence,
  total verifier reason precedence, Gate 4 deduplication, and pre-update
  no-op determination.
- v25: reason-code spelling alignment, complete bypass allowance, and
  consolidated counter-gap test coverage.
- v26: post-terminal classification separation, asymmetry-direction wording,
  and STARTUP_LOCKED versus DOWNGRADE_UNSAFE state separation.
- v27: precedence/index alignment and explicit post-terminal reason coverage.
- v28: counter-field scope, re-origin growth accounting,
  reason-code domain binding, equal-epoch durable mode clearing, and no-op
  test deduplication.
- v29: durable DOWNGRADE_UNSAFE mode-set lock/timeout,
  STARTUP_LOCKED exit trigger, and complete coverage.
- v30: STARTUP_LOCKED write abort contract, counter/journal transaction
  atomicity, and key/digest precondition ordering.
- v31: snapshot-failure precedence, deterministic pre-replay reason order,
  and bounded caller contracts for all mutation-surface refusal codes.
- v32: maintenance RW-lock applicability in both writer branches and explicit
  INSERT/NEW_ENTITY_GENESIS counter semantics.


## Non-goals

- No automatic drift repair.
- No full event-sourcing rewrite.
- No migration of legacy conversation/session storage.
- No Telegram, gateway, provider, UI, or prompt-loop changes.
- No new user-facing `HERMES_*` configuration variables.
- No cloud storage, inference, OCR, embedding, telemetry, or external service.
- No DGX migration or deployment as part of plan creation.
