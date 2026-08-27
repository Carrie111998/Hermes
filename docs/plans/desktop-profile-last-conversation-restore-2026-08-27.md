# Desktop Profile Last-Conversation Restore: Implementation Plan

## Goal
Restore each profile's last valid conversation when users switch profiles in Hermes Desktop, across both same-source profile-rail switches and cross-connection/fleet switches, while preserving immediate cross-profile isolation and preserving explicitly chosen blank New Chat drafts.

## Executive Summary
Hermes Desktop lands on the fresh-draft splash because live profile switches intentionally clear the outgoing conversation, while remembered navigation restoration is gated by a window-lifetime `restoredRef` and therefore never reruns for the newly active `(connection, profile)` scope. The same effect then persists the switch-generated `/`, overwriting the target scope’s remembered route. The safest fix is a targeted renderer-side restore transaction: preserve the immediate isolation reset, distinguish automatic isolation drafts from explicit blank drafts, suppress remembered-navigation writes during automatic transitions, and restore only the target scope’s last conversation after exact gateway/storage identity is proven and an authoritative scoped by-ID lookup validates ownership. Explicit New Chat remains durable, switch races are latest-wins, and restoration enters the existing route-resume machinery through an ownership-qualified forced-cold resume.

## Background
- `selectProfile()` records the target draft owner, synchronously issues the generic fresh-session edge, then asynchronously activates the target (`apps/desktop/src/store/profile.ts:754-800`). The profile rail and workspace group are current entry points (`apps/desktop/src/app/chat/sidebar/profile-switcher.tsx:368-384,415-445,477-483`; `apps/desktop/src/app/chat/sidebar/projects/workspace-group.tsx:126-128`).
- `selectConnection()` is a distinct two-phase, latest-wins transaction keyed by `connectionId::profile`: it dials first, commits inside the gateway-switch barrier, validates the exact target, and has separate pre-wipe and post-wipe failure behavior (`apps/desktop/src/store/connections.ts:269-457`; `apps/desktop/src/store/gateway-switch.ts:44-159`). Fleet profile squares supply the exact connection/profile pair (`apps/desktop/src/app/chat/sidebar/profile-switcher.tsx:224-235`).
- Both switch paths and explicit New Chat actions converge on the targetless numeric `$freshSessionRequest` (`apps/desktop/src/store/profile.ts:330-338,754-770,838-883`; `apps/desktop/src/store/connections.ts:311-324,423-449`). Wiring consumes every edge identically and calls `startFreshSessionDraft()` (`apps/desktop/src/app/contrib/wiring.tsx:508-521`), which clears route intent, session IDs, messages, and navigates to `/` (`apps/desktop/src/app/session/hooks/use-session-actions/index.ts:388-471`). No state records whether `/` came from automatic isolation or explicit user intent.
- The intentional reset was introduced by `a40e20e1368d6626197f0316361d33b80aff2dd8` to prevent the outgoing transcript remaining visible after a live profile re-home; it must remain the foreground isolation boundary. Reload-free profile switching originated in `b94b3622b5faabadf36d8d51f5804c0a655553e7`.
- Remembered route/session keys are already scoped by normalized profile and active connection suffix (`apps/desktop/src/store/session.ts:40-53,82-108,201-211`; `apps/desktop/src/lib/connection-scoped.ts:49-97,170-203`). `afe238ac7e3cd9a50c4f6bdacf2acff3c864714c` established the connection-scoped isolation needed across windows/backends.
- `useDesktopIntegrations()` owns both cold restore and subsequent persistence. Its `restoredRef` is a window-lifetime latch; live switches rerun the effect but cannot reread the destination scope, while the write branch persists the transitional `/` (`apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts:92-166`). The full root-cause and eliminated hypotheses are documented in `docs/investigations/desktop-profile-active-chat-restore-2026-08-27.md`.
- Current cold-start policy can restore arbitrary non-overlay routes, and `/` is not a durable blank sentinel because the surviving remembered session ID may be used as fallback (`apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts:104-165`). This plan intentionally differs for live switching: per user decision, restore only the last conversation and make an explicitly chosen blank New Chat draft durable.
- Exact active identity is available through `$activeConnectionId`, `$activeGatewayProfile`, `gatewayActivationEpoch()`, and wiring's `gatewayScope` (`apps/desktop/src/store/connections.ts:43-47`; `apps/desktop/src/store/gateway.ts:303-305,377-425`; `apps/desktop/src/app/contrib/wiring.tsx:523-544`). Profile activation descriptor lookup is best-effort and can retain the prior suffix (`apps/desktop/src/store/profile.ts:426-457,495-516,672-703`), so promise fulfillment alone is not proof of a safe persistence scope.
- Session list refresh has request/profile/activation-epoch stale guards, but `$sessionsLoading` and nonempty `$sessions` are not target-scope readiness signals (`apps/desktop/src/app/session/hooks/use-session-list-actions.ts:227-350`; tests at `use-session-list-actions.test.tsx:433-568`). Exact stored-session lookup exists in `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts:1390-1485`.
- Resume and owner routing are fail-closed patterns: exact owner resolution is represented in `apps/desktop/src/store/session-request-router.ts`, and route resume consumes monotonic explicit requests (`apps/desktop/src/store/session.ts:1076-1091`; `apps/desktop/src/app/session/hooks/use-route-resume.ts:108,135-170`). Prior ownership commits include `cb75983abfdceed3e544a3dea183bfd46904ee8d` and `07b87f1470aedf86e6ab6d33726b3c4a009a1ff5`.
- The warm runtime map is keyed by bare stored-session ID (`apps/desktop/src/app/session/hooks/use-session-actions/index.ts:858-867`), so automatic restoration must not reuse a cached runtime without proving exact connection/profile ownership.
- Existing test seams include restoration/persistence (`use-desktop-integrations.test.tsx:121-180,182-286,307-360`), route reset/resume races (`use-route-resume.test.tsx:158-202,393-463`), same-source selection (`store/profile-select-source.test.ts:46-93`), fleet revisions/failures (`store/connections.test.ts` around the `selectConnection()` transaction), gateway barriers (`store/gateway-switch.test.ts:92-229`), connection-scoped storage (`store/layout-connection-scope.test.ts:151-165`), and explicit blank close behavior (`app/chat/close-tab.test.ts:99-129`).
- `docs/plans/` and `docs/completed/` contained no earlier plan; the investigation report and commit history are the prior-art baseline.

## Orchestration Progress

- [x] Stage 1: Durable conversation storage and restore coordinator foundations — implemented; formatting passes, focused execution blocked by missing Desktop workspace dependencies
- [x] Stage 2: Typed draft provenance and both switch transactions — implemented; Prettier/esbuild parsing pass, test runner blocked by missing Desktop dependencies
- [x] Stage 3: Exact scoped lookup and ownership-forced resume — implemented; Prettier/esbuild/diff checks pass, runtime suites blocked by missing Desktop dependencies
- [x] Stage 4: Live/cold integration, cancellation, and full regression verification — implemented and independently reviewed; Prettier/esbuild/diff checks pass, runtime suites blocked by missing Desktop dependencies

## Execution Index

