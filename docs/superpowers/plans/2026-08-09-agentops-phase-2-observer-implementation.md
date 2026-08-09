# AgentOps Phase 2 Observer & Fleet Coverage Implementation Plan

> **For agentic workers:** Execute this authorized Phase 2 plan only in its
> isolated worktree. Steps use checkbox (`- [x]`) syntax for handoff and G2
> review; a checked item is backed by the tests named in that task.

**Goal:** Add an isolated, strictly read-only observer layer that inventories
the five approved Hermes Gateway profiles and collects bounded evidence from
logs, processes, plist configuration, Cron observations, SQLite health and Git
metadata. It must not alter a monitored target, a Gateway path, or Phase 1.

**Architecture:** Phase 2 is a new library surface under
`plugins/agentops/control`, with no plugin registration or daemon wiring. A
`FleetRegistry` owns immutable `observe_only` target definitions and snapshots.
Collectors return redacted `CollectionBatch` values, while a separate local
`ObserverStore` commits only those batches to an AgentOps-owned SQLite WAL
database. The Bridge is an unregistered, in-process bounded queue: a failed
consumer is contained in the queue and never changes a Gateway result. The
runtime-core Review Pack is declarative YAML rather than an executable plan.

**Tech Stack:** Python 3.10+ standard library, installed `psutil`, SQLite WAL,
PyYAML, pytest. No model client, network client, Dashboard, command runner, or
target-specific SDK is introduced.

## Global Constraints

- This implementation is authorized as Phase 2 only. Phase 1 remains frozen;
  no edit may be made to the existing plugin manifest, CLI, daemon, API, store,
  EventSpool, audit chain, config, or their tests.
- Every registered target has `AuthorityMode.OBSERVE_ONLY`. Registry methods
  deliberately omit authority mutation, action planning, remediation, and
  lifecycle-control methods.
- The implementation must never start, stop, restart, install, unload, or
  otherwise change a process, service, Cron job, Gateway, LaunchAgent, Git
  worktree, SQLite target, or business data. Collectors use direct bounded
  file/API reads only; they never invoke a command interpreter.
- AgentOps state writes are confined to a fixed `observer.db` underneath a
  validated existing AgentOps state directory. Target SQLite files use a
  read-only URI with `query_only=ON`; log/plist/Git reads reject symlinks.
- Raw collector data is redacted before becoming a `Signal`, Bridge payload, or
  observer-store row. Redaction is applied once by the collector boundary and
  again by `commit_collection`; secret-like fields and token/cookie value
  patterns are replaced, not echoed in errors.
- Individual collector errors are represented as unhealthy batches; fan-out
  continues. Bridge delivery errors are represented by a bounded local queue
  and never re-raised into the caller.
- No LLM, Dashboard, R1/R2/R3/R4, incident aggregation, or automatic repair
  capability may appear in this branch.

## File Structure

| File | Responsibility |
|---|---|
| `plugins/agentops/control/observer_models.py` | Frozen target, snapshot, cursor, signal, batch and Cron assertion contracts |
| `plugins/agentops/control/registry.py` | In-memory stable fleet registry and five-profile bootstrap inventory |
| `plugins/agentops/control/cursors.py` | Inode/offset cursors and lossless rotation/truncation decision logic |
| `plugins/agentops/control/redaction.py` | Structured/text redaction and secret re-scan gate |
| `plugins/agentops/control/observer_store.py` | AgentOps-only observer SQLite WAL persistence and atomic batch/cursor commit |
| `plugins/agentops/control/collectors/base.py` | Collector protocol, isolated fan-out and duplicate-signal suppression |
| `plugins/agentops/control/collectors/logs.py` | Regular-file bounded log reader using inode/offset cursors |
| `plugins/agentops/control/collectors/processes.py` | `psutil` process snapshot collector with command-line fingerprints only |
| `plugins/agentops/control/collectors/launchd.py` | Read-only plist configuration snapshot collector |
| `plugins/agentops/control/collectors/cron.py` | Execution result and independent business-assertion collector |
| `plugins/agentops/control/collectors/sqlite_health.py` | SQLite/WAL read-only integrity and size collector |
| `plugins/agentops/control/collectors/git_state.py` | Direct `.git` HEAD/config metadata collector; dirty state stays conservative unless supplied by a read-only observer |
| `plugins/agentops/bridge.py` | Unregistered bounded Bridge buffer and failure containment |
| `plugins/agentops/review_packs/runtime_core/manifest.yaml` | Declarative runtime-core Review Pack manifest |
| `tests/plugins/agentops/phase2/...` | Unit, contract, integration and static-boundary coverage for Phase 2 |

