---
title: "ARCH-004 implementation plan: redaction and SQLite/WAL safeguards"
status: IMPLEMENTATION_REVIEW_PASS
date: 2026-08-17
type: implementation-plan
ticket: ARCH-004
design_commit: a509c5bd1
target_repo: hermes-agent
---

# ARCH-004 implementation plan

## Gate state

- Ticket design: `DESIGN_REVIEW_PASS` at `a509c5bd1`.
- This document is the implementation plan and is not implementation
  authorization.
- Source edits, schema migration, commit, merge, deployment, and DGX changes
  remain closed until this plan receives independent implementation review and
  the owner separately authorizes implementation.
- Implementation review result vocabulary is closed: `PASS`, `REVISE`, or
  `BLOCKED`.

## Objective and preserved contracts

Harden the local runtime-state mutation boundary against sensitive-value
leakage and bounded SQLite/WAL contention without changing the gateway,
Telegram, prompt, provider, or materialized-state serving boundary.

The implementation must preserve:

- ARCH-001 schema version 1 and its migration-history checksum contract.
- ARCH-002 materialized runtime-state rows and atomic state-transition
  behavior.
- ARCH-003 metadata-only journal, HMAC identity digest, local key custody,
  generation markers, downgrade barrier, named maintenance lock, and read-only
  verifier.
- Per-conversation prompt-cache stability and the local-only/private-first
  boundary.
- Separate service-health, inbound-polling, Telegram-delivery, CI, merge,
  deployment, and rollback gates.

## Gate 0 — implementation preflight and source inventory

Before editing source, record a read-only inventory against the merged ARCH-003
baseline:

1. Verify repository root, remote, branch, commit, clean isolated worktree,
   and the ARCH-003 design/implementation dependencies.
2. Re-read the exact runtime-state surfaces and focused tests listed below.
3. Build a writer matrix covering initial migration, startup transition,
   journaled materialized mutation, read-only verification, and prior-writer
   downgrade behavior.
4. Confirm the database path and existing lock path are profile-scoped and
   that no Telegram/gateway call path is part of this ticket.
5. Confirm the ARCH-003 digest contract from the implementation and tests;
   do not invent a second identity format.

The preflight is complete only when every planned edit has an existing owner
surface and every new behavior has a named focused test. If the merged source
has materially diverged, stop and revise this plan before touching code.

## Planned file surfaces and boundaries

### Runtime-state implementation surfaces

- `runtime_state/retry_config.py`: add the closed, bounded application retry
  policy and transient-error classifier. Reuse the existing connection-level
  busy timeout. Do not create a user-facing environment variable or an
  unbounded retry helper.
- `runtime_state/locking.py`: retain the existing shared/exclusive named lock,
  fixed five-second bound, and platform-specific kernel/file locking. Only
  make the smallest change needed to expose the existing bounded timeout or
  to preserve its terminal classification; do not add per-operation lock
  names.
- `runtime_state/journal.py`: wrap the existing `BEGIN IMMEDIATE` journaled
  mutation boundary with the retry helper, preserve sequence allocation and
  rollback semantics, and add metadata-only bounded diagnostic capture. Reuse
  `DIGEST_PARAMETER_ID`, the existing HMAC tuple, and current table business
  keys. Do not redesign the journal or add content fields.
- `runtime_state/migrations.py`: preserve read-only preflight, WAL setup,
  exclusive startup/baseline locking, and atomic migration behavior. Add only
  the ARCH-004 writer-epoch/schema compatibility barrier required by the
  existing `runtime_state_journal_meta` contract.
- `runtime_state/schema.py`: extend the existing journal metadata schema only
  if the preflight proves an additive schema declaration is required. Keep
  ARCH-001 schema version and migration history distinct from journal metadata;
  never rewrite existing rows or drop tables.
- `runtime_state/verifier.py`: keep `mode=ro`, `query_only`, WAL, and
  `BEGIN DEFERRED` stable-snapshot behavior. It must remain read-only and must
  not use the mutation retry path or mutation terminal codes.
- `runtime_state/__init__.py`: export only stable public symbols needed by
  focused tests or existing callers; do not broaden core/gateway surface.

### Focused test surfaces

- `tests/runtime_state/test_runtime_state.py`: deterministic retry bounds,
  error classification, schema/preflight compatibility, rollback, and
  preserved WAL/busy-timeout contracts.
- `tests/runtime_state/test_arch003_journal.py`: journal sequence,
  generation/epoch, digest, metadata-only redaction, terminal codes, and
  read-only verifier regression coverage.
- `tests/runtime_state/test_arch004_contention.py`: new real
  `multiprocessing.Process` WAL/lock tests, with readiness events, bounded
  joins, terminate fallback, unique temporary paths, and `finally` cleanup.
- `tests/runtime_state/test_arch004_redaction.py`: sentinel-value injection
  tests for journal rows, diagnostic captures, and bounded error output;
  independently recompute the expected HMAC digest in the test module.
