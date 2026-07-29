# Review-required Kanban effect — repository candidate (default-off, not live)

> **Status: repository-only candidate. Default-off. Not wired into the live
> product and not applied to any running board.** This document describes a
> design that exists in the tree to be reviewed and exercised by tests. Nothing
> here runs in the dispatcher, cron, gateway, plugins, or config. A fresh
> production Kanban DB has none of the tables this feature uses.

## What it is

A durable, exactly-once "effect" mechanism that lets a worker declare, at the
moment it completes or blocks a task, that a GitHub PR must pass review at an
**exact head commit** before downstream QA/Release cards may proceed.

The producer writes a validated, typed record inside the **same transaction**
as the status change. A separate, explicitly-invoked reconciler later turns
that record into exactly one authoritative review card, using an **injected**
(never network-fetched-under-lock) GitHub observation to decide readiness.

Code lives in [`hermes_cli/kanban_effects.py`](../../hermes_cli/kanban_effects.py);
the producer hooks are in `complete_task` / `block_task` in
[`hermes_cli/kanban_db.py`](../../hermes_cli/kanban_db.py); the optional tool
argument is in [`tools/kanban_tools.py`](../../tools/kanban_tools.py). Tests are
in [`tests/hermes_cli/test_kanban_effects.py`](../../tests/hermes_cli/test_kanban_effects.py).

## Why it is default-off and how

The effect/outbox/lane tables are created **only** by the explicit installer
`kanban_effects.install_effects_schema(conn)`. They are deliberately **not** in
`SCHEMA_SQL`, and no `connect`, `init_db`, migration, dispatcher tick, cron,
gateway watcher, plugin, config path, or read-only operation ever creates them.
So:

* A normal board never gains these tables.
* The producer (`complete_task` / `block_task` with a `review_required`
  payload) validates the payload but **records nothing** when the tables are
  absent — a safe no-op. Passing no payload is byte-for-byte the old behavior.
* Only tests and an offline harness that call `install_effects_schema` see any
  effect at all.

## Data model (installer-only tables)

| table | role |
|---|---|
| `kanban_effect_lanes` | one lane per `(repo, pr, kind)`; holds the CAS `revision`, the bound `review_task_id`, and the reviewed `head_sha`. `UNIQUE(repo, pr_number, kind)`. |
| `kanban_effects` | the durable state machine: `state` (pending→leased→done/failed→dlq), `attempts`/`max_attempts`, `lease_owner`/`lease_expires_at`, monotonic `next_attempt_at` backoff, `payload_hash`, CAS `revision`, bound `target_task_id`. `UNIQUE(source_task_id, lane_id, payload_hash)` preserves independent source provenance while deduplicating replay. `CHECK (state <> 'done' OR target_task_id IS NOT NULL)` forbids a terminal *done* effect with no bound target; a partial `UNIQUE(lane_id) WHERE state='leased'` allows at most one in-flight reconcile per lane. |
| `kanban_effect_outbox` | the append-once source record written in the transition txn, keyed by the deterministic `effect_id`. |

### Canonical identities

* `canonical_lane_id(repo, pr, kind)` — deterministic per PR lane.
* `canonical_payload_hash(payload)` — deterministic, key-order-independent
  identity of *what review is required* (`repo`, `pr`, exact `head_sha`,
  `required_checks`). `downstream_task_ids` are a binding hint and are **not**
  part of payload identity.
* `canonical_effect_id(lane_id, payload_hash, source_task_id)` — one effect per
  source handoff + (lane, payload); distinct implementation sources retain
  provenance while duplicate replays collapse via `INSERT OR IGNORE`.

Correctness never relies on `tasks.idempotency_key`.

## Producer contract

```python
kb.complete_task(conn, task_id, summary="…", review_required={
    "kind": "review_required",
    "repo": "owner/name",
    "pr_number": 123,
    "head_sha": "<exact 40-char hex>",
    "required_checks": ["ci"],              # optional policy tightening
    "downstream_task_ids": ["t_qa", "t_rel"] # optional binding hint
})
```

* Absent payload → existing behavior, exactly.
* Present but invalid → `EffectValidationError` (typed `code`) **before** any
  state change; the task is not mutated.
* Present + valid + schema installed → one lane/effect/outbox record persisted
  inside the completion/block transaction.