## Task 1: Freeze the Phase 2 observer contracts and fleet inventory

**Files:**
- Create: `plugins/agentops/control/observer_models.py`
- Create: `plugins/agentops/control/registry.py`
- Test: `tests/plugins/agentops/phase2/unit/test_registry.py`

**Interfaces:**

```python
register_target(spec: TargetSpec) -> Target
record_target_snapshot(snapshot: TargetSnapshot) -> None
get_target(target_id: str) -> Target
list_targets() -> tuple[Target, ...]
coverage_report() -> FleetCoverage
```

- [x] Define frozen, validated data contracts for targets, snapshots, raw and
  redacted signals, batches, collector health, and independent Cron execution
  / business assertion states. Every `Target` pins observe-only authority.
- [x] Add a registry that refuses duplicate IDs and snapshots for unknown
  targets, returns ordered immutable inventories, and contains no authority
  change method.
- [x] Bootstrap exactly the five Phase 0 Gateway profile target IDs:
  `default`, `feishu3`, `feishu4`, `feishu5`, and `newbot`, each with the
  existing writer recorded as metadata rather than controlled by AgentOps.
- [x] Test duplicate rejection, snapshot recording, stable 100% five-target
  coverage, and immutable observe-only authority.

## Task 2: Redaction, cursors and AgentOps-only evidence persistence

**Files:**
- Create: `plugins/agentops/control/cursors.py`
- Create: `plugins/agentops/control/redaction.py`
- Create: `plugins/agentops/control/observer_store.py`
- Test: `tests/plugins/agentops/phase2/unit/test_cursors.py`
- Test: `tests/plugins/agentops/phase2/unit/test_redaction.py`
- Test: `tests/plugins/agentops/phase2/unit/test_observer_store.py`

**Interfaces:**

```python
advance_log_cursor(cursor: LogCursor | None, stat_result: os.stat_result) -> CursorDecision
redact_signal(signal: RawSignal, policy: RedactionPolicy = DEFAULT_POLICY) -> Signal
commit_collection(batch: CollectionBatch) -> None
```

- [x] Represent inode/offset cursors and explicitly classify first read,
  rotation and truncation. New offsets must never exceed the current regular
  file size.
- [x] Build a non-echoing redaction policy for structured sensitive keys and
  token, credential, cookie and authorization values. Apply a final secret
  scan to every produced signal.
- [x] Create a separate `observer.db` only within an already validated
  AgentOps state root. Enable WAL and write batches plus their next cursor in
  one transaction. The store accepts only redacted signals and re-applies the
  gate before persistence.
- [x] Test synthetic token/cookie/user content, cursor reset classification,
  target-DB path rejection, WAL storage, and atomic batch/cursor commit.

## Task 3: Implement isolated read-only collectors

**Files:**
- Create: `plugins/agentops/control/collectors/__init__.py`
- Create: `plugins/agentops/control/collectors/base.py`
- Create: `plugins/agentops/control/collectors/logs.py`
- Create: `plugins/agentops/control/collectors/processes.py`
- Create: `plugins/agentops/control/collectors/launchd.py`
- Create: `plugins/agentops/control/collectors/cron.py`
- Create: `plugins/agentops/control/collectors/sqlite_health.py`
- Create: `plugins/agentops/control/collectors/git_state.py`
- Test: `tests/plugins/agentops/phase2/contract/test_collector_protocol.py`
- Test: `tests/plugins/agentops/phase2/integration/test_log_rotation.py`
- Test: `tests/plugins/agentops/phase2/integration/test_read_only_collectors.py`

