# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

Task subscriptions also cover review feedback. A `changes_requested` review
event is delivered as an actionable review-BLOCK notification. Subscriptions
using `notify+wake` additionally wake the exact originating chat/thread/session
so the controller inspects the existing card and current run; `notify` remains
passive-only and `wake` remains wake-only. Review feedback never creates,
unblocks, requeues, or otherwise mutates a task.

## Single-dispatcher posture

Only one gateway owns the kanban dispatcher. The owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`.

**Why this matters:** dispatching is single-owner so multiple gateways do not
race to spawn the same work. Notification delivery is profile-owned instead:
each gateway polls only subscriptions for profiles whose platform adapters it
hosts. The atomic event claim prevents duplicate delivery across watcher
processes.

**The dispatcher lock is a hard backstop, not a preference.** The gateway that
runs the dispatcher holds an exclusive flock on
`<kanban-root>/kanban/.dispatcher.lock` (the machine-global kanban root,
shared across all profiles). A non-factory profile must **never** hold this
lock: a helper/secondary profile that accidentally flips `dispatch_in_gateway`
on (or lacks an explicit `kanban:` section and inherits the default `true`)
will silently race the default/factory gateway for every board.

**Only explicitly allowed profiles may even attempt the lock.** Since the
durable-lock rework (t_77f0d093), the dispatcher code gates on
`kanban.dispatch_profiles` (default `["default"]`): a gateway whose profile is
not named there logs "profile ... is not a dispatch profile" and never touches
`.dispatcher.lock` — even if its own `dispatch_in_gateway` is true. Factory
worker profiles (dev/lead/qa/reviewer), helpers and freshly created profiles
therefore cannot steal the lock from the main gateway. A second dispatcher is
an explicit opt-in:

```yaml
kanban:
  dispatch_profiles: ["default", "dispatcher-2"]
```

**The lock is durable, not a boot-time decision.** The holder writes a lease
record (owner pid/profile/host + per-tick heartbeat) into the lock file.
Contender gateways that find the lock contended at boot do not give up
forever: they re-check the flock every `kanban.lock_takeover_interval`
(default 30s), so a dead dispatcher-gateway is replaced within ~a minute
(incident 2026-08-13: the board starved for hours because nothing re-acquired
the flock after the holder died). A lease whose heartbeat is stale while the
owner pid is still alive means a wedged dispatcher loop — a live flock cannot
be stolen, so contenders log a rate-limited warning telling ops to restart
the owner gateway. A holder whose profile is not dispatch-eligible (or that
has been challenged by an eligible contender) releases the lock and stands
down on its next tick.

**Fresh profiles are safe by default.** `hermes profile create <name>` (without
`--clone`) seeds the new profile's `config.yaml` with the dispatcher off:

```yaml
kanban:
  dispatch_in_gateway: false
  enabled: false
```

So a brand-new non-factory profile starts its gateway without touching
`.dispatcher.lock`. Factory dispatcher profiles (e.g. `default`, and any
profile explicitly intended to dispatch) opt in with
`dispatch_in_gateway: true` in their own config. If you clone a profile with
`--clone` / `--clone-all`, the source's `kanban:` section is copied as-is —
check it after cloning so a clone of the dispatcher doesn't become a second
dispatcher by accident.

## Configuration

On the dispatch-owning gateway (typically the `default` profile), no change is
needed. On every other profile gateway, add to `~/.hermes/config.yaml`:

```yaml
kanban:
  dispatch_in_gateway: false
```

Or set the env var: `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens subscribed board DBs? | Dispatcher | Notifier |
|---|---|---|---|---|
| default (confirmed dispatch-lock owner) | true (default) | yes | yes | owned profiles + legacy unstamped subscriptions |
| writer, admin, coder, etc. | false | yes, when the profile has subscriptions | no | that gateway's owned profiles |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.). They do not dispatch tasks, and they skip boards
that have no subscriptions owned by their profiles.