- `tests/gateway/test_runtime_state_integration.py`: only a regression if the
  public runtime-state boundary changes; no new Telegram or gateway behavior.

Do not modify Telegram adapters, gateway conversation/session handling,
provider configuration, prompt construction, legacy `state.db`, generated
exports, evidence packets, or unrelated dirty-worktree files.

## Gate 1 — retry taxonomy and redaction boundary

Implement the smallest reusable policy on the existing runtime-state path:

1. Define constants/configuration for three total SQLite mutation attempts,
   100 ms base delay, 200 ms second delay, 500 ms cap, and uniform 0–50 ms
   jitter. The application-delay maximum is 450 ms; connection busy timeout
   remains 5000 ms.
2. Classify an exception as retryable only for native `SQLITE_BUSY` or
   `SQLITE_LOCKED`, with the normalized locked-message fallback where Python
   does not expose `sqlite_errorcode`. The fallback is limited to the exact
   normalized phrases `database is locked` and `database table is locked`; it
   must not classify schema, integrity, key-custody, malformed-history,
   read-only, or other messages as transient.
3. Preserve the closed mutation codes: `LOCK_TIMEOUT`,
   `RETRY_EXHAUSTED`, and `WRITE_ABORT`. A named-lock failure returns
   `LOCK_TIMEOUT` before SQLite is opened; three transient SQLite failures
   return `RETRY_EXHAUSTED`; any non-transient or defensive rollback failure
   returns `WRITE_ABORT`.
4. Define the output allowlist before writing diagnostics: operation category,
   opaque profile/entity digest, attempt/max attempts, elapsed milliseconds,
   UTC timestamp, schema/journal versions, writer generation/epoch,
   materialized counters, and the closed reason code.
5. Enforce the negative boundary in code and tests: no credentials,
   authorization headers, message bodies, prompt text, raw business keys,
   filesystem secrets, or fixture key bytes may enter journal rows,
   diagnostics, logs, or error messages. During retry handling, capture only
   allowlisted metadata; never log or persist the raw exception message,
   repr, traceback, SQL, or exception object.
6. Use the existing HMAC-SHA256 NUL-joined identity contract, lower-case
   32-character digest, and `DIGEST_PARAMETER_ID` unchanged. The production
   key is never emitted; tests use only temporary key custody.

The retry helper must be side-effect-neutral between attempts. Each failed
transaction is rolled back before delay/retry. The callback must not be
replayed after a committed mutation; no retry is allowed after a non-transient
failure or an ambiguous commit result.

## Gate 2 — SQLite/WAL contention and lock semantics

1. Keep the existing maintenance lock path (`db_path + ".maintenance.lock"`),
   ordinary shared lock for journaled mutations, and
   exclusive lock for startup/baseline. Preserve POSIX `flock` semantics and
   Windows `msvcrt` serialization.
2. Acquire the named lock first for at most 5000 ms. Only after it is held,
   execute up to three `BEGIN IMMEDIATE` attempts. Each attempt keeps the
   existing 5000 ms SQLite busy timeout. Never start an SQLite transaction when
   named-lock acquisition has already timed out.
3. Keep the journal sequence allocation, materialized callback, journal append,
   and commit in one atomic attempt. Roll back every failed attempt and never
   leave a partial materialized row or journal row. A transient failure at
   `BEGIN IMMEDIATE` or during the callback receives an explicit
   `conn.rollback()` before delay or the next attempt; the loop never relies
   on an implicit rollback and never retries an ambiguous commit result.
4. Keep the maximum mutation boundary explicit: 5000 ms named-lock wait +
   three 5000 ms SQLite waits + 450 ms inter-attempt delay = 20.45 seconds
   upper bound, excluding small local setup/cleanup overhead. Tests assert a
   bounded upper limit with tolerance rather than exact sleep timing.
5. Preserve verifier behavior: read-only URI, `query_only`, WAL, and one
   `BEGIN DEFERRED` snapshot without the named lock. Verifier failures remain
   verifier classifications, never mutation `RETRY_EXHAUSTED`.
6. Prove process-death recovery with real processes. The holder must signal
   readiness after acquiring the named lock/transaction; the test terminates
   it, joins with a deadline, and proves the lock is released and the next
   operation either completes safely or returns a closed terminal reason.

## Gate 3 — compatibility, epoch barrier, migration, and rollback

- Open a clean ARCH-002 fixture: schema version 1, ARCH-001 migration history,
  fixed materialized rows, and no ARCH-003 journal/meta tables, marker columns,
  or triggers. Prove rows survive the additive ARCH-003/ARCH-004 setup.
- Open an immutable ARCH-003 fixture and prove startup, read-only verification,
  journal continuity, and materialized-state preservation. Pin the fixture to
  merged ARCH-003 baseline `e8cdfd1e65191b68423afd7e12248d3c6e728e00`
  (implementation commit `9b777c0b1`) so its journal/meta/materialized schema
  is reproducible.