**Interfaces:**

```python
collect(target: Target, cursor: Cursor | None) -> CollectionBatch
collect_all(target: Target, collectors: Iterable[Collector], cursors: Mapping[str, Cursor]) -> tuple[CollectionBatch, ...]
```

- [x] Make collector calls isolated: a timeout/exception becomes one unhealthy
  batch, while the remaining collectors return their own batches.
- [x] Read only regular, non-symlink log files with a byte and line ceiling;
  preserve inode/offset state through rotation and truncation. Normalize the
  message fingerprint and remove duplicate signals across source log files.
- [x] Capture process facts through `psutil`, retaining a command-line hash
  rather than raw arguments. Read plist files with `plistlib`, retaining only
  label and a configuration fingerprint.
- [x] Model Cron success as two facts: execution state and business assertion.
  A zero exit with a failing assertion must make the batch unhealthy and emit
  an unhealthy signal.
- [x] Inspect target SQLite DB/WAL/SHM via read-only SQLite URI and file stats;
  inspect Git `HEAD` and config by direct file reads. The Git collector reports
  dirty state as `unknown` unless a read-only metadata callback explicitly
  supplies it, so it never asserts a false clean worktree.
- [x] Test collector protocol, isolated failure, log duplicate suppression,
  rotation/truncation recovery, Cron false-green detection, symlink refusal,
  and target SQLite unchanged after a collection.

## Task 4: Contain Bridge failures and declare the runtime-core Review Pack

**Files:**
- Create: `plugins/agentops/bridge.py`
- Create: `plugins/agentops/review_packs/runtime_core/manifest.yaml`
- Test: `tests/plugins/agentops/phase2/unit/test_bridge.py`
- Test: `tests/plugins/agentops/phase2/contract/test_review_pack_manifest.py`

- [x] Add an in-memory bounded FIFO queue with explicit drop accounting. Its
  delivery wrapper catches consumer failure, queues at most its fixed capacity,
  and returns a status; it neither registers itself with Hermes nor imports
  Gateway code.
- [x] Re-scan events at the Bridge boundary so no invalid or secret-bearing
  payload becomes queued or delivered.
- [x] Declare the `runtime_core` manifest with read-only collector inputs,
  evidence fields, redaction policy and no action/runbook section.
- [x] Test a closed/raising consumer cannot alter the caller's result or grow
  the buffer beyond its cap, and validate the manifest's observe-only fields.

## Task 5: Phase 2 verification and G2 handoff

**Files:**
- Update: this plan (checkboxes and G2 evidence after verified execution)
- Create: `docs/superpowers/specs/2026-08-09-agentops-phase-2-g2-evidence.md`

- [x] Run the complete Phase 1 + Phase 2 AgentOps test tree.
- [x] Run existing Hermes plugin discovery/CLI regression tests.
- [x] Run a static source boundary scan for write/control primitives and a
  Python compile pass. Run a compatible secondary Python version when present.
- [x] Record raw command output, stable five-target inventory, rotation and
  false-green evidence, failure isolation, Bridge containment, and remaining
  observer limitations. G2 remains a review request, not an approval claim.
- [x] Re-check the diff and worktree cleanliness, then commit only Phase 2
  source, tests, plan and evidence. Do not push, merge, install services, or
  begin Phase 3.

## Plan Self-Review

- [x] Every Phase 2 requirement has a named library boundary and a test family.
- [x] The implementation has no write target, service-control, shell, LLM,
  Dashboard, or remediation path.
- [x] Target database and Git cleanliness observations are conservative: failed
  or unavailable read-only inspection reports unhealthy/unknown evidence rather
  than a success inference.
- [x] G2 approval remains outside this plan and requires independent review.
