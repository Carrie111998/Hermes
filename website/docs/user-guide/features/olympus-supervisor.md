---
sidebar_position: 8
title: Olympus supervisor
description: Observe and rank Olympus work without launching it.
---

# Olympus supervisor

Phase A is a reboot-safe, observe-only coordinator for Olympus. Hermes Kanban
is its only task and mission-state queue. Mission Control JSON, Telegram
drafts, proposed provider goals, Hermes job diagnostics, and War Room evidence
are projections or observations; none can schedule work.

The supervisor has no provider launcher and never claims a card, creates a
worktree, edits a repository, consumes an approval, appends a ledger, changes a
service, or sends a live Telegram message.

## Canonical Kanban card

An Olympus card is an ordinary Kanban task with `tenant="olympus"`.
Dependencies use normal Kanban `task_links`. The task body is strict JSON:

```json
{
  "schema_version": "olympus-kanban-task/1",
  "enabled": true,
  "risk": "medium",
  "providers": ["codex", "claude"],
  "estimated_cost_usd": 0,
  "estimated_tokens": 0,
  "authority": {
    "status": "active",
    "recommendation_allowed": true,
    "authority_id": "authority-example",
    "revision": 1,
    "expires_at": "2026-08-01T00:00:00Z"
  },
  "approval": {
    "required": false,
    "status": "not_required"
  },
  "goal": {
    "objective": "Produce the bounded deliverable described by this card.",
    "max_turns": 20,
    "timeout_seconds": 1800,
    "allowed_paths": [],
    "forbidden_actions": ["push", "deploy"],
    "deliverables": ["result.md"]
  }
}
```

An active Kanban lease also requires `assigned_provider` and an exact
`assigned_slot` such as `codex:1`. Missing or conflicting ownership fails
closed because provider occupancy would otherwise be ambiguous.

## Selection and state machine

Only enabled, incomplete, unleased `ready` cards with resolved parents,
current recommendation authority, acceptable risk, an available compatible
provider, and a cost estimate within the configured proposal limits are
eligible.

Ranking is deterministic:

1. priority, descending;
2. unresolved dependency count, ascending;
3. risk, ascending;
4. creation time, ascending (older first);
5. compatible provider headroom, descending;
6. task ID.

Each selected card gets a deterministic `olympus-provider-goal/1` proposal
with bounded turns, timeout, cost, paths, deliverables, and explicit
prohibitions. `launch_authorized` and `authority_consumed` are always false.

Default proposal capacity is two Codex slots, two Claude slots, one Grok slot,
and one Hermes-orchestration slot. Capacity and provider availability are
configured under `olympus_supervisor.providers`; they account for
recommendations only and never authorize a launch. Active provider consumers
from job diagnostics count even when they are not Olympus jobs. If diagnostics
cannot identify non-Olympus occupancy, affected capacity is reported as
`ambiguous`, never available.

Supervisor states are `working`, `idle`, `waiting`, `blocked`, `stopped`, and
`failed`. Idle/waiting cycles use bounded exponential backoff while checking
the stop control, queue file, and heartbeat on short intervals. A configurable
`max_consecutive_cycle_failures` (default `3`) persists in the checkpoint.
Only a fully successful cycle resets it; reaching the ceiling stops fail-closed
with a durable failure record and a Telegram draft that is never sent.

Reconciliation reads the existing Hermes job-diagnostics snapshot. Stale,
dead, and blocked lanes are reported explicitly and retain their
`hermes jobs why-slow` and `resume-plan` inspection commands; the supervisor
never executes a resume plan.

## CLI

```bash
hermes olympus-supervisor run --board olympus --max-cycles 20 --max-cycle-cost-usd 0 --max-cycle-tokens 0 --json
hermes olympus-supervisor run-once
hermes olympus-supervisor inspect
hermes olympus-supervisor queue
hermes olympus-supervisor explain-next
hermes olympus-supervisor checkpoint
hermes olympus-supervisor health
hermes olympus-supervisor stop --reason "operator emergency stop"
hermes olympus-supervisor resume
hermes olympus-supervisor render-mission-control
hermes olympus-supervisor telegram-preview
```

