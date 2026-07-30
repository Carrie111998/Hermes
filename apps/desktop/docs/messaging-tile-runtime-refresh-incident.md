# Incident report: messaging session tiles did not refresh at runtime

## Status

Resolved and validated in a packaged Windows Desktop build.

This was a **Desktop renderer bug**, not a missing messaging capability and not a QQBot ingestion failure. Hermes already had runtime background synchronization for messaging conversations, but its transcript refresh target was limited to the primary chat surface. Messaging conversations opened as session tiles were omitted.

## Summary

Inbound QQBot messages reached the gateway and were persisted successfully, but an already-running Hermes Desktop window did not display them in an open QQBot session tile. Switching sessions or returning to the tile did not help. The messages appeared only after restarting Desktop because startup hydration read the latest persisted transcript from the database.

The bug was caused by a state-scope mismatch:

- messaging background refresh was gated by the primary chat's `selectedStoredSessionId`;
- session tiles intentionally do not mutate the primary chat's selection or runtime atoms;
- therefore, a messaging session visible in a tile could be absent from the refresh target set;
- the refresh callback also used the primary runtime and its global busy guard instead of resolving the target tile's runtime and busy state.

The fix makes open messaging transcripts explicit: collect messaging sessions from both the primary chat and all open session tiles, resolve each stored session to its own runtime and profile, skip only the target runtime when it is busy, and update that runtime's renderer cache.

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

### 1. Resolve all open messaging stored sessions

`resolveOpenMessagingStoredSessionIds()` builds a deduplicated target list from:

- the primary selected stored session;
- every open `$sessionTiles` entry.

It retains only rows whose source is recognized as messaging.

### 2. Resolve each target independently

`resolveMessagingTranscriptTarget()` maps each stored session to:

- its owning profile;
- its bound runtime session ID;
- its target runtime's busy state.

An unrelated busy primary runtime no longer blocks an idle messaging tile.

### 3. Refresh each transcript into its own cache entry

The wiring callback fetches all eligible open messaging transcripts and commits each result with `updateSessionState(runtimeSessionId, ..., storedSessionId)`. This preserves the Desktop invariant that background work updates its own cache without stealing the foreground.

### 4. Keep signatures scoped per stored session

The no-change signature remains keyed by profile and stored-session ID. Multiple open messaging tiles cannot suppress each other's updates.

### 5. Preserve the existing synchronization mechanism

The fix does not add another timer, websocket path, global store, or transport-specific QQBot special case. It extends the existing `sessions.changed` / visible-poll refresh path with correct target resolution.

## Changed files

- `apps/desktop/src/app/contrib/hooks/refresh-messaging-transcript.ts`
  - resolves open messaging stored sessions;
  - resolves each stored session to its own runtime/profile target.
- `apps/desktop/src/app/contrib/hooks/refresh-messaging-transcript.test.ts`
  - covers a messaging tile while the primary view is non-messaging;
  - covers per-runtime busy selection;
  - covers deduplication.
- `apps/desktop/src/app/contrib/wiring.tsx`
  - includes `$sessionTiles` in the background-refresh target set;
  - refreshes and commits each target independently.

## Regression tests

The focused behavior tests assert these contracts:

1. A messaging tile remains a refresh target while the primary view is a non-messaging session.
2. The target messaging runtime is refreshed even when an unrelated primary runtime is busy.
3. The same stored session is not refreshed twice when represented in both the primary view and a tile.
4. Existing background-sync and route-resume behavior remains green.

The verification commands are:

```bash
cd apps/desktop
npx vitest run \
  src/app/contrib/hooks/refresh-messaging-transcript.test.ts \
  src/app/contrib/hooks/use-background-sync.test.ts \
  src/app/session/hooks/use-route-resume.test.tsx
npm run typecheck
npx eslint \
  src/app/contrib/wiring.tsx \
  src/app/contrib/hooks/refresh-messaging-transcript.ts \
  src/app/contrib/hooks/refresh-messaging-transcript.test.ts
npm run build
```

## Production end-to-end validation

The final acceptance test used the normal production Windows Desktop profile and one continuously running formal Desktop instance.

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

All credentials, pairing codes, user identifiers, connection details, local paths, and real session IDs are intentionally omitted or represented as `[REDACTED]`.

## Risk assessment

The change is narrow:

- it runs only while at least one messaging transcript is open;
- it reuses the existing cadence and invalidation events;
- it skips busy target runtimes;
- it signature-gates unchanged transcripts;
- it deduplicates sessions represented in more than one surface;
- errors remain non-fatal and retry on the next existing refresh opportunity.

The only expected increase is one stored-message read per distinct open, idle messaging transcript on a refresh tick.

## Follow-up recommendations

1. Add an Electron end-to-end scenario with a non-messaging primary chat and a messaging session tile receiving two external database updates.
2. Treat “visible/open session” as an explicit target set in future background synchronization code; do not infer it from the primary selection atom.
3. Keep busy, runtime, profile, and signature state scoped to the target session.
4. Do not accept restart hydration as evidence of runtime synchronization.
5. Apply the same topology test to every background-written surface introduced later.
