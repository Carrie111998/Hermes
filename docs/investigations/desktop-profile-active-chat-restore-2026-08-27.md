# Investigation: Desktop Profile Active Chat Restoration

## Summary
Investigation complete. The live-switch gap is caused by a window-lifetime
restore latch combined with persistence of the intentional transitional `/`
route; backend session availability, route resume, and splash rendering are not
the initiating cause.

## Symptoms
- Switching between Hermes Desktop agent profiles shows the default splash screen.
- The previously active conversation for the selected profile is not restored automatically.
- The user must perform extra clicks to reopen the conversation.

## Background / Prior Research
- `b94b3622b5faabadf36d8d51f5804c0a655553e7` introduced reload-free, per-session profile switching and cross-profile session aggregation.
- `a40e20e1368d6626197f0316361d33b80aff2dd8` deliberately changed a real profile switch to open a fresh draft so the prior profile's conversation cannot remain visible in the new profile context; the current reset path is in `apps/desktop/src/store/profile.ts:756-770` and `apps/desktop/src/app/contrib/wiring.tsx:508-521`.
- `143942d497f8345d34b2168d0345f8f4ece1ef9c`, `c4212b94530025300166797cf0e406f857cc4645`, and `530d8148aa2f2d7111729d512e39d9e1570e82ce` progressively scoped cold-start session/route restoration per profile and validated ownership. Current persistence logic is in `apps/desktop/src/store/session.ts:40-110,192-211`; cold-start restore is in `apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts:95-180`.
- `afe238ac7e3cd9a50c4f6bdacf2acff3c864714c` further scoped remembered navigation by `(connection, profile)` to prevent cross-window/backend leakage.
- `fdf6f1d4c80f510c1d579e7fc3b2769f81a97892` defines the splash as renderer-local chrome for an empty, ready, primary fresh draft (`apps/desktop/src/app/chat/intro-visibility.ts:1-32`).
- History therefore indicates that showing a fresh draft on profile switch is intentional protection against displaying the wrong profile's conversation, while profile-scoped restoration already exists for cold start. The requested behavior likely requires extending the established ownership-safe restoration mechanism to live profile switches rather than removing isolation.

## Investigator Findings

### Verdict

The initial assessment is **proved, with one important refinement**: the gap is
not merely that live switches fail to invoke the cold-start restore. The same
one-lifecycle effect that skips the restore also persists the transitional `/`
route under the newly active scope. Once a successful switch settles on the
target `(connection, profile)` while the router is on `/`, overwriting that
scope's remembered route is deterministic. Source-scope overwrite and a write
under the wrong connection suffix depend on whether routing, activation, and
descriptor publication interleave.

The root cause is a scope/lifetime mismatch:

- restore readiness is represented by a window-lifetime boolean
  (`restoredRef`),
- the data is scoped by `(connection, profile)`, and
- `/` is both the intentional isolation route and an ordinary persistable route.

The switch correctly isolates the old conversation, but there is no second,
scope-aware phase that restores the new profile's owned navigation before `/`
is allowed to become durable.

### Proven Current Sequence

1. **The selector requests isolation before activation succeeds.** Profile rail
   clicks call `selectProfile()` (`apps/desktop/src/app/chat/sidebar/profile-switcher.tsx:368-384`,
   with default/condensed variants at `:415-445` and `:477-483`).
   `selectProfile()` computes whether this is a real switch, updates the next
   draft owner, and synchronously bumps `$freshSessionRequest` at
   `apps/desktop/src/store/profile.ts:754-770`. Only afterward does it start the
   asynchronous activation at `:785-800`. A same-profile retap does not request
   a reset; returning from All Profiles does.

2. **Wiring deliberately clears the foreground.** The changed request is
   consumed at `apps/desktop/src/app/contrib/wiring.tsx:508-521` and calls
   `startFreshSessionDraft()`. That callback clears the durable routed-session
   intent, navigates to `NEW_CHAT_ROUTE` (`/`), nulls active and selected session
   IDs, clears messages, and finally marks the empty draft ready at
   `apps/desktop/src/app/session/hooks/use-session-actions/index.ts:388-471`
   (the destructive/navigation core is `:417-431`; `/` is defined at
   `apps/desktop/src/app/routes.ts:9-10`). This is the intentional cross-profile
   isolation introduced by `a40e20e1368d6626197f0316361d33b80aff2dd8`, whose
   commit message says a switch starts a fresh draft so the prior profile's
   session cannot remain sticky.

