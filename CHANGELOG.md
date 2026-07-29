# Changelog

Notable developer-facing changes. This file starts at the introduction of the
review-required Kanban effect candidate; earlier history lives in the git log.

## Unreleased

### Added — repository-only candidate (default-off, **not live / not applied**)

- **Review-required Kanban effect candidate** (`hermes_cli/kanban_effects.py`).
  A durable, exactly-once effect that lets a worker declare, when it completes
  or blocks a task, that a GitHub PR must pass review at an exact head commit
  before downstream QA/Release cards proceed. This is a **repository-only**
  design, **default-off**, and is **not wired into the dispatcher, cron,
  gateway, plugins, or config** — it is exercised only by tests and an explicit
  offline harness.
  - The effect/outbox/lane tables are created **only** by the explicit
    `install_effects_schema()` installer. They are intentionally absent from
    `SCHEMA_SQL` and from every `connect`/`init_db`/migration/startup/read-only
    path, so a fresh normal Kanban DB has none of them.
  - Deterministic canonical lane/effect/payload identities (including the
    source task for effect provenance); durable effect
    state machine (pending→leased→done/failed→dlq) with lease/expiry, retry
    backoff, and DLQ; `UNIQUE` constraints + compare-and-swap for idempotency.
  - Injected, **network-free** GitHub observation interface; a missing
    required-check policy returns typed `NOT_READY(REQUIRED_CHECK_POLICY_MISSING)`.
    The reconciler never performs network I/O while a DB transaction is held.
  - Alert-only downstream containment (downstream ran without an exact-head
    APPROVE, unbound/manual review conflict, stale running review) and
    alert-only detection of legacy untyped review text (never auto-creates a
    review card).
  - The authoritative review card is assigned to the valid `review` profile.
  - A new head never spawns a *parallel* review while the bound old-head review
    is still active (running/done/review/blocked): the reconciler returns the
    typed `DRAINING_ACTIVE_REVIEW` disposition and leaves the lane unchanged.
  - Before minting, the reconciler detects manual/unbound parentless review
    cards for the same repo/PR and returns typed
    `CONFLICT_MANUAL_REVIEW_UNBOUND` instead of racing them; every generated
    card carries a durable, discoverable reverse effect identity (a
    `review_card_minted` event + `[hermes-review-effect]` body marker +
    `created_by='kanban_effects'` stamp).
  - SQLite durability: a *done* effect must have a non-null `target_task_id`
    (`CHECK`), and at most one *leased* effect exists per lane (partial
    `UNIQUE` index). `install_effects_schema()` is migration-safe — it rebuilds
    a pre-remediation table in place without losing rows.
  - Observation freshness defaults to 60s; before committing, the reconciler
    re-checks the full readiness predicate (temporal/status/policy/checks/head)
    against the already-injected observation under the lock — no network, and no
    `assert` for stale input.
  - An explicit, default-off `scan`/`list` harness CLI
    (`python -m hermes_cli.kanban_effects`) reconciles due effects against
    injected observations and prints typed per-effect dispositions. It is not
    wired into any dispatcher/cron/loop.
  - See `docs/kanban/review-required-effect.md` for the full design and the
    R01–R21 requirement map.

### Changed

- `hermes_cli/kanban_db.py`: `create_task` and `link_tasks` are refactored into
  transaction-internal helpers (`_create_task_locked`, `_link_tasks_locked`)
  behind behavior-unchanged public wrappers, so they can compose inside one
  outer write transaction with no nested `BEGIN`.
- `complete_task` / `block_task` accept an optional, schema-validated
  `review_required` payload. **Absent payload preserves existing behavior
  exactly.** A valid payload persists a source/outbox effect record inside the
  same transition transaction *when the effect schema is installed*; on a normal
  (default-off) board it is validated and then a no-op. An invalid payload is
  rejected with a typed error before any state change.
- `tools/kanban_tools.py`: `kanban_complete` / `kanban_block` expose the
  optional `review_required` argument (documented as experimental /
  repository-only / default-off).
