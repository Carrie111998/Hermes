# Desktop Transcript Provenance Phase 1 Implementation Plan

> **For agentic workers:** Execute this plan in order. Keep the candidate confined to the feature worktree, follow RED-GREEN-REFACTOR for every behavior change, and do not install, launch, push, or open a PR without separate authorization.

**Goal:** Prevent an unproven warm-cache runtime tail from being published before Desktop obtains persisted display authority, while retaining immediate paint for a correctly scoped proven cache and a delayed unproven fallback when authority cannot be obtained.

**Architecture:** Add an optional positive proof to `ClientSessionState`, centralize scope normalization and exact proof validation in a small pure helper, then gate only the two warm-resume pre-authority view publications. Successful identity-explicit REST hydration mints proof on the reconciled state; compatible-but-unverifiable REST results may still be displayed but remain unproven. Cold REST hydration also mints proof so a later warm resume can qualify for immediate paint.

**Tech Stack:** TypeScript, React hooks, Nanostores, Vitest, Testing Library, ESLint, Prettier, npm workspaces.

---

## Scope and safety invariants

- Work only in `D:\Projects\GitHub\NousResearch\hermes-agent\Desktop-Transcript-Provenance\source` on branch `fix/desktop-transcript-provenance`.
- Do not modify `C:\Users\60271\AppData\Local\hermes\hermes-agent`, launch Desktop, restart Gateway, install/package the candidate, change profiles/plugins/system settings, push, or create/update a PR.
- Preserve watch-window behavior, same-selected-session behavior, REST/runtime reconciliation, live overlay handling, and the bounded localStorage transcript-tail format.
- Do not infer provenance from non-empty messages, message count, runtime identity, compression presence, or a successful request alone.
- A REST result with no explicit matching `session_id` may remain display-compatible under existing logic, but it must not mint positive proof.
- Commits described below are local feature-branch checkpoints only.

## Task 1: Add the provenance type and pure proof helpers

**Files:**

- Modify: `apps/desktop/src/app/types.ts`
- Create: `apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.ts`
- Create: `apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.test.ts`

- [ ] **Step 1: Write the failing helper tests**

Create table-driven tests for:

1. default scope normalization (`connectionId: ''`, `profile: 'default'`);
2. string-profile scope and object scope normalization;
3. exact owner/session/lineage/coverage matching;
4. missing proof;
5. malformed source or coverage;
6. wrong connection, profile, stored session, and lineage root;
7. removal of stale proof without changing messages;
8. creation of a view-only state with `messages: []` while preserving the internal input object.

Use an explicit expected proof so the matching helper cannot silently normalize malformed cached metadata:

```ts
const expected = createPersistedDisplayTranscriptProvenance({
  storedSessionId: 'stored-A',
  lineageRootId: 'root-A',
  scope: { connectionId: 'remote-1', profile: 'work' }
})

expect(hasPersistedDisplayTranscriptProvenance({ transcriptProvenance: expected }, expected)).toBe(true)
expect(
  hasPersistedDisplayTranscriptProvenance(
    { transcriptProvenance: { ...expected, profile: 'default' } },
    expected
  )
).toBe(false)
```

- [ ] **Step 2: Run the new test and observe RED**

Run from the repository root:

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions/transcript-provenance.test.ts
```

Expected: FAIL because the helper module and provenance type do not exist. Record the failing test count and error text.

- [ ] **Step 3: Add the optional positive-proof type**

In `apps/desktop/src/app/types.ts`, export the exact approved shape and add the optional field:

```ts
export interface PersistedDisplayTranscriptProvenance {
  source: 'persisted-display'
  connectionId: string
  profile: string
  storedSessionId: string
  lineageRootId: string | null
  coverage: 'latest-page'
}

export interface ClientSessionState {
  storedSessionId: string | null
  transcriptProvenance?: PersistedDisplayTranscriptProvenance
  // existing fields remain unchanged
}
```

Do not add a default proof in `createClientSessionState`; absence is the fail-closed legacy state.

- [ ] **Step 4: Implement a narrow pure helper module**

Implement these responsibilities, keeping route-scope normalization in one place:

```ts
export type TranscriptProvenanceScope =
  | string
  | null
  | undefined
  | { connectionId?: string | null; profile?: string | null }