3. **Reset versus activation has no strict wall-clock order.** The fresh request
   is synchronous, but its consumer is a React passive effect; activation has
   already been launched. A warm/primary activation can publish before the
   router commits `/`, while a cold spawn usually publishes later. Current tests
   explicitly cover the fast case and prevent the old session from being
   re-resumed when profile B opens before `/` commits
   (`apps/desktop/src/app/session/hooks/use-route-resume.test.tsx:393-463`).
   Regardless of that race, a normal successful switch converges on the target
   active scope plus `/` and an empty fresh draft.

4. **Cold restoration is intentionally one-shot.** `useDesktopIntegrations()`
   declares `restoredRef = useRef(false)` and labels it a one-time lifecycle
   latch at `apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts:92-104`.
   Its only reads of `getRememberedRoute()` / `getRememberedSessionId()` and its
   ownership-safe navigation ladder are inside `if (!restoredRef.current)` at
   `:104-153`. Profile switching is explicitly reload-free
   (`apps/desktop/src/store/profile.ts:779-782`), so the ref stays true and that
   read path is never revisited for the target profile.

5. **The transitional route is then persisted.** The same effect owns later
   writes. With no routed session and a non-overlay path, it unconditionally
   calls `setRememberedRoute(locationPathname, activeProfile)` at
   `use-desktop-integrations.ts:156-166`; `/` satisfies that branch. The storage
   helper writes synchronously to the profile-and-connection-scoped key through
   `apps/desktop/src/store/session.ts:202-210` and
   `apps/desktop/src/lib/storage.ts:44-56,90-96`. Session ownership is not
   consulted for the no-session route branch.

6. **Target session arrival cannot heal the missed read.** `sessions` is an
   effect dependency (`use-desktop-integrations.ts:166`), but subsequent runs
   still see the closed lifecycle latch and only persist the current route. The
   cold-start safeguard that keeps remembered state intact while ownership data
   is absent (`:115-123`) is unreachable on a live switch.

### Exact Scope and Readiness Ordering

- Remembered keys are correctly scoped: `profileNavigationKey()` combines the
  encoded profile with `activeConnectionScopeSuffix()` at
  `apps/desktop/src/store/session.ts:44-53`. This rules out a fundamentally
  global persistence design as the current cause.
- A connection descriptor publication calls `setConnection()`, which immediately
  repoints connection-scoped persistence before consumers reconcile
  (`apps/desktop/src/store/session.ts:968-975`). Registry activation applies the
  active route and then publishes its active connection at
  `apps/desktop/src/store/gateway.ts:388-425,1364-1373,1437-1448`; the profile
  wrapper also resolves descriptor and socket together and batches profile plus
  connection publication at `apps/desktop/src/store/profile.ts:495-516` and
  `:672-703`.
- Wiring already derives the narrow exact reactive identity as
  ``${activeConnectionId}\0${activeGatewayProfile}`` at
  `apps/desktop/src/app/contrib/wiring.tsx:523-544`; `$activeConnectionId` is
  derived from the published connection descriptor at
  `apps/desktop/src/store/connections.ts:47`.
- The hook's current `profileReady` input is only
  `boot.phase === 'renderer.ready'` (`wiring.tsx:838-850`). Standard cold boot
  adopts the profile and awaits a `refreshSessions()` alongside config/cwd before
  publishing `renderer.ready` (`apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts:1029-1064`).
  That is a cold-start barrier; it remains true throughout every reload-free live
  switch and says nothing about the new target pair.
- Live list refresh already has strong guards. `refreshSessions()` captures the
  profile scope and `gatewayActivationEpoch()`, then publishes only if its owner
  predicate, request ID, current profile, and activation epoch still match
  (`apps/desktop/src/app/session/hooks/use-session-list-actions.ts:227-350`).
  The epoch changes on every active route selection, including same-profile
  source swaps (`apps/desktop/src/store/gateway.ts:303-305,377-390`). Tests reject
  stale prior-profile and same-profile/different-source responses at
  `apps/desktop/src/app/session/hooks/use-session-list-actions.test.tsx:433-568`.
