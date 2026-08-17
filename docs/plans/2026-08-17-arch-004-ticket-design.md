---
title: "ARCH-004 ticket design: redaction and SQLite/WAL safeguards"
status: DESIGN_REVIEW_PASS
date: 2026-08-17
type: ticket-design
ticket: ARCH-004
target_repo: hermes-agent
---

# ARCH-004 ticket design

## Ticket objective

Harden the local runtime-state boundary against two failure classes that can
amplify degradation:

1. sensitive values crossing the audit, diagnostic, or retry boundary; and
2. SQLite/WAL contention, retry, and lock behavior that can leave writers
   waiting indefinitely or make state transitions ambiguous.

The ticket must preserve the materialized runtime-state serving boundary and
the ARCH-003 metadata-only journal/verifier contract. It is a reliability and
data-minimization ticket, not a new chat or Telegram feature.

## Current dependency and evidence

- ARCH-002 and ARCH-003 are merged and deployed on `main`.
- ARCH-003 provides metadata-only journal events, local key custody, a
  read-only verifier, writer-generation markers, and a named maintenance lock.
- The DGX gateway is currently healthy at the recorded verification point, but
  service health and Telegram user-visible delivery remain separate gates.
- `runtime_state/retry_config.py` currently owns SQLite's connection-level
  busy timeout; application-level contention retry is explicitly deferred to
  ARCH-004.

## In scope

### 1. Redaction contract

- Define the closed set of runtime-state fields that may be persisted or
  emitted as metadata.
- Ensure journal rows, retry diagnostics, and failure messages contain no
  credentials, authorization headers, message bodies, prompt text, raw
  business identifiers, or filesystem secrets.
- Keep keyed entity identity as a profile-scoped digest with non-secret
  parameter and key-check metadata only.
- Add deterministic tests that inject sentinel secret values and prove they do
  not appear in journal rows, diagnostic records, or bounded error output.
- Preserve actionable reason codes without exposing the rejected value.
- The positive metadata allowlist is: operation category, opaque profile/entity
  digest, attempt number, maximum attempts, elapsed milliseconds, UTC
  diagnostic timestamp, schema/journal versions, writer generation/epoch,
  materialized counter values, and closed reason code. Human-readable text may
  describe the code, but never interpolates rejected input.
- Keyed-digest tests use a test-only deterministic key-custody fixture and
  compare the resulting opaque digest with an independently computed expected
  digest inside the test process. The fixture key is never emitted in journal,
  diagnostics, logs, or review packets; production tests verify only the
  non-secret key-check and verifier result.
- The digest contract is inherited unchanged from ARCH-003: HMAC-SHA256 over
  the UTF-8 NUL-joined tuple `(profile_name, entity_category,
  business_key)`, lower-case hexadecimal truncated to 128 bits (32
  characters), identified by `hmac-sha256:v1:128`. The business key is the
  existing table key for `session_state`, `task_state`, `approval_state`, or
  `compression_state`; no other fields enter the digest. Tests use an explicit
  temporary key-custody fixture under the test temporary directory, never the
  production auth file or an environment variable.
- The digest reference test reimplements HMAC-SHA256 directly in the test
  module instead of calling the production `_digest` helper, asserts the
  expected 32-character lower-case hex format and determinism, and scans all
  captured journal/diagnostic output for both the fixture sentinel and fixture
  key bytes. The key remains in the test process/temp fixture only.

### 2. SQLite/WAL contention contract

- Define a finite application-level retry policy for transient SQLite
  contention: three total attempts, a 100ms base delay, exponential delays of
  100ms then 200ms, a 500ms delay cap, and uniform 0-50ms jitter. The policy's
  maximum application delay is therefore 450ms; each SQLite connection keeps
  the existing 5000ms busy timeout. No retry occurs for schema, key-custody,
  malformed-history, or non-transient transaction errors.
- Use the closed mutation-side terminal codes `LOCK_TIMEOUT`,
  `RETRY_EXHAUSTED`, and `WRITE_ABORT`. `LOCK_TIMEOUT` remains the named-lock
  acquisition failure; `RETRY_EXHAUSTED` means all three transient SQLite
  attempts were consumed; `WRITE_ABORT` covers rollback or other defensive
  aborts. No free-form terminal code is permitted.
- Keep the existing connection busy timeout and named maintenance lock as
  separate controls. The shared maintenance lock is the ordinary journal
  writer/read side; the exclusive maintenance lock is the startup/baseline
  side. Both use the existing lock name and fixed 5000ms acquisition timeout;
  no per-operation lock names are introduced and neither control becomes an
  unbounded retry loop.