export function createPersistedDisplayTranscriptProvenance(input: {
  storedSessionId: string
  lineageRootId: string | null
  scope: TranscriptProvenanceScope
}): PersistedDisplayTranscriptProvenance

export function hasPersistedDisplayTranscriptProvenance(
  state: Pick<ClientSessionState, 'transcriptProvenance'>,
  expected: PersistedDisplayTranscriptProvenance
): boolean

export function withoutTranscriptProvenance(state: ClientSessionState): ClientSessionState

export function suppressTranscriptForView(state: ClientSessionState, suppress: boolean): ClientSessionState
```

Normalization rules:

```ts
const connectionId = typeof scope === 'object' && scope ? (scope.connectionId ?? '').trim() : ''
const rawProfile = typeof scope === 'string' ? scope : scope?.profile
const profile = rawProfile?.trim() || 'default'
```

`hasPersistedDisplayTranscriptProvenance` compares all six proof fields exactly. It must not coerce a malformed cached proof into a valid one. `withoutTranscriptProvenance` returns the original object when no proof exists. `suppressTranscriptForView` returns the original object unless suppression is requested and messages are non-empty; it never mutates the input.

- [ ] **Step 5: Run the focused helper test and observe GREEN**

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions/transcript-provenance.test.ts
```

Expected: PASS, with every normalization and exact-match vector green.

- [ ] **Step 6: Typecheck the new public state shape**

```powershell
npm run typecheck --workspace apps/desktop
```

Expected: exit code 0. If unrelated pre-existing failures occur, capture exact diagnostics and do not mark this step complete.

- [ ] **Step 7: Commit the type/helper checkpoint locally**

```powershell
git add apps/desktop/src/app/types.ts apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.ts apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.test.ts
git commit -m "feat(desktop): model persisted transcript provenance"
```

Expected: one local commit containing only the three listed files.

## Task 2: Reproduce and gate both warm pre-authority publications

**Files:**

- Modify: `apps/desktop/src/app/session/hooks/use-session-actions.test.tsx`
- Modify: `apps/desktop/src/app/session/hooks/use-session-actions/index.ts`

- [ ] **Step 1: Expose view publications in the existing test harness**

Extend `ResumeHarness` with:

```ts
onViewSync?: (sessionId: string, state: ClientSessionState) => void
```

Wire it without changing production code:

```ts
syncSessionStateToView: (sessionId, state) => onViewSync?.(sessionId, state)
```

Keep `onStateUpdate` because internal cache updates and visible publications are intentionally different in this candidate.

- [ ] **Step 2: Add the deterministic unproven-tail RED test**

Seed a warm state for `stored-A` with only `recent prompt/recent answer`, no provenance, and a stored row whose lineage root is `root-A`. Use a deferred `getLatestSessionMessages` result containing older plus recent rows. Let `session.activate` resolve before the REST deferred.

Capture every `onViewSync` snapshot and assert before REST resolution:

```ts
expect(viewSnapshots.length).toBeGreaterThanOrEqual(2)
expect(viewSnapshots.every(state => state.messages.length === 0)).toBe(true)
expect(sessionStateByRuntimeIdRef.current.get('rt-A')?.messages).toHaveLength(2)
```

After resolving REST with explicit `session_id: 'stored-A'`, assert that the first transcript-bearing publication contains both the older and recent rows and carries the expected proof.

- [ ] **Step 3: Run only the new reproduction and observe RED**

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx -t "does not publish an unproven warm transcript before persisted authority settles"
```

Expected: FAIL because at least the immediate warm sync and post-activate liveness sync expose the cached tail.

- [ ] **Step 4: Validate or clear the warm cache proof before publication**

Immediately after resolving `stored` in the warm branch:

1. build `expectedProvenance` only when the stored row exists;
2. derive `lineageRootId` from `stored._lineage_root_id ?? null`;
3. validate the cached proof against `sessionRestScope` and the selected stored ID;
4. clear invalid/stale proof from the internal cache state before any later fallback;
5. preserve an exact valid proof.

The control variables should be explicit:

```ts
const expectedProvenance = stored
  ? createPersistedDisplayTranscriptProvenance({
      storedSessionId,
      lineageRootId: stored._lineage_root_id ?? null,
      scope: sessionRestScope
    })
  : null

