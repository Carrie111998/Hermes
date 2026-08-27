# Desktop Transcript Provenance: Phase 1 Design

## Status

- Design date: 2026-08-27
- Upstream design base: `NousResearch/hermes-agent@7a1aafb4e1`
- Target issue subtype: the deterministic warm-cache, compressed-session subtype of #73646
- Candidate state: design only; no production implementation, installation, activation, push, or PR

## Problem

The Desktop warm-resume path currently proves that a cache entry belongs to the selected stored session, but it does not prove that the entry's `messages` came from the persisted display transcript. After context compression, a valid runtime cache may contain only the model-facing tail while the persisted display transcript contains the fuller conversation lineage.

On a warm switch, current `main` can publish that cache twice before the persisted REST transcript arrives:

1. the immediate warm-cache view sync;
2. the post-`session.activate` liveness sync, which still carries the cached messages;
3. the final persisted transcript reconciliation.

For an affected session, the visible sequence is therefore `tail-only -> tail-only with refreshed liveness -> persisted display transcript`. PR #82899 preserves the viewport across the later publication, but it deliberately retains this multi-publication behavior. PR #78499 repairs stale rows during message reconciliation and is not a first-publication eligibility contract. PR #79785 addresses whether the backend REST endpoint returns the full compression lineage and is not a renderer cache-origin contract.

## Goal

Introduce a positive, Desktop-internal proof that a cached message array was derived from a validated persisted display transcript. Use that proof to decide whether a warm cache may publish transcript messages before the current REST hydration completes.

Phase 1 must:

- prevent an unproven tail-only runtime projection from escaping through either pre-authority view sync;
- retain instant warm paint for a cache with valid persisted-display provenance;
- preserve live/inflight overlays on top of a proven persisted base;
- treat legacy/missing, wrong-owner, wrong-session, and wrong-lineage metadata as unproven;
- avoid a permanent blank view when REST hydration fails;
- remain internal to Desktop and avoid a backend/public protocol change.

## Non-goals

Phase 1 does not:

- fix the cold-switch blank/small-first-paint/backfill subtype of #73646;
- change scrolling or PR #82899's viewport publication revision;
- replace the message-level reconciliation performed by #78499 and related utilities;
- make a child-only REST endpoint complete; that remains the scope of #79565/#79785;
- add a backend-issued transcript revision or a root-to-tip coverage contract;
- change watch-window behavior;
- persist provenance in the bounded 40-message localStorage tail cache;
- modify or activate an installed Hermes runtime.

## Considered Approaches

### A. Compression/count heuristic

Gate warm paint when a session has a compression lineage and `message_count` exceeds the cached ChatMessage count.

This is small but unsound. Backend rows and rendered ChatMessages are not guaranteed to have one-to-one cardinality, sidebar counts can be stale, and equal counts do not prove equal content. Counts may reject good caches or approve wrong ones. This approach is rejected.

### B. Positive Desktop provenance (selected)

Carry optional structured provenance with `ClientSessionState`. Only a successful, identity-validated persisted REST hydration can mint the positive proof. Missing metadata is unknown/unproven. Runtime projections cannot mint it.

This keeps the contract close to the cached messages it qualifies, survives ordinary in-memory warm-cache reuse, and requires no backend migration. It is the selected Phase 1.

### C. Backend-issued revision and coverage token

Have the backend return a durable lineage revision, owner identity, paging watermark, and coverage assertion. The renderer can then verify both origin and freshness.

This is the long-term complete model, but it crosses API compatibility, old remote backends, lineage endpoint work, and #79785. It is deferred to Phase 2.

## Data Model

`ClientSessionState` gains the following optional positive proof. The Phase 1 field and type names are part of this design:

```ts
interface PersistedDisplayTranscriptProvenance {
  source: 'persisted-display'
  connectionId: string
  profile: string
  storedSessionId: string
  lineageRootId: string | null
  coverage: 'latest-page'
}

interface ClientSessionState {
  // existing fields...
  transcriptProvenance?: PersistedDisplayTranscriptProvenance
}
```

Only positive proof is represented in Phase 1. Absence means unknown/unproven; there is no default assumption that an untagged state is a runtime projection or a persisted display transcript.

`coverage: 'latest-page'` deliberately does not claim a complete root-to-tip lineage. It means that the visible base came from the validated Desktop display endpoint for the current scope. A future backend revision/coverage contract may add stronger coverage values without changing the fail-closed default.

Owner scope is part of the proof. As established by upstream commit `03f5302a22`, stored session IDs are unique only inside an owning `{connectionId, profile}` database. A proof minted for another owner must never qualify a cache entry even if the stored ID text matches.

`connectionId` and `profile` use the same normalization as the existing `sessionRestScope`: missing connection IDs become the empty string and missing/blank profiles become `default`. `lineageRootId` is the selected session row's `_lineage_root_id ?? null`; eligibility is checked against the current row resolved through `sessionMatchesStoredId`.

## Proof Rules

### Mint

The normal Desktop REST hydration path may mint provenance only after all of the following hold:

- the request returned successfully;
- the response belongs to the activated stored session;
- the active owner scope still matches the request scope;
- the selected compression lineage still matches the target session;
- the response is accepted by the existing non-empty/race guards;
- the reconciled display base is the one written into the state.

