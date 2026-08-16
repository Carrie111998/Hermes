---
title: "ARCH-003: runtime-state audit and replay verification"
status: DESIGN_REVIEW_REVISE_V3_PENDING
date: 2026-08-16
type: architecture
ticket: ARCH-003
target_repo: hermes-agent
---

# ARCH-003: runtime-state audit and replay verification

## Status

DESIGN_REVIEW_REVISE_V3_PENDING

This ticket is a design gate only. No source implementation, runtime-state
migration, DGX database inspection, deployment, or automatic repair is
authorized until the design receives the required independent review
consensus and a separate implementation gate is opened.

## Context

ARCH-002 stabilized the profile-scoped runtime-state transition contract at
the owner/version CAS boundary. The current runtime-state database has
materialized lifecycle tables and migration metadata, but it does not have an
append-only audit journal or a read-only verifier that can reconstruct and
compare state history.

Existing replay helpers in provider/conversation code are not a runtime-state
audit mechanism. ARCH-003 must add the smallest durable history boundary
needed for runtime-state consistency without replacing the materialized tables,
the legacy session database, or the gateway message loop.

## Goal

Make runtime-state changes traceable and verifiable:

1. Record successful runtime-state mutations as ordered, metadata-only audit
   events in the same SQLite transaction as the corresponding materialized
   state mutation.
2. Replay those events in memory without side effects.
3. Compare replayed expectations with the current materialized rows and return
   explicit `CONSISTENT`, `DRIFT`, or `UNKNOWN` results.
4. Stop safely on ambiguity instead of guessing or repairing state.

## Proposed design

### 1. Append-only audit journal

Add a runtime-state-owned journal table through a versioned migration. Each
event records only metadata required for verification, such as:

- event identity and deterministic ordering;
- profile/entity category and a non-reversible entity-key digest;
- operation category (`create`, `claim`, `transition`, `release`);
- previous/next lifecycle state when applicable;
- owner-version before/after;
- schema version and timestamp;
- bounded result/reason code.

The journal must not store message bodies, prompts, tool arguments, credentials,
tokens, filesystem paths, provider payloads, or arbitrary user data. Raw owner
tokens and business keys are not journal fields.

Successful CAS mutations and their audit event commit atomically. A failed,
stale, invalid, or same-state idempotent retry produces no state-changing
event. The journal is append-only from the runtime-state API; repair or
deletion is out of scope.

### 2. Read-only replay verifier

Provide a runtime-state-local verifier that:

- reads a bounded profile/entity event stream;
- reconstructs expected lifecycle, owner-version, and schema progression in
  memory;
- compares the result with the current materialized row;
- emits structured metadata-only results:
  `CONSISTENT`, `DRIFT`, or `UNKNOWN`;
- reports the first bounded mismatch category without exposing raw keys or
  user content;
- never writes the database, claims ownership, calls external services, or
  replays gateway/provider side effects.

Replay ordering, duplicate events, missing predecessors, unsupported schema
versions, and incomplete terminal state must stop with `UNKNOWN` rather than
being silently normalized.

### 3. Boundary and recovery contract

ARCH-003 does not decide that a drift is safe to repair. A verifier result is
evidence for a later, separately reviewed repair ticket. Any future repair
must require an explicit policy, owner authorization, bounded mutation, and
rollback evidence.

Full event-sourcing is also out of scope. Materialized rows remain the serving
boundary, and the audit journal is a verification record rather than a
replacement command log.

## Scope

- runtime-state schema migration for the journal;
- atomic event emission at the existing runtime-state mutation boundary;
- a read-only replay/drift verifier;
- metadata-only result types and redacted diagnostics;
- profile isolation and entity-key digest rules;
- hermetic tests for atomicity, ordering, duplicates, missing events, schema
  compatibility, drift, unknown state, terminal-state behavior, and cleanup;
- documentation of the audit/replay contract and later repair boundary.

## Non-goals

- No automatic repair or state mutation by the verifier.
- No full event-sourcing rewrite.
- No replacement of materialized runtime-state tables.
- No migration or replacement of legacy `state.db`/conversation sessions.
- No Telegram, platform, provider, model, UI, or gateway message-loop changes.
- No replay of external side effects, tool calls, messages, or provider requests.
- No cloud storage, telemetry, external inference, or new user-facing
  `HERMES_*` configuration variables.
- No DGX runtime database inspection, migration, restart, or deployment during
  design/review.

## Required design review questions

1. Is the journal payload genuinely metadata-only while still sufficient for
   deterministic consistency checks?
2. Does same-transaction emission cover every successful runtime-state mutation
   path without creating a second write boundary?
3. Are entity-key digests, profile scope, ordering, and schema versioning
   sufficient to prevent cross-profile replay or false consistency?
4. Does replay fail closed on duplicate, missing, malformed, or unsupported
   history rather than guessing?
5. Can the journal grow, be retained, and be compacted safely without turning
   ARCH-003 into full event-sourcing?
6. Are `DRIFT` and `UNKNOWN` operationally distinct and safe inputs for a
   future repair ticket?