const hasValidProvenance = Boolean(
  expectedProvenance && hasPersistedDisplayTranscriptProvenance(cachedViewState, expectedProvenance)
)
```

If `hasValidProvenance` is false, replace the internal cached state with `withoutTranscriptProvenance(cachedViewState)` before continuing. Do not discard its messages; they remain reconciliation and fallback input.

- [ ] **Step 5: Gate both pre-authority view syncs**

Compute suppression only for a cross-session, non-watch refresh:

```ts
const suppressUnprovenWarmTranscript =
  !resumedSameSelectedSession && shouldRefreshPersistedTranscript && !hasValidProvenance
```

Use a view-only projection at both existing publication points:

```ts
syncSessionStateToView(
  cachedRuntimeId,
  suppressTranscriptForView(cachedViewState, suppressUnprovenWarmTranscript)
)
```

and:

```ts
syncSessionStateToView(
  cachedRuntimeId,
  suppressTranscriptForView(activatedLivenessState, suppressUnprovenWarmTranscript)
)
```

Do not write the suppressed state back to `sessionStateByRuntimeIdRef`; only the view is blanked.

- [ ] **Step 6: Mint proof only from accepted, explicitly identified REST authority**

Track separately:

```ts
let acceptedPersistedDisplayTranscript = false
```

Set it only inside the existing accepted persisted branch and only when all of these are true:

- `expectedProvenance` exists;
- `persisted.session_id` is present;
- `persisted.session_id === storedSessionId`;
- `activatedStoredSessionId` is present and equals `storedSessionId`;
- the existing non-empty/race guard accepts the result;
- the current resume request remains current.

Keep the existing compatibility predicate for whether legacy REST rows may be displayed. The stricter predicate is only for minting proof.

When building `activatedState`, use:

```ts
transcriptProvenance: acceptedPersistedDisplayTranscript ? expectedProvenance : undefined
```

The existing final `syncSessionStateToView` becomes:

- the first transcript-bearing publication after proven hydration; or
- the delayed degraded fallback after failed/rejected/empty authority.

Because invalid proof was cleared earlier, the degraded fallback remains unproven.

- [ ] **Step 7: Run the deterministic reproduction and observe GREEN**

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx -t "does not publish an unproven warm transcript before persisted authority settles"
```

Expected: PASS; two or more pre-authority publications have empty messages, internal cache messages remain available, and the first later transcript publication is full and proven.

- [ ] **Step 8: Commit the warm gate checkpoint locally**

```powershell
git add apps/desktop/src/app/session/hooks/use-session-actions/index.ts apps/desktop/src/app/session/hooks/use-session-actions.test.tsx
git commit -m "fix(desktop): gate unproven warm transcript paint"
```

Expected: one local commit limited to the warm-resume implementation and its failing-first reproduction.

## Task 3: Cover eligibility, fallback, and same-session behavior

**Files:**

- Modify: `apps/desktop/src/app/session/hooks/use-session-actions.test.tsx`
- Modify if a test exposes a defect: `apps/desktop/src/app/session/hooks/use-session-actions/index.ts`
- Modify if a pure helper defect is exposed: `apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.ts`

- [ ] **Step 1: Add a valid-proof immediate-paint test**

Seed the warm state with an exact proof for the current `{connectionId, profile, storedSessionId, lineageRootId}`. Hold REST pending and assert the first publication still contains the cached transcript. Then resolve REST and assert normal refresh behavior remains intact.

- [ ] **Step 2: Add fail-closed eligibility table tests at the resume boundary**

For each variant, seed a non-empty warm cache and assert no transcript-bearing publication occurs before the deferred REST settles:

- no proof (legacy state);
- wrong `connectionId`;
- wrong `profile`;
- wrong `storedSessionId`;
- wrong `lineageRootId`;
- malformed proof cast from persisted/legacy data.

At least one case must use two stored rows with the same textual ID in different owner scopes to guard the profile-local identity rule introduced by upstream `03f5302a22`.

- [ ] **Step 3: Add delayed degraded fallback tests**

Cover both:

1. `getLatestSessionMessages` rejects;
2. REST returns an identity mismatch or an empty result rejected by the existing race guard.

Before settlement, assert every cross-session warm publication has no messages. After settlement, assert the freshest cached/live state is published and `transcriptProvenance` is absent. Assert no transport/error branch invents a positive proof.