| Work item | Goal | Done when | Key files | Dependencies | Size |
|---|---|---|---|---|---|
| 1. Durable conversation preference | Distinguish session, explicit blank, absence, and legacy values per exact scope | Versioned record round-trips, dual-write rollback behavior, legacy fallback, and connection isolation tests pass | `store/session.ts`, `store/session.test.ts`, `store/layout-connection-scope.test.ts` | None | M |
| 2. Restore coordinator and typed draft provenance | Give switch resets latest-only identity and distinguish automatic from explicit `/` | Coordinator generations and every reset producer—including direct Cmd-N, `/new`, Quick Entry, voice, and boot callers—is exhaustively classified at `startFreshSessionDraft()` | New `store/profile-conversation-restore.ts`, `store/profile.ts`, `use-session-actions/index.ts`, `wiring.tsx`, prompt/composer actions, `close-tab.ts`, `projects.ts` | Work item 1 for final persistence behavior | L |
| 3. Instrument both switch transactions | Begin/commit/cancel restores without changing each switch path's isolation/failure contract | Same-source and fleet success, supersession, All Profiles, pre/post-wipe failures, boot, and landed timeout tests pass | `store/profile.ts`, `store/connections.ts`, related tests | Work item 2 | L |
| 4. Exact restore lookup | Resolve remembered IDs against the authoritative target and distinguish not-found from inconclusive | Found, lineage, 404, auth/network/5xx, malformed, collision, and cancellation cases are classified correctly | `use-session-actions/utils.ts` and focused tests | Work item 1 | M |
| 5. Ownership-forced resume | Prevent bare-ID warm cache reuse from crossing profile/source ownership | Forced-cold requests bypass warm activation, retain unrelated background runtimes, and resume with exact owner | `store/session.ts`, `use-route-resume.ts`, `use-session-actions/index.ts`, tests | Work item 4 | M |
| 6. Live/cold restore and persistence effects | Coordinate exact-scope restoration, durable blank semantics, cancellation, retries, and write barriers | Full integration matrix passes without changing splash, route, list, or gateway-switch behavior | `use-desktop-integrations.ts`, `wiring.tsx`, integration tests | Work items 1–5 | L |
| 7. Regression and manual validation | Prove no isolation, cache, prompt, or switch regressions | Focused suites, Desktop typecheck/lint/full tests, and manual profile/fleet scenarios pass | Desktop package | Work items 1–6 | M |

## Current-State Analysis

### State ownership and existing reusable seams

- **Renderer navigation and blank-draft presentation**
  - React Router owns the visible path.
  - `startFreshSessionDraft()` in `app/session/hooks/use-session-actions/index.ts` clears selected/runtime IDs, transcript and view state, navigates to `NEW_CHAT_ROUTE`, and marks `$freshDraftReady`.
  - `$freshSessionRequest` in `store/profile.ts` is only a numeric edge; it does not say why the reset was requested.

- **Backend session truth**
  - Session rows and transcripts come from the backend.
  - `useSessionListActions.refreshSessions()` publishes guarded foreground list snapshots.
  - `resolveStoredSession()` performs cache/by-ID resolution, but its current explicit-owner path collapses all errors to `undefined` and therefore cannot distinguish authoritative not-found from transient failure.

- **Gateway/profile identity**
  - `$activeGatewayProfile`, `$activeConnectionId`, `$connection`, `gatewayActivationEpoch()`, and `activeConnectionScopeSuffix()` collectively identify the active route and persistence namespace.
  - Profile names alone are insufficient because two connections can both expose `default`.
  - A resolved socket/profile is not sufficient if the connection descriptor failed and connection-scoped storage still points at the previous suffix.

- **Remembered navigation**
  - `get/setRememberedSessionId()` and `get/setRememberedRoute()` in `store/session.ts` use keys scoped by normalized profile plus the active connection suffix.
  - Legacy global keys are discarded rather than migrated.
  - `sessionBelongsToProfile()` supports durable ID and lineage-root matches but validates only profile ownership.
  - `useDesktopIntegrations()` owns both the cold-start read and all subsequent writes.

- **Route resume**
  - `useRouteResume()` converts a routed durable ID into `resumeSession()`.
  - It already prevents re-resuming the outgoing session while a fresh `/` transition is pending.
  - `requestSessionResume()` provides a monotonic explicit-resume request and can carry an exact `SessionOwnerRoute`.
  - `resumeSession()` uses owner-aware backend routing, but its warm runtime map is keyed only by stored session ID. Its cache validation proves stored-ID equality, not connection/profile equality.

These seams should be extended rather than replaced. No backend, Electron, splash, or general session-list architecture change is required.

### Same-source profile switch: current sequence

Entry points include profile rail squares, condensed profile dropdown, default-profile pill, hotkeys, and profile groups in `workspace-group.tsx`. They all converge on `selectProfile()`.

1. `selectProfile(target)` synchronously:
   - Normalizes the target.
   - Decides whether this is a real switch:
     - target differs from `$activeGatewayProfile`, or
     - the user is returning from All Profiles.
   - Leaves All Profiles.
   - Seeds `$newChatProfile`, clears `$newChatRoute`, and captures the source used for the new draft.
   - Calls targetless `requestFreshSession()` for a real switch.

2. Wiring observes the numeric edge in a passive effect and calls `startFreshSessionDraft()`:
   - The outgoing transcript disappears.
   - Route becomes `/`.
   - Active and selected IDs become `null`.
   - Messages are cleared.
   - `$freshDraftReady` becomes true.

3. Independently, `activateOnCurrentSource()` asynchronously activates:
   - The legacy profile-only door for primary/explicit-local selection.
   - The exact registry `(connectionId, profile)` door for a remote source.

4. Activation publishes the active gateway route and connection descriptor. The descriptor lookup is intentionally best-effort; failure may leave the previous descriptor and therefore the previous storage suffix.

5. `useDesktopIntegrations()` sees the new profile/scope, but `restoredRef.current` is already true from cold boot. It skips all remembered-state reads.

6. Its persistence branch sees `/` as a non-overlay, non-session route and writes it under whichever profile/connection suffix is active at that render.

There is no strict wall-clock ordering between step 2 and activation publication because the reset consumer is a React passive effect. The normal converged state is nevertheless target profile + `/` + empty draft.

### Same-source failure and overlap behavior

- Activation failure occurs after the fresh request, so the previous scope can remain visible but blank.
- Rapid B→C selection can leave B’s activation or descriptor work settling after C’s intent unless a separate latest-only restore generation is used.
- A same-target retap outside All Profiles does not reset and must continue to be a no-op.
- Returning from All Profiles is intentionally a real context re-entry and should restore the concrete profile.

### Cross-connection/fleet switch: current sequence

Fleet squares call `selectConnection(connectionId, { profile })`.

1. `selectConnection()` derives exact `connectionId::profile` identity and increments `switchRevision`.
2. **Dial phase:** `openGatewayAgent()` opens the target without changing the foreground.
   - Failure or timeout here preserves the outgoing transcript and lists.
3. **Commit phase:** inside serialized gateway activation:
   - `beginGatewaySwitch()` raises the barrier.
   - The registered machine-context reset runs.
   - Gateway-bound session lists, selected/runtime IDs, messages, cached transcript tails, and related state are wiped.
   - Only then is the target socket activated and its descriptor published.
4. Exact `targetIsActive()` verification determines whether the switch landed.
5. The barrier is lowered as soon as commit settles.
6. Only the latest winning transaction:
   - Remembers the connection.
   - Leaves All Profiles for a user switch.
   - Seeds the target new-chat owner.
   - Calls `requestFreshSession()`.
   - Refreshes profile data.

### Fleet failure distinctions

- **Pre-wipe dial failure:** old foreground remains intact; no blank and no restore should occur.
- **Superseded queued commit:** no wipe, activation, reset, or restore from the superseded request.
- **Post-wipe activation failure:** the old source is repainted, then intentionally left on a recovery blank.
- **Activation timeout after target publication:** treated as a successful fail-open commit because exact target identity already landed; this case should restore the target conversation.
- **Boot-time source restoration:** is not a user live-switch request and must remain under cold-start restoration, not start a second live restore transaction.

### Root cause

The defect is a scope/lifetime mismatch:

- Remembered conversation data belongs to `(connection persistence suffix, normalized profile)`.
- Cold restoration readiness is represented by a window-lifetime boolean.
- `/` represents both a deliberate automatic isolation transition and an ordinary persistable route.

