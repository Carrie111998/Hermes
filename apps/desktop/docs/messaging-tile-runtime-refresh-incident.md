# Incident report: messaging session tiles did not refresh at runtime

## Status

Resolved. The primary runtime-refresh path was validated in a packaged Windows Desktop build; subsequent review hardening for pagination, lineage rotation, reconnect busy state, and asynchronous races was validated by focused tests, type checking, lint, and a production build.

This was a **Desktop renderer bug**, not a missing messaging capability and not a QQBot ingestion failure. Hermes already had runtime background synchronization for messaging conversations, but its transcript refresh target was limited to the primary chat surface. Messaging conversations opened as session tiles were omitted.

## Summary

Inbound QQBot messages reached the gateway and were persisted successfully, but an already-running Hermes Desktop window did not display them in an open QQBot session tile. Switching sessions or returning to the tile did not help. The messages appeared only after restarting Desktop because startup hydration read the latest persisted transcript from the database.

The bug was caused by a state-scope mismatch:

- messaging background refresh was gated by the primary chat's `selectedStoredSessionId`;
- session tiles intentionally do not mutate the primary chat's selection or runtime atoms;
- therefore, a messaging session visible in a tile could be absent from the refresh target set;
- the refresh callback also used the primary runtime and its global busy guard instead of resolving the target tile's runtime and busy state.

The fix makes open messaging transcripts explicit: collect direct runtime bindings from both the primary chat and all open session tiles, resolve each surface's stored-session metadata within its owning profile, skip only target runtimes whose authoritative state is busy, and update each matching renderer cache. Exact metadata fallback, lineage grouping, profile-qualified matching, and post-request revalidation cover restored or compressed sessions that are not represented by the current sidebar page.

## User-visible symptoms

The affected state had all of the following properties:

1. QQBot pairing and gateway connectivity were healthy.
2. New inbound messages reached the gateway.
3. The gateway persisted those messages to `state.db`.
4. SQLite integrity checks completed successfully.
5. The open Desktop window did not read or render the new messages.
6. Switching to another session and back to the QQBot tile did not refresh it.
7. There was no reliable runtime history catch-up.
8. Restarting the entire Desktop process made the messages appear.

The last point was misleading: it proved only that startup hydration worked. It did not prove that runtime synchronization worked.

## Impact

The production report involved QQBot, but the defect was not QQBot-specific. Any messaging-backed conversation opened as a session tile could be affected, including Telegram, Discord, WeChat, and other sources recognized by `isMessagingSource()`.

Primary-chat messaging transcripts could continue to refresh because they populated the atoms used by the old gate. The failure depended on presentation topology, not the transport used to ingest the message.

## Expected data flow

```text
Messaging platform
  -> gateway ingestion
  -> state.db persistence
  -> sessions.changed / visible polling
  -> resolve every open messaging transcript
  -> fetch stored messages for each transcript
  -> update the matching runtime session state
  -> render the new messages in its surface
```

## Broken data flow

```text
QQBot
  -> gateway ingestion                         OK
  -> state.db persistence                      OK
  -> Desktop background synchronization       running
  -> primary selected-session messaging gate  false
  -> QQBot tile target resolution              omitted
  -> tile runtime session-state update         never executed
  -> visible tile transcript                   stale

Desktop restart
  -> startup hydration
  -> read current state.db transcript
  -> messages become visible
```

## Why this is a bug, not an unimplemented feature

Hermes Desktop already contained all of the required mechanisms:

- messaging-session discovery;
- `sessions.changed` invalidation and compatibility polling;
- stored-message retrieval;
- runtime-to-stored-session mappings;
- per-runtime renderer session-state caches;
- startup transcript hydration.

`useBackgroundSync()` explicitly describes messaging turns written by background gateways and polls the open transcript because those turns do not arrive over the Desktop websocket. This establishes the intended behavior.

The missing piece was coverage for another existing presentation surface. `useSessionTileDelegate()` intentionally keeps tile activity out of the primary view:

```text
$activeSessionId and $messages remain the primary thread's state
```

That isolation is correct. The bug was assuming that primary-view state also described every transcript that was open on screen.

## Investigation

### 1. Verify ingestion before touching the renderer

The investigation first confirmed that pairing, gateway receipt, and database persistence were working. The persisted state passed SQLite integrity checks. This ruled out QQBot delivery loss, pairing failure, and database corruption.

### 2. Separate startup hydration from runtime refresh

Restarting Desktop loaded the messages, which localized the failure:

- the persisted transcript was valid;
- the startup load path could read it;
- the long-lived renderer state was not being reconciled after new writes.

### 3. Reject UI-only success signals

The following were treated as insufficient evidence:

- pairing succeeded;
- the database contained the message;
- a unit test passed;
- a newly built package was installed;
- messages appeared after restart.

The acceptance criterion remained: at least two consecutive, unique inbound messages must appear in the same production Desktop instance without a restart, reload, or tab switch.

### 4. Test the route/runtime mismatch hypothesis

An initial investigation found a real route/runtime mismatch: a routed stored session could be selected while the active runtime still belonged to another session. That path was corrected and tested separately.

A packaged build containing that correction still failed four consecutive runtime message probes. This was useful negative evidence: route resumption was not the complete root cause.

The latest upstream `main` already contains the route/runtime self-healing behavior, so this change does not duplicate it.

### 5. Trace the tile state boundary

The decisive source trace compared:

- `useBackgroundSync()` and its active-messaging gate;
- the transcript refresh callback in `contrib/wiring.tsx`;
- `$sessionTiles` and the per-runtime state cache;
- `useSessionTileDelegate()`, which intentionally avoids mutating the primary view.

The old gate and refresh callback were both derived from primary-view refs:

```text
selectedStoredSessionIdRef.current
activeSessionIdRef.current
busyRef.current
```

A QQBot session displayed in a tile could have its own valid stored-session ID, runtime binding, and cached renderer state while none of those primary refs described it. As a result, the background tick existed but had no eligible target.

## Root cause

The root cause was incomplete refresh-target resolution.

### Primary cause

The `activeIsMessaging` gate checked only the primary selected stored session. If the primary chat was a normal local session and QQBot was open in a tile, the gate was false and `useBackgroundSync()` did not poll a messaging transcript at all.

### Secondary cause

Even when the refresh callback ran, it used the primary active runtime and global primary busy state. A busy unrelated chat could suppress a messaging refresh, and a tile's own runtime was not selected explicitly.

### Why switching tabs did not repair the state

A session tile is deliberately isolated from the primary chat atoms. Focusing or revisiting it does not make it the primary selected session. Therefore, tab switching did not repair the target set and no dependable historical catch-up occurred.

## Fix design

### 1. Enumerate open renderer surfaces directly

`resolveOpenTranscriptSurfaces()` collects:

- the primary selected stored session together with its active runtime ID;
- every open `$sessionTiles` entry together with that tile's own runtime ID.

It deduplicates identical runtime bindings and does not infer a tile runtime through the stored-to-runtime reverse map. This matters after compression, when a durable tile can retain the lineage-root ID while its live runtime has rotated to a continuation-tip ID.

`resolveOpenMessagingCandidateIds()` is the lightweight poll gate. It checks both the capped messaging page and the known session cache, excludes sessions already known to be local, and retains only known messaging rows or unresolved open candidates for exact metadata resolution.

### 2. Resolve metadata and lineage independently

`resolveMessagingTranscriptTargets()` reads each target runtime's current stored ID, checks the current session caches within the surface's owning profile, and falls back to the profile-scoped exact-ID `getSession(id, profile)` path for restored, search-opened, project-opened, or older sessions outside the sidebar's recent window. The production adapter treats that scoped transport as authoritative and stamps the normalized requested profile on its result; alternate adapters must return metadata for the same profile before source or lineage processing. It then:

- keeps only sources recognized as messaging;
- preserves the owning profile;
- canonicalizes the live stored-session tip;
- groups root/tip aliases by profile and lineage root while preserving every renderer runtime that must be updated.

### 3. Use authoritative busy state and revalidate after I/O

The wiring reads `$sessionStates` before the cache ref because reconnect hydration publishes the authoritative busy snapshot to that atom. An unrelated busy primary runtime no longer blocks an idle messaging tile, while a target runtime restored as busy is not polled.

After each stored-message read, `refreshMessagingTranscriptTarget()` rechecks request generation, active-profile ownership, a monotonic profile epoch, open-surface membership, runtime identity, and busy state. A delayed response therefore cannot overwrite a newer poll, a running turn, a closed tile, a Profile switch (including an `A -> B -> A` round trip), or a runtime rebound to another session.

### 4. Refresh every renderer runtime without cross-suppression

One canonical transcript read can update every renderer runtime displaying that lineage. No-change signatures are scoped per runtime ID, so a current primary view cannot suppress a stale tile (or vice versa). The commit uses the authoritative full session state as the cache-update base before calling `updateSessionState(runtimeSessionId, ..., storedSessionId)`.

### 5. Preserve the existing synchronization mechanism

The fix does not add another timer, websocket path, global store, or transport-specific QQBot special case. It extends the existing `sessions.changed` / visible-poll refresh path with correct target resolution.

## Changed files

- `apps/desktop/src/app/contrib/hooks/refresh-messaging-transcript.ts`
  - enumerates direct primary/tile runtime surfaces;
  - resolves paginated metadata and canonical lineage targets;
  - guards asynchronous commits and per-runtime signatures.
- `apps/desktop/src/app/contrib/hooks/refresh-messaging-transcript.test.ts`
  - covers capped-sidebar misses and exact metadata fallback;
  - covers direct runtime binding and root/tip alias grouping;
  - covers authoritative busy state, stale-response suppression, and per-runtime signatures;
  - covers same-ID collisions, wrong-profile exact-resolution responses, and the profile-scoped production RPC adapter.
- `apps/desktop/src/app/contrib/wiring.tsx`
  - includes `$sessionTiles` in the background-refresh target set;
  - resolves metadata through the target profile and commits each still-valid target independently.

## Regression tests

The focused behavior tests assert these contracts:

1. A messaging tile remains a refresh candidate while the primary view is a non-messaging session.
2. An open tile outside the capped messaging sidebar page is resolved by exact stored-session ID.
3. Primary and tile surfaces use their direct runtime bindings instead of a rotation-sensitive reverse lookup.
4. Root/tip aliases are grouped by lineage while every renderer runtime remains an update destination.
5. A target published as busy is not resolved or fetched, and a runtime that becomes busy during I/O rejects the response.
6. A delayed older response cannot overwrite a newer response.
7. Transcript signatures are independent per renderer runtime.
8. A same-ID session from another profile cannot classify or hydrate an open surface.
9. A response started before a Profile round trip cannot commit after the original Profile becomes active again.
10. The production exact-session adapter queries and stamps the normalized target profile.
11. Existing background-sync and session-state behavior remains green.

The verification commands are:

```bash
cd apps/desktop
npx vitest run \
  src/app/contrib/hooks/refresh-messaging-transcript.test.ts \
  src/app/contrib/hooks/use-background-sync.test.ts \
  src/app/session/hooks/use-route-resume.test.tsx \
  src/store/session-states.test.ts
npm run typecheck
npx eslint \
  src/app/contrib/wiring.tsx \
  src/app/contrib/hooks/refresh-messaging-transcript.ts \
  src/app/contrib/hooks/refresh-messaging-transcript.test.ts
npm run build
```

Current follow-up verification results:

- focused Vitest selection: 4 files, 54 tests passed;
- Desktop type checking: passed;
- targeted ESLint: passed with no warnings;
- full Desktop lint: exit code 0, with 67 pre-existing warnings outside the changed files;
- production build: passed, including renderer, Electron main/preload, native dependency staging, and dist assertion;
- earlier complete Desktop Vitest baseline run: 3,890 passed, 22 failed, and 3 skipped. The failures matched the previously reproduced unchanged Windows/MSYS baseline: 19 platform/environment cases plus one locale-dependent time label and two Billing copy assertions. No changed or newly added test file failed; the profile-hardening changes above were subsequently covered by the fresh focused selection.

## Production end-to-end validation

The primary runtime-refresh acceptance test used the normal production Windows Desktop profile and one continuously running formal Desktop instance.

Conditions:

- no diagnostic Electron profile;
- no secondary Desktop instance;
- no restart between inbound messages;
- no reload;
- no tab switch used as a refresh trigger;
- at least two unique consecutive QQBot messages.

Result:

- each message was ingested and persisted;
- each message appeared automatically in the already-open QQBot transcript;
- the Desktop process remained the same for the runtime message sequence;
- the user confirmed that all probes appeared in real time.

Before that validation, the packaged renderer artifact was checked against the tested build by SHA-256. The packaged `app.asar` renderer entry matched the build output.

After that acceptance, an independent review identified additional edge cases involving the capped messaging sidebar page, root/tip alias rotation, reconnect-published busy state, overlapping asynchronous polls, and same-ID sessions across independent profiles. Those follow-up guards were verified with dedicated RED/GREEN regression tests, Desktop type checking, targeted and full lint, and a fresh production build. The continuously running formal Desktop instance was intentionally not reinstalled or restarted solely to repeat the already-passed primary acceptance sequence.

All credentials, pairing codes, user identifiers, connection details, local paths, and real session IDs are intentionally omitted or represented as `[REDACTED]`.

## Related but separate incident: updater blocked by a sibling gateway process

The same investigation also captured a distinct Desktop update failure. It did **not** cause the messaging transcript refresh gap and is not fixed by the renderer changes in this PR.

The Desktop log records the following order:

1. the updater verified `state.db` and created its emergency backup;
2. Desktop terminated its headless backend;
3. the `update-in-flight` gate deferred any backend restart;
4. the venv blocker scan found one remaining process using the installation;
5. the update aborted and identified that process as `python.exe -m hermes_cli.main gateway run` from the same managed venv.

Repeated attempts reported the same sibling gateway process. This distinguishes the observed blocker from the older self-respawn failure class documented in `electron/update-gate.ts`: the Desktop backend did not respawn inside the update critical section, but the separately running messaging gateway continued to hold the managed runtime.

The abort was therefore a safety response to a real install-tree owner, not evidence that the messaging database was corrupt or that the Gateway had stopped writing messages. A separate updater follow-up should coordinate the lifecycle of eligible local gateway siblings—or offer an explicit stop/update/restart flow—without terminating remote or independently managed services. The UI should also distinguish a Desktop backend from a messaging gateway when reporting blockers.

This PR deliberately keeps that updater lifecycle work out of the messaging refresh fix.

## Risk assessment

The change is narrow:

- it runs only while at least one known messaging transcript or unresolved open candidate exists;
- it reuses the existing cadence and invalidation events;
- it skips busy target runtimes;
- it scopes cache, exact metadata, lineage grouping, and commits to the target profile;
- it signature-gates unchanged transcripts per renderer runtime;
- it groups root/tip aliases while preserving all visible renderer destinations;
- errors remain non-fatal and retry on the next existing refresh opportunity.

The only expected increase is one stored-message read per distinct open, idle messaging lineage on a refresh tick. An uncached open surface may also perform the existing exact-ID metadata lookup once before source filtering.

## Follow-up recommendations

1. Add an Electron end-to-end scenario with a non-messaging primary chat and a messaging session tile receiving two external database updates.
2. Treat “visible/open session” as an explicit target set in future background synchronization code; do not infer it from the primary selection atom.
3. Keep busy, runtime, profile, and signature state scoped to the target session.
4. Do not accept restart hydration as evidence of runtime synchronization.
5. Apply the same topology test to every background-written surface introduced later.