- [ ] **Step 4: Add same-selected-session non-blanking test**

Render `ResumeHarness` with `selectedStoredSessionId="stored-A"`, seed current visible messages without proof, hold REST pending, and assert the liveness/view publications retain those messages. This protects active editing/watching behavior from the cross-session gate.

- [ ] **Step 5: Strengthen the existing live-overlay test**

Extend `preserves live cache updates that arrive while the persisted transcript is loading` to assert:

- older persisted rows are present;
- concurrent live delta is present exactly once;
- current prompt is not duplicated;
- final state carries the expected provenance;
- no pre-authority publication contains the unproven tail.

- [ ] **Step 6: Run each new test individually through RED then GREEN**

Use one `-t` filter per new behavior while implementing. Example:

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx -t "publishes a matching proven warm transcript immediately"
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx -t "publishes an unproven cache only after persisted refresh fails"
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx -t "does not blank a same-selected-session re-resume"
```

Expected: each test fails for the intended missing behavior before the smallest implementation adjustment and passes afterward. Record any test that passes immediately and explain which earlier change already supplied the behavior.

- [ ] **Step 7: Run the full session-actions test file**

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx
```

Expected: all tests in the file pass with no unhandled promise rejection.

- [ ] **Step 8: Commit the warm behavior matrix locally**

```powershell
git add apps/desktop/src/app/session/hooks/use-session-actions/index.ts apps/desktop/src/app/session/hooks/use-session-actions.test.tsx apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.ts
git commit -m "test(desktop): cover transcript provenance eligibility"
```

Expected: a local checkpoint containing the behavior matrix and only any minimal implementation correction it exposed.

## Task 4: Mint provenance on the cold REST hydration path

**Files:**

- Modify: `apps/desktop/src/app/session/hooks/use-session-actions.test.tsx`
- Modify: `apps/desktop/src/app/session/hooks/use-session-actions/index.ts`

- [ ] **Step 1: Add a failing cold-hydration proof test**

Set no warm runtime mapping. Provide a selected stored row with a known owner and lineage. Return:

- `getLatestSessionMessages`: a non-empty transcript with explicit matching `session_id`;
- `session.resume`: a matching `session_key`/`resumed` and an omitted runtime transcript.

After resume, inspect `sessionStateByRuntimeIdRef.current.get(resumed.session_id)` and expect the exact proof. This test must begin RED because the cold path currently stores no provenance.

- [ ] **Step 2: Distinguish accepted display rows from proof eligibility**

In the cold path, track whether the prefetched REST result:

- was accepted into `prefetchedTranscriptMessages`;
- explicitly names `storedSessionId` through `prefetchedResult.session_id`;
- matches the resumed stored identity;
- still belongs to the current resume and current owner/lineage.

Do not mint for a response with missing `session_id`, even though existing compatibility behavior may still display it. Do not mint from `session.resume` messages, durable localStorage tail paint, or REST fallback after a failed runtime resume unless that fallback is also explicitly identity-validated and written into a `ClientSessionState` for the correct runtime.

- [ ] **Step 3: Attach proof to the final cold state**

Resolve expected proof from the same `stored` row and `sessionRestScope` used by the request. Add it in the final `updateSessionState` object only when the accepted prefetch meets the strict proof predicate:

```ts
...(acceptedPersistedDisplayTranscript && expectedProvenance
  ? { transcriptProvenance: expectedProvenance }
  : { transcriptProvenance: undefined })
```

This ensures the next warm switch can paint immediately without widening the localStorage tail-cache contract.

- [ ] **Step 4: Add cold negative tests**

Assert no proof for:

- REST result without `session_id`;
- REST result naming another stored session;
- resume response naming a different stored session;
- watch-window path, which skips persisted prefetch;
- localStorage tail paint before REST authority.

- [ ] **Step 5: Run cold tests and the entire session-actions file**

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx -t "mints transcript provenance after an identity-validated cold REST hydrate"
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit the cold mint checkpoint locally**

```powershell
git add apps/desktop/src/app/session/hooks/use-session-actions/index.ts apps/desktop/src/app/session/hooks/use-session-actions.test.tsx
git commit -m "fix(desktop): retain authority across cold transcript hydrate"
```

