# AgentOps Default Core Read-Only Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bounded, in-memory, read-only observation loop for the trusted `default` Hermes deployment asset, producing daily summaries and a bounded Terra analysis input without changing Gateway, Target, business, LaunchAgent, or Cron state.

**Architecture:** `DefaultObservationLoop` binds one immutable default `Target` from `bootstrap_gateway_registry()`, revalidates the fixed deployment asset through an identity-checked `O_NOFOLLOW` read on every Launchd pass, constructs only existing read-only Process/Launchd/Log/Cron collectors, and executes them through the Phase 2 deadline-isolated `collect_all`. `ObservationLedger` is the sole Phase 3 sink: it stores only deep-frozen detached records in memory with hard run/signal/byte budgets, exposes UTC daily summaries and a fully bounded Terra input, and has no SQLite or filesystem writer. A separate cadence helper describes the natural seven-day run protocol but does not install a scheduler.

**Tech Stack:** Python 3.11+, existing Phase 2 collectors and `CollectionBatch` contracts, `MemoryObserverStore`, `FleetRegistry`, `pytest`, JSON-compatible immutable evidence.

## Global Constraints

- Authority is `observe_only`; no Gateway/LaunchAgent/Cron/Target/business-data writes.
- No LLM, model call, dashboard chat, automatic repair, restart/stop/start, `launchctl` write, or scheduler installation.
- Only the fixed default deployment asset is core scope; `feishu3`, `feishu4`, `feishu5`, and `newbot` remain registered but out of scope.
- Evidence is recursively redacted, detached/frozen, append-only, bounded by runs/signals/bytes, and never includes raw untrusted paths or secret material.
- SQLite persistence remains deferred; production observation uses memory only.
- Seven-day evidence is not claimed until an operator runs the documented cadence for seven real days.

### Task 1: Define the bounded observation ledger

**Files:**
- Create: `plugins/agentops/control/observation.py`
- Test: `tests/plugins/agentops/phase3/test_observation_loop.py`

**Interfaces:**
- Produces `ObservationLedger(max_runs: int, max_signals: int, max_bytes: int)`, `append(batch: CollectionBatch)`, `batches()`, `daily_summary(day)`, and `terra_input(day, max_items, max_bytes)`.
- Consumes only validated `CollectionBatch` values and Phase 2 `verify_redacted_signal`.

- [x] Write tests for append-only ordering, detached payloads, redaction rejection, run/signal/byte budget rejection, daily counts, and Terra input limits.
- [x] Implement immutable detached records with `verify_redacted_signal`, canonical JSON byte accounting, and fail-closed budget checks before mutation.
- [x] Run focused observation tests; ledger, summary, and Terra tests pass.

### Task 2: Implement the default read-only collection loop

**Files:**
- Modify: `plugins/agentops/control/observation.py`
- Test: `tests/plugins/agentops/phase3/test_observation_loop.py`

**Interfaces:**
- Produces `DefaultObservationLoop.create(...)`, `collect_once()`, `run_once()`, `collector_names`, and `target`.
- Consumes `bootstrap_gateway_registry`, `ProcessCollector`, `LaunchdCollector`, `LogCollector`, optional `CronCollector`, and `collect_all`.

- [x] Write tests with fake processes and temporary regular-file assets for exact command fingerprint/owner binding, plist/log/Cron collection, and injection/tamper fail-closed behavior.
- [x] Bind only `hermes:profile:default:gateway`; reject missing/disabled/untrusted default asset labels before collection.
- [x] Construct collectors with manifest budgets and fixed approved paths; never call lifecycle APIs or write input assets.
- [x] Commit each valid batch to the memory ledger and process evidence only when target/source/health/signals are valid; never use SQLite.
- [x] Run focused loop tests and verify all input file hashes are unchanged.

### Task 3: Add daily summary and Terra handoff contracts

**Files:**
- Modify: `plugins/agentops/control/observation.py`
- Create: `docs/superpowers/specs/2026-08-09-agentops-default-observation-g3-evidence.md`
- Test: `tests/plugins/agentops/phase3/test_observation_loop.py`

- [x] Test stable day bucketing, healthy/unhealthy/reason counts, and Terra input redaction plus byte/item budgets.
- [x] Implement summaries as pure projections over ledger records; Terra input contains only bounded metadata/signals, no actions or repair instructions.
- [x] Document the seven-day natural-run cadence (`collect_once` at the documented interval), P0/P1 injection/label validation, exit conditions, rollback (stop invoking the loop; no target rollback), and explicit non-claims.

### Task 4: Verify the complete read-only boundary

**Files:**
- Modify: `docs/superpowers/specs/2026-08-09-agentops-default-observation-g3-evidence.md`

- [x] Run Phase 3 + all AgentOps tests, plugin regression, compileall, static read-only scan, and `git diff --check`.
- [x] Record raw command results and known limitations; specifically state that seven-day online observation remains pending until naturally executed by the operator.
- [ ] Commit a clean worktree and hand the commit to Sol for independent review; do not push, merge, install scheduling, or enter Phase 4.