Because live switches do not reload, the target scope is never read. Once the target scope and `/` become visible together, `/` is synchronously persisted and can replace the target’s remembered route.

Backend session availability, splash rendering, and route resume are downstream effects, not the initiating cause.

### Hard constraints and invariants

1. The outgoing transcript must clear before another profile becomes usable in the foreground.
2. Automatic restoration may occur only for the latest explicit profile/source switch.
3. Exact identity means:
   - normalized profile;
   - connection route identity when present;
   - connection descriptor/storage suffix;
   - gateway activation epoch.
4. An automatic switch-generated `/` must never become a durable blank preference.
5. An explicit blank New Chat must durably prevent an older conversation from returning on a later switch or restart.
6. Live restore is conversation-only. It must not restore `/skills`, `/artifacts`, contributed pages, or overlays.
7. Cold start retains its existing valid non-overlay page restoration policy.
8. List emptiness, `$sessionsLoading`, `renderer.ready`, and activation promise fulfillment are not live-target readiness proofs.
9. A capped sidebar list cannot prove that an older remembered session was deleted.
10. Duplicate stored IDs across connections/profiles must fail closed.
11. Stale generations may finish network work but may not navigate, clear persistence, or release a newer transaction.
12. Restoration must use established resume semantics; it must not create a session, submit a prompt, rebuild prompt context as a new chat, or overwrite model/prompt choices independently of normal resume behavior.

## Detailed Design

### Scope: targeted transaction, not a broader switch refactor

Implement a small renderer navigation transaction shared by the two existing switch producers. Do not merge `selectProfile()` and `selectConnection()`:

- Same-source selection isolates before asynchronous activation and keeps background profile sockets/lists.
- Fleet switching dials first, atomically wipes at commit, and has materially different recovery rules.

A universal context-switch framework would obscure these distinctions. The shared abstraction should cover only restore intent, draft provenance, latest-generation ownership, and completion/cancellation.

### New profile-conversation restore coordinator

### Location and ownership

Add `apps/desktop/src/store/profile-conversation-restore.ts`.

This is a renderer-local Nanostores coordination module. It owns no backend truth and no persisted data. `selectProfile()` and `selectConnection()` produce transactions; wiring applies fresh drafts; `useDesktopIntegrations()` consumes and completes restoration.

### Types

Use explicit closed variants similar to:

```ts
type RestoreOrigin = 'profile-switch' | 'connection-switch'

interface RequestedRestoreTarget {
  connectionId: string | null
  profile: string
}

type ProfileConversationRestoreRequest =
  | {
      phase: 'activating'
      sequence: number
      origin: RestoreOrigin
      target: RequestedRestoreTarget
    }
  | {
      phase: 'committed'
      sequence: number
      origin: RestoreOrigin
      target: RequestedRestoreTarget
    }
  | {
      phase: 'navigating'
      sequence: number
      origin: RestoreOrigin
      target: RequestedRestoreTarget
      sessionId: string
    }
```

The coordinator owns:

- A monotonically increasing sequence.
- `$profileConversationRestore`, containing only the latest live request or `null`.
- `$appliedFreshDraftProvenance`, describing the reset currently responsible for the visible blank draft.

Fresh-draft provenance should be:

```ts
type AppliedFreshDraftProvenance =
  | {
      kind: 'automatic'
      cause:
        | 'profile-switch'
        | 'connection-switch'
        | 'switch-recovery'
        | 'context-recovery'
        | 'gateway-transition'
        | 'boot-transition'
      freshSequence: number
      restoreSequence?: number
    }
  | {
      kind: 'explicit'
      cause:
        | 'new-chat'
        | 'new-chat-in-profile'
        | 'new-chat-in-agent'
        | 'close-chat'
        | 'new-project-chat'
        | 'message-on-automatic-draft'
      freshSequence: number
    }
```

The provenance describes the currently applied `/` draft, not merely a requested reset. Wiring must set it immediately before calling `startFreshSessionDraft()`.

### Interfaces

Provide internal functions with these contracts:

- `beginProfileConversationRestore(origin, target) -> sequence`
  - Normalizes profile and trims connection ID.
  - Atomically supersedes the previous request.
  - Returns the new sequence.

- `commitProfileConversationRestore(sequence) -> boolean`
  - Changes only the matching latest request from `activating` to `committed`.
  - Returns false for stale/canceled requests.

- `markProfileConversationRestoreNavigating(sequence, sessionId) -> boolean`
  - Marks the route change as coordinator-owned before calling `navigate`.
  - Prevents the subsequent pathname effect from treating the automatic route as user cancellation.

- `completeProfileConversationRestore(sequence) -> void`
  - Clears only the matching latest request.

- `cancelProfileConversationRestore(sequence?, reason) -> void`
  - With a sequence, clears only that request.
  - Without a sequence, cancels the current request for an explicit user/context action.
  - Cancellation does not turn an automatic blank into an explicit one.

- `isCurrentProfileConversationRestore(sequence) -> boolean`
  - Used after every asynchronous boundary.

- `applyFreshDraftProvenance(intent)`, `clearAppliedFreshDraftProvenance()`
  - `startFreshSessionDraft()` calls the apply function synchronously at the reset choke point; integrations clear it after restoration or superseding navigation.

- `_resetProfileConversationRestoreForTests()`
  - Resets counters and atoms.

All calls are synchronous; asynchronous work remains in selectors and the integration hook.

### Typed fresh-session intent and applied provenance

Retain `$freshSessionRequest` as the compatibility edge, and add `$freshSessionIntent` with the same sequence:

```ts
interface FreshSessionIntent {
  sequence: number
  persistence: 'automatic' | 'explicit'
  cause: AppliedFreshDraftProvenance['cause']
  restoreSequence?: number
}

requestFreshSession(intent: Omit<FreshSessionIntent, 'sequence'>): void
```

Keep the argument mandatory for every `requestFreshSession()` producer. The function cancels a pending restore when the intent is explicit or is unrelated automatic recovery, increments the numeric edge, and publishes the correlated intent.

**Applied provenance must be classified at the real choke point, not only in wiring.** Extend `FreshSessionDraftOptions` so every `startFreshSessionDraft()` call supplies a required `intent` (or an equivalently required closed `provenance` field). At the start of `startFreshSessionDraft()`, record `$appliedFreshDraftProvenance` from that intent before route/session state is cleared. Wiring passes `$freshSessionIntent` through for counter-driven resets; direct callers must classify themselves. This compile-time boundary covers the producers that bypass `requestFreshSession()`:

- Cmd-N/keybind wiring (`apps/desktop/src/app/contrib/wiring.tsx:924-930`);
- `/new` (`apps/desktop/src/app/contrib/hooks/use-prompt-actions/slash.ts:494-496`);
- Quick Entry (`apps/desktop/src/app/contrib/wiring.tsx:671`);
- voice `start_new_session` (`apps/desktop/src/app/contrib/wiring.tsx:767-769`);
- gateway boot/connection wipe (`apps/desktop/src/app/contrib/wiring.tsx:781-786`);
- direct sidebar/workspace/session-action callers.

This replaces the weaker timing rule that wiring alone records provenance immediately before reset. Future direct callers cannot create an unclassified blank. `clearAppliedFreshDraftProvenance()` remains coordinator-owned when a route is restored, explicit navigation supersedes the draft, or the window tears down.

### Switch-producer behavior

### `selectProfile()`

For a real switch:

1. Capture the source that `activateOnCurrentSource()` will dial.
2. Begin a restore request before `requestFreshSession()`.
3. Issue an automatic fresh intent:
   - cause `profile-switch`;
   - persistence `automatic`;
   - matching restore sequence.