- `$sessionsLoading === false` is **not** a readiness signal: loading is raised
  only when the existing global list is empty
  (`use-session-list-actions.ts:238-246`). A normal live switch with old rows
  present remains `false` while the target fetch is in flight. Likewise,
  `sessions.length !== 0` proves only that some scope has rows, not that the
  target scope is authoritative.
- `useBackgroundSync()` does start a target refresh after connection/profile
  changes, but it is fire-and-forget (`apps/desktop/src/app/contrib/hooks/use-background-sync.ts:601-625`).
  Restoration therefore needs its own guarded completion/generation signal; it
  cannot infer readiness from the current atoms.

### Deterministic Versus Conditional Persistence

**Deterministic after a successful, fully published switch:** in a normal main
window, once the renderer observes the target profile, target connection suffix,
and `/`, `useDesktopIntegrations` writes `/` to the target's remembered-route
key. This does not depend on session-list readiness and happens even if the
window was already on `/`, because changing `activeProfile` reruns the effect.
Later list changes repeat the `/` write instead of restoring.

The `/` branch does **not** clear `lastSessionId`; only `lastRoute` is replaced
(`use-desktop-integrations.ts:160-165`). Consequently, a later cold launch can
still fall back from remembered route `/` to the surviving remembered session id
at `:142-145`. That does not help the current live switch because its one-time
read block remains closed, but it explains why a relaunch may appear to restore
state that repeated in-window profile swaps do not.

**Conditional interleavings:**

- If `/` commits before target activation, the source scope can also have its
  remembered route replaced before the target write. If activation publishes
  first, an outgoing session route is normally rejected for the target by the
  ownership check and the later `/` write still replaces the target route.
- If target descriptor lookup fails, the profile code deliberately keeps the
  previous connection descriptor (`profile.ts:506-515`), and null descriptors
  deliberately keep the previous storage suffix
  (`apps/desktop/src/lib/connection-scoped.ts:170-181`). A premature `/` can then
  land under the old connection suffix. That may spare the true target key, but
  it still performs no restoration and can corrupt a different scoped key.
- A failed activation still received the fresh request before the rejection is
  surfaced (`profile.ts:768-800`), so the old scope can be left on a fresh `/`
  even though the requested target never became active.
- Low-level localStorage failure is best-effort, but that is unrelated to the
  control-flow gap.

### Eliminated Hypotheses

1. **Splash logic is not causal.** `shouldShowIntro()` only renders the splash
   when the primary main chat is an empty, ready fresh draft with no routed,
   selected, or active session
   (`apps/desktop/src/app/chat/intro-visibility.ts:12-31`). The switch reset
   deliberately creates exactly that state. The splash is the visible result of
   no restore navigation, not the component preventing restoration.

2. **Missing backend session data is not required to reproduce the gap.** The
   target remembered key is never read after the lifecycle latch closes, so the
   failure occurs before any target session lookup. The scoped sidebar fetch
   reads the profile DBs and stamps ownership
   (`hermes_cli/web_routers/profiles.py:371-443`), and the renderer guards its
   publication as described above. An individual session can of course truly be
   deleted, but that is a separate stale-memory case, not an explanation for all
   live switches landing on the splash.

3. **Route-resume failure is not the initiating cause.** No remembered route is
   supplied, so route resume is never invoked. When navigation does change from
   `/` to `/:storedId`, `useRouteResume()` treats `pathnameChanged` as a resume
   trigger and calls `resumeSession()` while chat/gateway are ready
   (`apps/desktop/src/app/session/hooks/use-route-resume.ts:111-191`); the direct
   transition is covered at `use-route-resume.test.tsx:158-202`. Resume then has
   per-request stale suppression, exact owner routing, warm-cache ownership
   checks, concurrent REST/RPC hydration, and bounded recovery
   (`apps/desktop/src/app/session/hooks/use-session-actions/index.ts:802-940,
   943-1017,1294-1317,1386-1451`; `use-route-resume.ts:223-329`). A real resume
   failure yields loader/retry/error handling, not the untouched fresh-draft
   splash.