Expected: one local commit containing cold-path proof minting and its positive/negative tests.

## Task 5: Audit message-replacement boundaries and invalidate runtime-only replacement

**Files:**

- Inspect: `apps/desktop/src/app/session/hooks/use-session-actions/index.ts`
- Inspect: `apps/desktop/src/app/session/hooks/use-session-state-cache.ts`
- Inspect: `apps/desktop/src/app/session/session-state-cache.ts`
- Inspect: `apps/desktop/src/app/session/hooks/use-gateway-message-handler.ts`
- Inspect: `apps/desktop/src/app/session/hooks/use-message-stream.ts`
- Modify: `apps/desktop/src/app/session/hooks/use-prompt-actions/slash.ts`
- Modify: `apps/desktop/src/app/session/hooks/use-prompt-actions/index.test.tsx`
- Modify: `apps/desktop/src/app/session/hooks/use-session-state-cache.ts`
- Modify: `apps/desktop/src/app/session/hooks/use-session-state-cache.test.tsx`
- Modify only the narrow writer(s) proven to replace a persisted base with a runtime-only projection.
- Add tests beside the writer's existing tests.

- [ ] **Step 1: Enumerate every direct message replacement on `ClientSessionState`**

Run:

```powershell
rg -n "messages:\s|\.messages\s*=|updateSessionState\(" apps/desktop/src/app/session apps/desktop/src/app/chat
```

Classify each writer as:

- persisted-base preserving (may preserve proof);
- live/inflight overlay preserving (may preserve proof);
- metadata-only (must preserve proof);
- runtime-only full replacement (must clear proof);
- new/reconstructed/unknown origin (must remain unproven).

Record the classification in the implementation evidence; do not broaden the code into a generic cache framework.

- [ ] **Step 2: Add a failing test for each actual runtime-only full replacement**

Seed a state with valid proof, invoke the existing writer/event path that replaces messages from runtime/model projection, and assert `transcriptProvenance` becomes absent. Do not fabricate a new production API solely to satisfy this test.

The audit found one such boundary: successful manual `/compress` in `use-prompt-actions/slash.ts` replaces the whole visible transcript with `session.compress` response messages. Extend the existing `/compress` test harness state with optional seed provenance, then run:

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-prompt-actions/index.test.tsx -t "clears persisted transcript provenance when manual compression replaces the transcript"
```

Expected RED: the response messages replace the transcript, but the previously seeded `transcriptProvenance` remains present.

The audit also found a scope-identity invalidation boundary in
`use-session-state-cache.ts`: `ensureSessionState` updates `storedSessionId`
after an auto-compression rotation but currently retains proof minted for the
previous stored session. Add a focused test to
`use-session-state-cache.test.tsx`, seed a state with proof for `stored-A`,
rotate it to `stored-A-next`, and run:

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-state-cache.test.tsx -t "clears transcript provenance when the stored session id rotates"
```

Expected RED: the cached state owns `stored-A-next`, but still carries the
proof for `stored-A`.

- [ ] **Step 3: Clear proof at the narrow replacement boundary**

Use an explicit `transcriptProvenance: undefined` in the `/compress` replacement updater. Preserve proof for append/delta/enrichment operations that retain the same persisted display base.

When `ensureSessionState` changes `storedSessionId`, clear
`transcriptProvenance` on the new state object. This is an identity-scope
invalidation, not a message-origin inference; ordinary metadata updates and
same-id calls continue to preserve proof.