4. Start activation.
5. Attach commit/cancel directly to the `activateOnCurrentSource(target)` promise: commit immediately on activation success and cancel only on activation rejection.
   - Do not wait for `isLocalDesktopProfile()` or the startup-profile `remember()` IPC.
   - Do not attach cancellation to the current conflated tail `.catch()`: a `remember()` IPC failure after successful activation must not cancel an already committed restore.
6. Surface activation and persistence errors through their existing UX without changing restore authority after a successful activation.
   - The previous scope may remain on an automatic blank.
   - That blank must remain non-durable.

Refactor the current `Promise.all([activation, shouldRememberStartupProfile])` chain so restore commitment follows activation itself. Startup-profile `remember()` remains best-effort bookkeeping after successful activation and does not gate restore.

Do not begin restoration for:

- A same-profile retap outside All Profiles.
- `newSessionInProfile()`.
- `newSessionInAgent()`.

Returning from All Profiles to a concrete profile is a real switch and should begin/commit restoration even when the gateway is already active.

### `selectConnection()`

Extend options additively:

```ts
interface SelectConnectionOptions {
  profile?: null | string
  initiator?: 'boot' | 'user'
}
```

Default `initiator` to `user`. Make it the single source of truth for boot versus user behavior: `initializeConnectionsRegistry()` passes `initiator: 'boot'`, and the existing `restoreOnBoot` inference is replaced rather than allowed to disagree or misclassify a user's first click after a failed/missing boot adoption.

For a user switch:

1. Begin the restore request before the dial phase, using the exact requested `connectionId` and target profile.
2. Do not apply a fresh draft during phase-one dial.
3. On revision supersession, leave the older request replaced by the newer generation.
4. After commit and `targetIsActive()` verification:
   - Commit the matching restore sequence.
   - Issue automatic `connection-switch` fresh intent with that sequence.
   - Continue existing remember/profile refresh bookkeeping.
5. A timeout with `targetIsActive() === true` follows this successful path.
6. On pre-wipe failure:
   - Cancel the request.
   - Do not reset or alter draft provenance.
7. On post-wipe non-landed failure:
   - Cancel the target restore.
   - Run current repaint recovery.
   - Issue automatic `switch-recovery` fresh intent with no restore sequence.
8. A queued superseded switch that never commits emits no fresh draft and no restore.
9. Handle the already-active early-return branch (`connections.ts:305-328`) explicitly: when a user exits All Profiles onto the already-active exact pair, begin and commit the restore synchronously, then issue the correlated automatic `connection-switch` draft; there is no dial/barrier to await.

For `initiator: 'boot'`, do not begin a live restore transaction. Any reset caused by the source adoption is automatic/non-durable, and cold-start restore remains the sole reader.

### All Profiles

`setShowAllProfiles(true)` must cancel any pending live restore synchronously because there is no longer one exact foreground profile owner. It does not create or persist a blank.

`setShowAllProfiles(false)` alone does not restore. Concrete re-entry through `selectProfile()` or `selectConnection()` owns restoration.

### Exact settled identity

Add a controller-facing snapshot type:

```ts
interface ConversationRestoreScope {
  activationEpoch: number
  connectionId: string | null
  gatewayScope: string
  profile: string
  storageSuffix: string
}
```

Wiring derives it from:

- `$activeConnectionId`;
- normalized `$activeGatewayProfile`;
- `gatewayActivationEpoch()`;
- `activeConnectionScopeSuffix()`;
- the existing `gatewayScope`.

The integration hook may begin a committed restore only when:

1. Request sequence is still latest.
2. Request profile equals snapshot profile.
3. For an explicit registry source, request connection ID equals snapshot connection ID.
4. `$connection` describes the same normalized profile and expected registry source.
5. Storage suffix is the suffix derived from that descriptor, not a retained prior descriptor.
6. Gateway state is open.
7. Activation epoch is unchanged from scope capture.
8. Matching automatic draft provenance has been applied.
9. Route is `/`.
10. Active and selected session IDs are null.
11. No session creation or newer user intent is underway.

For profile-only activation, a null requested connection ID means “legacy/profile door,” not a wildcard. The implementation must validate the published descriptor/profile combination before reading storage. If descriptor lookup failed and the old suffix remains active, identity is inconclusive: do not read or write either scope and do not navigate.

**Implementation gate:** first verify how shared-primary remote profile routes stamp `$connection.profile` with a focused real-activation mock test, then finalize the exact descriptor predicate from that evidence. If the descriptor intentionally exposes a backend `targetProfile`, compare against the Desktop profile using the same route/target mapping already represented by `SessionOwnerRoute`; do not weaken the check to bare profile equality.

### Durable remembered-conversation state

### New schema

Add a new connection/profile-scoped key:

`hermes.desktop.lastConversation.profile.<encoded-profile><connection-suffix>`

The JSON value is a versioned tagged record:

```ts
type RememberedConversation =
  | { version: 1; kind: 'blank' }
  | { version: 1; kind: 'session'; sessionId: string }
```

Storage absence means no durable preference. Malformed/unknown-version values are ignored and fall back to the existing keys.

### API

Add to `store/session.ts`:

- `getRememberedConversation(profile) -> RememberedConversation | null`
- `setRememberedConversation(value, profile) -> void`
- `clearRememberedConversationIfSession(profile, sessionId) -> void`

Continue exposing `get/setRememberedSessionId()` and `get/setRememberedRoute()` for compatibility.

### Compatibility ladder

When no valid new record exists:

1. A session-shaped remembered route is the first legacy conversation candidate.
2. Existing `lastSessionId` is the fallback candidate.
3. `/` is **not** interpreted as an old blank sentinel because current versions wrote it during automatic transitions.
4. After a legacy session candidate is authoritatively validated, write the new session record.
5. After an authoritative not-found, clear the matching legacy candidate instead of migrating it.

### Write semantics

#### Valid session route

Write the new session record only from an existing ownership proof; do not add an RPC to every persistence effect. Accept either (a) a successful exact scoped restore/resume result for that route, or (b) the current owned-session row/owner-route evidence already used by normal routed-session persistence. If an aged-off open session lacks current list evidence, retain its previous durable record until the next authoritative resume rather than clearing or rewriting it.

After that proof:

- Write `{ kind: 'session', sessionId, version: 1 }`.
- Dual-write `lastSessionId = sessionId`.
- Write `lastRoute = current session route`.

Use the durable stored/lineage ID already used by routing; do not replace it merely because lookup returned a compressed tip.

#### Explicit blank

Once an explicit fresh intent has actually applied and the renderer is on `/` with no selected/runtime session:

- Write `{ kind: 'blank', version: 1 }`.
- Clear `lastSessionId`.
- Write `lastRoute = '/'`.

Order the compatibility writes so rollback code cannot resurrect the old session: clear the old ID and write `/` before finalizing the new blank record.

#### Automatic blank

For profile isolation, connection isolation, switch recovery, or context recovery:

- Do not write `{ kind: 'blank' }`.
- Do not write `/`.
- Do not clear a remembered session.
- Keep this rule after the restore transaction is canceled or exhausts retries; automatic provenance remains until the route changes or a later explicit blank adopts it.

#### Non-session route

- Continue persisting valid non-overlay routes for cold start.
- Do not change the remembered-conversation record.
- Therefore:
  - Cold start may still restore `/skills`.
  - A live profile switch ignores `/skills` and restores the remembered conversation.
  - If the conversation record is blank, live switching remains blank.

#### Stale/deleted session

Only after authoritative exact-target not-found:

- Remove the matching new session record.
- Clear matching `lastSessionId`.
- Clear `lastRoute` only if it is a session route for the same durable ID.
- Leave an unrelated non-session route intact.
- Do not replace stale state with a blank tombstone; stale deletion means absence, not explicit user preference.

### Old/new version behavior

