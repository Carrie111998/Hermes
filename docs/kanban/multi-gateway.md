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

## Notifier ownership is per transport-owner profile

Each gateway derives the profiles it can currently serve from its connected
adapter registries:

- the process's primary adapters belong to its startup profile;
- multiplexed secondary adapters belong to the corresponding secondary profile.

For each serviceable transport-owner profile, the gateway takes a non-blocking
advisory lock under the shared Kanban home. The lock scope is the
**credential-owning profile**, not the whole machine, the dispatcher, or the
logical runtime profile selected by `profile_routes`:

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

Notification subscriptions store two independent identities:

- `source_profile` is the logical runtime/session namespace that created the
  task and must receive a future wake turn.
- `notifier_profile` is the transport-owner profile whose credential-bound
  adapter/listener can send the notification.

Blank legacy rows map both identities to `default`. A gateway sends a row only
when it owns `notifier_profile` and that profile has the requested platform
adapter connected. A shared primary bot can therefore own delivery for a
secondary routed runtime without rewriting the task's logical source.

For gateway-created tasks and auto-subscriptions, Hermes stamps the effective
inbound `source.profile` separately from the adapter provenance. This is
load-bearing in multiplex mode: a task routed to the `writer` runtime stays
logically owned by `writer` even when the default profile owns the shared bot and
the command executes inside that gateway process. Unstamped sources fall back to
the process's startup profile.

A named standalone gateway recognizes its own profile as primary. A multiplex
gateway resolves secondary-profile subscriptions through
`_profile_adapters[profile]`; it never silently falls back to the primary bot.

## Failover

Ownership is reconciled every notifier tick. When a profile loses all connected
adapters, its gateway releases that profile lock immediately. Eligible peers
retry lock acquisition at most one second later while they are waiting. If the
adapter reconnects after another peer took ownership, the original gateway stays
the lock loser, so there is still only one sender.

Subscription cursors, event claims, and per-chunk acknowledgements remain in the
board DB across handoff. A process crash releases the advisory transport lock;
its durable event lease expires after a bounded interval (60 seconds by
default), after which a peer can reclaim only the unfinished tail. Already
acknowledged chunks are not resent.

## Delivery and retry semantics

The notifier provides durable delivery with one stable logical identity per
event chunk. Claiming an event stores an owner and lease but does not advance the
subscription cursor. Chunk boundaries, `pending` versus `attempting` state,
attempt counts, stable delivery keys, and acknowledgements are persisted in
`kanban_notify_deliveries`. The cursor advances only when the exact event has no
unacknowledged chunks. A crash before send replays the unsent chunk; a crash
after send but before acknowledgement replays the same delivery key so
idempotent transports suppress a second user-visible message. Transports whose
upstream API offers no idempotency primitive retain the unavoidable
at-least-once ambiguity at that one crash boundary; already acknowledged
prefixes are never resent.

Blocked and scheduled events render a complete human review brief with `ASK`,
`WHY GATED`, `SCOPE`, `ROLLBACK`, and `REPLY`; scheduled reviews also retain
their `WINDOW`. Optional `DEADLINE` and `SAFE DEFAULT` fields are preserved, and
every brief carries explicit `APPROVE <task-id>` / `VETO <task-id>` targets. The
original reason is never cut at 160 characters. Non-chunking adapters receive
ordered chunks within their `MAX_MESSAGE_LENGTH`; native-chunking adapters
receive the complete payload.

For push adapters, each successful chunk is acknowledged immediately and retries
resume at the first unfinished chunk. For the stateless API server, the wake
self-post uses `source_profile`'s `/p/<profile>/v1/chat/completions` route
and the stable delivery key as `Idempotency-Key`; it succeeds before the event is
acknowledged. Failure counters remain diagnostic only: they never delete the
subscription or its unsent tail. A terminal task subscription is removed only
after every required chunk/wake for the terminal event is acknowledged.

Independent subscriptions are delivered concurrently with a fixed upper bound,
while a per-subscription advisory lock serializes owner transfer and sends.
Every push-send, wake, and artifact attempt has a timeout shorter than the
ownership lease. This prevents one hung chat from blocking the entire notifier
or takeover without allowing two profiles to race the same recipient.

## Troubleshooting

1. Confirm the subscription's `notifier_profile` matches the credential-owning
   gateway profile or one of its multiplexed secondary adapter profiles; confirm
   `source_profile` matches the runtime/session namespace that should be woken.
2. Confirm that same profile has the subscribed platform adapter connected.
3. A non-dispatch gateway should still notify; `dispatch_in_gateway: false` is not
   a notification-disable switch.
4. A duplicate same-profile gateway may be healthy but waiting on the notifier
   ownership lock. It must not poll board DBs until it wins.
5. If notifications stop after adapter loss, check gateway logs for profile-lock
   release/acquisition and reconnect state. Cursor state survives the handoff.
6. Repeated delivery errors increment diagnostic attempt counters but retain the
   subscription. Repair the adapter/API endpoint or explicitly unsubscribe the
   dead recipient; Hermes will continue from the durable unfinished tail.
7. If a gateway died mid-send, allow one lease interval for takeover. Inspect
   `notify_inflight_owner`, `notify_lease_expires_at`, and
   `kanban_notify_deliveries.last_error` when diagnosing a stuck row.
