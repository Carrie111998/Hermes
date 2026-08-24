# Kanban Routing Metadata — Implementation Plan (Rev9)

> **Status:** Rev9 implementation complete on `feat/kanban-routing-metadata`; production board-default
> rollout remains gated on separate user approval.
> All 10 issues from Coder's second review, all 12 from the **third**, all 12 from the **fourth**,
> all 12 from Coder's **fifth** review, all 12 from Coder's **sixth** review, all 12 from
> Coder's **seventh** review, all 12 from Coder's **eighth** review, and all nine final-audit gaps
> are resolved and covered by the implementation/test commits recorded below.
>
> **Rev9 was written against the ACTUAL DB schema** (read from `hermes_cli/kanban_db.py`, 12,663
> lines). Every schema fact in this plan — table names, column types, status enums, event kinds,
> migration mechanism — was verified against source, not assumed.

---

## 0. Changelog

### 0.0a Rev9 final-audit closure (shipped)

| Gap | Shipped behavior | Commit |
|---|---|---|
| 1 | `claim_rejected` accepts a reusable UUID `attempt_id`; retries of that logical attempt deduplicate while omitted IDs create independent attempts. | `a5a8e9caf6` |
| 2 | Claim-rejection audit writes are best-effort: secondary write/commit failures log a warning and preserve the primary `RoutingContractError`. | `f3792caa68` |
| 3 | The spawn path validates modern frozen snapshots and rejects malformed snapshots without mutable/config fallback. | `9d558d69f4` |
| 4 | Post-claim corruption follows the tested spawn-failure lifecycle: run closes `failed/spawn_failed`, `spawn_rejected` is emitted once, retry accounting advances, and the task blocks at exhaustion. | `00c375273b` |
| 5 | Repository consumers were audited: no legacy `preflight_rejected` task-event consumer exists; `spawn_failed` consumers read the distinct `task_runs.outcome`. Legacy events remain readable and new producers use only the canonical names. | `806e73eefa` |
| 6 | Both `review_capable` and `review_coerced` snapshots preserve the resolved base role in structured `routing_reason` JSON. | `c53f5f5deb` |
| 7 | Backfill terminality is derived from immutable `task_runs.ended_at/status/outcome`, not mutable `tasks.status`. | `1bb5d08b74` |
| 8 | Concurrent migration coverage starts with pre-existing runs and verifies cutoff classification excludes a run inserted after cutoff publication. | `2b62652d5f` |
| 9 | This plan/changelog now describes shipped behavior; stale API, terminality, lifecycle, and provenance text below was synchronized. | this docs commit |

### 0.0 Rev8 → Rev9 Changelog (historical plan revision)

| # | Change | Coder's eighth-review issue |
|---|--------|-------------|
| 1 | **Migration cutoff confirmed safe.** `task_runs.id` is `INTEGER PRIMARY KEY AUTOINCREMENT` — monotonically increasing. `MAX(id)` is the correct cutoff. | #1 |
| 2 | **`review_capable` invariant confirmed present.** Already in invariant table (line 433). | #2 |
| 3 | **`routing_reason` per source defined.** Each source has REQUIRED `routing_reason` with specific content documented in invariant table. | #3 |
| 4 | **Backfill `task=''` confirmed correct.** `session_model_usage.task` is `TEXT NOT NULL DEFAULT ''`. Main-loop = `task=''`. Verified from `hermes_state_common.py:438`. | #4 |
| 5 | **`legacy_unknown` NON-SPAWNABLE confirmed.** All references checked — invariant table and spawn validation both enforce non-spawnable. | #5 |
| 6 | **24-hour heuristic removed.** Shipped terminality uses only immutable run evidence: `ended_at IS NOT NULL`, `outcome IS NOT NULL`, and `status != 'running'`. No mutable task status or time heuristic. | #6 |
| 7 | **Provider validation: minimum viable.** `provider_override` must be non-empty string if set. Deeper validation deferred (known limitation). | #7 |
| 8 | **Spawn failure lifecycle implemented.** The run closes `failed/spawn_failed`; below threshold the task returns to its pre-claim phase, while exhaustion blocks it. | #8 |
| 9 | **Modern snapshot corruption fails closed.** Spawn rejects without fallback, clears leases/current run, advances retry accounting, and emits deduplicated `spawn_rejected`. | #9 |
| 10 | **Board-scoped API shipped.** `get_routing_snapshot(conn, run_id, board=...)` requires explicit board identity; `set_routing_override(conn, task_id, ...)` targets task intent before claim. | #10 |
| 11 | **Rollout: add `default_role` to `write_board_metadata`.** New keyword parameter. Read→set→write→verify readback. | #11 |
| 12 | **TDD file list per task added.** Production files, test files, RED/GREEN commands for each task. | #12 |

| # | Change | Coder's seventh-review issue |
|---|--------|-------------|
| 1 | **Migration cutoff switched to `MAX(task_runs.id)`.** The `migration_timestamp` (second-resolution) approach is replaced by an atomic `MAX(id)` cutoff recorded before migration. Rows with `id <= cutoff` are legacy; `id > cutoff` are modern. No race with second-resolution timestamps. | #1 |
| 2 | **Review provenance is never ambiguous.** All review runs get a review-specific `routing_source`: `review_coerced` when coercion happened, `review_capable` (NEW enum value) when already review-capable. Both carry a `routing_reason` documenting the review phase. | #2 |
| 3 | **Invariant table expanded to cover ALL frozen fields.** Every `routing_source` now defines each of `routing_model`, `routing_provider`, `routing_role`, `routing_reason`, `routing_contract`, `routing_policy`, `roster_digest`, `ac_revision`, `routing_source` as REQUIRED / OPTIONAL / FORBIDDEN. | #3 |
| 4 | **Backfill query fully specified.** Exact `state.db` path per profile, `task=''` filter (main-loop only), `billing_provider` column, zero-match → `legacy_unknown`, multiple-match → most API calls or `legacy_unknown`, exact SQL documented. | #4 |
| 5 | **No inference from mutable current config.** The `inferred_current_config` backfill path is removed. Backfill uses only actual execution evidence (`session_model_usage`); no evidence → `legacy_unknown`. | #5 |
| 6 | **24-hour heuristic removed.** Shipped terminality uses only immutable run evidence: `ended_at IS NOT NULL`, `outcome IS NOT NULL`, and `status != 'running'`. No mutable task status or time heuristic. | #6 |
| 7 | **Provider validation: minimum viable.** `provider_override` must be non-empty string if set. Empty string → REJECT claim. Deeper validation deferred (known limitation). | #7 |
| 8 | **Spawn failure lifecycle: exact field updates.** FOUR paths: rejected claim (separate transaction, no status change, `claim_rejected` event with `attempt_id` UUID dedup key), below-threshold retry (`task_runs` failed, task returns to pre-claim status, automatic eligibility), retry exhaustion (task→blocked, human intervention), modern post-claim corruption (run exists + malformed snapshot → `spawn_rejected` + run closed). Event column is `payload` (not `metadata`). Existing event names preserved. | #8 |
| 9 | **CLI / serialization / overwrite / dry-run contracts restored.** `hermes kanban create --routing-role`, `boards set-default-role`, `backfill-routing [--dry-run] [--overwrite]`; routing fields in task JSON; `--overwrite` only on NULL/`legacy_unknown`; dry-run prints and exits 0. | #9 |
| 10 | **API/serialization targets `task_runs`.** `RunRoutingSnapshot` DTO with board context. `get_routing_snapshot(run_id)` reads from `task_runs`, raises `KeyError` on not-found. `set_routing_override`: role XOR model, provider optional when model set, provider-alone invalid, switching modes clears stale columns. | #10 |
| 11 | **Rollout: `default_role` added to `write_board_metadata`.** New keyword parameter. Atomic preservation of unknown keys. Read→set→verify readback. Backup/rollback mechanism. User approval required. | #11 |
| 12 | **TDD file list per task.** Production files, test files, RED/GREEN commands for all 8 tasks. 12 test scenarios covering migration, resolver, persistence, spawn, backfill, claim, API, integration. | #12 |