`run` is the bounded short-soak surface. `--max-cycles` is mandatory and must
be between 1 and 100; Phase A has no authorized unbounded production mode. The
command completes exactly that many successful cycles unless STOP, duplicate
ownership, or the persisted failure ceiling ends it. A restart with the same
bounds resumes the unfinished soak instead of starting the requested cycle
count again. Reissuing the same bounds after the completed checkpoint is
idempotent, which prevents a crash after the final checkpoint from duplicating
the soak.

Short-soak mode requires explicit task `estimated_cost_usd` and
`estimated_tokens` values. The effective per-cycle ceilings come from
`olympus_supervisor.max_cycle_estimated_cost_usd` and
`olympus_supervisor.max_cycle_estimated_tokens`; the optional CLI values can
only make those configured ceilings stricter. Both defaults are zero. The
shorter command below is therefore equivalent when those reviewed defaults
remain in force:

```bash
hermes olympus-supervisor run --board olympus --max-cycles 20 --json
```

`queue` and `explain-next` are fresh read-only evaluations. `resume` only
clears the stop control; it does not start a cycle. Every action supports
`--json`.

## Queue identity

The checkpoint keeps two independently verifiable identities:

- **Semantic queue identity** drives generation, backoff, recommendation
  identity, and in-cycle drift refusal. It covers task definitions,
  dependencies, status, authority, priority, provider routing, and active
  ownership.
- **Operational occupancy snapshot** records volatile claim tokens, lease
  expiry, worker PIDs, heartbeats, and run start/end timestamps. Changes to
  those fields alone do not churn the semantic generation.

Every snapshot still validates task/run lease agreement, active ownership, and
slot conflicts before either identity is accepted. Semantic mutations or
conflicting ownership fail closed.

## Persistent paths

The default state root is profile-safe:
`$HERMES_HOME/olympus-supervisor/`.

- `checkpoint.json`: atomic restart checkpoint, semantic queue generation,
  operational occupancy identity, short-soak progress, and retry count.
- `heartbeat.json`: current supervisor health.
- `supervisor-lease.json` and `supervisor.lock`: duplicate prevention.
- `STOP.json`: explicit global stop.
- `proposed-goals.json`: prepared goals; never an execution queue.
- `projections/mission-control.json`: non-authoritative read-only projection.
- `drafts/telegram-outbox.json`: draft-only test sink (`sent: false`).
- `last-failure.json`: most recent fail-closed diagnostic.

Mission Control should read its projection. It must never write task state back
through this file. Telegram delivery is deliberately absent in Phase A.

## Reboot persistence (inactive)

The repository ships
`packaging/launchd/ai.hermes.olympus-supervisor.plist.inactive`. It contains
bounded-cycle/cost/token placeholders and `Disabled=true`; this file is not
loaded or installed by Hermes. It cannot silently start an unbounded loop, and
`KeepAlive=false` prevents launchd from defeating the retry ceiling.

For a separately authorized installation:

1. replace `__HERMES_EXECUTABLE__`, `__HERMES_HOME__`,
   `__HERMES_AGENT_REPOSITORY__`, `__MAX_CYCLES__`,
   `__MAX_CYCLE_COST_USD__`, and `__MAX_CYCLE_TOKENS__`;
2. remove the `Disabled` key from the installed copy;
3. install it at
   `~/Library/LaunchAgents/ai.hermes.olympus-supervisor.plist`;
4. validate with `plutil -lint`;
5. start with `launchctl bootstrap gui/$(id -u) <installed-plist>`.

Logs are `$HERMES_HOME/logs/olympus-supervisor.log` and
`olympus-supervisor.error.log`. Health is
`hermes olympus-supervisor health`.

Normal stop is the explicit stop file:

```bash
hermes olympus-supervisor stop --reason "maintenance"
```

The loop observes the control during a cycle and during backoff, preserves its
checkpoint, and exits without touching Kanban. A separately authorized service
unload uses `launchctl bootout gui/$(id -u) <installed-plist>`.

Rollback is: set the stop control, boot out the installed definition, move the
installed plist aside, and keep the state directory for inspection. Emergency
disable is the same stop command; do not delete the checkpoint or Kanban DB.
After review, `resume` clears only the stop file. A service must still be
started separately.