- Use a strictly newer ARCH-004 internal writer epoch in the existing durable
  `runtime_state_journal_meta` contract. An older ARCH-003 writer must set the
  existing downgrade-unsafe state and return `WRITE_ABORT` before opening a
  mutation transaction. The order is named-lock acquisition (bounded five
  seconds) -> epoch/downgrade check -> `BEGIN IMMEDIATE`; the epoch failure is
  a closed, redacted error and occurs before any SQLite transaction opens. Do
  not introduce a second lock name or destructive downgrade migration.
- Verify an ARCH-003 rollback copy can start/read/verify without deleting the
  journal or materialized rows. New ARCH-004-only writes are not promised for
  the older writer and must be explicitly blocked.
- Any migration failure must roll back atomically. No destructive repair,
  event-sourcing conversion, auto-replay, or user-facing config migration is
  part of ARCH-004.

## Gate 4 — focused implementation verification

Run in this order and record each result separately:

1. Deterministic unit/regression tests for policy bounds, classifier,
   redaction allowlist, digest reference, terminal codes, rollback, and
   ARCH-003 journal/verifier invariants.
2. Real multiprocess tests for WAL writer contention, lock timeout, retry
   exhaustion, rollback, and process-death recovery. No mock lock or exact
   sleep assertion closes this gate.
3. Compatibility fixtures for clean ARCH-002, prior ARCH-003, epoch barrier,
   read-only verification, and materialized-state preservation. The verifier
   path is exercised independently of the mutation retry helper: it opens only
   the read-only, query-only, `BEGIN DEFERRED` snapshot, never acquires the
   named lock, and never calls a retry-wrapped mutation.
4. Focused gateway integration regression only if the public boundary changed.
5. Full repository test/format/static checks appropriate to the touched files.

Evidence must label synthetic/deterministic, multiprocess, CI, DGX runtime,
and Telegram delivery separately. Local tests cannot be reported as CI,
runtime, or delivery PASS.

## Gate 5 — implementation review and delivery gates

After source implementation and focused tests, create a new metadata-only
implementation-review packet containing the exact correction set, planned and
changed repo-relative files, test matrix, compatibility/rollback contract, and
result vocabulary. Send the identical packet to exactly one authenticated
Claude reviewer and one authenticated AGY reviewer. A `REVISE` finding is
fixed and returned to the same family; `BLOCKED` is not a PASS.

Only after both reviewers PASS and the correction set is reconciled may the
following independent gates proceed: latest-head CI, commit/push, merge,
immutable release build, DGX deployment with rollback copy and bounded health
retry, service health, inbound polling, and Telegram user-visible delivery.
No DGX source, service, database, or configuration mutation is permitted while
this implementation plan is only under review.

## Rollback and failure handling

- Keep the prior ARCH-003 immutable release and rollback metadata intact.
- If migration or startup fails, fail closed and leave the prior database
  recoverable; do not delete journal/meta tables or rewrite materialized rows.
- If a mutation exhausts retries, return `RETRY_EXHAUSTED` after complete
  rollback. If the named lock times out, return `LOCK_TIMEOUT` without SQLite
  mutation. All other defensive failures return `WRITE_ABORT`.
- If implementation review cannot obtain the required authenticated Claude and
  AGY evidence, mark the plan `IMPLEMENTATION_REVIEW_BLOCKED` and do not
  implement, merge, or deploy.

## Review close condition

This plan may move to `IMPLEMENTATION_REVIEW_PASS` only when the identical
metadata-only packet has an independently verified `PASS` from one
authenticated DGX `.hermes` Claude reviewer and one dedicated authenticated
DGX `.hermes` AGY reviewer, with no unresolved correction set. That condition
is now satisfied; the next gate is separately authorized source
implementation, not automatic implementation or deployment.

## Implementation-plan review record

- Final metadata-only packet SHA-256:
  `5c833b287e7f8437f683300e1eeeb236356f585dd34c8a9cfadab02769eabd59`.
- Authenticated DGX `.hermes` Claude reviewer: host `55-0940189-03`, working
  directory `.hermes`, executable
  `/home/cwliao/.nvm/versions/node/v20.20.2/bin/claude`, model `haiku`;
  returned `PASS` with no correction set.
- Dedicated authenticated DGX `.hermes` AGY reviewer: host
  `55-0940189-03`, user `cwliao`, working directory `.hermes`, executable
  `/home/cwliao/.local/bin/agy`, version `1.1.13`; returned `PASS` with no
  correction set.
- The Claude review initially returned six concrete `REVISE` items; they were
  reconciled into this plan and the packet before the final Claude `PASS`.
- Implementation-plan cross-review consensus is `PASS`. This authorizes
  planning closure only; it does not authorize source implementation, schema
  migration, commit, merge, deployment, or DGX mutation.