4. **The fresh reset itself is not an accidental regression.** Commit
   `a40e20e1368d6626197f0316361d33b80aff2dd8` deliberately added it after
   `b94b3622b5faabadf36d8d51f5804c0a655553e7` introduced live profile switching.
   Removing or delaying isolation until after restoration would reintroduce the
   prior profile's visible conversation and violate the Desktop context-switch
   invariant.

### Narrow Existing Signals and APIs to Reuse

- The existing restoration ladder:
  `getRememberedRoute`, `getRememberedSessionId`, `routeSessionId`,
  `sessionBelongsToProfile`, overlay rejection, and `sessionRoute`
  (`use-desktop-integrations.ts:20-32,104-150`). Cold and live restoration should
  call one shared attempt function rather than maintain two policies.
- Exact settled identity: the existing `gatewayScope` pair in
  `wiring.tsx:523-527`, plus `gatewayActivationEpoch()` for asynchronous stale
  suppression.
- Authoritative list barrier: `refreshSessions(shouldPublish)` and its existing
  request/profile/epoch checks (`use-session-list-actions.ts:227-350`). Promise
  resolution alone is insufficient because a superseded request can resolve
  without publishing.
- Existing request-generation patterns: `requestSessionResume()` carries a
  monotonically increasing sequence (`apps/desktop/src/store/session.ts:1076-1091`),
  route resume consumes it once (`use-route-resume.ts:108,135-170`), and
  connection switching uses latest-only `switchRevision` plus exact
  `connectionId::profile` validation
  (`apps/desktop/src/store/connections.ts:269-334,410-457`).
- Exact by-ID fallback, if required for an aged-out remembered session:
  `resolveStoredSession()` checks cache, performs a scoped by-ID GET, stamps
  ownership, and fails closed for an explicit owner
  (`apps/desktop/src/app/session/hooks/use-session-actions/utils.ts:1390-1485`).

`$freshSessionRequest` alone is **not** enough: it is targetless and is also used
for explicit new-chat flows (`newSessionInProfile`, `newSessionInAgent`, project
deletion, and connection recovery). Resetting the restore latch on every fresh
edge would make Cmd-N/explicit new-chat actions resurrect old conversations.
`$gatewaySwapTarget` is likewise profile-only and transient, so it cannot prove
same-profile/different-connection ownership.

### Viable Designs and Tradeoffs

#### Recommended: explicit successful-switch restore intent

Keep the immediate fresh reset, but add a distinct latest-only navigation
restore request for context switches. The request should carry a generation and
the final exact connection/profile identity, and should publish only after the
selector's activation succeeds and still owns latest intent. `selectProfile`
already captures the source it dials; the cross-connection selector already has
the stronger `switchRevision`/`targetIsActive()` template.

On receipt, `useDesktopIntegrations` should:

1. mark that exact scope/generation as pending and suppress persistence of the
   transitional `/` for it;
2. await or observe an authoritative target `refreshSessions()` publication;
3. recheck generation, exact scope, activation epoch, and current route before
   every read or navigation;
4. read the connection-scoped remembered route/id and run the existing ownership
   ladder;
5. navigate with `{replace: true}` when valid, or clear only after an
   authoritative negative result; and
6. release the pending write barrier without allowing stale generations to
   publish or navigate.

This preserves immediate isolation and makes rapid B→C switches latest-wins. It
also cleanly excludes explicit New Chat / `newSessionInProfile()` because those
flows should remain fresh.

`refreshSessions()` currently returns `void`. The safest narrow API refinement
is to return whether the captured current-scope response actually published (or
publish a small scope-generation completion token). After awaiting, do not read
the hook's captured `sessions` array—it is the pre-refresh render. Either let a
`[sessions, readyGeneration]` effect perform validation on the next render, or
re-read `$sessions.get()` after rechecking the exact generation/scope.

#### Smaller but unsafe variants

- Replacing the boolean with `restoredScopeRef` alone still cannot distinguish a
  context switch from explicit New Chat and can restore under a half-published
  connection descriptor.
- Resetting `restoredRef` on `$freshSessionRequest` resurrects sessions for
  intentional fresh-draft actions.
- Waiting on `$sessionsLoading` or `sessions.length` accepts old-scope rows as
  target readiness.