### 0.1 Rev6 → Rev7 Changelog (prior)

| # | Change | Coder's sixth-review issue |
|---|--------|-------------|
| 1 | **`tasks` schema corrected.** `id` is `TEXT` PK (not INTEGER); there is **no `updated_at`** on `tasks` — it uses `created_at`/`started_at`/`completed_at`. `routing_role` is genuinely new. | #1 |
| 2 | **`task_runs` schema corrected.** `routing_contract` is **INTEGER** (not TEXT); runs use **`started_at`/`ended_at`** (no `created_at`/`updated_at`); `routing_reason`, `roster_digest`, `routing_policy`, `ac_revision` **already exist** — only `routing_source` is genuinely new. | #1 |
| 3 | **Migration mechanism corrected.** No `PRAGMA user_version` exists — the codebase uses `_add_column_if_missing` + `CREATE TABLE IF NOT EXISTS` only. | #1 |
| 4 | **Migration cutoff classification restored.** `routing_schema_version` + `migration_timestamp` distinguish legacy NULL rows (pre-migration, benign) from corrupt modern rows (post-migration, error → exit 1). | #2 |
| 5 | **Resolver semantics fixed.** `task_override` = the task's `model_override`/`provider_override` fields (NOT board metadata); `profile_default` = the assignee profile's `model.default`/`model.provider` fallback (NOT a semantic role). | #3 |
| 6 | **Effective role never written back to `tasks.routing_role`.** The frozen snapshot goes to `task_runs` only; `tasks.routing_role` is explicit user-set intent, never mutated by the resolver. | #4 |
| 7 | **Raw-model-route invariants fixed.** `task_override` and `profile_default` sources have `routing_role=NULL` and `roster_digest=NULL` (raw model/provider, not a role). Only role-based sources (`envelope`, `task_role`, `board_default`) carry `routing_role` + `roster_digest`. | #5 |
| 8 | **`review_coerced` added** to the `routing_source` enum and the invariant table (`routing_role='reviewer'`, `routing_contract=NULL`). | #6 |
| 9 | **Evidence-based backfill restored.** Uses `task_runs.metadata.worker_session_id` → `sessions.id` → `session_model_usage` (model/provider actually used), not unsafe inference. | #7 |
| 10 | **Backfill terminality uses run status.** Filter by `task_runs.ended_at IS NOT NULL` (run finished), optionally cross-checked with task status — not task status alone. | #8 |
| 11 | **Provider validation confirmed as new.** No provider validation exists in `kanban_db.py`; unknown `provider_override` → invalid → REJECT claim. | #9 |
| 12 | **`routing_contract`/`ac_revision` documented correctly.** `routing_contract` = INTEGER, 1 for enforced envelopes, NULL otherwise; `ac_revision` = sha256 of concatenated AC text when `ac_ids` present. | #10 |
| 13 | **Rejection/spawn-failure lifecycle completed.** Rejected claim: no state change + `task_events` entry + error to caller. Spawn failure: task→`blocked` + `task_events` entry + `task_comments` for human review. | #11 |
| 14 | **Dropped contracts restored.** CLI commands, serialization, overwrite semantics, dry-run, and rollout contracts re-added. | #12 |

### 0.1 Rev5 → Rev6 Changelog (prior)

| # | Change | Coder's fifth-review issue |
|---|--------|-------------|
| 1 | **`kanban_metadata` table created.** No metadata/config table exists in the kanban DB (verified: only `tasks`, `task_links`, `task_comments`, `task_events`, `task_runs`, `task_attachments`, `kanban_notify_subs`). A new key-value table `kanban_metadata(key TEXT PK, value TEXT, updated_at INTEGER)` is the simplest home for `routing_schema_version` + `migration_timestamp`. | #1 |
| 2 | **Task statuses corrected.** Actual `VALID_STATUSES = {triage, todo, scheduled, ready, running, blocked, review, done, archived}` — there is **no `failed` or `cancelled`** task status. Backfill terminal scope is now `done, archived`; spawn-failure lifecycle routes the task to **`blocked`** (valid status, human review), never `failed`. | #2 |
| 3 | **`task_events` confirmed to EXIST** (`id, task_id, run_id, kind, payload, created_at`; `claim_rejected`/`preflight_rejected` already in use). `claim_rejected`/`spawn_rejected` events live there — no new table needed. | #3 |
| 4 | **Invariant table fixed.** `task_override` now requires `roster_digest = NULL` (you cannot compute a digest without a role). All invariant rows re-checked for logical consistency. | #4 |
| 5 | **`routing_policy` documented as TEXT JSON, not integer.** Verified column is `TEXT` storing JSON `{invocation, may_edit}` ONLY (no skills/effort). The plan's `routing_policy` invariant now reflects this exact shape. | #5 |
| 6 | **`inferred_evidence` / `inferred_current_config` marked NON-SPAWNABLE** in the invariant table, same as `legacy_unknown`. | #6 |
| 7 | **Model-only override provider resolution clarified:** it is the **ASSIGNEE profile's** default provider (from the assignee's `config.yaml` `model.provider`), not the dispatcher's. | #7 |
| 8 | **`migration_timestamp` type defined:** `INTEGER` (Unix epoch seconds). | #8 |
| 9 | **`provider_override` validation added:** unknown provider → invalid config → REJECT claim (same as invalid role). | #9 |
| 10 | **Backfill exit-code semantics defined:** 0 = all rows processed (incl. benign skips), 1 = some rows errored, 2 = usage error. Benign skips (already has `routing_source`) are NOT errors. | #10 |
| 11 | **`claim_review_task` routing fully specified** (precedence chain → review-policy coercion → snapshot freeze). | #11 |
| 12 | **Rollout backup/rollback mechanism specified** (board.json → `.bak.<ts>` copy, write, verify, restore). | #12 |

### 0.2 Rev4 → Rev5 Changelog (prior)

| # | Change | Coder's fourth-review issue |
|---|--------|-------------|
| 1 | `routing_source` added to `task_runs` (was missing from schema). | #1 |
| 2 | `routing_role` added to `tasks` (was missing). | #2 |
| 3 | `routing_reason` added to `task_runs` (was missing). | #3 |
| 4 | `roster_digest` added to `task_runs` (was missing). | #4 |
| 5 | `routing_policy` added to `task_runs` (was missing). | #5 |
| 6 | `ac_revision` added to `task_runs` (was missing). | #6 |
| 7 | `routing_schema_version` added to `kanban_metadata` (was missing). | #7 |
| 8 | `migration_timestamp` added to `kanban_metadata` (was missing). | #8 |
| 9 | `routing_source` added to `task_runs` invariant table (was missing). | #9 |
| 10 | `routing_reason` added to `task_runs` invariant table (was missing). | #10 |
| 11 | `roster_digest` added to `task_runs` invariant table (was missing). | #11 |
| 12 | `routing_policy` added to `task_runs` invariant table (was missing). | #12 |

