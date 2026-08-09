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
  validated existing AgentOps state directory. Target SQLite files are never
  opened through SQLite: Phase 2 records regular-file metadata and
  `integrity=unknown`; log/plist/Git reads reject symlinks.
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
- [x] Inspect target SQLite DB/WAL/SHM only through regular-file metadata and
  report integrity as unknown; inspect Git `HEAD` and config by direct file
  reads. The Git collector reports
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

## Sol G2 Remediation Addendum (2026-08-09)

**Trigger:** Independent G2 review returned `changes_requested`. The earlier
G2 evidence is superseded until every item below is implemented, negatively
tested and independently re-reviewed. This addendum changes no authorization:
Phase 2 remains an isolated read-only observer only.

### Remediation scope and invariants

- [x] **Target SQLite isolation.** Target DB/WAL/SHM are never opened through
  SQLite while an external writer may be live. The collector records only
  regular-file metadata and `integrity=unknown`; a test keeps a writer open
  and proves every target file hash is unchanged.
- [x] **End-to-end redaction.** JSON and quoted key/value log messages are
  parsed before normalization, recursively redacted, then re-scanned at
  collector and observer-store boundaries. Password/token/cookie canaries may
  not appear in a persisted signal, snapshot, error or Bridge payload.
- [x] **Lossless multi-source log cursors.** Cursors include a stable source
  fingerprint as well as target and collector identity. Advancing a cursor is
  limited to bytes actually converted into at most `max_lines` signals; signal
  deduplication remains independent of source identity.
- [x] **Collection-run evidence and Cron safety.** Persist every collection
  run's observation ID, time, health and reason. Signal occurrence state tracks
  first/last seen and count. Missing, stale or failing Cron assertions are
  unhealthy even if exit code is zero.
- [x] **Bridge immutability and concurrency.** The Bridge canonicalizes and
  deep-copies event data, revalidates when enqueueing/draining, protects queue
  operations with a lock and has capacity/injection race tests.
- [x] **Git containment and standard worktree layouts.** Resolve refs only
  under canonical git/common directories after every parent symlink check;
  reject traversal. Support normal `.git` directories, gitdir files,
  `commondir`, and packed refs by direct bounded reads.
- [x] **Collector binding and budgets.** Targets own canonical approved asset
  identities. Collectors reject unbound sources, impose wall-clock deadlines,
  byte/item ceilings and rate limits, and report bounded failure evidence.
- [x] **ObserverStore preflight.** Before any SQLite connection or WAL change,
  verify AgentOps marker, ownership, modes, fixed path/inode constraints and
  exact known schema/version/integrity. An unrelated existing database must be
  byte-for-byte and journal-mode unchanged.
- [x] **Review Pack completion.** Declare pack/version, target kinds, explicit
  probes/budgets, assertion IDs/severity/mandatory states, input
  classifications, production-read/dry-run/no-write behavior, failure runbook
  and retention, with a strict manifest contract test.
- [x] **Evidence and interpreter parity.** Replace invalidated G2 claims with
  fresh results. Run Phase 1+2 and plugin regressions, security scans,
  compileall and a Python 3.14 test environment with required dependencies
  isolated from the main environment.

### Additional files and tests

| Area | Expected implementation/tests |
|---|---|
| Models/registry/cursors | source-aware `CursorKey`, immutable snapshots/assets; cursor loss tests |
| Logs/redaction/store | JSON canary E2E, partial-line consumption, multi-log state, schema preflight and run-history tests |
| Collectors | no-target-SQLite-open, live-WAL hashes, Git traversal/worktree/packed-ref, asset binding/deadline/budget tests |
| Bridge | deep-copy, concurrent capacity and delivery revalidation tests |
| Review pack/evidence | expanded manifest contract and corrected G2 matrix |

### G2 re-review gate

- [x] All remediation checkboxes have targeted negative tests.
- [x] Fresh full verification is recorded without masking unavailable
  interpreter dependencies.
- [x] Worktree is clean after a remediation-only commit; no push, merge or
  Phase 3 transition has occurred.

## Sol G2 Remediation Addendum 2 (2026-08-09)

**Trigger:** Sol's second G2 review found additional counterexamples. The
previous remediation evidence is superseded again until the exact invariants
below are implemented and independently re-reviewed.

- [x] **Exact store schema preflight.** Validate every expected table column,
  SQLite type, primary/foreign key and uniqueness constraint, schema version
  monotonicity and store marker in a read-only connection before opening a
  writable handle. Same-name incompatible v1 stores, DELETE→WAL changes and
  bytes changes are negative-tested and rejected.
- [x] **Full-record secret gate.** Redact and rescan every persisted string,
  including run reason, source/collector metadata, snapshot version, IDs,
  timestamps, report/manifest fields and sidecars. A canary cannot occur in
  DB/WAL/SHM, spool, quarantine or emitted report data.
- [x] **Cron freshness and manifest authority.** Execution freshness is checked
  independently from exit code. Every mandatory assertion must be declared by
  the loaded Review Pack; unknown, missing, stale and failed assertions are
  unhealthy, including assertions with `mandatory=True` that are absent from
  the manifest.
- [x] **Git exact registered-root containment.** `.git`, gitdir, commondir,
  loose refs and packed refs resolve only beneath the registered repository
  root or its explicitly registered canonical Git metadata roots. Every parent
  component is checked for symlinks.
- [x] **Process identity binding.** Match target profile label, command
  fingerprint and owner; a PID observed under another Profile is not accepted
  merely because a service label exists.
- [x] **Worker and inspection budgets.** Collector deadlines expose worker
  lifecycle/termination state and isolate unkillable workers. Process budgets
  count every inspected item, not only emitted matches; all budget failures
  remain bounded and observable.
- [x] **Bridge exactly-once drain acknowledgement.** Concurrent drains claim
  an item once, mark it in-flight and remove it only after successful delivery;
  no event is delivered twice.
- [x] **Monotonic evidence commits.** Out-of-order collection batches cannot
  move `last_seen`, cursors or source observations backward; observation IDs
  and sequence/ordering guards reject stale commits.
- [x] **Executable Review Pack validation.** A loader resolves collector entry
  points, enforces capability/target-kind compatibility and validates all
  budget/rate limits before any collector can run. Missing, over-budget or
  mismatched declarations are rejected.

### Additional second-round tests

- [x] incompatible same-name v1 schema, full-field canary scan, stale/unknown
  Cron and manifest-mandatory assertion tests
- [x] Git symlink/root escape and cross-profile PID tests
- [x] timed-out worker lifecycle, inspected-item budget, concurrent Bridge
  drain, out-of-order signal/cursor and manifest loader contract tests