- **New code reading old scoped keys:** falls back as above; validates before migrating.
- **Old code reading new writes after rollback:**
  - Session writes are dual-written to existing keys.
  - Blank writes clear the old session ID and write `/`, so old code also remains blank.
  - Old code ignores the new JSON key safely.
- No migration of legacy global unsuffixed keys is introduced.

### Ownership-safe lookup

Add `resolveStoredSessionForRestore()` beside `resolveStoredSession()` in `use-session-actions/utils.ts`.

### Input

```ts
interface RestoreLookupTarget {
  connectionId: string | null
  profile: string
  storageSuffix: string
}

type RestoreLookupResult =
  | { status: 'found'; session: SessionInfo; ownerRoute?: SessionOwnerRoute }
  | { status: 'not-found' }
  | { status: 'inconclusive'; reason: string }
```

The helper must always perform an authoritative scoped by-ID request for automatic/cold restoration. It must not accept a matching bare-ID cache entry as proof.

- Registry target: call by ID with exact `{ connectionId, profile }`.
- Legacy/profile-only target: call by ID through the already-proven active target profile.
- Stamp returned rows with Desktop profile and connection ownership using the same policy as `resolveStoredSession()`, then upsert them into `$sessions`.
- Preserve the candidate stored ID for lineage routing.

### Result classification

- `found` only when:
  - Returned ID or lineage root matches the requested durable ID.
  - Returned/derived profile matches the target.
  - Any connection identity matches the target.
- `not-found` only for an explicit target-scoped 404/session-gone result.
- `inconclusive` for:
  - Timeout or abort.
  - Connectivity failure.
  - 401/403 or other auth failure.
  - 5xx.
  - Missing compatibility capability.
  - Malformed result.
  - Conflicting profile/connection ownership.
  - Response that does not match the requested ID/lineage.

Reuse the repository’s existing session-gone/error classifier rather than introducing a second interpretation of backend errors. If that classifier currently lives inside `use-session-actions/index.ts`, move only the pure classification helper to the existing error utility module or the smallest shared location.

### Retry policy

For `inconclusive` results:

- Retry at most two additional times while all transaction guards remain true.
- Use bounded delays of 500 ms and 1,000 ms.
- Drive these delays with Vitest fake timers in unit/integration tests; assert cancellation clears pending timers and exhaustion never requires wall-clock sleeps.
- Abort or logically strand work immediately on cancellation/unmount.
- After exhaustion:
  - Preserve remembered state.
  - Remain on the automatic blank.
  - Complete the transaction so it does not block later work.
  - Log a scoped diagnostic; do not report “session deleted.”
  - The sidebar and switching away/back provide recovery.

Do not use session-list absence as a fallback negative result.

### Restore algorithm in `useDesktopIntegrations()`

Split the current combined effect into three responsibilities:

1. Cold-start restoration.
2. Live-switch conversation restoration.
3. Remembered-state persistence.

Extract shared candidate selection and lookup completion logic so cold/live restoration cannot drift on ownership or stale-clearing policy.

### Live-switch algorithm

Beginning any live restore synchronously supersedes/closes an unfinished cold-start attempt. Cold and live restoration share a controller generation/abort boundary so a fast profile choice during boot cannot produce two navigations. The cold attempt must recheck its captured exact scope and absence of a live transaction after every await, using the same stale-result discipline as live restore.

For each committed sequence:

1. Wait until exact target scope and matching automatic draft provenance are present.
2. Capture scope, activation epoch, transaction sequence, route, and user-intent generation.
3. Read `getRememberedConversation(target.profile)` only after the storage suffix is proven.
4. Interpret state:
   - `blank`: complete and stay on `/`.
   - `session`: use its ID.
   - absent: inspect legacy session-route/last-ID candidates.
   - no candidate: complete and stay on the non-durable automatic blank.
5. Perform `resolveStoredSessionForRestore()`.
6. After every await/retry delay, recheck:
   - transaction sequence;
   - target profile/connection;
   - activation epoch;
   - storage suffix;
   - automatic provenance;
   - route `/`;
   - selected/runtime IDs null;
   - no creating session;
   - no newer navigation/message intent.
7. On `found`:
   - Migrate legacy candidate if necessary.
   - Mark the transaction `navigating`.
   - Call `requestSessionResume(candidateId, ownerRoute, { forceCold: true })`.
   - Navigate to `sessionRoute(candidateId)` with `{ replace: true }`.
   - Let `useRouteResume()` dispatch the established resume.
8. On `not-found`:
   - Clear matching persisted conversation state.
   - Complete without navigation.
9. On exhausted `inconclusive`:
   - Preserve persistence.
   - Complete without navigation.
10. When the matching restored route is observed, clear automatic draft provenance.

### Cold-start algorithm

Preserve existing gates for main vs HUD/browser windows and explicit deep links.

At initial `/` after `profileReady` and exact scope readiness:

1. Read remembered non-overlay route and remembered-conversation state.
2. If the remembered route is valid, non-session, non-overlay, **and not `NEW_CHAT_ROUTE`**, restore it as today.
3. If conversation state is `blank`, finish cold restoration at `/`; do not fall back to old session keys.
4. Otherwise choose the session candidate using the compatibility ladder.
5. Validate it through the same exact by-ID resolver and result policy as live restore.
6. Use forced-cold explicit resume plus route replacement on success.
7. Clear only on authoritative not-found.
8. Preserve memory on inconclusive exhaustion.

A non-`/` initial route remains user/deep-link authority and closes the cold latch without restoration.

Because by-ID lookup becomes authoritative, `sessions.length` is no longer the cold session-readiness barrier. The `sessions` input remains necessary for safe ongoing persistence.

### Persistence and user-cancellation behavior

The persistence effect must consult the current coordinator atom synchronously, not rely only on a render-captured prop. This closes the fast-activation window between beginning a transaction and React rerendering.

Suppress all remembered-navigation writes while a live restore is activating, committed, or navigating. This prevents both:

- transitional `/` writes;
- an outgoing same-profile session route being written under a newly active connection before `/` commits.

Cancellation rules:

| Trigger | Restore result | Blank provenance |
|---|---|---|
| Newer profile/source choice | Older sequence superseded | New switch owns its own provenance |
| Explicit New Chat / profile new-chat / agent new-chat | Cancel immediately | Explicit; persist blank after reset applies |
| Close lone main chat | Cancel immediately | Explicit |
| Path-less project “new session” | Cancel immediately | Explicit |
| Project deletion kicks open session out | Cancel | Automatic context recovery; non-durable |
| Enter All Profiles | Cancel | Existing route retained; no blank write |
| User selects another chat | Cancel before/at selection; pathname effect is backup | Route/session persistence follows selected chat |
| User navigates to a page/deep link | Cancel | Page persists normally if non-overlay |
| Message submission on automatic blank | Cancel and promote draft to explicit before create | Persist blank until created session supersedes it |
| Activation failure | Cancel matching sequence | Automatic blank remains non-durable |
| Component teardown | Abort/logically strand work | No persistence mutation |

The route observer must recognize a `navigating` transaction whose pathname equals its target session route and must not cancel it as user navigation.

### Gateway wipe, streaming, and submit interactions

