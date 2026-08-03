# State-DB Corruption → Silent Worker Drain (Incident 2026-08-03)

## Summary

On 2026-08-03 the root/default `~/.hermes/state.db` suffered SQLite B-tree
corruption in the `messages` table and the FTS trees; the Quill and Orion
profiles had malformed FTS indexes as well. Kanban-dispatched workers
started normally, failed their **first canonical transcript write**,
exited, and the two-failure circuit breaker drained the fleet.

Manual recovery preserved **1,608 sessions** and **46,937 / 46,950
messages**; 13 unreadable rows remain only in timestamped backups.

This document traces the exact write paths, corruption classes, and
restart/recovery paths, and pins the contract for a pre-dispatch health
probe that must be implemented so a corrupt store can never spawn a fleet
of doomed workers.

## Canonical message write paths

Every agent turn that produces transcript rows funnels into one chokepoint:

```
agent._flush_messages_to_session_db(messages, conversation_history)
  run_agent.py:1920  (serializes per-agent with _session_persist_lock)
  └─ _flush_messages_to_session_db_unlocked  run_agent.py:1932
     └─ _ensure_db_session()                 (lazy SessionDB row creation)
     └─ SessionDB.append_message(...)        hermes_state.py:5450
        └─ INSERT INTO messages ...          (single write txn)
           └─ FTS sync triggers fire:
              messages_fts_insert/delete/update   hermes_state_common.py:340-373
              messages_fts_trigram_*              hermes_state_common.py:406-444
              messages_fts_cjk_*                  hermes_state.py:1436-1471
```

Call sites of `_flush_messages_to_session_db`:
- `agent/conversation_loop.py:6077` — assistant tool-call turn, BEFORE tool
  side effects (crash-resilience persist).
- `agent/conversation_loop.py:6787, 6859` — turn-boundary flushes.
- `agent/codex_runtime.py:784`, `agent/tool_executor.py:152` — codex /
  tool-result persistence.
- `cli.py:7958, 10851`, `hermes_cli/cli_commands_mixin.py:963, 1134`,
  `agent/conversation_compression.py:2120` — CLI / compression paths.
- `gateway/shutdown_flush.py` — final flush on shutdown.

### The failure contract

`agent/conversation_loop.py:6089-6097`: if `_flush_messages_to_session_db`
raises (or returns falsy), the loop sets `_turn_exit_reason =
"session_persistence_failed"`, produces no final response, and breaks.
`run_agent.AIAgent._format_turn_completion_explanation` (run_agent.py:3514)
renders that reason to the user as:

> ⚠️ No reply: the turn was stopped because session storage could not be
> written (the transcript would have been lost on restart). Check disk
> space / permissions for the state DB, then send your message again.

A kanban worker whose run ends with `session_persistence_failed` does not
`kanban_complete`; the dispatcher counts it as a failed attempt and
auto-blocks the task after `kanban.failure_limit` (default 2) consecutive
non-successes — the fleet drain.

## Corruption classes and current behavior (verified on this checkout)

| Class | Detected by | Self-heals? | Path |
|---|---|---|---|
| Malformed schema (duplicate `sqlite_master` rows) | open-time `DatabaseError`, `is_malformed_db_error` | Yes — `SessionDB()` open calls `repair_state_db_schema` (backup first), one-shot per process | `hermes_state.py:1958-1982` |
| FTS write corruption (`messages_fts*` shadow b-trees reject trigger writes) | `_db_opens_cleanly` write probe; `SessionDB._is_fts_write_corruption_error` | Yes — `_execute_write` one-shot in-place FTS `'rebuild'` + retry | `hermes_state.py:2317, 2414-2451`; `tests/state/test_fts_runtime_rebuild.py` |
| FTS read corruption (MATCH/snippet rank fails) | `_db_opens_cleanly` read probe (`DatabaseError` class) | Yes — `search_messages` one-shot rebuild | `hermes_state.py:1129-1173` |
| **`messages` table b-tree corruption** (incident class) | `_db_opens_cleanly` `integrity_check` | **No** — `repair_state_db_schema` escalates through rebuild/REINDEX/dedup/drop-FTS and every pass fails; manual restore required | `hermes_state.py:1217-1380` |
| Stale B-tree index (e.g. `idx_messages_session`) | `_db_opens_cleanly` `integrity_check` ("wrong # of entries") | Yes — Strategy 0.5 `REINDEX` | `hermes_state.py:1299-1320` |

