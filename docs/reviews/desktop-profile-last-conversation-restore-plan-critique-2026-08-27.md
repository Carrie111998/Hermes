# Critique: Desktop Profile Last-Conversation Restore Plan

Reviewed: `docs/plans/desktop-profile-last-conversation-restore-2026-08-27.md` (the plan) against the generated-plan section of `prompt-exports/oracle-plan-2026-08-27-111021-profile-restore-plan-b90a.md` (the baseline), with targeted code spot-checks of every load-bearing seam the two documents assert.

Settled decisions respected throughout: restore last conversation only; explicitly blank New Chat is durable; both same-source and cross-connection/fleet switches are in scope. No scope expansion is proposed.

## 1. Export → plan fidelity

**No implementation-bearing content is missing, weakened, or generalized.** A line-granularity diff of the export's generated plan against the plan doc (headings and numbering normalized) matches at ratio 0.972; every differing hunk is either an addition the plan makes (Goal, Background with commit/test-seam references, Execution Index, References) or a heading rename. No baseline line was dropped or reworded. Anyone auditing this plan against the export needs to look only at what the plan *adds*, not what it might have lost.

## 2. Under-specified seams, contradictions, incorrect references, missing dependencies

### F1 (high) — The typed-intent mechanism misses most fresh-draft producers; the classification table promises behavior the mechanism cannot deliver

The plan's mechanism is: `requestFreshSession(intent)` becomes mandatory-typed, wiring observes `$freshSessionIntent`, and wiring marks `$appliedFreshDraftProvenance` "immediately before `startFreshSessionDraft()`". But `requestFreshSession()` has only 8 production call sites (`close-tab.ts:48`, `connections.ts:322,437,448`, `profile.ts:769,849,876`, `projects.ts:203,1137`) — while `startFreshSessionDraft()` is invoked **directly**, bypassing the atom entirely, from:

- `wiring.tsx:924-930` — `useKeybinds({ startFreshSession: startFreshSessionDraft })`: the rebindable new-chat hotkey (the plan's "Generic Cmd-N/new-chat action" row).
- `use-prompt-actions/slash.ts:494-496` — the `/new` slash action.
- `wiring.tsx:671` — `useQuickEntryBridge({ startFreshSessionDraft, submitText })` (global Quick Entry hotkey).
- `wiring.tsx:767-769` — voice-conversation payload with `start_new_session !== false`.
- `wiring.tsx:781-786` — `useGatewayBoot({ beforeConnectionSwitch: () => startFreshSessionDraft({ preserveRoute: true, ... }) })`.

Consequence: after a profile switch, provenance sits at `automatic` and — per the plan's own rule — persists "until the route changes or a later explicit blank adopts it". A Cmd-N or `/new` applied through any of these paths leaves provenance stale-`automatic`, so the persistence effect treats an **explicit** blank as automatic and never writes `{kind:'blank'}`. Switching away and back then resurrects the old conversation — a direct violation of invariant 5 and the settled durability decision, and a regression the plan's own classification table ("Generic Cmd-N/new-chat action … Durable blank: **Yes**") claims is handled. No file in the File-by-File Impact owns making that row true; `use-keybinds.ts`, `slash.ts`, `use-quick-entry-bridge.ts`, and `use-gateway-boot.ts` appear nowhere in the plan. See §3 for the precise correction.

### F2 (medium) — `selectConnection()`'s early-return branch issues a fresh reset outside the instrumented path

`connections.ts:305-328`: when `pendingTarget === null` and the target pair is already active (reachable when returning from All Profiles on the already-active square), the function seeds the draft owner, calls `requestFreshSession()` (~line 322), and returns — no dial, no barrier, no `targetIsActive()` verification. The plan's numbered `selectConnection()` steps (begin before dial, commit after verification) never mention this branch; only the `connections.test.ts` addition ("Returning from All Profiles on the already-active pair restores") implies it. The design section must state that this branch begins and commits its restore synchronously (trivially — the target is already active) and issues the automatic `connection-switch` intent there.

### F3 (medium) — Cancel-on-activation-rejection must not hook the existing conflated `.catch`

`profile.ts:787-803`: the current chain is `Promise.all([activateOnCurrentSource(target), shouldRememberStartupProfile]).then(→ remember IPC).catch(→ notify)`. That single `.catch` fires for activation rejection *and* for `profile.remember()` IPC rejection after a successful activation. The plan says to "commit … immediately after activation success" and "if activation rejects, cancel that sequence" but does not name this trap: if cancel is attached to the existing catch, a transient remember-IPC failure will cancel an already-committed restore. Commit and cancel must hook the activation promise specifically; the plan should say so explicitly.

### F4 (medium) — The message/create submission boundary has no concrete owner

The plan assigns "at session creation/message submission start, cancel pending restore and promote an automatic draft to explicit" to `use-session-actions/index.ts`, while the wiring section hedges "message/create submission boundary **if that boundary is owned here**". It is not: message submission lives in `use-prompt-actions` / `use-composer-actions` (`submitPromptText` — see `slash.ts:152,178`), and session creation is in `use-session-actions`. Neither `use-prompt-actions/index.ts` nor `use-composer-actions` appears in the File-by-File Impact, so the "message-on-automatic-draft" promotion — a row in the classification table and the cancellation table — has no implementing file.

### F5 (medium) — Cold latch vs. live transaction interplay is unspecified

Today the `restoredRef` latch can still be open when a live switch happens (fast switch during the boot window, while the cold effect waits on `sessions.length`): the effect re-runs with the new `activeProfile` and may read/navigate the *new* scope under the still-open latch (`use-desktop-integrations.ts:99-137`). The plan splits cold and live restore into separate effects but never states that `beginProfileConversationRestore()` must close/supersede an unfinished cold restore, nor that the cold algorithm rechecks scope/transaction after its awaits the way the live algorithm does (live steps list a full recheck discipline; cold steps 1-8 list none). Without that, cold and live can both navigate for one switch.

### F6 (low) — New persistence write's ownership proof is unspecified

The current write branch gates session-route persistence on list membership: `routedSessionId && sessionBelongsToProfile(sessions, routedSessionId, activeProfile)` (`use-desktop-integrations.ts:155-160`). The plan says the new `{kind:'session'}` record is written "after a routed session has exact active-scope ownership" without saying whether that proof remains list-based or becomes by-ID. If list-based, a session aged off the capped sidebar while still open stops refreshing the record (probably acceptable, but must be stated); if by-ID, that is a new RPC on every route settlement. Either is defensible; the plan must pick one.

### F7 (low) — Cold algorithm step 2 must explicitly exclude `NEW_CHAT_ROUTE`

Cold step 2 says "if the remembered route is a valid non-session, non-overlay route, restore it as today" — but "as today" includes a `route !== NEW_CHAT_ROUTE` exclusion the sentence drops. The combination *session conversation record + `lastRoute === '/'`* is newly reachable and meaningful (e.g. the user backs out of a session to the splash without an explicit New Chat; the non-session-route branch then writes `/` while the conversation record keeps the session). As written, step 2 would restore `/` and stop, ignoring the session record — restart would then behave differently from a live switch for identical stored state. Step 2 needs the explicit `route !== NEW_CHAT_ROUTE` guard so the conversation record is consulted.

### F8 (low) — `initiator` option vs. the existing `restoreOnBoot` inference

`connections.ts:289` already infers boot: `restoreOnBoot = pendingTarget === null && $activeConnectionId.get() === null`, and behavior is pinned to it (#93197, All-Profiles preservation). The plan adds `SelectConnectionOptions.initiator` without reconciling the two. Note also the inference can misfire: if boot adoption never landed (no saved connection), the *user's first click* also satisfies it. Decision needed: `initiator` becomes the single source (driving `restoreOnBoot` too) or stays separate with a test proving they agree.

### F9 (low) — Incorrect reference and a missing test file in verification

- The `selectProfile()` section says "Do not wait for startup-profile persistence or `refreshActiveProfile()`". `refreshActiveProfile()` is not in `selectProfile()`'s chain — it is `selectConnection()`'s bookkeeping (`connections.ts:447-448`). For `selectProfile` the accurate statement is: don't wait on the `Promise.all` (activation + `isLocalDesktopProfile`) or the `remember()` IPC.
- The verification command list omits the new `src/app/session/hooks/use-session-actions/restore-resume.test.tsx` the plan itself creates.

## 3. Details the code disproves, or a simpler design replaces

**Verified accurate (no correction needed):** every "Before" interface and load-bearing line reference I checked matches the code — `requestSessionResume(sessionId, ownerRoute?)` (`session.ts:1076-1091`, including the `setSessionOwnerHint` side effect); `resumeSession(storedSessionId, replaceRoute = false, capturedOwner?)` (`use-session-actions/index.ts:797-799`); exactly two `takeWarmCache()` uses (defined `index.ts:858`, used at 877 and 973); `resolveStoredSession()`'s explicit-owner path collapsing all errors to `undefined` (`utils.ts:1416-1425`); the combined restore/write effect and window-lifetime `restoredRef` (`use-desktop-integrations.ts:92-166`); `close-tab.ts:44-53` lone-main fallback with stacked promotion clean; `projects.ts:203` path-less new-session and `projects.ts:1137` deletion kick; `selectProfile()`'s synchronous reset-then-async-activation ordering (`profile.ts:756-803`); the fleet dial/commit/barrier/revision structure and post-wipe recovery reset (`connections.ts:330-457`). The root cause and both current-state sequences are faithful to the code.

**Disproven by the code — the mechanism of F1:** the claim that making `requestFreshSession(intent)` mandatory "forces every compile-time call site to classify its reset" is false as a completeness argument: the majority of fresh-draft producers never call `requestFreshSession()` (see F1's list). Until corrected, the closed `cause` union's `'new-chat'` variant has no producer, and the classification table's Cmd-N row is unimplementable as specified.

**Precise correction for F1 (a strictly simpler design than the plan's provenance timing contract):** classify at the single choke point every producer already calls — `startFreshSessionDraft()`. It already accepts an options argument (`{ preserveRoute, workspaceTarget }`, `wiring.tsx:783`); extend it with a mandatory intent/cause (wiring's `$freshSessionIntent` consumer passes the typed intent through). That one change (a) forces classification of the *complete* producer set at compile time — keybinds, slash, quick-entry, voice, and gateway-boot included; (b) makes `$appliedFreshDraftProvenance` a simple function of the applied draft rather than a "wiring must set it immediately before calling" timing contract that every future caller must remember; and (c) leaves the coordinator, `$freshSessionIntent`, restore transaction, and all persistence semantics exactly as planned. Keep `requestFreshSession(intent)` typed for the switch producers (they need the `restoreSequence` correlation); the correction only moves where *applied* provenance is recorded. The alternative — migrating five additional direct callers onto `requestFreshSession()` — also works but touches more files and leaves the choke point unguarded against the next caller.

## 4. Requirements, edge cases, and architectural problems absent from both

- **Streaming/turn-in-flight during a switch (lifecycle/failure):** neither document addresses switching profile while a response is streaming in the outgoing session, or restoring a target session that has a live runtime elsewhere. The wipe clears messages, but late stream-completion callbacks racing the restore's `navigate` is untested territory; the plan's test matrix has no case for it.
- **Double reset on fleet commit (ordering):** `useGatewayBoot.beforeConnectionSwitch` runs a `preserveRoute: true` fresh draft on connection switches while the winning `selectConnection()` transaction separately issues its automatic intent after the barrier. When both fire for one switch, the plan doesn't specify which owns provenance or whether the preserve-route reset must be exempt from (or participate in) classification. Under the §3 correction each call classifies itself, but the interaction still needs a stated rule and a test.
- **Coordinator survival across the gateway-switch wipe (ownership):** the machine-context reset deliberately wipes gateway-bound stores and does *not* call `requestFreshSession()` (`gateway-switch.ts:174`). The new coordinator/provenance atoms must never be registered with that reset — trivially true for plain module atoms today, but nothing pins it; one line in the coordinator test file ("wipe does not clear restore request or provenance") makes it a contract instead of an accident.
- **Testability of retries:** the 500 ms/1000 ms bounded retry delays need a declared fake-timer strategy in the integration harness; otherwise the exhaustion cases are either slow or flaky. Neither document mentions timer control.
- **Quick Entry / voice blank-on-failure semantics:** those paths call `startFreshSessionDraft()` and then immediately submit; on submit *failure* the draft stays blank. Whether that blank is durable-explicit (persisted tombstone) or transient is undefined; it changes what a later switch restores.

## 5. Questions whose answers materially change the design or order

1. **How does `$connection.profile` stamp for legacy per-profile remote overrides?** `resolveStoredSession()`'s comment (`utils.ts:1455-1460`) says a per-profile remote override strips the alias so the backend answers as its own `default`. If the descriptor then carries the backend's profile rather than the Desktop key, identity predicate #4 ("`$connection` describes the same normalized profile") fails for every remote-override profile and live restore goes permanently inconclusive for them. The plan's "validation unknown" flag is right; this decides whether remote-override profiles get live restore at all, and whether the predicate must map through `SessionOwnerRoute.targetProfile` from day one.
2. **When exactly does `beforeConnectionSwitch` fire relative to `selectConnection()`'s commit** — only for switches initiated outside it (`newSessionInAgent`, profile remote-source picks), or also inside it? Determines whether the double-reset rule (§4) is a real path or a theoretical one, and whether `newSessionInAgent`'s connection swap needs restore-adjacent handling.
3. **Is restore confirmed for the already-active early-return branch** (F2)? The test list implies yes; the design section should say it, since that branch has no commit/verification structure to hang begin/commit on.
4. **List-membership or by-ID ownership proof for the new session-record write** (F6)? Decides whether aged-out-but-open sessions keep refreshing their record, and whether persistence adds RPCs.
5. **Single boot signal?** Should `initiator: 'boot'` replace the `restoreOnBoot` inference (F8), including fixing the user-first-click-when-boot-never-adopted misfire, or must the inference survive for #93197 compatibility?
6. **Quick Entry / voice submit-failure blanks: durable or transient** (§4)? A one-word answer fixes a persistence row in the classification table.

## Verdict

The plan is a faithful, verified-against-code superset of the baseline export — the architecture (coordinator, exact-scope gate, authoritative by-ID lookup, forced-cold resume, durable tombstone) is sound and its code claims check out. The one material defect is F1: the provenance mechanism instruments the wrong producer surface, which would silently break the settled explicit-blank durability decision through Cmd-N, `/new`, Quick Entry, and voice. Fixing it at the `startFreshSessionDraft()` choke point (§3) removes the defect and simplifies the design. The remaining findings are specification gaps (F2-F9) and unstated interactions (§4) that an implementer would otherwise discover as bugs or ambiguous tests mid-flight.