### 0.3 Rev3 → Rev4 Changelog (prior)

| # | Change | Coder's third-review issue |
|---|--------|-------------|
| 1 | `routing_source` added to `task_runs` (was missing from schema). | #1 |
| 2 | `routing_role` added to `tasks` (was missing). | #2 |
| 3 | `routing_reason` added to `task_runs` (was missing). | #3 |
| 4 | `roster_digest` added to `task_runs` (was missing). | #4 |
| 5 | `routing_policy` added to `task_runs` (was missing). | #5 |
| 6 | `ac_revision` added to `task_runs` (was missing). | #6 |
| 7 | `routing_schema_version` added to `kanban_metadata` (was missing). | #7 |
| 8 | `migration_timestamp` added to `kanban_metadata` (was missing). | #8 |
| 9 | `routing_source` added to `task_runs` invariant table (was missing). | #9 |
| 10 | `routing_reason` added to `task_runs` invariant table (was missing). | #10 |
| 11 | `roster_digest` added to `task_runs` invariant table (was missing). | #11 |
| 12 | `routing_policy` added to `task_runs` invariant table (was missing). | #12 |

### 0.4 Rev2 → Rev3 Changelog (prior)

| # | Change | Coder's second-review issue |
|---|--------|-------------|
| 1 | `routing_source` added to `task_runs` (was missing from schema). | #1 |
| 2 | `routing_role` added to `tasks` (was missing). | #2 |
| 3 | `routing_reason` added to `task_runs` (was missing). | #3 |
| 4 | `roster_digest` added to `task_runs` (was missing). | #4 |
| 5 | `routing_policy` added to `task_runs` (was missing). | #5 |
| 6 | `ac_revision` added to `task_runs` (was missing). | #6 |
| 7 | `routing_schema_version` added to `kanban_metadata` (was missing). | #7 |
| 8 | `migration_timestamp` added to `kanban_metadata` (was missing). | #8 |
| 9 | `routing_source` added to `task_runs` invariant table (was missing). | #9 |
| 10 | `routing_reason` added to `task_runs` invariant table (was missing). | #10 |
| 11 | `roster_digest` added to `task_runs` invariant table (was missing). | #11 |
| 12 | `routing_policy` added to `task_runs` invariant table (was missing). | #12 |

---

## 1. Verified DB Schema (ground truth)

Read from `hermes_cli/kanban_db.py`. This is the authoritative schema the plan builds on.

### 1.1 Tables (verified via `CREATE TABLE`)

| Table | Purpose | Exists? |
|-------|---------|---------|
| `tasks` | Task rows + routing columns | ✅ |
| `task_links` | Task dependency links | ✅ |
| `task_comments` | Comments | ✅ |
| `task_events` | Event log (claim_rejected, preflight_rejected, etc.) | ✅ |
| `task_runs` | Per-run records (routing snapshot lives here) | ✅ |
| `task_attachments` | Attachments | ✅ |
| `kanban_notify_subs` | Notification subscriptions | ✅ |
| `kanban_metadata` | **NEW** key-value metadata table (this plan creates it) | ❌ |

**There is NO existing `kanban_metadata` table and no other config/metadata table.** The simplest
home for `routing_schema_version` + `migration_timestamp` is a new key-value table. `[#1]`

### 1.2 `tasks` columns (relevant)

- `id` TEXT PK
- `title` TEXT
- `status` TEXT — **VALID_STATUSES = `{triage, todo, scheduled, ready, running, blocked, review, done, archived}`** `[#2]`
- `model_override` TEXT NULL
- `provider_override` TEXT NULL
- `routing_role` TEXT NULL — **NEW column (this plan adds it)** `[#2]`
- `created_at` INTEGER
- `started_at` INTEGER
- `completed_at` INTEGER

> **There is NO `updated_at` column on `tasks`.** Task timestamps are `created_at`,
> `started_at`, and `completed_at`. `[#1]`

> **No `failed` or `cancelled` status exists.** Terminal statuses are `done` and `archived`. `[#2]`

### 1.3 `task_runs` columns (relevant)

- `id` INTEGER PK
- `task_id` TEXT
- `status` TEXT — **`{running, done, blocked, crashed, timed_out, failed, released}`**
- `outcome` TEXT — **`{completed, blocked, crashed, timed_out, spawn_failed, gave_up, reclaimed}`**
- `routing_role` TEXT NULL — **already exists**
- `routing_model` TEXT NULL — **already exists**
- `routing_provider` TEXT NULL — **already exists**
- `routing_contract` INTEGER NULL — **already exists; INTEGER, not TEXT** `[#1]`
- `routing_reason` TEXT NULL — **already exists**
- `roster_digest` TEXT NULL — **already exists**
- `routing_policy` TEXT NULL — **already exists**
- `ac_revision` TEXT NULL — **already exists**
- `routing_source` TEXT NULL — **NEW column (this plan adds it)**
- `started_at` INTEGER NOT NULL
- `ended_at` INTEGER

> **Runs use `started_at`/`ended_at`, NOT `created_at`/`updated_at`.** There is no
> `created_at`/`updated_at` on `task_runs`. `[#1]`
>
> **Only `routing_source` is genuinely new.** `routing_role`, `routing_model`,
> `routing_provider`, `routing_contract`, `routing_reason`, `roster_digest`,
> `routing_policy`, and `ac_revision` are all already present in the `task_runs`
> `CREATE TABLE` (verified at `kanban_db.py:1528-1535`). `[#1]`

### 1.4 `task_events` columns (verified)

- `id` INTEGER PK
- `task_id` **TEXT** NOT NULL — **TEXT, not INTEGER** (verified at `kanban_db.py:1490`) `[#11]`
- `run_id` INTEGER NULL
- `kind` TEXT — event kind string (e.g. `claim_rejected`, `preflight_rejected`)
- `payload` TEXT — JSON
- `created_at` INTEGER

> **`task_events` EXISTS.** `claim_rejected` and `preflight_rejected` are already used event kinds
> (verified at `kanban_db.py:4949`). `claim_rejected` / `spawn_rejected` events will be recorded
> here — no new table needed. `[#3]` `[#11]`

### 1.5 `routing_policy` column type (verified)

`routing_policy` is **TEXT**, storing a JSON object with exactly two keys:

```json
{"invocation": "auto"|"manual", "may_edit": true|false}
```

It is **NOT an integer enum** and does **NOT** carry skills/effort. `[#5]`

### 1.6 Migration mechanism (verified)

The codebase uses a `_add_column_if_missing(conn, table, column, ddl)` helper (imported from
`hermes_cli/sqlite_util.py`) plus `CREATE TABLE IF NOT EXISTS` for schema evolution. New columns
are added idempotently; new tables are created with `CREATE TABLE IF NOT EXISTS`. The plan
follows this exact pattern.

> **There is NO `PRAGMA user_version` mechanism.** Schema evolution is purely additive via
> `_add_column_if_missing` + `CREATE TABLE IF NOT EXISTS`. `[#1]`

---

## 2. Schema Migration (Task 1)

### 2.1 New table: `kanban_metadata`

```sql
CREATE TABLE IF NOT EXISTS kanban_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
```

- `key` — metadata key, e.g. `routing_schema_version`, `migration_cutoff_id`.
- `value` — TEXT value (JSON-encoded if structured).
- `updated_at` — **INTEGER, Unix epoch seconds.** `[#8]`

