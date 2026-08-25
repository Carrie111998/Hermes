# Desktop Engineering Guide

Read this with the repository `AGENTS.md` and [`DESIGN.md`](./DESIGN.md).
Never weaken an invariant to make a change easier.
## What this app is

Desktop is a native chat surface, not the dashboard or an embedded TUI. Three
parties are authoritative: **Electron** owns machine/process lifecycle and the
narrow native bridge; **the renderer** owns the experience; **the backend**
owns agent work. The renderer never reaches for Node/Electron directly or
rebuilds agent behavior in React.

It talks to `tui_gateway` over JSON-RPC using `@hermes/shared`; it does not
depend on the dashboard frontend and spawns headless `hermes serve`.
`backendSupportsServe()` alone rewrites older runtimes to
`dashboard --no-open` so a new app can still start an un-upgraded runtime.
## Decide state by authority

Put state with whoever may be right: **backend** for anything another Hermes
surface can change (renderer copies are caches), **Electron** for machine facts,
and **renderer** only for this window's presentation. Shared renderer state
lives in feature-owned stores, server data in the query layer, interaction
detail in the component, and hot non-painting coordination in a ref. Persisted
keys declare their global/connection/profile/session/project/window scope.
## Identity is not incidental

Sessions have more than one identity, and conflating them is a recurring source
of "session not found" and vanishing history. Durable navigation and anything
the user pins keys off the **stable** identity; live streaming keys off the
**runtime** identity; state that must outlive compression keys off the
**lineage root**. Translate at the boundary, never inward.
## Bot Mode is one canonical forever-chat

One bot has exactly one canonical chat: the registry row keyed by
`(profile, session titled exactly "Bot Chat")`. On every open, resolve it with
the exact-title `session.list {title, include_hidden: true}` lookup; hidden rows
must resolve, and a compression lineage selects its live tip. A lookup error
fails closed: abort, never treat failure as "missing" and create another chat.

Only a confirmed missing row permits creation. Creation is **adopt-before-mint**:
repeat the registry lookup immediately before minting; adopt a row found there,
otherwise create one hidden `Bot Chat` and start the bot intro. This prevents
concurrent opens and silently rejected title collisions from forking history.

Never store or consult a session-ID pin, even as fallback or verification.
Never choose by recency, visibility, `last_session`, or where the user left off;
side chats remain sidebar sessions and never become the bot row's target. There
is no per-bot session browser. Registry preview, activity, and open paths must
identify the same lineage tip. The canonical-chat registry, creation, hiding,
and `profiles.list` regression tests encode this data-loss contract.

## Server truth is cached, not owned

- **Merge, don't clobber:** refresh cannot drop live or pinned rows.
- **Be optimistic, then honest:** roll failed writes back; server truth wins.
- **Guard against the past:** stale responses never overwrite newer intent.
- **Isolate the foreground:** background work updates its own cache quietly.
- **Coalesce noise, flush signal:** batch cosmetics, not terminal transitions;
  preserve reference identity on no-ops.
## Switching context is a re-home, not a reboot

Changing profile, connection, or mode keeps the shell and user work; only the
gateway-bound view is repopulated. Do not conflate three switch shapes:

- A **connection/mode apply** (local <-> remote <-> cloud) is the soft re-home:
  wipe gateway-bound stores, then reconnect; query invalidation is insufficient.
- A **runtime home change** (switching the underlying `HERMES_HOME` profile)
  is hard: the window reloads and state resets by remount.
- A **live profile swap** activates another profile's socket while background
  profiles keep streaming; lists merge rather than wipe.

After any swap the active socket, profile, and connection atoms must agree, or
calls route to the wrong backend.
## Cross everything as an observable ladder

Across versions, profiles, topologies, and partial installs, use one observable
ladder:

1. Precedence is written down, in one place, as data or a pure function.
2. A candidate is trusted only after validation at the right boundary;
   existence is not proof.
3. A failed *read* falls to the next rung; a failed *authoritative write*
   surfaces or rolls back, never silently retargeting.
4. A missing capability may enable a compatibility path; a transient failure
   retries, bounded, ending in a real recovery affordance.
5. One resolver owns each policy, so every caller gets the same answer.

Never reuse one-time credentials. Only confirmed 401/403 means reauthenticate;
timeouts, network, and server failures remain connectivity errors. Test the
connection leg actually used, including WebSocket/auth. Runtime compatibility
fallbacks stay narrow, version-tied, and tested.
## Keep the waist narrow, grow at the edges

New capability uses the smallest sufficient surface. Internal registries are
composition seams, not a public plugin ABI; never build one for one consumer.

When the capability is **agent-callable** (open a pane, read the in-app
browser, react to a message), it is a property of the SESSION's client, not the
backend host: wire availability off the session source the app already sends on
`session.create` (`source: 'desktop'`), never off a backend env var — that
process may be a remote gateway. See the root `AGENTS.md`, "Surface capability
is a property of the SESSION."

**Slash commands are curated client-side, then dispatched.** The backend
supplies built-ins, `quick_commands`, and skills. In
`desktop-slash-commands.ts`, `isDesktopSlashCommand()` gates execution,
`isDesktopSlashSuggestion()` discovery, and
`isDesktopSlashExtensionCommand()` identifies non-built-ins. Curation hides
noise, not activated extensions: keep extension commands in both suggestions
and catalog filtering, or skills work when typed but vanish from the palette.
## Respect the person using it

- Never navigate, move focus, or open a surface because something *happened*
  in the background. Offer; don't hijack.
- Empty, loading, reconnecting, degraded/stale, and recovery-exhausted are
  distinct states with honest copy and a way out.
- Keyboard ownership follows focus; one cancel gesture does one thing, and
  expensive stateful surfaces stay alive when hidden — visibility is not
  lifecycle.
- Performance is felt: keep hot-path state local, don't subscribe heavy trees
  to per-frame updates, and prove speed on a long transcript.
## Testing as a habit of proof

Favor invariants over snapshots. Exercise real resolver failures, identity and
scope boundaries, rollback/order races, and both sides of routed adapters.
## The taste test before you hand off

- Does every piece of state live with its authority, at the narrowest scope?
- Would a background event ever steal the foreground or the user's focus?
- Does each resolver have one home, a validated ladder, a bounded end?
- Do local, remote, and profile routing agree, and does async failure leave a
  usable UI?
- Does it pass the [`DESIGN.md`](./DESIGN.md) checklist and update all locales?
