# Bridge-Authoritative Sidebar Reconciliation Design

Date: 2026-07-31

Status: approved design, pending implementation plan

## Context

The Claude-to-Codex visibility-first rollout depends on duplicate-safe recovery
before a native Codex Desktop task is created. The installed sidebar broker
skill currently asks Codex Desktop to execute an exact signed-marker query:

```text
list_threads(query=<exact signed marker>, limit=20)
```

The installed native Codex operation does not support that contract. Its
available schema accepts only a bounded `limit`; it has no query, cursor, or
pagination field. A bounded recent-task listing cannot prove that a marker is
absent from the complete Codex task catalog, and visible task summaries may
truncate the marker. It therefore cannot safely authorize creation or exclude
an older matching task.

Project-aware `create_thread` is available and can place a new task in the
saved `.hermes` project. The remaining blocker is authoritative reconciliation
and negative proof, not project-aware creation.

This design amends the reconciliation boundary in
`2026-07-30-session-sidebar-visibility-first-design.md`. Session Bridge remains
the sole authority for discovery, identity, reservations, and recovery. The
broker becomes a query-free delivery executor.

## Decision

Move exact signed-marker reconciliation into Session Bridge's indexed Codex
catalog scanner. Before Session Bridge issues a creation-capable lease, it must
durably establish exactly one of three results:

- `recovered`: exactly one authenticated Codex task matches the source marker;
- `absence_proven`: a complete, current catalog scan found no authenticated
  match and no earlier creation reservation forbids a new create;
- `blocked`: the scan is incomplete or stale, multiple authenticated matches
  exist, identity is inconsistent, or prior dispatch makes absence unsafe.

The broker consumes this result from the lease. It does not search Codex,
interpret titles or summaries, or establish negative proof. Only a current
`absence_proven` result may authorize one project-aware native creation.

## Goals

- Support the installed query-less Codex Desktop thread API safely.
- Recover an existing task by exact authenticated marker without creating a
  replacement.
- Authorize creation only after a complete and current negative catalog proof.
- Preserve at-most-once creation through races, crashes, restarts, and lost
  native responses.
- Keep reconciliation, reservation, and canonical linkage authoritative in
  Session Bridge.
- Keep the broker small, deterministic, and incapable of inventing identity.

## Non-goals

- This design does not add search or pagination to Codex Desktop.
- It does not use a bounded recent-task listing as global negative proof.
- It does not infer identity from task title, `[Claude]` tags, project, cwd,
  timestamp, or truncated summary text.
- It does not modify Codex private databases or application state directly.
- It does not authorize replacement creation after ambiguous native dispatch.
- It does not change the readable preview, latest-five, placement, or source
  handoff contracts in the July 30 visibility-first design.

## Authority boundary

### Codex catalog scanner

The existing Session Bridge Codex scanner is the only discovery authority. It
must inspect the complete discoverable native task catalog through supported
read surfaces and authenticate the full signed marker from task content. A
title or summary hit may identify a candidate for inspection but never proves
identity.

Each successful scan publishes a durable catalog generation containing:

- a monotonic generation or equivalent immutable scan identity;
- scan completion time;
- completeness and provider-health state;
- the covered native inventory boundary;
- authenticated marker-to-task matches;
- fixed bounded failure reasons for unreadable or incomplete inventory.

A scan generation is usable for creation only while it satisfies the configured
freshness contract and remains the current complete generation.

### Session Bridge

For each sidebar delivery candidate, Session Bridge resolves the exact signed
marker against the current complete catalog generation. It durably records:

- source provider and canonical source session ID;
- bridge ID and policy/delivery generation;
- marker digest, never the signing secret;
- catalog generation and completion time;
- match cardinality;
- exact recovered Codex task ID when cardinality is one;
- reconciliation state and fixed reason;
- immutable proof digest bound to the job and reservation.

Session Bridge alone decides whether the job may bind, wait, quarantine, or
reserve a create.

### Sidebar broker

The broker receives at most one current lease. Its allowed reconciliation
behavior is limited to:

- bind or verify the exact `recovered_thread_id` supplied by Session Bridge;
- request a create reservation for an `absence_proven` lease;
- perform one native project-aware create after reservation succeeds;
- immediately report and bind the exact returned task ID;
- report fixed failures or ambiguity without creating a replacement.

The broker must not call `list_threads` for marker discovery, paginate native
tasks independently, select a candidate heuristically, or reinterpret a
blocked result.