- [ ] **Step 4: Run writer-focused tests plus session actions**

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions.test.tsx
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-prompt-actions/index.test.tsx -t "usePromptActions /compress"
```

Also run the exact existing test file(s) owning any modified stream/gateway writer. Expected: all focused files pass.

- [ ] **Step 5: Close the audit without speculative edits**

If the audit proves there is no runtime-only full replacement outside the already modified resume paths, make no code change and record the evidence. If it discovers a writer outside the named files whose ownership test is not already identified above, pause execution and amend this plan with that exact production path, exact test path, RED command, and expected failure before modifying it. Do not create an empty commit.

## Task 6: Regression and quality verification

**Files:**

- Verify all changed files.
- Do not modify unrelated failures merely to make a broad command green.

- [ ] **Step 1: Run focused provenance and session-action tests**

```powershell
npm run test:ui --workspace apps/desktop -- src/app/session/hooks/use-session-actions/transcript-provenance.test.ts src/app/session/hooks/use-session-actions.test.tsx
```

Expected: exit code 0 with exact test counts recorded.

- [ ] **Step 2: Run directly related transcript/reconciliation regressions**

Run the current files owning tail grafting, resume reconciliation utilities, and resume structure:

```powershell
npm run test:ui --workspace apps/desktop -- src/app/chat/transcript-backfill.test.ts src/app/session/hooks/use-session-actions/utils.test.ts src/app/session/hooks/use-session-actions/resume-structural-parts.test.ts
```

Expected: exit code 0. The #82899 warm-publication behavior currently lives in `use-session-actions.test.tsx`, which was already run in Step 1; do not claim a separate #82899 test file.

- [ ] **Step 3: Run Desktop TypeScript and lint gates**

```powershell
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop -- --max-warnings=0
```

Expected: both exit 0. If baseline warnings prevent `--max-warnings=0`, rerun lint on the exact changed TypeScript files and report both the broad baseline and changed-file result.

- [ ] **Step 4: Format only the changed source files**

Run Prettier with the exact changed file list, not the whole repository:

```powershell
npm exec --workspace apps/desktop -- prettier --write src/app/types.ts src/app/session/hooks/use-session-actions/index.ts src/app/session/hooks/use-session-actions/transcript-provenance.ts src/app/session/hooks/use-session-actions/transcript-provenance.test.ts src/app/session/hooks/use-session-actions.test.tsx
git diff --check
```

Expected: Prettier completes and `git diff --check` exits 0.

- [ ] **Step 5: Run the wider Desktop UI suite**

```powershell
npm run test:ui --workspace apps/desktop
```

Expected: exit code 0. If it fails for an environment or pre-existing reason, preserve the exact test names, stack traces, exit code, and demonstrate whether each failure reproduces on the unchanged base before classifying it as unrelated.

- [ ] **Step 6: Review the complete feature diff and boundaries**

```powershell
git status --short --branch
git diff origin/main...HEAD --stat
git diff origin/main...HEAD -- apps/desktop/src/app/types.ts apps/desktop/src/app/session/hooks/use-session-actions
git log --oneline --decorate origin/main..HEAD
```

Confirm:

- no backend/public protocol files changed;
- no localStorage transcript-tail schema changed;
- no watch-window policy changed;
- no installed Hermes path changed;
- no build/package/install/activation/push/PR occurred;
- no unrelated refactor entered the diff.

- [ ] **Step 7: Perform a final unfinished-marker and changed-file review**

```powershell
rg -n "TO[D]O|TB[D]|FIXM[E]|not implement[e]d" apps/desktop/src/app/types.ts apps/desktop/src/app/session/hooks/use-session-actions
git diff --check origin/main...HEAD
```

Expected: no unfinished marker in changed code and no whitespace errors.

- [ ] **Step 8: Commit any verification-only corrections locally**

If formatting or review required real changes:

```powershell
git add apps/desktop/src/app/types.ts apps/desktop/src/app/session/hooks/use-session-actions/index.ts apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.ts apps/desktop/src/app/session/hooks/use-session-actions/transcript-provenance.test.ts apps/desktop/src/app/session/hooks/use-session-actions.test.tsx
git commit -m "chore(desktop): finalize transcript provenance candidate"
```

Do not create an empty commit.

## Final delivery checklist

- [ ] Report the base SHA and every local candidate commit SHA.
- [ ] List all modified files and summarize why each changed.
- [ ] Provide the RED evidence for the deterministic two-publication leak.
- [ ] Provide focused, related, typecheck, lint, formatting, and wider-suite commands with exit codes and test counts.
- [ ] Separate any environment/pre-existing failure from product regressions using unchanged-base evidence.
- [ ] State compatibility behavior for legacy states and REST responses without explicit identity.
- [ ] State the remaining Phase 2 gap: no backend-issued revision/full-lineage coverage proof.
- [ ] Give rollback instructions: discard/remove the isolated feature worktree or reset only this feature branch; the installed runtime requires no rollback because it was never changed.
- [ ] State exactly: `only implemented in the isolated feature worktree; not installed, not activated, not pushed, and no PR created or updated`.