`block_task` takes the same optional `review_required` argument and records the
effect in each successful block routing (`blocked`, dependency→`todo`, loop→`triage`).

## Reconciler

`reconcile_effect(conn, effect_id, observer, *, now)`:

1. **Lease** the effect (own txn, CAS on `revision`).
2. **Observe** GitHub through the injected `GithubObserver` — strictly
   **outside** any DB transaction (no network under lock).
3. **Evaluate readiness** (pure): an observation older than the freshness
   window (**60s** by default) is `OBSERVATION_STALE`; a missing required-check
   policy returns `NOT_READY(REQUIRED_CHECK_POLICY_MISSING)`; a head mismatch is
   `STALE_HEAD`; a closed PR / merge conflict / unfinished / failed check are
   each typed NOT_READY. Not-ready schedules a backoff retry, or moves to the
   DLQ once `max_attempts` is exhausted.
4. **One outer write transaction**: re-read the effect payload hash + revision,
   **re-check the FULL readiness predicate** (temporal/status/policy/checks/exact
   head) against the already-injected observation — under the lock, with no new
   network call and no `assert` — CAS the lane forward, **create or reuse
   exactly one parentless exact-head review card**, re-parent the pre-created
   downstream QA/Release cards under it and demote any `todo`/`ready` ones to
   `todo`, then bind `effect.target_task_id` + `lane.review_task_id` and mark
   the effect `done`. The transaction-internal `_create_task_locked` /
   `_link_tasks_locked` helpers compose here with **no nested `BEGIN`**.

Before minting a fresh card the reconciler returns a typed disposition and
leaves the lane unchanged (retried, never silently dropped) when:

* the bound old-head review is still active (running/done/review/blocked) at a
  different head — `DRAINING_ACTIVE_REVIEW`, so no *parallel* review is spawned;
* a manual/unbound parentless review card already exists for the same repo/PR —
  `CONFLICT_MANUAL_REVIEW_UNBOUND`, so a human reconciles it first.

Every effect-minted card carries a durable, discoverable **reverse effect
identity**: a `review_card_minted` event (`effect_id`/`lane_id`/`head_sha`) plus
a `[hermes-review-effect]` body marker and a `created_by='kanban_effects'` stamp.
`find_unbound_review_cards` / `effect_identity_of_card` use these to tell an
auto-created card apart from a manual one.

The injected observation interface performs **no network I/O**; the offline
harness and tests use `InMemoryGithubObserver`.

## Offline harness CLI (explicit, default-off)

`python -m hermes_cli.kanban_effects scan [--now N] [--observations FILE]
[--dry-run]` reconciles every due effect against **injected** observations
(a JSON list — never the network) and prints a typed per-effect disposition
(`reconciled` / `not_ready` / `draining` / `conflict` / `stale` / `skipped`).
`… list [--state S]` dumps effect rows. On a normal default-off board (no effect
tables) it fails loudly (exit 2). Nothing invokes it automatically — there is no
reconcile loop, timer, cron, or dispatcher wiring.

## Downstream containment (alert only)

Nothing is auto-remediated. The scanner surfaces:

* `DOWNSTREAM_RAN_WITHOUT_EXACT_HEAD_APPROVE` — a downstream card is
  running/done without a compatible exact-head APPROVE (R18).
* `UNBOUND_REVIEW_CONFLICT` — a manual/unbound review card conflicts with the
  lane's single authoritative card (R19).
* `STALE_RUNNING_REVIEW` — a running review whose head has moved on; drain it,
  do not spawn a parallel review (R20).

Legacy untyped, free-text review requests (`detect_legacy_review_text`) produce
an **alert only** and never auto-create a review card (R21).

## Explicitly out of scope (not wired)

No dispatcher tick, cron, plugin, gateway watcher, or config reads or drives any
of this. There is no automatic reconciliation loop. The reconciler and scanner
are invoked only by tests and a would-be offline harness.

## Requirement map

R01–R21 are enumerated in the module docstring of `hermes_cli/kanban_effects.py`
and each is covered by a test in `tests/hermes_cli/test_kanban_effects.py`,
including 20-worker concurrency (exactly-once), fake-clock retry/DLQ, and the
no-nested-`BEGIN` regression.