- `BEGIN IMMEDIATE` is opened after the ordinary shared maintenance lock and
  before sequence allocation/callback execution, acquiring the SQLite writer
  reservation before a journal sequence can be chosen. WAL readers are not
  blocked; competing writers wait through the fixed busy timeout and then use
  the finite retry policy. The privileged startup/baseline path uses the
  exclusive maintenance lock before its own `BEGIN IMMEDIATE` transaction.
- The read-only verifier opens a `mode=ro`, `query_only` WAL connection and
  starts `BEGIN DEFERRED` to read the event stream and materialized row from
  one stable snapshot. It never upgrades that transaction or writes SQLite and
  does not need the named lock; WAL snapshot isolation is the concurrency
  boundary for verification.
- One operation acquires the named lock first, for at most 5000ms, then runs up
  to three `BEGIN IMMEDIATE` attempts under that lock. Each attempt may spend
  at most the 5000ms SQLite busy timeout; the two inter-attempt delays and
  jitter add at most 450ms. The stated upper bound for this mutation boundary
  is therefore 20.45s (5000ms lock acquisition + three 5000ms SQLite waits +
  450ms delay). A lock timeout never opens SQLite; busy exhaustion rolls back
  every attempt before releasing the lock.
- `LOCK_TIMEOUT` means the named lock was not acquired within 5000ms and the
  operation did not open SQLite. `RETRY_EXHAUSTED` means all three SQLite
  attempts returned transient busy/locked conditions and no commit occurred.
  `WRITE_ABORT` means any non-transient mutation failure or defensive rollback
  after SQLite opened. The read-only verifier never returns these mutation
  codes; it keeps its existing verifier-side codes.
- Error categorization is closed: an SQLite exception is retryable only when
  its native code is `SQLITE_BUSY` or `SQLITE_LOCKED` (with the normalized
  `database is locked`/`database table is locked` message fallback for Python
  versions without `sqlite_errorcode`). Missing tables, malformed databases,
  read-only/schema errors, integrity errors, key-custody errors, and all other
  exceptions are non-transient and map to their existing preflight/verifier
  result or mutation-side `WRITE_ABORT`; they are never retried.
- The same lock file path is used for ordinary and privileged paths. On POSIX,
  shared ordinary locks use `flock(LOCK_SH)` and startup/baseline uses
  `flock(LOCK_EX)`; on Windows the existing `msvcrt` byte-range lock serializes
  both modes, so the safety contract is serialization rather than shared-reader
  concurrency. No SQLite PRAGMA changes the OS lock mode.
- Preserve `BEGIN IMMEDIATE` write-lock-first behavior for journaled mutations
  and the `BEGIN DEFERRED` read-only snapshot for verification.
- Prove that lock timeout, SQLite busy exhaustion, rollback, and process death
  release behavior leave no partial journal/materialized mutation.
- Exercise at least two real processes against a temporary WAL database; unit
  mocks alone do not close this gate.
- The process test uses real `multiprocessing.Process` children and a ready
  event, not mocked locks or sleeps as the correctness signal. One child holds
  the named lock/SQLite transaction; the parent terminates that child after
  readiness; a second child then proves bounded recovery or the closed
  terminal code. Every child is joined with a fixed deadline, with a bounded
  terminate fallback and unique temporary database/lock paths cleaned in a
  `finally` block. The test asserts outcome and upper-bound completion, not a
  fragile exact timing.

### 3. Compatibility and operational boundary

- Preserve ARCH-001 schema compatibility and ARCH-003 journal semantics.
- Keep rollback to the prior ARCH-003 immutable release possible without
  deleting the journal or losing materialized state. Also run a clean ARCH-002
  compatibility fixture; no compatibility claim is made for versions older
  than ARCH-002 without a separate ticket.
- Compatibility testing is in scope for this ticket: prior ARCH-003 startup,
  read-only verification, rollback, and materialized-state preservation, plus
  a clean ARCH-002 fixture. The ticket does not promise that an older writer
  can perform new ARCH-004 mutations; it proves safe startup/rollback and
  explicit write-blocking where required.
- The clean ARCH-002 fixture is deterministic schema version 1 with the ARCH-001
  migration history, materialized rows populated by fixed test data, and no
  ARCH-003 journal/meta tables, marker columns, or triggers. The fixture is
  opened by the ARCH-004 migration path, which must add the journal contract
  without losing those rows. The prior ARCH-003 fixture is the corresponding
  post-journal immutable state used for startup, read-only verification,
  rollback, and write-blocking checks.