- Calling `ensureGatewayProfile()` from the integration hook duplicates selector
  ownership and creates another activation race.

#### Optional robustness extension

The current cold ladder treats a nonempty page that lacks a remembered session
as authoritative. Because the sidebar page is bounded, a valid old unpinned
session can be absent. A scoped by-ID validation through the existing
`resolveStoredSession()`/`getSession()` path before clearing would avoid that
false negative, at the cost of an extra REST request and a less tidy dependency
boundary. This is not necessary to prove the reported gap, but is safer if live
restore is expected to honor arbitrarily old remembered chats.

### Recommended Exact Edit Locations (future implementation)

1. **`apps/desktop/src/store/profile.ts:330-338,754-800`** — define and emit a
   typed latest-only successful profile-navigation restore request; keep
   `requestFreshSession()` in its current pre-activation isolation position.
   Do not emit from `newSessionInProfile()` / `newSessionInAgent()`
   (`:838-880`).
2. **`apps/desktop/src/store/connections.ts:269-334,410-438`** — if fleet
   cross-connection profile squares should share the behavior, emit the same
   request only from the already verified winning commit. Reuse `switchRevision`
   and `targetIsActive()`; do not broaden boot-time restore or failed-switch
   recovery.
3. **`apps/desktop/src/app/contrib/wiring.tsx:508-527,838-850`** — pass the exact
   gateway scope, restore request/generation, and guarded refresh contract to the
   integration hook. Keep the existing fresh-draft effect as the isolation
   owner.
4. **`apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts:92-166`** —
   extract the current read/ownership ladder, add exact-scope live restore and a
   pending transitional-route write barrier, and retain the current deep-link,
   overlay, HUD/browser-window, and cold-start semantics.
5. **`apps/desktop/src/app/session/hooks/use-session-list-actions.ts:227-350`** —
   only if needed, make guarded publication completion observable; preserve all
   current request/profile/activation-epoch guards.
6. **Do not edit** `intro-visibility.ts` or weaken `use-route-resume.ts`; both are
   behaving correctly for the state/routes they receive.

### Test Locations and Required Cases

- Extend
  `apps/desktop/src/app/contrib/hooks/use-desktop-integrations.test.tsx:128-180,
  307-360`. The current "stale-result suppression" test jumps directly from a
  valid A route to a valid B route and therefore misses the actual transient `/`
  sequence. Add:
  - A session → pending fresh `/` → B exact scope: B's remembered route is not
    overwritten;
  - B list initially unavailable: key remains intact, then restores after the
    guarded B publication;
  - wrong-owner/stale B route is cleared only after an authoritative result;
  - rapid B→C: late B readiness cannot navigate or persist;
  - same profile name on two connections restores only the matching connection;
  - explicit New Chat and `newSessionInProfile()` remain fresh.
- Extend `apps/desktop/src/store/profile-select-source.test.ts:46-93` to prove
  `selectProfile()` emits a restore request only for the latest successful exact
  target, while new-session helpers do not.
- If `refreshSessions()` exposes publication status, extend
  `apps/desktop/src/app/session/hooks/use-session-list-actions.test.tsx:433-568`
  for success, superseded request, stale profile, and same-profile/source-change
  outcomes.
- Add a remembered-**route** connection-scope assertion beside the existing
  remembered-session test at
  `apps/desktop/src/store/layout-connection-scope.test.ts:151-165`; current route
  storage tests at `apps/desktop/src/store/session.test.ts:1041-1101` cover only
  per-profile isolation.
- Keep the route-resume regressions at
  `apps/desktop/src/app/session/hooks/use-route-resume.test.tsx:158-202,393-463`
  as proof that restored navigation resumes the target while the outgoing route
  cannot self-heal during isolation.

### Root Cause Statement

Hermes Desktop intentionally clears the foreground to `/` before a live profile
activation, but remembered navigation restoration is gated by a window-lifetime
boolean rather than the active `(connection, profile)` scope. Since live profile
switches do not reload, the target's remembered route/id is never read. The
shared persistence half of that effect then treats the intentional isolation
route as ordinary navigation and, after exact target publication, overwrites the
target remembered route with `/` without waiting for target-owned session data.

## Investigation Log