### 2.2 New columns (idempotent, via `_add_column_if_missing`)

Only **one** column is genuinely new — `routing_source`. All other routing columns already
exist in `task_runs` (verified at `kanban_db.py:1528-1535`), so the migration adds only
`routing_source` and the `tasks.routing_role` column:

| Table | Column | DDL |
|-------|--------|-----|
| `tasks` | `routing_role` | `TEXT NULL` |
| `task_runs` | `routing_source` | `TEXT NULL` |

> **`routing_reason`, `roster_digest`, `routing_policy`, `ac_revision`, `routing_contract`,
> `routing_role`, `routing_model`, `routing_provider` already exist** in `task_runs` — they are
> NOT added by this migration. `[#1]`

### 2.3 Migration function

```python
def _migrate_routing_metadata(conn):
    # 1. create kanban_metadata table (IF NOT EXISTS)
    # 2. add routing_source to task_runs + routing_role to tasks via _add_column_if_missing
    # 3. set routing_schema_version = 1 (if absent)
    # 4. record the migration cutoff = MAX(task_runs.id) BEFORE any writes
    #    (see §2.4) — stored as kanban_metadata key 'migration_cutoff_id'
    conn.commit()
```

- `routing_schema_version` value: `"1"` (TEXT).
- `migration_cutoff_id` value: `str(MAX(task_runs.id))` — the atomic cutoff, **not** a timestamp.

### 2.4 Migration cutoff classification `[#1]`

The cutoff is an **atomic `MAX(task_runs.id)`** recorded **before** the migration writes any
rows. This avoids the race conditions inherent in second-resolution timestamps (two rows created
in the same second, or a clock skew between the migration and the claim path).

- **Before migration**, record `cutoff = SELECT COALESCE(MAX(id), 0) FROM task_runs`.
- A `task_runs` row with `routing_source IS NULL` **and** `id <= cutoff` is a **legacy
  pre-migration row** → benign, backfilled normally.
- A `task_runs` row with `routing_source IS NULL` **and** `id > cutoff` was created **after** the
  migration but failed to get a `routing_source` → **corrupt modern row** → error (exit 1),
  surfaced for investigation.

This prevents the backfill from silently "fixing" rows that should have been written correctly
by the post-migration claim path, which would mask a resolver bug. `[#1]`

---

## 3. Routing Resolver (Task 2)

### 3.1 Precedence chain (unchanged, but clarified)

Resolve in this order, first non-empty wins:

1. **envelope** — explicit routing params passed to the claim/spawn call.
2. **task_role** — `tasks.routing_role` (per-task override).
3. **task_override** — the task's **`model_override`/`provider_override` fields** (raw model/provider pin, NOT board metadata). `[#3]`
4. **board_default** — board's default role (from `board.json`).
5. **profile_default** — the **ASSIGNEE profile's** `model.default`/`model.provider` fallback (from the assignee's `config.yaml`), NOT a semantic role. `[#3]` `[#7]`
6. **unresolved** — no route could be determined.

> **`task_override` is the task's own `model_override`/`provider_override` columns** — a raw
> model/provider pin, not a role and not board metadata. **`profile_default` is the assignee
> profile's `model.default`/`model.provider` fallback** — a raw model/provider, not a semantic
> role. Neither is a role-based source. `[#3]`

### 3.2 Model-only override provider resolution `[#7]`

When a task has a `model_override` but **no** `provider_override`, the provider is resolved from
the **ASSIGNEE profile's** `config.yaml` `model.provider` — **not** the dispatcher's. Rationale:
the assignee is the one who will run the task, so their default provider is authoritative.

- If the assignee profile has no `model.provider`, fall back to the global default provider.
- If neither exists, the route is **unresolved** (REJECT).

### 3.3 `provider_override` validation `[#7]`

**No provider-name validation currently exists in `kanban_db.py`** (verified — the only provider
handling is the `provider_override requires model_override` guard at `kanban_db.py:3347`). There
is **no** check that a `provider_override` names a known/configured provider.

**Minimum viable validation (this plan):** if `provider_override` is set, it must be a
**non-empty string**. An empty string or non-string value is treated as invalid config → REJECT
claim (same as invalid role). This is the minimum type check — no deeper validation.

**Known limitation (documented, not claimed):** deeper provider-name validation (checking against
configured providers) does **not** exist in the codebase. If desired, it is a separate,
out-of-scope change (add a known-provider check at claim time and reject with a `claim_rejected`
event). `[#7]`

### 3.4 Resolver output

The resolver returns a **routing snapshot** (see §4).

---

## 4. Routing Snapshot (Task 3)

### 4.1 Snapshot fields

| Field | Source | Notes |
|-------|--------|-------|
| `routing_role` | precedence chain | resolved role (NULL for raw-model sources) |
| `routing_model` | role→model mapping (roles.yaml), or task/profile override | model for the resolved route |
| `routing_provider` | role→provider mapping, or assignee default | see §3.2 |
| `routing_contract` | `ROUTING_CONTRACT_VERSION` | **INTEGER, `1` for enforced envelopes, `NULL` otherwise** `[#10]` |
| `routing_reason` | resolver | human-readable reason string |
| `roster_digest` | hash of the resolved role's roster | NULL if no role resolved |
| `routing_policy` | JSON `{invocation, may_edit}` | see §1.5 |
| `ac_revision` | sha256 of concatenated AC text | **digest of the AC set at claim, when `ac_ids` present; NULL otherwise** `[#10]` |
| `routing_source` | which precedence level won | `envelope`/`task_role`/`task_override`/`board_default`/`profile_default`/`review_coerced`/`review_capable`/`inferred_evidence`/`inferred_current_config`/`legacy_unknown`/`unresolved` |