## Reconciliation states

### Recovered

Exactly one native task contains a marker whose signature and identity fields
authenticate the expected bridge, source session, target provider, and policy
generation. Session Bridge returns its exact task ID and uses the bind-only
path. Native creation is not authorized.

### Absence proven

A current, complete catalog generation contains zero authenticated matches,
the job has never crossed the native-dispatch reservation boundary, and no
conflicting canonical link exists. Session Bridge may issue a lease that is
eligible to request one create reservation.

Zero matches alone are insufficient. Absence must be bound to the exact
catalog generation, job, marker digest, placement generation, and reservation
state.

### Blocked

Creation and heuristic binding are prohibited when any of these conditions is
true:

- the catalog scan is incomplete, failed, or stale;
- more than one authenticated task matches;
- a marker authenticates conflicting source or bridge identity;
- a canonical source link conflicts with catalog evidence;
- a previous native create was reserved or dispatched but no exact task ID is
  recoverable;
- the proof cannot be revalidated transactionally.

The durable fixed reason distinguishes retryable scanner state from
`marker_conflict`, `native_create_ambiguous`, and other needs-attention states.

## Transactional creation authorization

The lease may advertise `absence_proven`, but it does not itself permit native
dispatch. Immediately before calling `create_thread`, the broker requests the
existing create-reservation boundary using the lease token and proof digest.

Session Bridge grants the reservation only if, in one transaction:

1. the lease is current and belongs to the expected broker;
2. the recorded catalog generation is still current, complete, and fresh;
3. its authenticated match cardinality remains zero;
4. the source, marker, placement, policy, and delivery generations still
   match;
5. no canonical Codex link has appeared;
6. no prior create reservation or dispatch exists for the job;
7. no conflicting active lease or reservation exists.

Any changed condition rejects the reservation and returns the job to
reconciliation without native creation. A successful reservation is durable
before dispatch and authorizes exactly one create attempt.

After the native operation returns an exact task ID, the broker persists that
ID through Session Bridge before rename, verification, or any other action.
The scanner later authenticates the task marker and completes the canonical
link.

## Crash and ambiguity rules

- Crash before create reservation: the lease may expire and reconcile again.
- Crash after reservation but before provably absent dispatch: recovery uses
  the durable reservation state; it never assumes that another create is safe.
- Exact native task ID returned: bind it immediately before further work.
- Native response lost or ambiguous after reservation: reconcile only against
  the exact authenticated marker. Do not replacement-create.
- Zero matches after any create reservation: enter or retain
  `native_create_ambiguous`; zero is not new creation authority.
- Exactly one later match: recover and bind that exact task.
- Multiple later matches: enter `marker_conflict` / `needs_attention` and
  preserve every task.
- Restart preserves catalog proof, reservation, lease, returned task ID, and
  canonical linkage.

This deliberately sacrifices liveness when native dispatch is ambiguous in
order to preserve duplicate safety.

## Lease contract

The sidebar lease exposes a bounded reconciliation object equivalent to:

```text
reconciliation_state: recovered | absence_proven | blocked
reconciliation_generation: <immutable catalog generation>
reconciliation_proof_digest: <opaque digest>
recovered_thread_id: <exact ID only for recovered>
create_eligible: <true only for absence_proven>
fixed_reason: <bounded reason when blocked>
```

The concrete representation may extend the existing lease fields rather than
introduce a nested object, but it must preserve these semantics. Public status
must not expose raw signed markers, lease tokens, source messages, signing
material, or unbounded provider errors.

Backward compatibility is fail-closed: a lease lacking a valid authoritative
reconciliation result cannot create a native task.

## Concurrency invariants

For each canonical source and delivery generation:

- at most one current authoritative reconciliation proof exists;
- at most one native create reservation may be committed;
- at most one native create call may be authorized;
- at most one canonical Codex task may be linked;
- an existing exact match always takes precedence over creation;
- multiple exact matches are never collapsed heuristically;
- a stale or superseded proof can never authorize dispatch.

Database constraints and transactional compare-and-set behavior must enforce
these invariants independently of broker cooperation.

## Observability

Bounded health and status data should expose:

- current Codex catalog generation, completion time, freshness, and
  completeness;
- counts for `recovered`, `absence_proven`, scanner-blocked,
  marker-conflict, and create-ambiguous jobs;
