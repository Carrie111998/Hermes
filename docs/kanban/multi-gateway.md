# Multi-gateway deployment

Hermes supports multiple gateway processes concurrently: standalone gateways
started under different profiles, duplicate processes for failover, and one
multiplex gateway that serves several profiles' adapters.

Kanban has two independent ownership concerns:

- **Dispatch ownership** decides which gateway claims and spawns ready tasks.
- **Notifier ownership** decides which gateway polls and delivers each profile's
  subscribed task events.

Do not use the dispatcher flag to disable notifications. A notifier-only gateway
is a supported deployment.

## Dispatcher ownership

Only one gateway should run the embedded dispatcher. The dispatch owner keeps
`kanban.dispatch_in_gateway: true` (the default). Other standalone gateways set:

```yaml
kanban:
  dispatch_in_gateway: false
```

This flag controls only task dispatch. The gateway still starts its Kanban
notifier and can open board DBs after it acquires notifier ownership.

## Notifier ownership is per profile

Each gateway derives the profiles it can currently serve from its connected
adapter registries:

- the process's primary adapters belong to its startup profile;
- multiplexed secondary adapters belong to the corresponding secondary profile.

For each serviceable profile, the gateway takes a non-blocking advisory lock
under the shared Kanban home. The lock scope is the **profile**, not the whole
machine and not the dispatcher:

| Deployment | Result |
|---|---|
| standalone `default` + standalone `writer` | both poll and notify concurrently |
| two `writer` gateways | exactly one polls `writer`; the peer waits |
| multiplex gateway serving `default` + `writer` | it may own both profile locks and routes each row through that profile's adapter |
| notifier-only gateway (`dispatch_in_gateway: false`) | polls and delivers normally; never dispatches tasks |

A lock loser does not enumerate boards or open board SQLite connections. If the
lock substrate is unavailable, notifier polling fails closed rather than falling
back to duplicate-prone uncoordinated delivery.

## Routing and profile stamps

Notification subscriptions store `notifier_profile`. Blank legacy rows belong to
`default`. A gateway sends a row only when it owns that profile and that profile
has the requested platform adapter connected.

For gateway-created tasks and auto-subscriptions, Hermes stamps the effective
inbound `source.profile`. This is load-bearing in multiplex mode: a task created
through the `writer` bot stays owned by `writer` even though the command executes
inside the default gateway process. Unstamped sources fall back to the process's
startup profile.

A named standalone gateway recognizes its own profile as primary. A multiplex
gateway resolves secondary-profile subscriptions through
`_profile_adapters[profile]`; it never silently falls back to the primary bot.

## Failover

Ownership is reconciled every notifier tick. When a profile loses all connected
adapters, its gateway releases that profile lock immediately. Eligible peers
retry lock acquisition at most one second later while they are waiting. If the
adapter reconnects after another peer took ownership, the original gateway stays
the lock loser, so there is still only one sender.

Subscription cursors remain in the board DB across handoff. Reacquisition does
not replay already-delivered events.

## Delivery and retry semantics

The notifier claims one event item at a time. A successfully delivered item keeps
its cursor progress even if a later item fails. Only the failed item is rewound,
so a scheduled review followed by a transient completion failure does not resend
the scheduled review.

Blocked and scheduled events render a complete human review brief with `ASK`,
`WHY GATED`, `SCOPE`, `ROLLBACK`, and `REPLY`; scheduled reviews also retain
their `WINDOW`. Optional `DEADLINE` and `SAFE DEFAULT` fields are preserved, and
every brief carries explicit `APPROVE <task-id>` / `VETO <task-id>` targets. The
original reason is never cut at 160 characters. Non-chunking adapters receive
ordered chunks within their `MAX_MESSAGE_LENGTH`; native-chunking adapters
receive the complete payload.

For push adapters, every chunk must succeed before the item is acknowledged. For
the stateless API server, the wake self-post is the delivery: it succeeds before
the cursor advances, and failure rewinds the item. Twelve consecutive send/wake
failures drop the dead subscription rather than retrying forever.

## Troubleshooting

1. Confirm the subscription's `notifier_profile` matches the gateway profile or
   one of its multiplexed secondary profiles.
2. Confirm that same profile has the subscribed platform adapter connected.
3. A non-dispatch gateway should still notify; `dispatch_in_gateway: false` is not
   a notification-disable switch.
4. A duplicate same-profile gateway may be healthy but waiting on the notifier
   ownership lock. It must not poll board DBs until it wins.
5. If notifications stop after adapter loss, check gateway logs for profile-lock
   release/acquisition and reconnect state. Cursor state survives the handoff.
6. Repeated delivery errors increment the subscription failure counter. After
   twelve consecutive failures the row is intentionally removed.
