# Plugin Observability Hooks — Design Proposal

**Branch:** `feat/plugin-observability-hooks`
**Target:** `hermes_cli/plugins.py` (VALID_HOOKS), `hermes_cli/kanban_db.py`, `gateway/run.py`
**Concrete consumer:** kanban-advanced (board_keeper, intervention tracking, postmortem data)
**Status:** SALVAGE design basis per #64231 batch disposition (teknium1, 2026-08-13)

---

## Summary

Today plugins that need to observe kanban worker lifecycle (spawn, crash, stale
claim) or track manual card mutations must poll the board via cron. This adds
latency (1-minute cron ticks) and burns tokens on polling queries.

This proposal adds observer hooks that let plugins react to events immediately
instead of polling.

## Upstream alignment (August 2026 expansion wave)

The base `kanban_task_{claimed,completed,blocked}` hooks already shipped on
`main` (see `VALID_HOOKS` in `hermes_cli/plugins.py`). This RFC now covers the
**remaining** worker/mutation observers, renamed to the `on_<noun>_<event>`
observer convention per the #64231 batch disposition, and optionally folded
into the inter-plugin event bus.

1. **Inter-plugin event bus** (`ctx.emit` / `ctx.subscribe`, #64164) — a plugin
   that receives a lifecycle hook may re-publish it onto the event bus so
   non-kanban plugins (telemetry, quotas) can subscribe without registering the
   hook themselves. Manifest `emits:` / `listens:` declarations expose the
   surface via `hermes plugins show`.
2. **Manifest v2** (`manifest_version`, `api_version`, `requires_plugins`,
   `python_dependencies`, `config_schema`, #64165) — this RFC's observers are
   additive hook surface; no new manifest fields are required beyond the
   optional `emits`/`listens` declarations.

---

## Proposal 1: Worker lifecycle observers

> Naming follows the #64231 taxonomy: observer-only, `on_<noun>_<event>`, fires
> AFTER durable state is written, exceptions swallowed. The already-shipped
> `kanban_task_claimed` / `kanban_task_completed` / `kanban_task_blocked` keep
> their names; everything below is new. Fire sites are referenced by function
> name (line numbers are current against `origin/main` @ `1706502aa` and will
> drift).

### `on_kanban_worker_spawned` (post-spawn observer)

Fires in the **DISPATCHER** process AFTER `_default_spawn` (`kanban_db.py:10027`)
returns successfully and the worker PID is persisted in the tasks table.
Carries `task_id`, `board`, `assignee`, `run_id`, `profile_name`,
`worker_pid: int`. Use this for "worker is actually running" events.

### `on_kanban_worker_exited` (tick-derived observer)

Fires in the **DISPATCHER** process from `detect_crashed_workers`
(`kanban_db.py:8496`) when a worker PID is discovered dead. Carries `task_id`,
`board`, `assignee`, `run_id`, `profile_name`, `exit_kind`
(`clean_exit` | `nonzero_exit` | `signaled`), `exit_code`. Rate-limited exits
emit a separate `on_kanban_worker_rate_limited` event.

### `on_kanban_worker_stale_claim` (tick-derived observer)

Fires in the **DISPATCHER** process when `release_stale_claims`
(`kanban_db.py:4701`) reclaims a timed-out claim.

```python
# Kwargs: task_id, board, assignee, run_id, profile_name,
#         seconds_stale: int
```

### `on_kanban_dispatch_tick` (absorbs #56066)

Fires in the **DISPATCHER** process after `_dispatch_tick_lock` exits (lock
defined at `kanban_db.py:1534`, taken at `kanban_db.py:9279`), renamed from the
#56066 `kanban_dispatch_tick` (which fired inside the lock). Carries `board`,
`tick_start`, `tick_end`, `dispatched_count: int`, `blocked_count: int`.

### Concrete use case (kanban-advanced)

Our `board_keeper` cron polls the board every minute to detect:
- Stale `running` cards → trigger salvage
- Crashed workers → trigger re-dispatch
- Worker exit → trigger auto_unblock

With these hooks, `board_keeper` becomes event-driven — zero polling latency,
zero token burn on "nothing changed" checks.

---

## Proposal 2: `on_kanban_task_updated` observer

Fires after any UPDATE to a task row (body edit, assignee change, title change,
description update). Observer-only — return values ignored. (The #64231 verdict
phrases the rename as `on_kanban_worker_*` for the worker-lifecycle subset; this
mutation observer follows the same `on_` convention under the `kanban_task`
noun.)

```python
# Kwargs: task_id, board, assignee, profile_name,
#         changed_fields: list[str]  # e.g. ["body", "assignee"]
```

### Concrete use case (kanban-advanced)

Our intervention tracking requires operators to manually run
`kanban_intervention_inc.sh` after editing a card. With this hook, the
intervention counter increments automatically — no manual step, no forgotten
increments.

---

## Proposal 3: Event-bus re-publish (optional, #64164)

The lifecycle hooks fire through the plugin hook mechanism. A plugin that
receives one (e.g. kanban-advanced) may additionally **re-publish** it onto the
inter-plugin event bus so plugins that did not register the hook can still
subscribe. Emission is plugin-initiated, best-effort, and additive:

```python
# Inside a kanban-advanced hook callback:
ctx.emit("worker_exited", {"task_id": task_id, "exit_code": 0})
```

- `ctx.emit` takes the **bare** event name only; the namespace is forced to
  `<plugin_key>:` (`manifest.key or manifest.name`). Passing an already-namespaced
  name (any `:`) is rejected with `ValueError` — fail-closed. The `hermes:`
  prefix is reserved for core.
- The manifest declares `emits: ["worker_exited", "worker_spawned", ...]` and
  `listens: [...]` for `hermes plugins show` discoverability. Declarations are
  advisory in v1 — a plugin may emit/subscribe without declaring.
- Delivery is fire-and-forget through a host-owned single-worker queue; dispatch
  is bounded by `_EVENT_EMIT_DEPTH_CAP` (8) and `_EVENT_PENDING_CAP` (64), so a
  blocked subscriber cannot back-pressure the emitter.

---

## Implementation notes

All hooks follow the same pattern as the shipped kanban lifecycle hooks:
- Fire AFTER the write txn commits (observer safety)
- Swallowed exceptions (a misbehaving plugin can't break board state)
- Standard kwargs: `task_id`, `board`, `assignee`, `run_id`, `profile_name`

### Mutation boundary

`on_kanban_task_updated` must fire for every write path that mutates task
state, regardless of which module performs the write:

**kanban_db.py paths:**
| Function | Changed fields |
|----------|---------------|
| `create_task` | All fields (initial) |
| `complete_task` | status→done, summary, result, metadata |
| `block_task` | status→blocked, last_failure_error |
| `unblock_task` | status→ready/todo, block_recurrences |
| `_set_status_direct` | status |
| `recompute_ready` | status→ready (promotion) |
| `_record_task_failure` | status→blocked, consecutive_failures |

**Dashboard plugin_api.py paths:**
| Endpoint | Changed fields |
|----------|---------------|
| `PATCH /tasks/:id` (L838) | priority, title, body |
| Bulk update (L1254) | priority |

**Payload contract:** `changed_fields` is a list of field names that were
mutated, not just a generic "updated" signal. Consumers can filter on specific
fields without diffing DB state.

### Timing contract

| Hook | Fires | Latency |
|------|-------|---------|
| `on_kanban_worker_spawned` | Post-spawn, after `_default_spawn` + PID persistence | Immediate |
| `on_kanban_worker_exited` | Tick-derived via `detect_crashed_workers` | ≤ dispatcher tick interval |
| `on_kanban_worker_rate_limited` | Tick-derived, separate event kind | ≤ dispatcher tick interval |
| `on_kanban_worker_stale_claim` | Tick-derived via `release_stale_claims` | ≤ dispatcher tick interval |
| `on_kanban_dispatch_tick` | Post-tick, after `_dispatch_tick_lock` exits | ≤ dispatcher tick interval |
| `on_kanban_task_updated` | Post-commit, after any task mutation write | Immediate |

Exit events are NOT immediate — `_default_spawn` is fire-and-forget. Workers are
discovered dead on the next dispatcher tick. Plugins requiring lower latency
should combine hook observation with `kanban_heartbeat` liveness tracking.

---

## Related

- PR #58541 — kanban lifecycle hooks (pre_complete, unblocked, created)
- #64231 — hook taxonomy + batch disposition (SALVAGE verdict for this RFC)
- #64164 — inter-plugin event bus (`ctx.emit` / `ctx.subscribe`)
- #64165 — manifest v2 (`emits`/`listens` declarations)
- #56066 — folded: `kanban_dispatch_tick` → `on_kanban_dispatch_tick`
- kanban-advanced board_keeper architecture
