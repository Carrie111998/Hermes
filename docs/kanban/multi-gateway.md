# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

## Independent dispatcher and notifier ownership

Only one gateway owns the kanban dispatcher. The dispatch-owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`.

Notification ownership is separate. Every gateway with a connected adapter is
eligible for a machine-global notifier lease, regardless of
`kanban.dispatch_in_gateway`. The first eligible gateway to acquire
`<kanban-home>/kanban/.notifier.lock` becomes the notifier owner and polls all
boards. Other gateways keep retrying the lease but do **not** enumerate or open
board DBs while they are non-owners. If the owner exits, the OS releases the
advisory lock and a waiting gateway takes over.

Subscriptions remain stamped with the profile that created them
(`notifier_profile`). The elected owner routes through that profile's adapter
when it hosts one, preserving profile isolation; subscriptions stamped
`default` continue to use the default adapter. For installations with disjoint
profile gateways, start the intended connected-adapter gateway first, or use a
multiplex gateway that hosts the profiles whose subscriptions it must deliver.

**Why this matters:** tying notifier startup to `dispatch_in_gateway` makes
`dispatch_in_gateway: false` silently disable completion and blocked-event
delivery. Letting every gateway poll independently fixes delivery but makes N
processes open every SQLite board. The separate lease preserves notifier-only
operation while keeping exactly one board-polling process.

## Configuration

On the dispatch-owning gateway (typically the `default` profile), no change is
needed. On every other profile gateway, add to `~/.hermes/config.yaml`:

```yaml
kanban:
  dispatch_in_gateway: false
```

Or set the env var: `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens per-board DBs? | Runs dispatcher? | Runs notifier? |
|---|---|---|---|---|
| dispatch owner + notifier lease owner | true (default) | yes | yes | yes |
| dispatcher-only gateway (notifier lease held elsewhere) | true | yes, for dispatch | yes | waits for lease |
| notifier-only lease owner | false | yes | no | yes |
| connected non-owner | false | no | no | waits for lease |

The notifier lease does not enable dispatch and the dispatcher flag does not
grant the notifier lease. These are two independent ownership decisions.
