# Issue #83391: Desktop Messaging Profile Scope

## Context

The desktop renderer stores the selected management profile through
`setApiRequestProfile()`. Profile-aware REST helpers spread `profileScoped()`
into their `HermesApiRequest`, allowing Electron to route local profiles to the
correct pooled backend and shared remote profiles to the correct `?profile=`
scope.

The three messaging-platform helpers omit that request field:

- `getMessagingPlatforms()`
- `updateMessagingPlatform()`
- `testMessagingPlatform()`

As a result, Electron routes these requests through the primary/default
backend even after the user selects another profile. The backend endpoints
already accept a profile, and the web dashboard already scopes the same three
operations correctly.

## Goals

- Read messaging credentials and status from the selected desktop profile.
- Write messaging configuration only to the selected desktop profile.
- Test messaging state against the selected desktop profile.
- Preserve existing single-profile behavior when no profile is selected.

## Non-Goals

- Change backend endpoint behavior or profile resolution.
- Change the Messaging Platforms page or its state management.
- Change gateway multiplexing or Electron backend-pool architecture.
- Add manual query-string construction in the renderer helpers.

## Design

Add `...profileScoped()` to each of the three messaging-platform API request
descriptors in `apps/desktop/src/hermes.ts`. This uses the existing desktop
profile-routing contract rather than introducing another profile source or
passing profile names through page components.

The resulting data flow is:

1. The profile store calls `setApiRequestProfile()` when the active profile
   changes.
2. A messaging helper includes that profile in its `HermesApiRequest`.
3. Electron routes a local profile to its pooled backend, or appends the
   profile query parameter when a shared remote backend requires it.
4. The existing backend endpoint reads, writes, or tests the target profile.

When no profile is active, `profileScoped()` returns an empty object, preserving
the primary/default backend path for single-profile users. Request paths,
methods, bodies, platform ID encoding, and existing error propagation remain
unchanged.

## Alternatives Considered

1. Automatically scope selected endpoint prefixes inside the Electron API
   bridge. This could prevent future omissions, but it broadens routing policy
   in a shared boundary and risks scoping endpoints that are intentionally
   global.
2. Pass the active profile explicitly from the Messaging Platforms page into
   every helper. This duplicates global profile state across components and
   makes the helper API inconsistent with the rest of the desktop bridge.

The existing `profileScoped()` contract is the smallest and most consistent
fix.

## Testing

Extend `apps/desktop/src/hermes-profile-scope.test.ts` with a regression test
that selects a non-default profile, calls all three messaging helpers, and
asserts that every captured `HermesApiRequest` carries that profile. The test
also checks the expected paths, methods, and update body so the read, write,
and test operations are each covered.

The test must fail against the current implementation because all three
requests omit `profile`, then pass after the production change. Run the focused
desktop test first, followed by the desktop test suite and the repository's
required full verification suite.

## Compatibility And Rollout

No configuration or data migration is required. The fix changes only request
routing when a non-default profile is active; default-profile and
single-profile behavior remains unchanged.

Known limitation: when multiple profiles share one remote backend, switching
profiles does not remount `MessagingView`. Its platform data lives in local
React state and does not respond to `invalidateProfileScopedQueries()`, so the
page can continue showing the previous profile's credentials until it is
manually refreshed. This change intentionally fixes request routing only;
refreshing local Messaging state on a shared-remote profile switch is follow-up
work.