- oldest job awaiting a complete catalog proof;
- reservation rejection counts by fixed reason;
- broker attempts to use missing, stale, or invalid proofs;
- count of recovered existing tasks versus newly created tasks.

Status must make a stale scanner distinguishable from a stuck broker. It must
not expose full markers, proof material, transcript content, or secrets.

## Testing

Implementation follows test-driven development and uses the required
`scripts/run_tests.sh` wrapper.

### Scanner and proof tests

- the current Codex native schema is modeled without a query parameter;
- a complete catalog scan can produce a durable zero-match proof;
- an incomplete, failed, or stale scan cannot produce creation authority;
- exact full-marker authentication produces one recovered task ID;
- title, tag, and truncated-summary matches do not authenticate identity;
- multiple authenticated matches produce a conflict, never a selected task;
- proof digests are bound to job, marker, catalog, placement, and delivery
  generations.

### Reservation and race tests

- a current zero-match proof authorizes one reservation and one create call;
- a newer catalog generation invalidates the earlier proof;
- a match appearing between lease and reservation rejects creation;
- a canonical link appearing between lease and reservation rejects creation;
- concurrent reservation requests commit at most one winner;
- stale, replayed, or cross-job proof digests fail closed;
- any previous create reservation prevents later zero-match recreation.

### Recovery and fault-injection tests

- restart before reservation safely reconciles again;
- restart after reservation never blindly creates;
- returned task ID is persisted before rename or verification;
- lost native response enters ambiguity without replacement creation;
- a later exact match recovers and binds the original task;
- persistent zero matches after dispatch remain ambiguous;
- multiple later matches enter needs-attention;
- scanner degradation preserves jobs, reservations, and canonical links.

### Broker contract tests

- the broker performs no marker search through `list_threads`;
- a lease without authoritative reconciliation cannot create;
- a recovered lease takes the bind-only path;
- an absence-proven lease must revalidate and reserve before create;
- a blocked lease makes no native mutation;
- one lease causes at most one project-aware native create;
- ambiguity and unsupported-tool failures settle safely without replacement.

### End-to-end acceptance

- an existing authenticated task is recovered without a create call;
- a genuinely missing source receives exactly one `.hermes` task;
- its readable summary and latest five messages satisfy the existing content
  contract;
- crashes at every reservation and binding boundary create no duplicate;
- current and historical sessions are reconciled through the complete catalog,
  not only the most recent bounded native listing.

## Rollout

1. Keep the production sidebar automation paused and continuous delivery
   disabled while the query-based broker contract is incompatible.
2. Add durable catalog-generation and reconciliation-proof state with creation
   disabled.
3. Backfill and validate exact authenticated marker mappings across the full
   discoverable Codex catalog.
4. Enable proof computation in shadow mode and confirm every pending or retry
   job resolves to recovered, absence-proven, or a fixed blocked reason.
5. Deploy the query-free broker lease contract and reservation revalidation.
6. Run focused scanner, race, crash, and broker tests.
7. Run one recovered-task canary and prove zero create calls.
8. Run one genuinely missing-session canary and prove one create and one link.
9. Restart Session Bridge and the broker across controlled reserved states.
10. Resume the one-minute automation, deliver the newest five missing sessions,
    inspect placement and readable content, then continue newest-first recovery.

Rollback pauses new leases and the broker automation while preserving catalog
proofs, reservations, native task IDs, canonical links, and every created task.
It never deletes or replaces a Codex task.

## Acceptance criteria

- The sidebar broker no longer depends on unsupported
  `list_threads(query=...)` behavior.
- Every creation-capable lease is backed by a current complete catalog absence
  proof and a successful transactional revalidation.
- Existing authenticated tasks are recovered by exact ID without creation.
- Stale, incomplete, conflicting, or ambiguous state fails closed.
- Any prior native create reservation permanently prevents blind replacement
  creation.
- Repeated scans, concurrent wakes, crashes, and restarts preserve at-most-one
  create reservation, create authorization, and canonical link per source.
- The project-aware `.hermes` placement and readable summary/latest-five
  requirements remain intact.
- Focused, fault-injection, end-to-end, and production canaries pass before
  continuous delivery resumes.

## Supersession

This document supersedes only the July 30 design's implication that the broker
can search the complete native Codex catalog for an exact marker after an
ambiguous or missing binding. All other visibility-first requirements remain
in force.

The implementation plan must update the Session Bridge requirement
traceability audit and name the tests proving each new acceptance criterion.