Key asymmetry: the FTS classes (Quill/Orion's malformed indexes) self-heal
on the first write, so those workers survive. The `messages` b-tree class
(root/default) does not — `append_message` raises
`sqlite3.DatabaseError: database disk image is malformed`, the repair
ladder fails, and the worker drains.

## Detector / repair machinery that exists today

- `hermes_state._db_opens_cleanly(db_path) -> Optional[str]` — the probe:
  fresh connection, `PRAGMA journal_mode`, `PRAGMA integrity_check`,
  `SELECT COUNT(*) FROM sessions`, FTS5 read probe on all three FTS tables,
  rolled-back FTS write probe. Returns `None` when healthy, else a reason.
- `hermes_state.repair_state_db_schema(db_path, backup=True)` — the repair
  ladder: in-place FTS rebuild → REINDEX → sqlite_master dedup → drop FTS +
  VACUUM. Never touches canonical `sessions`/`messages` rows; timestamped
  backup first.
- Used today by `hermes doctor`, `hermes sessions` (sessions_cmd.py),
  `hermes_cli/session_recovery.py`, and the `SessionDB()` open-time
  malformed-schema self-heal.

## Gap: no pre-dispatch health probe (FIXED)

The kanban dispatcher (`hermes_cli/kanban_db.py` `dispatch_once` →
`_default_spawn`) spawns `hermes -p <assignee> ... chat -q "work kanban
task ..."` subprocesses **without checking the assignee's state.db
health**. A worker pointed at a corrupt store opens the DB fine (schema is
healthy), fails its first write, and drains the fleet.

### Implementation (landed with this task)

`tests/state/test_state_db_corruption_worker_drain.py` pins this API:

```python
# hermes_cli/kanban_db.py
def pre_dispatch_state_db_probe(profile_name: str) -> Optional[str]:
    """None if the profile's state.db is healthy enough to spawn a worker;
    else a human-readable reason naming the corruption.

    Resolves the profile's HERMES_HOME state.db (via
    hermes_cli.profiles.resolve_profile_env / hermes_constants) and
    delegates to hermes_state._db_opens_cleanly.
    """
```

`dispatch_once` calls this per assignee before `_default_spawn` (memoized
per normalized profile per tick so a fan-out tick probes once). On a
non-None result the dispatcher quarantines instead of spawning a worker
doomed to `session_persistence_failed`:

- **Ready tasks** are hard-blocked via `block_task(kind="capability")`
  with the high-signal diagnostic
  `profile <name> store unhealthy: <error>; worker blocked` — the exact DB
  path + SQLite error (e.g. `database disk image is malformed`, or the
  affected FTS index from the probe's `fts5 read probe failed on
  messages_fts: ...`) land in the block reason / synthesized run, so the
  card surfaces on the board with evidence instead of silently cycling.
- **Review tasks** have no ready/running transition to block; they are
  skipped without claiming (stay in the review lane until the store is
  fixed) and a `quarantined` event records the diagnostic.
- **Crash enrichment:** `detect_crashed_workers` probes the assignee's
  store for the low-signal `pid N not alive` class and, when the store is
  unhealthy, records
  `profile <name> store unhealthy: <error>; worker blocked` in the run
  history instead — so the requeue/auto-block story is the storage cause,
  not a generic vanished-pid message.
- **Queue-drain alert contract events:** the gate also emits
  `profile_quarantined` (on quarantine, once per profile per tick) and
  `profile_store_healthy` (when a previously-quarantined profile's store
  heals) — the exact events `hermes_cli.kanban_health.check_queue_drain`'s
  default event-stream provider scans for. This is the wiring the
  queue-drain alert (task 2) documented as its seam; without it the alert
  can never fire from a real quarantine. See
  `tests/hermes_cli/test_kanban_store_quarantine.py` (event-contract
  tests) and `tests/hermes_cli/test_kanban_queue_drain_alert.py`.
- The store is never replaced, repaired, or deleted by the gate;
  `_db_opens_cleanly` is read-only + rolled-back-write, and recovery
  relies on the existing timestamped backups / `repair_state_db_schema`
  (see `RECOVERY.md` at the repo root for the safe offline recovery
  procedure).

The detector contract was already proven: `_db_opens_cleanly` flags both
the `messages` b-tree class and the FTS class on real fixtures (see
`TestProbeDetectorContract` in the test file). Tests live in
`tests/hermes_cli/test_kanban_store_quarantine.py` (probe behavior,
quarantine gate, healthy-profile dispatch, dry-run, review lane, crash
diagnostic) and `tests/state/test_state_db_corruption_worker_drain.py`
(incident reproduction + the now-passing wiring contract).

## Recovery / restart paths (from the incident)

1. `_db_opens_cleanly` → detect reason.
2. `repair_state_db_schema` → attempts FTS rebuild / REINDEX / dedup /
   drop-FTS; takes a timestamped raw backup first (`_backup_db_file`).
3. If the ladder cannot recover (`repaired: False`), manual restore from a
   timestamped backup. The incident's manual recovery preserved 1,608
   sessions and 46,937/46,950 messages; 13 unreadable rows remain only in
   the timestamped backups (the restored DB dropped those rows).

## Test summary

`tests/state/test_state_db_corruption_worker_drain.py`:

- PASS against current code (incident reproduction):
  - `append_message` raises `DatabaseError: database disk image is
    malformed` against a real corrupted `messages` b-tree.
  - The corruption is silent to plain reads (sessions still readable).
  - The failure maps to `session_persistence_failed` →
    "session storage could not be written".
  - `repair_state_db_schema` cannot repair the messages b-tree class.
  - FTS corruption self-heals on write; messages b-tree corruption does
    not (the fleet-drain asymmetry).
  - The probe detector (`_db_opens_cleanly`) flags both incident classes.
- FAIL against current code (the contract to implement later):
  - `test_pre_dispatch_state_db_probe_exists_in_kanban_dispatch` — asserts
    `hermes_cli.kanban_db.pre_dispatch_state_db_probe` exists and is wired
    into dispatch. This was the expected red test; with the
    implementation landed it now passes (see the Implementation section
    above and `tests/hermes_cli/test_kanban_store_quarantine.py`).