- The coordinator and applied-provenance atoms are renderer navigation intent and must **not** be registered in the gateway-bound reset lifecycle. Add a contract test that `beginGatewaySwitch()`/machine-context wipe does not erase the pending restore generation.
- A fleet commit can apply two resets: the `beforeConnectionSwitch` preserve-route reset and the winning post-commit correlated `/` reset. Classify the former as automatic `gateway-transition` with no restore sequence; it must not replace/cancel the pending transaction. The later correlated `connection-switch` reset becomes the restore-owning provenance. Pin the ordering with a fleet integration test.
- Switching while the outgoing session is streaming must preserve existing stale callback/request guards: late deltas or completion from the outgoing runtime may update background-owned state but may not repaint the cleared foreground or cancel/navigate the target restore. Add a regression test using a deferred stream completion.
- Quick Entry and voice are explicit user attempts to start a new conversation. Promote their draft to durable explicit blank before submission; if submission fails, the blank remains durable. A successful create/resume replaces it with the new session record.
- Message submission cancellation/promotion is owned at the shared prompt/composer submission boundary (`use-prompt-actions` / composer actions), before dispatching create/send. Session creation keeps a defensive cancellation check in `use-session-actions`; wiring should not guess ownership with an optional callback.

### Forced-cold resume and runtime cache safety

Extend resume request and resume interfaces additively:

### Before

```ts
requestSessionResume(sessionId, ownerRoute?)
resumeSession(sessionId, replaceRoute?, capturedOwner?)
```

### After

```ts
requestSessionResume(sessionId, ownerRoute?, options?: { forceCold?: boolean })
resumeSession(sessionId, replaceRoute?, capturedOwner?, options?: { forceCold?: boolean })
```

Add `forceCold?: boolean` to `SessionResumeRequest`.

`useRouteResume()` forwards this option only for the matching monotonic request.

When `forceCold` is true, `resumeSession()`:

- Skips both warm-cache checks for that invocation.
- Does not call `session.activate` using a bare-ID cached runtime.
- Continues through exact metadata resolution and `session.resume`.
- Uses the captured exact owner route when available.
- Replaces the bare-ID runtime mapping with the newly resumed target runtime when normal resume publication completes.
- Does not delete unrelated background runtime state merely because a colliding stored ID exists elsewhere.

This is narrower than converting all runtime caches to composite owner keys, while making automatic restoration safe. A full composite-cache refactor should remain separate.

Normal sidebar clicks retain their existing warm behavior unless they carry the forced-cold restore request.

### Automatic-versus-explicit reset classification

| Existing entry point | Classification | Live restore? | Durable blank? |
|---|---|---:|---:|
| Real `selectProfile()` switch | Automatic profile isolation | Yes | No |
| Return from All Profiles via `selectProfile()` | Automatic profile isolation | Yes | No |
| Same-target `selectProfile()` retap | No reset | No | No change |
| Winning user `selectConnection()` commit | Automatic connection isolation | Yes | No |
| Boot `selectConnection()` adoption / pre-commit gateway wipe | Automatic boot or gateway transition | Cold restore only | No |
| Fleet pre-wipe failure | No reset | No | No change |
| Fleet post-wipe recovery | Automatic switch recovery | No | No |
| Fleet landed timeout | Successful automatic connection isolation | Yes | No |
| `newSessionInProfile()` | Explicit new chat | No | Yes |
| `newSessionInAgent()` | Explicit new chat | No | Yes |
| Generic Cmd-N, `/new`, Quick Entry, or voice new-chat action | Explicit new chat | No | Yes, including submit failure |
| Close lone loaded main chat | Explicit close-to-blank | No | Yes |
| Path-less `goToProject(..., {newSession:true})` | Explicit project new chat | No | Yes |
| Project deletion kicks open chat out | Automatic context recovery | No | No |
| Enter All Profiles | Context cancellation only | No | No |
| User submits on automatic blank | Explicit adoption of blank | No | Yes until session creation succeeds |
| User selects another session during restore | User navigation | Cancel | Selected session replaces memory |
| User navigates to page during restore | User navigation | Cancel | Conversation unchanged |

`workspace-group.tsx` needs no direct provenance change because `newSessionInProfile()` owns its explicit classification centrally.

## File-by-File Impact

## New files

### `apps/desktop/src/store/profile-conversation-restore.ts`

- Add restore request, sequence, draft-provenance types and atoms.
- Add begin/commit/navigating/complete/cancel/current-check functions.
- Add test reset helper.
- Own latest-only coordination; no persistence or backend calls.

### `apps/desktop/src/store/profile-conversation-restore.test.ts`

Cover:

- Monotonic generations.
- Stale commit/cancel no-ops.
- New begin superseding old.
- Automatic vs explicit applied provenance.
- Navigating-state route ownership.
- Test reset semantics.

### `apps/desktop/src/app/session/hooks/use-session-actions/restore-resume.test.tsx`

Add a focused resume harness for:

- `forceCold` bypassing a bare-ID warm runtime.
- Same stored ID cached for another owner.
- Exact owner route passed to `session.resume`.
- Background cached runtime state not deleted solely by forced-cold bypass.
- Newly resumed runtime replacing the stored-ID mapping.

If an existing session-actions hook test file already provides the necessary harness, place these cases there instead; do not create two competing harnesses.

## Modified production files

### `apps/desktop/src/store/profile.ts`

- Add `$freshSessionIntent` and typed `requestFreshSession(intent)`.
- Keep numeric `$freshSessionRequest` as a compatibility edge.
- Update `selectProfile()` to begin restore before isolation, commit immediately after activation success, and cancel on failure.
- Separate activation completion from startup-profile persistence.
- Classify:
  - `newSessionInProfile()` and `newSessionInAgent()` as explicit.
  - `setShowAllProfiles(true)` as restore cancellation.
- Update every module-internal fresh request with an explicit cause.

Depends on the coordinator module.

### `apps/desktop/src/store/connections.ts`

- Add `SelectConnectionOptions.initiator`.
- Have boot initialization pass `initiator: 'boot'`.
- Begin user restore before dial.
- Commit only after latest revision and exact target verification.
- Emit automatic connection isolation on successful/landed-timeout commit.
- Cancel on pre-wipe and post-wipe failure.
- Emit automatic recovery blank after post-wipe failure.
- Preserve existing barrier, timeout, repaint, and revision behavior.

Depends on typed fresh intents and the coordinator.

### `apps/desktop/src/store/session.ts`

- Add versioned remembered-conversation type and profile/connection-scoped key.
- Add read/write/conditional-clear APIs.
- Add compatibility fallback helpers without changing legacy-global discard.
- Extend `SessionResumeRequest` and `requestSessionResume()` with `forceCold`.
- Keep existing remembered route/session APIs for rollback compatibility.

Persistence changes can land independently before the live coordinator begins using them.

### `apps/desktop/src/app/contrib/wiring.tsx`

- Observe `$freshSessionIntent` instead of deriving behavior from the bare numeric edge.
- Pass the typed intent into `startFreshSessionDraft()` for counter-driven resets; the session-action choke point records applied provenance.
- Classify direct keybind, Quick Entry, voice, and boot reset calls explicitly.
- Pass the exact restore scope, current transaction, gateway state, active/selected IDs, and creation-state guard to `useDesktopIntegrations()`.
- Continue using existing `gatewayScope`.
- Add synchronous cancellation at the main chat/session selection boundary.
- Keep message/create promotion in the shared prompt/composer submission owner, not an optional wiring callback.
- Do not change model/config refresh behavior or the immediate reset.

Depends on profile/session/coordinator interfaces.

### `apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts`

- Replace the single combined restore/write effect with cold restore, live restore, and persistence effects.
- Extract shared conversation candidate and outcome handling.
- Add latest-generation, exact-scope, activation-epoch, pathname, provenance, and selected/runtime guards.
- Add bounded retry and cleanup/abort handling.
- Suppress writes while a transaction is active.
- Persist explicit blanks using the new tombstone.
- Keep non-overlay page persistence and main/HUD/browser window restrictions.
- Before native/deep-link navigation, cancel a pending automatic restore because those events are explicit external user intent.

Depends on scope inputs, resolver result type, and new storage APIs.

### `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts`