### Oracle Synthesis - Minimal safe lifecycle seam
**Hypothesis:** An explicit successful-switch restore intent plus a transitional-route write barrier is the narrowest safe correction.
**Findings:** Confirmed, with refinements: the intent/barrier must begin synchronously before the isolation reset; restoration should prefer exact target-scoped by-ID validation; activation success requires exact scope/epoch verification; and bare-ID warm cache reuse must be bypassed or owner-qualified.
**Evidence:** `profile.ts:754-800`; `use-desktop-integrations.ts:92-166`; `use-session-actions/index.ts:858-867`; selected gateway/session ownership and stale-generation primitives listed above.
**Conclusion:** Recommended design is a small latest-only renderer transaction, not a reset-latch change or session-list rewrite.

### Initial Assessment - Desktop profile/session restoration
**Hypothesis:** Profile switching resets renderer chat state without restoring the selected profile's last active session, or the active-session identity is stored globally rather than per profile.
**Findings:** The reset is intentional, remembered navigation is already scoped
by connection and profile, but restoration runs only once per renderer lifetime.
The later persistence branch records the transitional `/` under the new scope.
**Evidence:** See the exact selector, wiring, restore/persist, session-list,
gateway-publication, and route-resume file:line traces in Investigator Findings.
**Conclusion:** The first half of the hypothesis is confirmed. The alternative
global-identity premise is disproved by the current scoped key implementation.

## Root Cause
See **Investigator Findings → Root Cause Statement**. The defect is the
window-lifetime restore latch plus unguarded persistence of the transitional `/`
route across a reload-free, connection/profile-scoped switch.

## Recommendations
1. Preserve the immediate pre-activation fresh-draft isolation. Begin a latest-only restore transaction synchronously before `requestFreshSession()`, carrying a sequence plus the captured target connection/source and normalized profile. Activation success must be verified against the final exact gateway scope and activation epoch; promise fulfillment alone is insufficient.
2. Add renderer-local provenance for the automatically generated isolation `/` and suppress only that route's persistence while its transaction is pending. Release on validated restoration, explicit user navigation/new-chat intent, replacement by a newer switch, or teardown—not merely on an inconclusive lookup.
3. After exact activation and connection-storage rescoping, prefer a target-scoped by-ID lookup of the remembered chat over global session-list readiness. Restore only on exact profile/connection ownership (including lineage where supported); treat timeout, ambiguity, capped-list absence, and transient failure as inconclusive rather than stale.
4. Recheck sequence, exact scope, activation epoch, automatic-route provenance, null selected/runtime IDs, and absence of newer user intent immediately before navigating through existing resume machinery.
5. Force this automatic restoration through an ownership-validated/cold resume path, or qualify warm runtime cache entries by exact owner. The current `runtimeIdByStoredSessionIdRef` is keyed by bare stored-session ID (`use-session-actions/index.ts:858-867`), so identical IDs across profiles/sources are a hidden cross-owner cache hazard.
6. Keep the first implementation limited to explicit same-window `selectProfile()` switches. Do not trigger it from generic `$freshSessionRequest`, explicit New Chat flows, background activations, or connection-switch transactions.

### Minimum transaction contract
- Begin payload: monotonic sequence, captured connection/source identity, normalized target profile.
- Ready proof: same latest sequence, settled gateway scope, activation epoch, and matching connection-storage scope.
- Cancellation: newer profile selection; explicit New Chat/chat/page selection; message submission; All Profiles; connection/source switch; activation or identity-proof failure; teardown.
- Negative lookup policy: clear stale memory only after an authoritative exact-target not-found result.

### What must not change
- Do not remove or delay the isolation reset, change splash visibility, reset the cold-start latch on profile changes, infer readiness from `renderer.ready`/`$sessionsLoading`/nonempty `$sessions`, weaken route-resume race guards, wipe background gateways/lists, or replace backend ownership validation with renderer memory.

## Preventive Measures
- Test every context switch as an ordered invariant: old foreground clears first; only the latest exact target may restore; transitional routes never become durable; stale activation/list/lookup completions cannot navigate; and explicit new-chat actions never restore.
- Add duplicate-ID tests across profiles and connections so renderer warm caches cannot bypass exact ownership.
- Treat route provenance and scope transitions as explicit state rather than inferring user intent from the final pathname.