- ARCH-004 uses a strictly newer internal writer epoch in the existing durable
  `runtime_state_journal_meta` contract. An ARCH-003 writer observing that newer
  epoch sets the existing downgrade-unsafe mode, and its mutation entry returns
  `WRITE_ABORT` before opening SQLite. No lock-name change is used to gate old
  writers.
- Keep configuration in `config.yaml`; do not add user-facing non-secret
  `HERMES_*` environment variables.
- Keep the implementation local-only and metadata-only.
- Record bounded write-block intervals and reason codes without recording
  content or secrets.

## Explicit non-goals

- No Telegram adapter, polling, outbound delivery, or user-session behavior
  changes.
- No gateway prompt/tool-schema changes and no prompt-cache invalidation.
- No automatic state repair, event-sourcing migration, or broad replay
  redesign.
- No new cloud inference, OCR, telemetry, storage, or external service.
- No schema migration or deployment before the ticket's independent design and
  implementation review gates pass.

## Planned implementation surfaces

Confirm these against the merged source during implementation preflight; do
not edit them merely because they are listed here:

- `runtime_state/retry_config.py` — bounded application retry policy;
- `runtime_state/migrations.py` and `runtime_state/schema.py` — WAL,
  compatibility, and migration invariants;
- `runtime_state/locking.py` and `runtime_state/journal.py` — lock/retry
  interaction and atomic journal boundary;
- `runtime_state/verifier.py` — read-only failure classification;
- focused runtime-state tests, including multi-process WAL and redaction
  coverage.

Telegram, gateway conversation handling, provider adapters, prompt
construction, legacy `state.db`, and unrelated dirty-worktree files remain
out of scope.

## Acceptance gates

1. **Identity/preflight:** confirm repository, merged ARCH-003 baseline,
   writer matrix, database files, rollback requirement, and current failure
   boundary.
2. **Design review:** one authenticated Claude reviewer and one authenticated
   AGY reviewer inspect the identical metadata-only packet and return explicit
   `PASS`, `REVISE`, or `BLOCKED`.
3. **Redaction tests:** sentinel secrets are absent from every in-scope
   journal/diagnostic/error surface; reason codes remain observable.
4. **Contention tests:** real multi-process WAL tests cover success, bounded
   retry exhaustion, lock timeout, rollback, and recovery after holder death.
5. **Compatibility tests:** prior-version startup, rollback, journal
   read-only verification, and materialized-state preservation are proven.
6. **Implementation review:** the same correction set is independently
   reviewed after implementation; unresolved review findings block merge.
7. **CI/merge/deploy:** latest-head CI, immutable release identity, bounded
   DGX deployment local-state health retry, rollback copy, and cleanup are
   separate delivery gates. The DGX local-state gate is owned by the
   deployment operator after merge and checks only release-marker/hash match,
   effective service working directory, active/running state, exit status,
   restart count in the bounded window, and rollback-copy presence. It is
   advisory to Telegram delivery and can never close the Telegram gate.

## Design-review packet

The reviewers receive only this metadata summary and the acceptance gates:

- ticket: ARCH-004;
- objective: redaction plus bounded SQLite/WAL contention safeguards;
- constraints: local-only, metadata-only, prompt-cache stable, preserve
  materialized serving state and ARCH-003 compatibility;
- non-goals: Telegram behavior, automatic repair, event-sourcing, cloud
  services, and unrelated worktree files;
- required evidence: sentinel redaction tests, real multi-process WAL tests,
  rollback/compatibility tests, and explicit bounded terminal reasons;
- result vocabulary: `PASS`, `REVISE`, `BLOCKED`.

No source text, evidence text, absolute path, secret, token, prompt, or
generated artifact is part of the review packet.

## Review record and current gate

- Final bounded packet SHA-256: `224b81eaaa19a236f8e886536c35c4d89e2a508867b8e7135c711411b57937f7`.
- Authenticated DGX `.hermes` Claude Haiku reviewed that exact packet and
  returned `PASS` with an empty correction set after three correction rounds.
- Authenticated DGX `.hermes` AGY `1.1.13` reviewed that exact packet and
  returned `PASS` with correction set `None` after the marker smoke confirmed
  the executable was authenticated and reachable.
- Claude + AGY design consensus is **PASS**. The ticket is ready for a
  separately authorized implementation plan/review; no implementation,
  migration, commit, merge, deploy, or DGX modification is authorized by this
  document.
