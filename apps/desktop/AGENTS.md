# Desktop Engineering Guide

Read this with the repository `AGENTS.md` and [`DESIGN.md`](./DESIGN.md).
Root rules still apply; `DESIGN.md` owns the visual/interaction contract.

<!-- Progressive subdirectory hints cap each file at 8,000 characters. Keep this
file below 7,500 so future edits retain margin. -->

## Authority and boundaries

Desktop is its own Electron + React chat surface. It is not the dashboard and
does not embed the TUI.

- **Electron** owns machine facts: process/window lifecycle, native filesystem
  and git access, install/update, and a narrow typed capability bridge.
- **The renderer** owns navigation, presentation, and window-local interaction.
  It never reaches for Node/Electron directly.
- **The agent backend** owns sessions, tools, model calls, and streaming. Agent
  behavior stays behind the gateway rather than being reimplemented in React.

Put state with the authority allowed to be right. Backend-shaped data in the
renderer is a cache, Electron owns runtime facts, and components own only local
presentation. Shared renderer state lives in small feature-owned stores;
request-shaped server data uses the query layer; hot coordination that must not
paint stays in refs. Persisted keys declare their scope (global, connection,
profile, session, project, or window) so one context cannot bleed into another.

## Identity and reconciliation

Do not conflate session identities: stable/durable identity keys navigation and
persisted UI, runtime identity keys live streaming, and lineage identity keys
state that survives compression. Translate explicitly at boundaries.

The renderer reconciles backend truth:

- Merge refreshes without dropping live or pinned rows.
- Paint optimistic direct manipulation from a snapshot; visibly roll back a
  failed write and let authoritative refresh win.
- Guard async results with request tokens/generations so stale work cannot
  overwrite newer intent.
- Only the foreground selection publishes to the shared view; background work
  updates its own cache.
- Coalesce cosmetic churn but flush terminal transitions immediately.
- Preserve reference identity on no-op updates to avoid expensive rerenders.

## Switching context

A switch re-homes the workspace rather than rebooting everything:

- **Connection/mode apply** (local/remote/cloud) keeps the shell mounted, clears
  gateway-bound stores explicitly, then reconnects. Query invalidation alone is
  insufficient.
- **Runtime `HERMES_HOME` change** is a hard re-home: tear down the primary
  backend, reload/remount the renderer, and reset window-scoped state. Never
  model it as a soft in-renderer switch.
- **Live profile swap** changes the active socket while background profiles keep
  streaming; lists merge, and only explicit user selection starts a fresh draft.

After a switch, active socket, profile, connection atoms, REST routing, and
filesystem routing must agree. Reserve full-screen boot state for a genuinely
unusable backend.

## Resolver and compatibility contract

Every discovery/auth/version/capability policy has one ordered resolver:

1. Express precedence once as data or a pure function.
2. Validate each candidate at the boundary; existence is not proof.
3. Failed reads may fall through; failed authoritative writes surface or roll
   back rather than silently retargeting.
4. Distinguish missing capability from transient failure.
5. Bound retries and end with a real recovery action.

Mint a fresh one-time OAuth/WebSocket ticket for every dial; never reuse it or
fall back to a cached URL. Only confirmed 401/403 or explicitly tagged auth
rejection means reauthentication. Network, timeout, malformed-response, and
server failures stay connectivity errors. Only long-lived token/local auth may
reuse a cached URL as a lower rung. Connection tests exercise the actual
WebSocket/auth leg, not only an HTTP status probe.

Desktop and runtime update on separate clocks. Compatibility fallbacks must be
narrow, tied to an identified older runtime, preserve the feature, and have a
test. Do not build a universal extension ABI for one consumer; internal
registries are composition seams, and Hermes plugin surfaces are not
interchangeable.

## Backend and transport

The renderer talks to `tui_gateway` over JSON-RPC. Framework-agnostic transport
lives in `apps/shared` (`@hermes/shared`: `JsonRpcGatewayClient` and WebSocket URL
helpers), which the web dashboard also consumes; Desktop has no build/runtime
dependency on the dashboard frontend.

Electron normally spawns headless `hermes serve`. `serve` and `dashboard` share
backend setup but neither launches the other; headless mode does not build or
mount the SPA. `backendSupportsServe()` may rewrite the command to legacy
`dashboard --no-open` only for an older runtime that does not register `serve`.
Do not broaden that fallback.

## Slash commands and extensions

The backend already exposes built-ins, user `quick_commands`, and skill-derived
commands through `commands.catalog` and `complete.slash`. Do not add another RPC.

`src/lib/desktop-slash-commands.ts` curates Desktop built-ins:

- `isDesktopSlashCommand` gates execution and allows non-built-in extensions.
- `isDesktopSlashSuggestion` gates discovery for both empty-query catalog and
  typed-query completion paths.
- `isDesktopSlashExtensionCommand` identifies skills/quick commands. Both the
  suggestion path and `filterDesktopCommandsCatalog` must allow it through.

Curation hides built-ins with no Desktop surface; it must not hide user-enabled
extensions. Typed extension commands dispatch through `slash.exec`, then
`command.dispatch`; a skill result becomes a normal prompt. Desktop-owned
built-ins remain local or use `commands.catalog`.

Regression check:

```bash
cd apps/desktop
npx vitest run src/lib/desktop-slash-commands.test.ts
```

## Experience and performance

- Background events never navigate, move focus, or open a surface. Offer; do not
  hijack.
- Empty, loading, reconnecting, degraded/stale, and exhausted-recovery are
  distinct states with honest copy and a way out.
- Focus owns keyboard input; one cancel gesture performs one action.
- Expensive stateful surfaces remain mounted when hidden; visibility is not
  lifecycle.
- Keep hot state local/narrowly derived, avoid per-frame subscriptions in heavy
  trees, coalesce pointer work, and avoid layout reads immediately after writes.
  Prove performance with realistic transcripts/content, not an empty demo.

## Proof before handoff

Test behavior at seams: resolver fallthrough, identity/scope boundaries,
optimistic rollback, stale-response ordering, local/remote adapters, and profile
routing. Confirm that async failure leaves usable UI and recovery, background
work cannot steal the foreground, hot interactions stay cheap, every locale is
updated, and the [`DESIGN.md`](./DESIGN.md) checklist passes.