- Add authoritative `resolveStoredSessionForRestore()`.
- Bypass bare-ID cache proof.
- Return `found` / `not-found` / `inconclusive`.
- Validate exact target identity and lineage.
- Reuse existing row upsert logic.
- Accept cancellation signal if the underlying `getSession()` API supports it; otherwise rely on transaction guards and bounded timeout.

### `apps/desktop/src/app/session/hooks/use-session-actions/index.ts`

- Extend `resumeSession()` with `forceCold`.
- Gate both `takeWarmCache()` uses on `forceCold`.
- Preserve owner-routed `session.resume`, transcript prefetch, model/runtime hydration, retry, and prompt semantics.
- Extend `FreshSessionDraftOptions` with required typed intent and record applied provenance inside `startFreshSessionDraft()` before clearing state.
- At session creation start, defensively cancel pending restoration; prompt/composer submission owns the primary explicit promotion before dispatch.

Depends on extended resume types/coordinator.

### `apps/desktop/src/app/session/hooks/use-route-resume.ts`

- Forward `sessionResumeRequest.forceCold` to `resumeSession()`.
- Preserve the existing pathname, fresh-draft, reconnect, and stale request guards.
- Do not alter retry counts or gateway-open logic.

### `apps/desktop/src/app/session/hooks/use-prompt-actions/` and `apps/desktop/src/app/chat/hooks/use-composer-actions.ts`

- Classify `/new` as explicit when calling `startFreshSessionDraft()`.
- At the common prompt submission boundary, cancel pending restoration and promote an automatic blank to explicit before create/send; Quick Entry and voice therefore remain durably blank if submission fails.
- Add focused tests for `/new`, normal composer send, Quick Entry, voice, and failed submission.

### `apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts` / wiring boot callback

- Classify preserve-route machine-context resets as automatic `gateway-transition` or `boot-transition`.
- Ensure they neither persist a blank nor replace the pending correlated restore provenance.

### `apps/desktop/src/app/chat/close-tab.ts`

- Classify the lone-loaded-main fallback as explicit `close-chat`.
- Promotion of a stacked session remains unchanged and must not write a blank.

### `apps/desktop/src/store/projects.ts`

- Classify path-less `goToProject(..., { newSession: true })` as explicit `new-project-chat`.
- Classify project-deletion kick-to-intro as automatic `context-recovery`.

## Modified test files

### `apps/desktop/src/app/contrib/hooks/use-desktop-integrations.test.tsx`

Expand the harness with exact scope, gateway state, selected/runtime IDs, transaction and resolver controls.

Required cases:

- A session → automatic `/` → B scope does not overwrite B memory.
- B restore waits for exact committed scope.
- B restore succeeds through scoped by-ID lookup even when sidebar rows are empty.
- Remembered blank stays blank.
- Explicit blank writes tombstone, clears old ID, and survives remount.
- Automatic blank writes nothing.
- Rapid B→C: late B found/not-found/inconclusive results cannot navigate or clear.
- Same profile on two connection suffixes restores only its matching conversation.
- Authoritative not-found clears matching state.
- Timeout/auth/5xx preserves state.
- Wrong-owner/conflicting response is inconclusive and never navigates.
- User session/page navigation cancels.
- Message submission, `/new`, Quick Entry, and voice promote automatic blank to explicit; failed submission retains the tombstone.
- Fast live switch while cold restore is unresolved permits only the live generation to navigate.
- Deferred outgoing stream completion cannot repaint or redirect the target.
- Cold non-session page restore remains intact and remembered `/` does not short-circuit conversation state.
- Cold blank prevents legacy session fallback.
- HUD/browser windows neither restore nor persist.

### `apps/desktop/src/store/profile-select-source.test.ts`

Add assertions that:

- Restore begins before automatic isolation.
- Request commits only after activation success.
- Activation failure cancels.
- Rapid selections only leave the latest committed request.
- Returning from All Profiles restores.
- Same-profile retap does not.
- `newSessionInProfile()` is explicit and produces no restore.
- `newSessionInAgent()` gets equivalent coverage in this file or the coordinator test.

### `apps/desktop/src/store/connections.test.ts`

Update fresh-request mocks to inspect typed intent. Add:

- User successful switch begins and commits one restore.
- Boot restore starts no live transaction.
- Pre-wipe failure cancels without blank/provenance.
- Queued superseded switch cannot commit restore.
- Post-wipe failure emits automatic recovery blank, not target restore.
- Landed activation timeout commits restore.
- Rapid source/profile choices leave only latest exact target.
- Returning from All Profiles on the already-active pair begins/commits synchronously and restores.
- `initiator: 'boot'` is the sole boot signal, including the user's first click after no boot adoption.
- The preserve-route gateway-transition reset survives the wipe and is superseded by the winning correlated reset without canceling restore.

### `apps/desktop/src/store/session.test.ts`

Add:

- JSON session/blank/absence round trips.
- Malformed and unknown-version fallback.
- Old scoped session/route candidate compatibility.
- `/` alone is not migrated as a blank.
- Explicit blank clears old session compatibility key.
- Session dual-write rollback behavior.
- Conditional stale clear affects only the matching ID.
- Lineage-root candidate remains durable.
- Profile encoding remains identical.

### `apps/desktop/src/store/layout-connection-scope.test.ts`

Add connection-scope assertions for:

- Remembered route.
- Remembered conversation session.
- Remembered blank.
- Same profile name on remote A and B retaining independent values.
- Null connection preserving the active scope.

### `apps/desktop/src/app/session/hooks/use-route-resume.test.tsx`

Add:

- Forced-cold monotonic resume request reaches `resumeSession`.
- Owner route and `forceCold` are forwarded together.
- Superseded forced-cold request is not reused for later pathname navigation.
- Existing “gateway opens before `/` commits” test remains unchanged.

### `apps/desktop/src/app/chat/close-tab.test.ts`

- Assert lone-main close requests explicit blank intent.
- Assert stacked-session promotion still requests no blank.

### `apps/desktop/src/app/session/hooks/use-session-list-actions.test.tsx`

No production API change is required, but add or retain proof that:

- Empty/populated/loading states cannot be treated as target-authoritative.
- Stale profile and same-profile/different-source responses remain rejected.

The restore tests must mock exact by-ID lookup rather than wait on this hook.

## Intentionally unchanged production files

- `app/chat/intro-visibility.ts`: correctly renders the state it receives.
- `app/routes.ts`: `/` and session-route parsing remain correct.
- `store/gateway-switch.ts`: its wipe/barrier/recovery ownership remains unchanged.
- `app/chat/sidebar/profile-switcher.tsx`: already calls the correct same-source and exact fleet selectors.
- `app/chat/sidebar/projects/workspace-group.tsx`: `newSessionInProfile()` centrally owns explicit classification.
- `store/boot.ts`: `renderer.ready` remains cold-start readiness only.
- `app/session/hooks/use-session-list-actions.ts`: restoration no longer infers readiness from lists.
- `store/session-request-router.ts`: existing exact RPC routing contract is reused.

## Risks and Migration

## Breaking/internal API risk

Making fresh intent classification mandatory changes every internal `requestFreshSession()` call. Land the signature and all call-site updates atomically. Keep the numeric atom temporarily to avoid breaking unknown secondary consumers; remove it only in a later cleanup after repository-wide search confirms no readers remain.

## Persistence migration

The new key is additive and lazily populated:

- Existing scoped values continue to work.
- No eager global migration runs.
- Stale legacy data is never canonicalized until validated.
- Dual writes make rollback safe.
- Explicit blank is rollback-safe because old session state is cleared.

## Descriptor ambiguity

A best-effort descriptor failure can leave the old connection suffix active after socket activation. The design deliberately fails closed: it leaves the automatic blank visible and preserves both scopes’ storage rather than risk cross-backend restoration.

Validate shared-primary/remote-override descriptor profile behavior with the real gateway test harness before hard-coding the final predicate.