7. Does the design preserve prompt-cache stability and avoid changing the
   gateway conversation loop?

## Acceptance gates

1. The bounded design packet is reviewed by exactly one authenticated Claude
   reviewer and one authenticated AGY reviewer using identical metadata-only
   content.
2. Each reviewer returns an explicit `PASS`, `REVISE`, or `BLOCKED` with
   scope and reasons. Missing authentication or missing verdict is not PASS.
3. Any `REVISE` result is reconciled into one correction set and returned to
   the same reviewer family before implementation.
4. After design PASS, implementation must separately pass focused hermetic
   tests, relevant CI, independent implementation review, merge, deployment,
   runtime health, and rollback gates.
5. No future repair or event-sourcing ticket may be inferred as approved from
   this design.

## Review packet boundary

The reviewer packet contains only the ticket key, goal, named repository
surfaces, proposed event categories, result states, scope/non-goals, design
questions, and expected verdict schema.

It must not contain source text, full event samples containing identifiers,
PDF/DOCX content, secrets, tokens, absolute paths, prompts, message bodies,
generated evidence, or reviewer instructions that authorize implementation.

## Design review reconciliation v1

Status: DESIGN_REVIEW_REVISE_PENDING

The identical metadata-only packet (SHA-256
`cdda4fee9b65e35edcd045adf689a86226029263df0da6513491769a90249946`) received:

- authenticated Claude Opus: `REVISE`;
- authenticated AGY: `PASS`.

The Claude correction set is bounded to this design and does not authorize
implementation:

1. Specify a keyed entity-key digest over profile scope plus entity key, with
   runtime-state-owned key material never journaled; digests from different
   profiles are non-comparable.
2. Specify a per-profile/entity monotonic sequence allocated in the same
   transaction, with uniqueness; timestamps are diagnostic only.
3. Require one runtime-state CAS chokepoint for all mutation paths, sharing the
   caller's connection and transaction, with a bypass-path hermetic test.
4. Define an append-only sealed baseline/watermark event for verified compaction;
   missing predecessors without a baseline return `UNKNOWN`.
5. Define result algebra: complete verified history is required for `DRIFT`;
   `UNKNOWN` is absorbing; unknown/newer journal versions and open-ended
   reason codes return `UNKNOWN`.
6. Define a concrete replay bound; exceeding it returns `UNKNOWN`.
7. State that retention, replay bounds, and compaction policy are internal
   runtime-state constants, not user-facing `HERMES_*` variables.
8. State that verification is out-of-band and never called from the gateway
   conversation or prompt-construction path.

These corrections must be re-reviewed by the same Claude family before any
implementation gate. AGY's PASS applies only to the pre-correction packet and
does not waive the Claude re-review. No source, migration, test, DGX runtime,
deployment, or repair action is authorized by this reconciliation.

## Design review reconciliation v2

The authenticated Claude re-review of reconciliation v1 returned `REVISE`
with four bounded residual corrections:

1. Add a non-secret digest-parameter identifier covering key generation and
   digest algorithm/version. A mismatch or unrecognized identifier returns
   `UNKNOWN`; define rotation behavior and require a sealed baseline for any
   comparable post-rotation segment.
2. Journal emission failure, constraint violation, or rejection aborts the
   enclosing runtime-state transaction; errors cannot be swallowed. Add this
   to atomicity tests.
3. Define sealed baseline contents as lifecycle state, owner-version, and the
   sealed sequence position. Require first post-baseline sequence continuity;
   malformed, duplicated, non-contiguous, or unknown-digest baselines return
   `UNKNOWN`.
4. Define concurrent sequence allocation behavior: loser retries with a fresh
   allocation inside the same transaction or aborts, with neither path
   emitting without a committed mutation or leaving a replay-visible gap.

These corrections remain design-only and must be re-reviewed by the same
authenticated Claude family, then by AGY on the final corrected packet. No
implementation, migration, testing, DGX mutation, deployment, or repair is
authorized.

## Design review reconciliation v3

The authenticated Claude re-review of reconciliation v2 closed all seven design
questions and returned two bounded residual corrections:

1. Define migration-time origin for entities that predate the journal. Choose
   either atomic sealed baseline events per existing entity, including lifecycle
   state, owner-version, initial sequence, and current digest-parameter
   identifier, or explicitly make pre-journal entities permanently
   non-verifiable until a later mutation establishes history. Add the matching
   migration/compatibility test.
2. Require event-stream and materialized-row reads within one read-only
   transaction/snapshot. If snapshot consistency cannot be established, return
   `UNKNOWN`, never `DRIFT`; add a concurrent-mutation replay test.

These are the final design-only corrections currently identified by the same
authenticated Claude family. No implementation, migration, live-state test,
DGX mutation, deployment, or repair action is authorized.

## Planned implementation evidence

Not run. No source files, migrations, tests, DGX runtime, or deployment have
been changed by this ticket.

## Current next action

Re-review the reconciled correction set with the same authenticated Claude
family using the same bounded packet revision. Only a Claude PASS on the
corrected design can close this design gate; implementation remains separate.