Minting occurs on the state containing the final reconciled persisted base plus any accepted live overlay, not on the raw REST payload before reconciliation.

### Preserve

Provenance may survive transformations that preserve the same persisted display base, including:

- appending or updating a current live/inflight overlay;
- enriching an existing message with local attachments or structured display parts;
- updating non-message session metadata and liveness;
- equivalent persisted refreshes for the same owner/session/lineage.

### Clear or reject

Provenance must be cleared, or considered invalid at the read gate, when:

- messages are replaced from a runtime/model projection without a persisted base;
- the owner connection or profile changes;
- the stored session or compression lineage changes;
- a cache entry predates the field or omits it;
- proof metadata is malformed or inconsistent with the selected session;
- a state is reconstructed from an origin that cannot prove display authority.

The implementation plan must audit all direct `ClientSessionState.messages` replacement sites in the warm/resume and stream paths. Unknown writers fail closed; no writer may infer proof from non-emptiness, message count, or runtime identity.

## Warm Resume Publication Flow

For a switch to a different warm session:

1. Resolve and validate the warm cache using the existing runtime/stored-session ownership checks.
2. Validate `transcriptProvenance` against the selected owner, stored session, and current lineage.
3. If valid, retain the current immediate warm paint and background persisted refresh.
4. If invalid or absent, keep the cached messages in the internal session state for reconciliation, but construct a view-only pre-authority state with no transcript messages.
5. Use that same view-only message policy for both the immediate sync and the post-activate liveness sync. The second sync must not leak the internal cached messages.
6. For an unproven cache, after a valid persisted hydration, reconcile the persisted display base with current live changes, mint provenance on the reconciled state, and perform the first transcript-bearing publication once.

For a same-selected-session re-resume, Phase 1 does not blank or suppress the already visible transcript. The user may be watching or submitting into that live state; existing concurrent-overlay protections remain authoritative.

Watch windows retain their existing path because they intentionally attach a live mirror and skip the normal persisted refresh.

## Failure and Fallback

Phase 1 must not leave the view blank indefinitely when the REST authority cannot be obtained.

- While REST is pending, an unproven cross-session warm cache remains suppressed.
- If REST succeeds and validates, publish the proven reconciled transcript.
- If REST definitively fails, returns a rejected identity, or produces a response that existing race guards cannot accept, publish the freshest cache state (including concurrent live updates) only as a degraded fallback after the authority attempt finishes.
- The fallback remains unproven. It is not saved or promoted as persisted display authority, so a later resume will retry hydration.
- Existing activation/session-not-found recovery remains responsible for dropping dead runtime mappings and falling through to cold resume.

This policy trades an early incorrect intermediate paint for a bounded loading interval, while preserving the current best-effort availability behavior on real authority failures.

## Testing Strategy

Implementation must follow test-driven development. The deterministic #73646 warm-cache reproduction is added first and observed failing on the exact implementation base.

Required RED/GREEN vectors:

1. An unproven compressed tail-only cache never appears before a delayed persisted transcript resolves.
2. The post-activate liveness sync also cannot leak that tail-only cache.
3. A successful persisted response publishes the fuller display transcript and mints valid provenance.
4. Live/inflight changes arriving during hydration survive on top of the persisted base and do not duplicate the prompt.
5. A valid same-owner/session/lineage proven cache still paints immediately.
6. Missing, malformed, wrong-owner, wrong-session, and wrong-lineage proofs are rejected.
7. Same-selected-session re-resume preserves the current visible/live transcript.
8. REST failure triggers delayed degraded fallback and does not mint provenance.
9. A runtime-only replacement clears or invalidates prior provenance.
10. Existing empty-REST race, attachment preservation, pending clarify, and compressed running-session regressions remain green.

Verification gates:

- focused `use-session-actions` and structural resume tests;
- provenance helper/type tests if a helper module is introduced;
- Desktop renderer/Electron/E2E TypeScript checks;
- ESLint with zero new errors and no warning regression in changed files;
- Prettier and `git diff --check`;
- the directly related #82899 and #78499 test files where applicable;
- a wider Desktop UI run or an explicit, evidence-backed statement of any environmental blocker/flakiness.

## Compatibility and Scope Controls

- The field is optional, so legacy in-memory states are accepted structurally but treated as unproven.
- No serialized backend schema or model-tool surface changes.
- No default-shell, runtime, gateway, profile, plugin, system configuration, or installed Desktop changes.
- The proof gate is Desktop-internal and limited to transcript first-publication eligibility.
- The implementation should avoid a general cache framework or unrelated session/process refactor.

## Phase 2 Boundary

Phase 2 is considered only after Phase 1 demonstrates value and exposes concrete gaps. It would require a backend-issued contract containing at least:

- owner identity;
- compression lineage identity;
- transcript revision/watermark;
- page coverage and continuation information;
- compatibility behavior for older remote backends.

Phase 2 must be coordinated with #79565/#79785 rather than inventing a second lineage authority. It is not authorized by this design.

## Acceptance State

The Phase 1 candidate is acceptable only if tests prove that no unproven cross-session warm-cache messages escape before the authority attempt settles, while proven caches retain immediate paint and failure fallback remains available. Completion claims must distinguish unit/static evidence from any live Desktop reproduction.