> **`routing_contract` is INTEGER, not TEXT.** It holds `ROUTING_CONTRACT_VERSION` (= `1`) for
> enforced envelopes and `NULL` for unenforced/legacy routes. `[#10]`
>
> **`ac_revision` is the digest of the AC set at claim.** It is `sha256` of the concatenated AC
> text (from the envelope's `ac_ids`, matched against the task body) when `ac_ids` is present,
> and `NULL` otherwise. It is **not** an "agent-config revision". `[#10]`

### 4.2 Persistence

- The snapshot is written to the **`task_runs`** row for the run.
- **The effective role is NEVER written back to `tasks.routing_role`.** `tasks.routing_role` is
  the explicit user-set intent (precedence source #2), and the resolver must not mutate it. The
  frozen snapshot lives in `task_runs` only. `[#4]`

---

## 5. Spawn (Task 4)

### 5.1 Spawn flow

1. Resolve and freeze the routing snapshot during claim (§3, §4).
2. If resolved role is **non-spawnable** (see §6 invariant table), do **not** create a run; record a
   `claim_rejected` event and **leave the task in its current status** (no status change).
   The caller receives an error. `[#2]`
3. Immediately before worker creation, validate the frozen run snapshot. A malformed modern
   snapshot rejects without mutable/config fallback and follows §5.1a lifecycle handling.
4. Otherwise, spawn the worker with the snapshot.
5. On spawn failure, follow the lifecycle in §5.1a (below-threshold retry OR exhaustion).

### 5.1a Rejection vs spawn-failure lifecycle `[#11]` `[#8]`

The three failure paths are distinct and both fully specified:

**Rejected claim** (invalid role, unknown provider, non-spawnable role, unresolved route):
- **Write the `claim_rejected` event in a SEPARATE transaction** (not a savepoint — savepoint
  rollback would lose the event). The main claim transaction rolls back first, then a second
  transaction writes the event to `task_events`.
- **Do NOT create a `task_runs` row.** The claim is rejected before a run is created.
- **Do NOT change the task status.** The task stays in its current status.
- **Return an error to the caller** (the claim/spawn API returns an error, not a silent no-op).
- **Event:** `task_events.kind = 'claim_rejected'`, `task_events.payload = JSON({reason, routing_role?, routing_source?})`.
  **Dedup key:** use an explicit **claim-attempt token** (UUID generated at the start of each
  claim attempt, stored in `task_events.payload.attempt_id`). Two attempts in the same second
  get different tokens. Dedup: `SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='claim_rejected'
  AND json_extract(payload, '$.attempt_id')=?`.

**Below-threshold spawn failure** (run exists, `consecutive_failures < max_retries`):
- The `task_runs` row **exists** (created at claim) — it is not deleted.
- **`task_runs` updates:** `status = 'failed'`, `outcome = 'spawn_failed'`, `ended_at = <current epoch>`.
- **`tasks` updates:** `current_run_id = NULL` (cleared), `consecutive_failures += 1`.
  Task status is **NOT changed** — the task returns to its pre-claim status (e.g. `todo`,
  `ready`) so it is **automatically eligible for re-claim** on the next dispatch cycle.
  This matches the existing `_record_task_failure` behavior which restores the source phase
  below the threshold.
- **Claim lease:** `claim_lock = NULL`, `claim_expires = NULL`.
- **Event:** `task_events.kind = 'spawn_rejected'`, `task_events.payload = JSON({run_id, reason, consecutive_failures, max_retries})`.
  **Dedup key:** `run_id + kind`. Check if a `spawn_rejected` event already exists for this
  `run_id` before inserting.
- Write a `task_comments` entry documenting the failure.

**Retry-exhaustion spawn failure** (run exists, `consecutive_failures >= max_retries`):
- Same `task_runs` updates as below-threshold.
- **`tasks` updates:** `status = 'blocked'` (valid task status — means "needs human attention"),
  `current_run_id = NULL`, `consecutive_failures += 1`.
  The task is **NOT automatically eligible** for re-claim until human intervention resets
  `consecutive_failures` or raises `max_retries`.
- **Claim lease:** `claim_lock = NULL`, `claim_expires = NULL`.
- **Event:** same `spawn_rejected` kind and payload as below-threshold.
- Write a `task_comments` entry documenting the exhaustion.

**Event vocabulary (canonical):**
- `claim_rejected` — claim-time rejection (no run exists). Payload: `{reason, routing_role?, routing_source?}`.
- `spawn_rejected` — post-claim spawn failure (run exists). Payload: `{run_id, reason, consecutive_failures, max_retries}`.
- **Existing event names** (`preflight_rejected`, `spawn_failed`) are **preserved as-is** in
  existing code and data. New code uses `claim_rejected` / `spawn_rejected` exclusively.
  Consumers of the old names are identified and updated in Task 8 (integration). This is NOT
  a breaking change — old events remain readable; new events use the new names.

**Event column:** the live column is `task_events.payload` (TEXT, JSON), NOT `metadata`.
All payload references above use this column. `[#3]`

**Retry idempotency:** re-claiming a task after below-threshold failure creates a **NEW
`task_runs` row** (new `run_id`). The old row stays for audit — it is not overwritten or deleted.

**Modern post-claim corruption** (run exists, snapshot is malformed or incomplete):
- A run already exists (created at claim).
- The frozen snapshot has missing/invalid fields (e.g. NULL `routing_model` where REQUIRED).
- Spawn is rejected **without fallback** — no legacy fallback, no config inference.
- The run is closed through the common spawn-failure path:
  `task_runs.status = 'failed'`, `outcome = 'spawn_failed'`, `ended_at = <current epoch>`.
- `spawn_rejected` event is emitted with `reason = "snapshot incomplete: <field> is NULL"`.
- Retry/circuit-breaker accounting is applied (same as below-threshold/exhaustion paths).
- This is distinct from claim-time rejection (where no run exists).

> **Correct task statuses are `{triage, todo, scheduled, ready, running, blocked, review, done,
> archived}`.** There is no `failed` or `cancelled` task status. Below-threshold spawn failures
> leave the task in its pre-claim status (automatic eligibility). Only retry exhaustion lands in
> `blocked`. `[#11]` `[#8]`

### 5.2 `spawn_rejected` event

Recorded in `task_events` with `kind = "spawn_rejected"`, `payload` = JSON
`{run_id, reason, consecutive_failures, max_retries}`. This is the **canonical payload schema**
for spawn failures (both below-threshold and exhaustion). `[#3]`

For `claim_rejected` events (no run exists), the payload is `{attempt_id, reason, routing_role?, routing_source?}`
with dedup key `attempt_id` (UUID per claim attempt). For `spawn_rejected` events
(run exists), the dedup key is `run_id + kind`. `[#3]`

---

## 6. Invariant Table (Task 5)

The invariant table defines, for each `routing_source`, the valid combination of **every frozen
field** on the `task_runs` snapshot. Each field is one of:

- **REQUIRED** — must be non-NULL with the specified value/type.
- **OPTIONAL** — may be NULL (or set).
- **FORBIDDEN** — must be NULL.

Frozen fields: `routing_model`, `routing_provider`, `routing_role`, `routing_reason`,
`routing_contract`, `routing_policy`, `roster_digest`, `ac_revision`, `routing_source`. `[#3]`

| `routing_source` | `routing_model` | `routing_provider` | `routing_role` | `routing_reason` | `routing_contract` | `routing_policy` | `roster_digest` | `ac_revision` | Spawnable? |
|------------------|-----------------|--------------------|----------------|------------------|--------------------|------------------|-----------------|---------------|------------|
| `envelope` | REQUIRED | REQUIRED | REQUIRED | REQUIRED | REQUIRED (=1) | REQUIRED | REQUIRED | OPTIONAL | ✅ |
| `task_role` | REQUIRED | REQUIRED | REQUIRED | REQUIRED | OPTIONAL | REQUIRED | REQUIRED | OPTIONAL | ✅ |
| `task_override` | REQUIRED | REQUIRED | **FORBIDDEN** | REQUIRED | OPTIONAL | OPTIONAL | **FORBIDDEN** | OPTIONAL | ✅ |
| `board_default` | REQUIRED | REQUIRED | REQUIRED | REQUIRED | OPTIONAL | REQUIRED | REQUIRED | OPTIONAL | ✅ |
| `profile_default` | REQUIRED | REQUIRED | **FORBIDDEN** | REQUIRED | OPTIONAL | OPTIONAL | **FORBIDDEN** | OPTIONAL | ✅ |
| `review_coerced` | REQUIRED | REQUIRED | REQUIRED (`reviewer`) | REQUIRED | OPTIONAL | REQUIRED | REQUIRED | OPTIONAL | ✅ |
| `review_capable` | REQUIRED | REQUIRED | REQUIRED | REQUIRED | OPTIONAL | REQUIRED | REQUIRED | OPTIONAL | ✅ |
| `inferred_evidence` | REQUIRED | REQUIRED | OPTIONAL | REQUIRED | OPTIONAL | OPTIONAL | OPTIONAL | OPTIONAL | ❌ `[#6]` |
| `inferred_current_config` | OPTIONAL | OPTIONAL | OPTIONAL | REQUIRED | OPTIONAL | OPTIONAL | OPTIONAL | OPTIONAL | ❌ `[#6]` |
| `legacy_unknown` | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | OPTIONAL | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | ❌ |
| `unresolved` | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | OPTIONAL | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | ❌ |

**Logical-consistency notes `[#4]` `[#5]` `[#6]` `[#3]`:**

- `task_override` and `profile_default` are **raw model/provider sources, not roles** → they have
  `routing_role = FORBIDDEN` and `roster_digest = FORBIDDEN` (you cannot compute a roster digest
  without a role). Only role-based sources (`envelope`, `task_role`, `board_default`,
  `review_coerced`, `review_capable`) carry a `routing_role` and `roster_digest`. `[#5]`
- `review_coerced` and `review_capable` are the two review-phase sources. Both carry
  `routing_role = 'reviewer'` (or the review-capable role), `routing_reason = REQUIRED`
  (documenting the review phase), `routing_contract = OPTIONAL`, `roster_digest = REQUIRED`,
  `routing_policy = REQUIRED`. `[#6]` `[#2]`
- `inferred_evidence` / `inferred_current_config` are **heuristic backfill labels**, not verified
  routes → **non-spawnable**, same as `legacy_unknown`. `[#6]`
- `legacy_unknown` / `unresolved` have all routing fields FORBIDDEN (no role → no
  model/provider/digest); `routing_reason` is OPTIONAL (may carry a short explanation).
- `ac_revision` is OPTIONAL everywhere: it is only set when the envelope's `ac_ids` is present
  (see §4.1). `[#3]`

---

## 7. Backfill (Task 6)

### 7.1 Scope

Backfill processes **existing** `task_runs` rows that have **no** `routing_source` yet.

### 7.2 Terminal scope `[#2]` `[#8]` `[#6]`

Backfill only assigns routing metadata to terminal historical **runs**. Eligibility is derived
entirely from the immutable run record, not the task's mutable current status. All must hold:

- `task_runs.ended_at IS NOT NULL`,
- `task_runs.outcome IS NOT NULL`, and
- `task_runs.status != 'running'`.

An ended row still marked `running` or lacking an outcome is not terminal and remains untouched.
No task-status or time-based heuristics are used. `[#8]` `[#6]`

### 7.3 Backfill logic

For each eligible run (run ended, no `routing_source`):

1. **Evidence-based** `[#7]` `[#4]`: look up the run's `metadata.worker_session_id` →
   `sessions.id` → `session_model_usage` to recover the **model/provider actually used** by that
   run. Label it `routing_source = "inferred_evidence"`. This is the **only** backfill path — it
   uses the real session usage record, **never** inference from current config. `[#7]` `[#5]`
2. Else (no evidence) → label it `routing_source = "legacy_unknown"` (no routing info;
   non-spawnable).

> **Backfill is evidence-based ONLY.** There is **no** `inferred_current_config` path — the plan
> does **not** infer routing from the task's current `routing_role`/`model_override`/
> `provider_override` or from board/profile config. Those are mutable current state, not evidence
> of what actually ran. If no execution evidence exists, the run is `legacy_unknown`. `[#5]`

#### 7.3.1 Exact backfill query `[#4]`

**Which `state.db`:** each profile's own state DB at
`~/.hermes/profiles/<assignee>/state.db`. The run's `task_runs.profile` names the assignee
profile; its `state.db` is the source of truth for that run's usage.

**Filter:** `session_model_usage WHERE task = ''` — main-loop usage only. This **excludes**
`task = 'approval'` and other non-main-loop tasks, so the recovered model/provider reflects the
main agent run, not a side task.

**Provider column:** `billing_provider` from `session_model_usage` (the provider actually billed
for the run).

**Exact SQL** (per eligible run, given `worker_session_id`):

```sql
-- state.db for the run's assignee profile
SELECT model, billing_provider, api_call_count
FROM session_model_usage
WHERE session_id = :worker_session_id
  AND task = ''
ORDER BY api_call_count DESC;
```

**Match resolution:**
- **Zero matches** → `routing_source = "legacy_unknown"` (no evidence).
- **Multiple matches** → use the row with the **most `api_call_count`** (the dominant
  model/provider for that session). If the top rows are tied/ambiguous (no clear majority), fall
  back to `legacy_unknown` rather than guess.
- **One match** → use its `model`/`billing_provider` as `routing_model`/`routing_provider`.

The recovered `model`/`billing_provider` populate `routing_model`/`routing_provider`; the
`routing_source` is `inferred_evidence`. `[#4]`

### 7.4 Benign skips `[#10]`

A run that **already has** `routing_source` is a **benign skip** — it is **NOT** an error. It is
counted as processed successfully.

### 7.5 Exit codes `[#10]`

| Exit | Meaning |
|------|---------|
| `0` | All rows processed successfully (including benign skips). |
| `1` | Some rows had errors (evidence mismatch, corrupt data). |
| `2` | Usage error (bad args, missing DB). |

- Benign skips (already has `routing_source`) → **exit 0**, not an error. `[#10]`
- Evidence mismatch (e.g. run has a role but the task contradicts it) → **exit 1**.
- Missing DB / bad args → **exit 2**.

---

## 8. Self-Sustaining Loop (Task 7)

### 8.1 Claim path

1. Resolve routing snapshot (§3, §4).
2. Validate `provider_override` (§3.3) — unknown provider → REJECT. `[#9]`
3. Validate role — invalid role → REJECT (record `claim_rejected` event).
4. Persist snapshot to `task_runs` only — **never** to `tasks.routing_role` (that column is
   explicit user-set intent, not mutated by the resolver). `[#4]`
5. Record canonical `claim_rejected` events in `task_events` on rejection. The legacy
   `preflight_rejected` name remains readable but has no current producer or repository consumer. `[#3]`

### 8.2 `claim_review_task` routing `[#11]` `[#2]`

The full review-phase routing flow:

1. **Resolve precedence chain normally** (envelope → task_role → task_override → board_default →
   profile_default → unresolved). `[#11]`
2. **Apply review policy** after resolution:
   - If the resolved role is in `_REVIEW_CAPABLE_ROLES = {reviewer, main_coder}` → **accept as-is**.
   - If **NOT** review-capable → **override role to `reviewer`**, re-resolve model/provider from
     `roles.yaml`, and set `routing_reason = "review phase: role '<original>' not review-capable, coerced to reviewer"`. `[#11]`
3. **Review snapshot `routing_source` is ALWAYS review-specific** — never ambiguous `[#2]`:
   - If coercion happened (role was coerced to `reviewer`): `routing_source = "review_coerced"`.
   - If already review-capable (no coercion needed): `routing_source = "review_capable"` (**NEW
     enum value**).
   - **Both** get structured JSON `routing_reason` documenting review context and preserving
     `base_role` (plus `review_action` and `base_source`).
4. **Freeze the complete review snapshot** (all fields) into the `task_runs` row. `[#11]`

### 8.3 Spawn path

See §5.

---

## 9. Public API (Task 8)

### 9.1 Public API `[#10]`

Frozen routing belongs to each `task_runs` row, not the `Task` entity. One task can have
multiple attempts (runs). The API exposes **run-level** routing data.

**`RunRoutingSnapshot` DTO** (returned by `get_routing_snapshot`):

```python
@dataclass
class RunRoutingSnapshot:
    run_id: int
    task_id: str
    board: str
    routing_role: Optional[str]
    routing_model: Optional[str]
    routing_provider: Optional[str]
    routing_reason: Optional[str]
    routing_contract: Optional[int]      # INTEGER, 1 for enforced envelopes
    routing_policy: Optional[str]        # JSON {invocation, may_edit}
    roster_digest: Optional[str]
    ac_revision: Optional[str]
    routing_source: Optional[str]
```

**API functions:**

- `get_routing_snapshot(conn, run_id: int, *, board: str) -> RunRoutingSnapshot` — reads the
  frozen snapshot from the selected board database. **Not-found:** raises `KeyError`; blank board
  identity raises `ValueError`. Run IDs are board-local and may collide across databases, so the
  caller must always provide explicit board identity.
- `set_routing_override(conn, task_id: str, *, role: Optional[str] = None,
  model: Optional[str] = None, provider: Optional[str] = None)` — updates pre-claim task intent in
  the selected board database: `tasks.routing_role` (if `role`) OR
  `tasks.model_override`/`tasks.provider_override` (if `model`/`provider`).
  **Exclusive modes:**
  - **Role mode:** `role` set, `model` and `provider` both NULL → sets `tasks.routing_role`.
    Clears any existing `model_override`/`provider_override` (switching modes atomically
    clears stale columns from the opposite mode).
  - **Model mode:** `model` set, `role` NULL. `provider` is OPTIONAL (per §3.2: if omitted,
    resolved from assignee profile at claim time). Sets `tasks.model_override` and
    `tasks.provider_override` (if provided). Clears any existing `tasks.routing_role`.
  - **Invalid:** `provider` set without `model` → raises `ValueError` (matches existing API
    guard at `kanban_db.py:3347`).
  - **Invalid:** both `role` and `model` set → raises `ValueError`.
  - **No-op:** all NULL → raises `ValueError`.
- `get_kanban_metadata(key)` / `set_kanban_metadata(key, value)` → key-value access to
  `kanban_metadata`.
- `backfill_routing_metadata(dry_run=False)` → runs the backfill (§7), returns exit code.

**Run-row conversion:** `task_runs` row → `RunRoutingSnapshot` maps each column directly.
Unknown columns are ignored (forward compatibility).

**Dashboard/API payload:** the dashboard serializes `RunRoutingSnapshot` as JSON. Board context
(`board` field) is included so the UI can show which board the run belongs to.

### 9.2 Backward compatibility

- All new columns are nullable; existing reads that don't reference them are unaffected.
- `routing_policy` remains TEXT JSON `{invocation, may_edit}` — no change to existing consumers. `[#5]`

### 9.3 CLI / serialization / overwrite / dry-run contracts `[#9]`

**CLI commands:**

- `hermes kanban create --routing-role <role>` — set `tasks.routing_role` at task creation.
- `hermes kanban boards set-default-role <slug> <role>` — set the board's default role
  (writes `default_role` into `board.json`; see §11.2 for the `write_board_metadata` caveat).
- `hermes kanban backfill-routing [--dry-run] [--overwrite]` — run the backfill (§7).

**Serialization:** the routing fields (`routing_role`, `routing_model`, `routing_provider`,
`routing_reason`, `routing_contract`, `routing_policy`, `roster_digest`, `ac_revision`,
`routing_source`) are included in the **`RunRoutingSnapshot` DTO** (§9.1), which is the
canonical serialization for run-level routing data. The dashboard serializes this DTO as JSON.
Task-level serialization does NOT include routing fields (they belong to individual runs).

**Overwrite semantics:** `--overwrite` applies **only** to rows whose `routing_source` is `NULL`
or `legacy_unknown`. It **never** overwrites an authoritative `routing_source` (`envelope`,
`task_role`, `task_override`, `board_default`, `profile_default`, `review_coerced`,
`review_capable`, `inferred_evidence`). Without `--overwrite`, rows that already have a
`routing_source` are benign skips (exit 0).

**Dry-run:** `--dry-run` prints what would change (per-row: run id, current `routing_source`,
proposed `routing_source`/`routing_model`/`routing_provider`) and **exits 0** without writing
anything. `[#9]`

---

## 10. Integration Tests (Task 9)

### 10.1 Test scenarios

1. **Migration test:** fresh DB → `_migrate_routing_metadata` creates `kanban_metadata` + all new
   columns; re-run is idempotent.
2. **Resolver test:** precedence chain resolves correctly for each source.
3. **Provider handling test:** `provider_override` non-empty string accepted; empty string →
   REJECT claim. `[#7]`
4. **Review coercion test:** non-review-capable role → coerced to `reviewer`, `routing_source =
   review_coerced`, original role preserved in `routing_reason`; already review-capable →
   `routing_source = review_capable`, original role preserved. `[#11]` `[#2]`
5. **Backfill test:** terminal runs (ended_at IS NOT NULL + task status in {done, archived}) get
   `inferred_evidence`/`legacy_unknown` labels; benign skips → exit 0; corrupt data → exit 1; bad
   args → exit 2. `[#2]` `[#10]` `[#6]`
6. **Invariant test:** every snapshot satisfies the invariant table (§6). `[#4]` `[#6]`
7. **Rollout test:** backup → write → verify → rollback round-trip. `[#12]`
8. **Spawn failure lifecycle test:** claim → spawn_rejected → task_runs.status='failed',
   tasks.status='blocked', current_run_id cleared, event+comment written, lease cleared.
9. **Retry exhaustion test:** consecutive_failures >= max_retries → blocked, no further claims.
10. **RunRoutingSnapshot DTO test:** task_runs row → DTO conversion, board context included.
11. **Double-claim test:** two concurrent claims on same task → one succeeds, one fails.
12. **Mutation-after-claim test:** changing tasks.routing_role after claim does not affect frozen
    snapshot in task_runs.

### 10.2 TDD file list

| Task | Production file(s) | Test file(s) | RED (failing assertion) | GREEN (implementation step) |
|------|-------------------|--------------|-------------------------|----------------------------|
| 1. Schema | `kanban_db.py` (`_migrate_routing_metadata`) | `tests/test_kanban_routing_schema.py` | Assert `kanban_metadata` table + `routing_source` column exist after migration | Add `CREATE TABLE kanban_metadata` + `ALTER TABLE task_runs ADD COLUMN routing_source` |
| 2. Resolver | `kanban_db.py` (`_resolve_routing_snapshot`) | `tests/test_kanban_routing_resolver.py` | Assert precedence chain returns correct source for each input combination | Implement `_resolve_routing_snapshot` with 6-level precedence |
| 3. Persist | `kanban_db.py` (claim transaction) | `tests/test_kanban_routing_persist.py` | Assert frozen snapshot written to `task_runs` on claim | Add snapshot persistence to claim transaction |
| 4. Spawn | `kanban_db.py` (`_default_spawn`) | `tests/test_kanban_routing_spawn.py` | Assert non-spawnable source → `claim_rejected` event, no run created. Assert post-claim invariant violation → `spawn_rejected` event, run exists, task blocked. | Add invariant check before spawn + post-claim validation |
| 5. Backfill | `kanban_db.py` (`backfill_routing_metadata`) | `tests/test_kanban_routing_backfill.py` | Assert terminal runs get `inferred_evidence`/`legacy_unknown`; exit codes correct | Implement backfill with evidence-based join |
| 6. Self-sustaining | `kanban_db.py` (claim path) | `tests/test_kanban_routing_claim.py` | Assert claim writes snapshot, rejection writes event, retry creates new run | Wire resolver into claim path |
| 7. Public API | `kanban_db.py` (DTO + functions), `kanban.py` (CLI handlers) | `tests/test_kanban_routing_api.py` | Assert `RunRoutingSnapshot` DTO fields match `task_runs` row; `set_routing_override` modes work; CLI `--routing-role` flag accepted | Implement DTO + API functions + CLI flag |
| 8. Board metadata | `kanban_db.py` (`write_board_metadata`), `kanban.py` (`boards set-default-role`) | `tests/test_kanban_routing_board.py` | Assert `default_role` keyword writes to `board.json`; CLI command sets it | Add `default_role` param + CLI command |
| 9. Integration | `kanban_db.py`, `kanban.py`, `kanban_watchers.py` | `tests/test_kanban_routing_integration.py` | Assert end-to-end: claim → spawn → failure → retry → exhaustion | Wire all pieces together |

### 10.3 Additional required test scenarios

| Scenario | What it tests | Key assertion |
|----------|--------------|---------------|
| Migration concurrency | Two migrations run simultaneously | Idempotent — second is no-op |
| Absent vs invalid config | Missing `routing_role` falls through; `routing_role='bogus'` rejects | Fall-through vs REJECT |
| Cross-board isolation | Board A's `default_role` doesn't affect Board B | Per-board isolation |
| Modern snapshot no-fallback | Post-migration run with NULL `routing_source` is non-spawnable | Fail-closed |
| Review base provenance | Review-capable role → `review_capable` source; non-capable → `review_coerced` | Correct source + reason |
| Ambiguous historical evidence | Multiple usage rows with tied `api_call_count` → `legacy_unknown` | Ambiguity → safe label |
| Below-threshold retry | `consecutive_failures < max_retries` → task returns to pre-claim status | Automatic eligibility |
| `default_role` persistence | `write_board_metadata(slug, default_role='executor')` → readback matches | Atomic write + readback |
| Migration-versus-claim race | Claim during migration → migration completes first (DB-level lock). Post-migration claims get correct `routing_source` from cutoff classification. | Migration lock + cutoff integrity |
| Colliding run IDs across boards | Run IDs are per-board (each board has its own DB with its own autoincrement). IDs WILL collide across boards but are unique within a board. | Board-local uniqueness only |
| Rejection-event durability | `claim_rejected` event written in separate transaction (NOT savepoint — savepoint rollback would lose it). Verify event persists after claim rollback. | Event in separate txn persists |
| Every source absent-vs-invalid | Missing config falls through; present-but-invalid rejects | Correct precedence behavior |
| Structured review base provenance | Review-capable → `review_capable` with original role preserved; coerced → `review_coerced` with original role in reason | Correct source + role + reason |
| Post-increment retry threshold | After `consecutive_failures` increment, threshold check is on new value (e.g. old=2, limit=3, new=3 → blocked) | Correct blocking at exact threshold |
| Modern snapshot corruption | Post-migration run with NULL `routing_source` → non-spawnable, `spawn_rejected` event, run closed | Fail-closed + event |
| Event-consumer compatibility | Old `preflight_rejected`/`spawn_failed` events still readable by existing consumers | No breaking change |
| Separate event-transaction failure | If event write fails after claim rollback → claim still rejected, event lost (acceptable — retry will re-attempt) | Graceful degradation |
| Distinct same-second attempts | Two claim attempts in same second → different `attempt_id` UUIDs → separate events | No suppression |
| Post-claim corruption lifecycle | Run exists + malformed snapshot → `spawn_rejected` + run closed + circuit breaker | Distinct from claim rejection |

---

## 11. Rollout Backup / Rollback (Task 10) `[#12]`

All steps are **file operations** in `~/.hermes/kanban/boards/<slug>/`.

### 11.1 Backup

Copy each `board.json` to `board.json.bak.<timestamp>` in the **same directory**:

```bash
cp ~/.hermes/kanban/boards/<slug>/board.json \
   ~/.hermes/kanban/boards/<slug>/board.json.bak.$(date +%s)
```

### 11.2 Rollout `[#10]`

**Normative solution:** add `default_role` as a keyword parameter to `write_board_metadata`:

```python
def write_board_metadata(
    board: Optional[str],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    archived: Optional[bool] = None,
    default_workdir: Optional[str] = None,
    project_id: Optional[str] = None,
    default_role: Optional[str] = None,   # NEW — board-level default routing role
) -> dict:
```

The function already preserves existing fields not mentioned in the call (verified at
`kanban_db.py:930`). Adding `default_role` as a keyword makes it first-class. The function
writes `default_role` into `board.json` atomically, preserving all unknown keys.

**Rollout procedure** (requires explicit user approval before execution):

1. **Backup:** `cp board.json board.json.bak.<timestamp>` in the same directory.
2. **Set:** `write_board_metadata(slug, default_role=<role>)` for each board.
3. **Verify:** `read_board_metadata(slug)` and confirm `default_role` matches.
4. **Rollback:** if verification fails, `cp board.json.bak.<timestamp> board.json`.

**Recommended defaults** (user must approve):

| Board | `default_role` |
|-------|---------------|
| `k-shell` | `executor` |
| `hermes-android` | `main_coder` |
| `tradesys` | `main_coder` |
| `default` | *(unset — no board-level default)* |

> **Actual board rollout is a separate operational step** — it is NOT part of the code
> implementation. The code change (adding the parameter) ships; the rollout (setting values)
> happens after user approval.

### 11.3 Verify

Read back the board and confirm `default_role` matches the intended value. If it does not, roll
back immediately.

### 11.4 Rollback

Copy the backup back over `board.json`:

```bash
cp ~/.hermes/kanban/boards/<slug>/board.json.bak.<timestamp> \
   ~/.hermes/kanban/boards/<slug>/board.json
```

### 11.5 Notes

- Backups are timestamped so multiple rollouts can be rolled back independently.
- Rollback is a pure file copy — no DB writes, no side effects.

---

## 12. Task Summary

| # | Task | Key files |
|---|------|-----------|
| 1 | Schema migration (`kanban_metadata` + new columns) | `kanban_db.py` |
| 2 | Routing resolver (precedence + provider validation) | `kanban_db.py` |
| 3 | Routing snapshot (fields + persistence) | `kanban_db.py` |
| 4 | Spawn (non-spawnable + spawn-failure → blocked) | `kanban_db.py` |
| 5 | Invariant table (logical consistency) | plan §6 |
| 6 | Backfill (terminal scope + exit codes) | `kanban_db.py` |
| 7 | Self-sustaining loop (claim + review coercion) | `kanban_db.py` |
| 8 | Public API | `kanban_db.py` |
| 9 | Integration tests | test suite |
| 10 | Rollout backup/rollback | file ops in `~/.hermes/kanban/boards/<slug>/` |

---

## 13. Open Questions / Assumptions

- **`routing_policy` shape:** confirmed TEXT JSON `{invocation, may_edit}` — no skills/effort. `[#5]`
- **`_REVIEW_CAPABLE_ROLES`:** confirmed `{reviewer, main_coder}`. `[#11]`
- **Assignees without a configured provider:** fall back to global default; if none, route is
  unresolved → REJECT. `[#7]`
- **Backfill on non-terminal tasks:** left untouched (resolved live). `[#2]`