## Runtime cache collision

Forced-cold restoration removes the immediate hazard without rekeying every runtime cache. Normal non-restore opens retain the existing bare-ID map risk. Do not expand this work into a global composite-key migration unless the new collision test proves the forced-cold bypass cannot be isolated.

## Cross-window writes

LocalStorage is shared across windows and best-effort. Exact connection/profile keys prevent cross-scope bleed, but simultaneous windows on the same exact scope remain last-writer-wins as today. The plan does not introduce a new synchronization policy.

### Implementation discovery gates

Before wiring exact identity, add a focused real-activation harness test for legacy per-profile remote overrides. Determine whether `$connection.profile` carries the Desktop alias or backend target profile; if it differs, validate through the existing `SessionOwnerRoute.targetProfile` mapping rather than weakening to profile-only matching. This is an implementation gate, not an open product question.

Before finalizing the fleet effect ordering, pin whether `beforeConnectionSwitch` fires for the same `selectConnection()` commit and assert the coordinator survives the gateway-bound wipe. The design already handles both resets; the test establishes the concrete ordering.

## Failure and Edge-Case Matrix

| Condition | Navigation | Persistence | Recovery |
|---|---|---|---|
| No remembered conversation | Stay automatic `/` | No write | User may choose chat/new chat |
| Remembered explicit blank | Stay `/` | Preserve blank | Explicit choice honored |
| Exact session found | Replace to durable route | Write/migrate session after validation | Existing resume/retry UI |
| Exact 404/gone | Stay `/` | Clear only matching session memory | Sidebar remains usable |
| Timeout/network/5xx | Stay `/` after bounded retries | Preserve memory | Switch away/back or select row |
| 401/403 | Stay `/`; existing connection auth UX remains authoritative | Preserve memory | Reauthenticate/retry |
| Conflicting owner/duplicate ID | No navigation | Preserve memory | Fail closed |
| Descriptor/storage suffix not settled | No lookup/navigation | No reads or writes in uncertain scope | Bounded wait, then automatic blank |
| Rapid B→C or live switch during unresolved cold restore | Only latest live target may navigate/clear | Older/cold completions no-op | Latest wins |
| User clicks chat while B pending | User chat wins | Persist clicked chat after ownership validation | B canceled |
| User navigates page | Page wins | Persist non-overlay page; conversation unchanged | B canceled |
| User sends, Quick Entry, or voice starts on blank | Old restore canceled | Blank becomes explicit before dispatch; failure keeps it blank | New session later replaces it |
| Fleet pre-wipe failure | Old chat remains | Unchanged | Existing error |
| Fleet post-wipe failure | Old source recovery blank | Automatic blank not persisted | Repaint old source |
| Fleet landed timeout | Target automatic blank, then restore | Target scope only | Treated as committed |
| Outgoing stream completes after switch | No foreground repaint/navigation | Target persistence unchanged | Background ownership/stale guards absorb completion |
| Session compressed | Restore durable/root ID | Keep durable ID | Resolver accepts lineage match |
| Remembered session aged off list | Exact by-ID lookup still finds it | No list-based clearing | Restores normally |

## Rejected Alternatives

1. **Reset `restoredRef` on every profile change**
   - Cannot distinguish explicit New Chat from profile isolation and can read under a half-published connection suffix.

2. **Reset on `$freshSessionRequest`**
   - Generic fresh requests include Cmd-N, close-tab, project actions, and recovery; this would resurrect conversations after deliberate blank choices.

3. **Key restoration only by profile**
   - Same-named profiles across connections would leak sessions.

4. **Wait for `$sessionsLoading === false` or nonempty `$sessions`**
   - Both can describe old-scope rows while a target fetch is still in flight.

5. **Use list absence as deletion proof**
   - The sidebar page is bounded; an aged-out valid session could be cleared.

6. **Delay or remove the isolation reset**
   - Reintroduces the outgoing-transcript leak the current behavior intentionally fixed.

7. **Restore arbitrary remembered routes on live switch**
   - Conflicts with the settled conversation-only behavior and could make background profile activity redirect users to pages.

8. **Trust the bare stored-ID warm runtime map**
   - Duplicate IDs across profiles/sources can bind the wrong runtime.

9. **Merge the profile and connection switch implementations**
   - Their dial, wipe, failure, background-socket, and cancellation contracts are different.

## Implementation Order

1. **Add remembered-conversation storage**
   - Add schema/APIs and session/layout scope tests.
   - Independently compilable; no callers changed yet.

2. **Add the restore coordinator**
   - Add atoms/functions and focused unit tests.
   - Independently compilable.

3. **Introduce typed fresh intents**
   - Add `$freshSessionIntent`.
   - Atomically update every `requestFreshSession()` call site with explicit classification.
   - Update close-tab/project/profile tests.
   - Keep existing numeric atom during migration.

4. **Instrument same-source and fleet producers**
   - Add begin/commit/cancel behavior.
   - Add `SelectConnectionOptions.initiator`.
   - Update profile and connection transaction tests.
   - This step must land atomically with the coordinator imports.

5. **Add authoritative restore lookup**
   - Implement structured outcomes and scoped by-ID behavior.
   - Add lookup tests for found, lineage, 404, transient, owner conflict, and duplicate IDs.

6. **Add forced-cold resume**
   - Extend resume request, route-resume forwarding, and `resumeSession()`.
   - Add cache-collision tests.
   - Independently testable before integrations consume it.

7. **Update wiring**
   - Apply typed fresh provenance.
   - Supply exact scope and cancellation signals.
   - Keep existing isolation/reset and gateway refresh effects intact.

8. **Refactor `useDesktopIntegrations()`**
   - Extract cold/live/persistence responsibilities.
   - Add live transaction, bounded lookup, write barrier, explicit blank, and cancellation behavior.
   - Land with its expanded tests.

9. **Run race/regression suites**
   - Ensure existing route-reset, gateway-switch, source-switch and persistence tests remain green.
   - Confirm no changes to splash or session-list production logic are needed.

10. **Finalize documentation**
    - Update `docs/plans/desktop-profile-last-conversation-restore-2026-08-27.md` with the implemented interfaces and move it to the repository’s completed-plan location only after code lands.

## Verification commands

Run focused Vitest files from `apps/desktop` using the package's npm-managed toolchain:

```bash
npm exec vitest -- run \
  src/store/profile-conversation-restore.test.ts \
  src/store/profile-select-source.test.ts \
  src/store/connections.test.ts \
  src/store/gateway-switch.test.ts \
  src/store/session.test.ts \
  src/store/layout-connection-scope.test.ts \
  src/app/contrib/hooks/use-desktop-integrations.test.tsx \
  src/app/session/hooks/use-route-resume.test.tsx \
  src/app/session/hooks/use-session-actions/restore-resume.test.tsx \
  src/app/session/hooks/use-session-list-actions.test.tsx \
  src/app/chat/close-tab.test.ts
```

Then run the verified package scripts:

```bash
npm run typecheck
npm run lint
npm run test:ui
npm test
```

Use `npm run check` only when the full Desktop platform/install matrix is appropriate for the implementation environment; do not introduce one-off scripts.

## References
- `docs/investigations/desktop-profile-active-chat-restore-2026-08-27.md`
- `apps/desktop/AGENTS.md:81-101`
- Commits: `b94b3622b5faabadf36d8d51f5804c0a655553e7`, `a40e20e1368d6626197f0316361d33b80aff2dd8`, `afe238ac7e3cd9a50c4f6bdacf2acff3c864714c`, `530d8148aa2f2d7111729d512e39d9e1570e82ce`, `cb75983abfdceed3e544a3dea183bfd46904ee8d`, `07b87f1470aedf86e6ab6d33726b3c4a009a1ff5`
