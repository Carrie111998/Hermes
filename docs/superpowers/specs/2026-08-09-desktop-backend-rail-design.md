# Desktop Backend Rail

## Problem

Hermes Desktop can connect to both the local Mac backend and a configured remote
backend, but the profile rail currently models only profile names. Both
backends expose a root profile named `default`, so the catalog merge keeps one
canonical row and the user cannot switch between the two roots from the UI.
The current workaround is changing the primary gateway in Settings, which is
not a session-level selector.

## Goals

- Show two fixed rail controls, `Mac` and `Remote`, when the corresponding
  backend is available.
- Make each control select the root of its own backend without treating either
  root as a user-created profile.
- Keep both backend connections alive so switching does not require changing
  gateway settings or recreating the active session.
- Preserve existing named-profile behavior, ordering, colors, creation, and
  deletion flows.
- Keep failed background connection attempts from replacing a healthy active
  connection.

## Non-goals

- Creating, deleting, or renaming profiles automatically.
- Changing remote-server data or credentials.
- Adding a second copy of the same `default` row to the profile catalog.
- Reworking the Settings gateway configuration screen.

## User experience

The left side of the existing profile rail gains two fixed controls:

- `Mac` with a monitor icon selects the local root backend.
- `Remote` with a globe icon selects the configured remote root backend.

The controls are separate from the colored named-profile squares. The selected
backend has the active treatment; named profiles retain their existing active
treatment when selected. A backend that is not configured, authenticated, or
reachable remains visible only when it has a meaningful status to communicate,
is marked unavailable, and cannot replace the current active gateway on error.

When a remote backend is configured and healthy, Desktop starts it as the
primary connection and prewarms the local root as a secondary connection. The
Mac control targets the local secondary through the existing forced-local
target alias. The Remote control targets the remote primary root. Switching
between them updates the active connection descriptor and gateway without
changing the gateway settings mode.

## Architecture and data flow

1. Introduce a small backend-target model distinct from `ProfileInfo`, with
   `local` and `remote` root targets.
2. Keep `default` profile catalog metadata canonical to the local root; backend
   controls carry the source identity that the catalog intentionally omits.
3. Extend the profile/gateway selection path with a backend target so selecting
   `Mac` always uses `{ localOnly: true }`, while selecting `Remote` uses the
   remote primary target even though both roots are named `default`.
4. Reuse the gateway registry's persistent primary/secondary sockets and its
   existing local target alias. Do not close the inactive root on a switch.
5. Prewarm the inactive root after boot and after a successful backend switch;
   prewarm failures are best-effort and do not disturb the foreground gateway.

## Error handling

- If the remote descriptor or authentication is unavailable, keep the local
  root active and show the remote control as unavailable.
- If the local secondary fails to start, keep the remote root active and expose
  the normal reconnect affordance when the user selects Mac.
- A failed switch leaves the previous active gateway and connection descriptor
  intact.
- Reconnect and pool-recreation paths preserve the backend target identity,
  including the local alias for the local root.

## Verification

- Unit tests cover target mapping, duplicate `default` roots, active-target
  selection, and failure preservation.
- Existing profile catalog, gateway, Electron routing, UI, typecheck, and
  packaging tests remain green.
- Manual packaged Desktop verification confirms both fixed controls render,
  each control switches the real active backend, and the inactive backend stays
  connected.
