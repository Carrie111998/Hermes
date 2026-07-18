# Review Log — HTR

---

## 2026-07-18 — Task 3: Task Card + Result Contract + Artifact Manifest

**Implementer:** Cursor  
**Scope:** `htr/contracts.py`, `htr/artifacts.py`, `htr/schemas.py`, `htr/events.py`, `htr/__init__.py`, tests, docs  
**Production Runtime modified:** No  
**DECO / HEAL integrated:** No  
**Verification pipeline:** No  
**delegate_task modified:** No  
**SQLite introduced:** No

### Changes

**A. Task Card (`htr/contracts.py`)**

- `make_task_card`, `write_task_card`, `read_task_card`
- Path: `tasks/<task_id>/task_card.json` (atomic write)
- Does not mutate task status or create attempts

**B. Attempt Result**

- `make_attempt_result`, `result_fingerprint`
- `submit_attempt_result` in `htr/events.py`
- Writes `output/result.json`, appends `attempt_result_submitted` event, status → `result_submitted` only
- Result idempotency keyed on `result_fingerprint` in event payload (retry-safe after success)

**C. Artifact Manifest (`htr/artifacts.py`)**

- `read/write_artifact_manifest`, `add_artifact`, `list_artifacts`
- `ArtifactConflict` on path+kind mismatch
- Idempotent duplicate path+kind+metadata/checksum/size
- No lifecycle events on add

**D. Checksum**

- `compute_sha256(path)` — streaming via existing `sha256_file`

**E. Schemas**

- Added `task_card`, `attempt_result`, `artifact_entry`; enhanced `artifact_manifest` validation

### Task 3 self-review (final acceptance pass)

Checklist A–L verified. Fixes applied:
- Removed `result_path` from replay core identity
- Early `result_fingerprint` computation in `submit_attempt_result`
- Added 15+ checklist tests (replay no-op, actor conflict, identity mismatch, exports, schemas)

### Verification

```bash
cd /home/unaliu/.hermes/hermes-agent
source venv/bin/activate
python3 -m pytest tests/htr/ -v
# 143 passed in 1.08s
```

### Known limitations (accepted, non-blocking)

- No verification execution or pass/fail decision
- No HEAL execution or runtime integration
- No event replay; JSON snapshot remains operational read source
- No concurrent writer locks
- Artifact manifest is metadata only
- `htr` not yet in `pyproject.toml`

---

## 2026-07-18 — Task 2.1: State/Event API Idempotency Ordering Fix

**Implementer:** Cursor  
**Scope:** `htr/events.py`, `tests/htr/test_events.py`, docs  
**Production Runtime modified:** No  
**DECO / HEAL:** No  
**delegate_task modified:** No  
**SQLite:** No

### Problem

- `apply_task_transition` / `apply_attempt_transition` ran `_resolve_idempotent_event` before transition validation, allowing duplicate `event_id` to bypass invalid current-state transitions.
- `_semantic_fingerprint` omitted `previous_status`, risking false idempotent match between e.g. `created->running` and `blocked->running`.

### Fix

**A. Transition ordering (task + attempt)**

1. Read current status snapshot
2. Compute `previous_status`
3. `assert_valid_*_transition(previous_status, new_status)` — **before** idempotency
4. Build candidate event
5. `_resolve_idempotent_event`
6. Append event
7. Atomic write status snapshot

**B. `_semantic_fingerprint`**

Now includes: `event_type`, `run_id`, `task_id`, `attempt_id`, `previous_status`, `new_status`, `actor`, `payload` (excludes `created_at`).

**C. `register_attempt`**

Confirmed order: candidate → idempotent resolve → return existing if match → `AttemptAlreadyRegistered` only for different `event_id` → bootstrap → append → update attempts.

### Verification

```bash
cd /home/unaliu/.hermes/hermes-agent
source venv/bin/activate
python3 -m pytest tests/htr/ -v
# 86 passed in 0.65s
```

### New tests

- Duplicate `event_id` + currently invalid transition → `InvalidTransition` (task + attempt)
- Same `event_id` + different `previous_status` → `EventConflict`
- `register_attempt` same `event_id` retry → idempotent return after first success

---

## 2026-07-18 — Task 2: Task/Attempt State Machine + Event Log API

**Implementer:** Cursor  
**Scope:** `htr/state.py`, `htr/events.py`, `htr/schemas.py`, `htr/__init__.py`, `tests/htr/test_state.py`, `tests/htr/test_events.py`, docs  
**Production Runtime modified:** No  
**DECO / HEAL integrated:** No  
**delegate_task modified:** No  
**SQLite introduced:** No  
**Verification pipeline:** No (transitions only)  
**Runtime controller:** No

### Changes

**A. `htr/state.py`**

- TaskStatus / AttemptStatus string constants
- Legal transition tables per Owner spec
- `is_valid_*` / `assert_valid_*` transition helpers
- Terminal status helpers
- Exceptions: `HTRStateError`, `InvalidTransition`, `EventConflict`, `AttemptAlreadyRegistered`, `EventValidationError`

**B. `htr/events.py`**

- Event envelope: `make_event`, `append_task_event`, `read_task_events`, `event_exists`
- Lifecycle APIs: `apply_task_transition`, `register_attempt`, `apply_attempt_transition`
- Order: append event → atomic write status snapshot
- Idempotency: same `event_id` + matching semantic fingerprint → return existing
- Semantic fingerprint excludes `previous_status` and `created_at` (retry-safe)
- `register_attempt`: calls `create_attempt_workspace`, appends event, updates `task_status.attempts`
- Same `attempt_id` + different `event_id` → `AttemptAlreadyRegistered`

**C. `htr/schemas.py`**

- Added `event` schema validation (lightweight, no pydantic)

**D. Tests**

- `test_state.py`: 42 tests (legal/illegal transitions, terminal helpers)
- `test_events.py`: 13 tests (round trip, idempotency, lifecycle, field preservation)

### Verification

```bash
cd /home/unaliu/.hermes/hermes-agent
source venv/bin/activate
python3 -m pytest tests/htr/ -v
# 82 passed in 0.55s
```

### Known limitations

- No event replay / snapshot rebuild
- No concurrent write locking
- `htr` not yet in `pyproject.toml`
- Verification / HEAL are state values only

### Open items (for next tasks)

- Task 2-pre: `pyproject.toml` packaging
- Verification pipeline execution
- Runtime controller hooks
- Tool audit binding
- DECO/HEAL bridges
- C-03 Runtime guard

---

## 2026-07-18 — Task 1.1: HTR Core Foundation Hardening

**Implementer:** Cursor  
**Scope:** `htr/io.py`, `tests/htr/test_io.py`, docs only  
**Production Runtime modified:** No  
**DECO / HEAL integrated:** No  
**State machine implemented:** No

### Changes

**A. `atomic_write_json` hardened**

- Unique temp via `tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp")`
- UTF-8 JSON write, flush, `os.fsync` on temp fd
- `os.replace` into target
- Best-effort parent directory fsync
- Temp file cleanup on exception
- Removed fixed `{name}.tmp` pattern

**B. Workspace creation idempotency**

- `_init_json_if_missing()` — write JSON only when file absent
- `_touch_jsonl()` — create empty file only when absent (never truncate)
- `create_run_workspace` — does not overwrite existing `run_manifest.json`
- `create_task_workspace` — does not overwrite existing `task_status.json`
- `create_attempt_workspace` — does not overwrite `attempt_status.json` / `artifact_manifest.json`
- Repeated calls preserve `created_at`, status, attempts, JSONL content

**C. Reserved paths documented in docstrings**

- `task_card.yaml` — not created by bootstrap
- `output/result.json` — not created by bootstrap

### Verification

```bash
cd /home/unaliu/.hermes/hermes-agent
source venv/bin/activate
python3 -m pytest tests/htr/ -v
# 27 passed in 0.29s
```

### Open items (unchanged, for Task 2)

- Attempt registration → state/event API, not create_*
- `htr` packaging in pyproject.toml → Task 2-pre or Task 2
- C-03 enforcement → deferred

---

## 2026-07-18 — Owner correction: external component locations (post Task 0)

**Source:** Owner  
**Scope:** Documentation alignment only

### Corrected paths

| Component | Path |
|-----------|------|
| DECO policy | `~/hermes-data/hooks/policy_engine.py` + `policy.yaml` |
| HEAL | `~/hermes-data/hooks/heal_overseer.py`, `heal_diagnose.py`, `heal_evolve.py` |
| Side-effect collector | `~/hermes-data/hooks/side_effect_collector.py` |
| L0 Task Card (SOUL) | `~/.hermes/SOUL.md` |

### Conflict reclassification

- **C-01 / C-02:** False alarms (repo boundary — components external to `hermes-agent`)
- **Real gaps:** C-04, C-07; C-05 greenfield → addressed by Task 1
- **ADR-007 clarified:** HTR lifecycle file-only; existing Hermes SQLite untouched
- **C-03:** Deferred — enforce via `max_spawn_depth=1` or equivalent in later task
- **C-08:** Writer confirmed; bridge deferred

Updated: `02_ARCHITECTURE_DECISIONS.md`, `08_CONTEXT_SUMMARY.md`

---

## 2026-07-18 — Task 1: HTR Core Foundation

**Implementer:** Cursor  
**Scope:** New `htr/` + `tests/htr/` only  
**Production Runtime modified:** No  
**Risk:** Low

### Files added

| Path | Purpose |
|------|---------|
| `htr/ids.py` | 10 prefixed ID generators + validate/parse |
| `htr/paths.py` | `~/.hermes/runs/` path contract + traversal guard |
| `htr/io.py` | Atomic JSON/JSONL IO, sha256, workspace bootstrap |
| `htr/schemas.py` | run/task/attempt/manifest validation |
| `htr/__init__.py` | Public exports |
| `tests/htr/test_*.py` | 22 unit tests (all use `tmp_path`) |

### Verification

```bash
cd /home/unaliu/.hermes/hermes-agent
source venv/bin/activate
python3 -m pytest tests/htr/ -v
# 22 passed in 0.24s
```

### Acceptance checklist (Task 1)

- [x] pytest `tests/htr/` all pass
- [x] Tests use `tmp_path` only
- [x] ID format + uniqueness validated
- [x] Path traversal rejected
- [x] Atomic write round-trip
- [x] Full run/task/attempt workspace tree created

### Risks / notes

- `htr/` not yet listed in `pyproject.toml` `[tool.setuptools.packages.find]` — imports work via repo root on `sys.path` (same as tests). Packaging entry can be added in a later task if needed.
- Default runs root uses `hermes_constants.get_hermes_home() / "runs"` when available; tests override with `base_dir`.
- C-05 (runs workspace) foundation delivered; state machine / events not in scope.

### Conflict status (unchanged policy)

| Conflict | Status after Task 1 |
|----------|---------------------|
| C-03 nested delegation | Policy: `max_spawn_depth=1` for HTR (not enforced in code yet) |
| C-04 self-reported results | Deferred — Phase 1 later task |
| C-07 signed audit | Deferred — Phase 1 later task |

---

## 2026-07-18 — Task 0: Baseline landing + repository reconnaissance

**Implementer:** Cursor  
**Scope:** Documentation only (`docs/runtime_project/*`)  
**Production Runtime modified:** No  
**Tests run:** None (docs-only task; test entry confirmed but not executed)

### Deliverables

| File | Action |
|------|--------|
| `docs/runtime_project/00_PROJECT_BRIEF.md` | Created |
| `docs/runtime_project/01_ARCHITECTURE_BASELINE.md` | Created |
| `docs/runtime_project/02_ARCHITECTURE_DECISIONS.md` | Created |
| `docs/runtime_project/03_PHASE_PLAN.md` | Created |
| `docs/runtime_project/04_CURSOR_RULES.md` | Created |
| `docs/runtime_project/05_TASK_QUEUE.md` | Created |
| `docs/runtime_project/06_ACCEPTANCE_CHECKLIST.md` | Created |
| `docs/runtime_project/07_REVIEW_LOG.md` | Created |
| `docs/runtime_project/08_CONTEXT_SUMMARY.md` | Created |

### Repository reconnaissance report (full)

#### A. Repository topology

| Repository | Path | Version / Notes |
|------------|------|-----------------|
| **hermes-agent (primary)** | `/home/unaliu/.hermes/hermes-agent` | v0.18.2, git install, origin `NousResearch/hermes-agent`, HEAD `d59b79fa` |
| **Hermes runtime home** | `/home/unaliu/.hermes` | Sessions, profiles, config, runtime artifacts |
| **ebay_swarm (domain)** | `/home/unaliu/ebay_swarm` | eBay pipeline + overseer/heal prototypes |
| **Windows mirror** | `C:\Users\Unaliu\.workbuddy\hermes\ebay_swarm_code` | Partial copy of swarm code (not primary truth) |

#### B. Integration point map

##### 1. `delegate_task` entry

| Path | Current responsibility | HTR mapping |
|------|------------------------|-------------|
| `tools/delegate_tool.py` | Spawns child agents; `role=leaf|orchestrator`; blocks tools for children; returns summary array to parent | **Primary hook** for Orchestrator–Worker protocol envelope |
| `tools/async_delegation.py` | Background `delegate_task(background=true)` pool | Phase 1 background dispatch candidate |
| `tools/process_registry.py` | Tracks delegate fan-out lifecycle | Attempt lifecycle reference |
| `gateway/run.py`, `gateway/session_context.py` | Gateway integration for delegation events | Event persistence integration point |

**Behavior notes:**

- Leaf (`role='leaf'`) cannot call `delegate_task` (aligned with ADR-011 for leaf).
- Orchestrator children **can** nested-delegate when `delegation.max_spawn_depth >= 2` and `orchestrator_enabled=true` (**conflicts** with baseline "only Main Agent orchestrates").
- Subagent results are **self-reported summaries**, not verified artifacts.

##### 2. Tool runtime / tool call entry

| Path | Current responsibility | HTR mapping |
|------|------------------------|-------------|
| `agent/tool_executor.py` | Sequential/concurrent tool dispatch | Inject run/task/attempt context here |
| `tools/registry.py` | Tool registration and schema | Audit binding at registration/invoke boundary |
| `run_agent.py` | Agent loop wrapper | Top-level orchestration entry |
| `agent/conversation_loop.py` | Main turn loop; uses `task_id` for VM/file isolation | Distinct from HTR Task/Attempt IDs |
| `agent/tool_dispatch_helpers.py` | Batching, result message shaping | Evidence capture hook |
| `tools/terminal_tool.py`, `tools/file_tools.py` | Side-effecting tools; container `task_id` scoping | Tool evidence sources |

##### 3. Audit log / tool audit

| Path | Current responsibility | HTR mapping |
|------|------------------------|-------------|
| `gateway/session.py` | Session store (SQLite primary + legacy JSON); request dumps | Session-level audit, not attempt-level signed audit |
| `agent/trajectory.py` | Optional ShareGPT trajectory JSONL | Training/debug, not HTR contract audit |
| `agent/verification_evidence.py` | SQLite ledger of command verification evidence | Partial overlap with Evidence Verification (coding-focused) |
| `~/.hermes/sessions/*.json` | Persisted session transcripts | Historical tool call records |
| `~/.hermes/side_effects.json` | Runtime side-effect log (104KB+) | **Side-effect ledger data exists; writer code not found in repos** |

**Gap:** No "Signed Tool Audit" module binding `tool_call_id` to `attempt_id` with checksum/immutability as baseline requires.

##### 4. DECO policy / gate / approval

| Path | Current responsibility | HTR mapping |
|------|------------------------|-------------|
| `tools/approval.py` | Dangerous command detection + human/async approval | **Closest to DECO L0/L3** |
| `agent/tool_guardrails.py` | Per-turn loop detection (warn/hard-stop) | **Closest to DECO L2/L4 risk gate (partial)** |
| `agent/file_safety.py` | File mutation safety | Policy adjunct |
| Profile skills (docs only) | e.g. `code-review-gate`, `execution-pregate` under `~/.hermes/profiles/liuqiong/skills/devops/` | Conceptual DECO docs, not runtime module |

**Critical gap:** No code module named DECO with L0–L5 planes. ADR-010 assumes reuse — **must be resolved by Architect** (implement DECO vs map existing gates vs external package).

##### 5. Hermes HEAL (overseer / diagnose / evolve)

| Path | Current responsibility | HTR mapping |
|------|------------------------|-------------|
| **Not found in hermes-agent core** | — | Baseline HEAL is greenfield in core |
| `ebay_swarm/docs/overseer/overseer_agent.py` | Domain loop: detect → fix → verify → red-light stop | Domain overseer prototype, not generic HEAL |
| `ebay_swarm/docs/overseer/heal_submit_fails.py` | Submit failure healing | Domain heal action |
| `ebay_swarm/docs/overseer/oh_heal_dispatcher.py` | HEAL dispatch helper | Domain-specific |
| `~/.hermes/profiles/*/skills/devops/self-healing-system/` | Skill documentation | Guidance only |
| `agent/curator.py`, `agent/error_classifier.py` | Agent self-improvement / error handling | Different semantics from HEAL cycle |

**Gap:** No generic `overseer → diagnose → evolve → new attempt` pipeline in hermes-agent.

##### 6. Side-effect collector / ledger

| Path | Status |
|------|--------|
| `~/.hermes/side_effects.json` | **Exists** (active log with tool entries e.g. `write_file`) |
| Source writer in hermes-agent / ebay_swarm | **Not found** in recon grep |
| `tui_gateway/server.py` | `_mirror_slash_side_effects` — slash command mirroring only |

**Uncertainty:** Side-effect ledger may come from hook, plugin, profile mod, or manual process — needs Architect/Owner confirmation.

##### 7. Verification pipeline (existing)

| Path | Layer | HTR alignment |
|------|-------|---------------|
| `agent/verification_evidence.py` | Command evidence SQLite | Partial Evidence layer (coding) |
| `agent/verification_stop.py` | Blocks completion without fresh evidence | Analogous to gate, not Task/Attempt verifier |
| `agent/verify_hooks.py` | `pre_verify` hook directives | User/plugin policy, not contract verification |

**Gap:** No Contract Verification or Domain Verification framework as baseline defines.

##### 8. Config entry

| Path | Purpose |
|------|---------|
| `~/.hermes/config.yaml` | Primary runtime config (via `hermes_constants.get_config_path()`) |
| `~/.hermes/.env` | Secrets |
| `cli-config.yaml.example` | Example CLI config |
| `hermes_constants.py` | `HERMES_HOME`, profile paths, config resolution |

Delegation knobs: `delegation.max_spawn_depth`, `delegation.orchestrator_enabled`, `delegation.max_concurrent_children` in config.yaml.

##### 9. Logs / run / workspace conventions (current)

| Path | Purpose |
|------|---------|
| `~/.hermes/sessions/` | Session JSON + request dumps |
| `~/.hermes/profiles/{profile}/workspace/` | Profile workspace CWD |
| `~/.hermes/profiles/{profile}/logs/` | Profile logs |
| `~/.hermes/verification_evidence.db` | Verification evidence SQLite |
| `~/.hermes/state.db` (via gateway session store) | Gateway session state |

**Gap:** Baseline `~/.hermes/runs/{run_id}/` tree **does not exist yet** — greenfield for HTR.

##### 10. Test framework

**hermes-agent:**

| Item | Value |
|------|-------|
| Framework | pytest |
| Config | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Test file count | ~2106 files under `tests/` |
| Runner | `scripts/run_tests.sh` |
| Manual command | `pytest tests/ -v` (from repo root with venv) |
| Markers | `integration`, `real_concurrent_gate`, `real_agent_prewarm` |

**ebay_swarm:**

| Item | Value |
|------|-------|
| Tests | `test_pipeline_profiles.py`, `test_bee_profiles.py` (per SKILL docs) |
| Runner | `python3` direct / pipeline scripts |

**Task 0 test execution:** Not run (documentation-only). Recommended Phase 1 smoke: `scripts/run_tests.sh tests/agent/test_verification_evidence.py -q`

#### C. Architecture conflicts (material)

| ID | Baseline rule | Current code reality | Severity |
|----|---------------|----------------------|----------|
| C-01 | DECO L0–L5 reusable plane | No DECO module found | **Blocker for ADR-010** |
| C-02 | Generic Hermes HEAL cycle | Only domain overseer in ebay_swarm + skills docs | **Blocker for HEAL integration** |
| C-03 | Leaf-only workers; Main orchestrates | `delegate_task` supports nested orchestrator children | **High — ADR-011** |
| C-04 | Subagent result ≠ completion | Parent trusts summary; prompts say "self-reports not verified facts" but no Runtime gate | **High — ADR-002** |
| C-05 | `~/.hermes/runs/{run_id}/` workspace | Not present; uses profile workspace + sessions | **Expected greenfield** |
| C-06 | Phase 1 no new DB | Hermes already uses SQLite for sessions/evidence | **Integration design needed (ADR-007)** |
| C-07 | Signed tool audit + attempt binding | Session/transcript level only | **High — core Phase 1 work** |
| C-08 | Side-effect ledger | Data file exists, collector source unknown | **Medium — uncertain** |

#### D. Phase 1 likely landing zones (recommendation only — not implementing)

1. **New package:** `htr/` or `agent/htr/` — Task Runtime Controller, state machine, event JSONL, workspace IO
2. **Hooks:** `tools/delegate_tool.py` — emit/consume structured task events
3. **Hooks:** `agent/tool_executor.py` — append signed tool audit records scoped to attempt
4. **Hooks:** `tools/approval.py` + `agent/tool_guardrails.py` — DECO adapter facade (pending Architect decision)
5. **New dir:** `~/.hermes/runs/` — attempt workspace root
6. **Tests:** `tests/htr/` — e2e trusted task loop

#### E. Uncertainties requiring Architect review

1. Where does DECO live today (if anywhere off-repo)?
2. What writes `~/.hermes/side_effects.json`?
3. Should HTR live in upstream hermes-agent fork vs local branch vs separate package?
4. How to coexist with Hermes SQLite stores under ADR-007?
5. Is ebay_swarm overseer in scope for generic HEAL or domain plugin only?

### Stop conditions encountered

| Condition | Triggered? | Notes |
|-----------|------------|-------|
| Need modify production Runtime | No (stopped at docs) | Phase 1 will require Runtime hooks |
| Need change baseline | No | Conflicts documented, not silently changed |
| Cannot find delegate_task / tool runtime | **No** — found | |
| Cannot find DECO / HEAL | **Partial** — real generic modules **not found** | Documented as blockers |
| Architecture conflicts | **Yes** | C-01..C-08 documented |
| Test framework unconfirmed | **No** — pytest confirmed | Not executed in Task 0 |

### Cursor self-assessment

Task 0 completed within constraints. Recon based on read-only inspection of WSL paths. DECO/HEAL generic integration points not found — correctly flagged rather than invented.

**Awaiting:** GPT-5.6-Sol review → Task 1 scope + allowed file list.

---

## Task 4 — Manual Verification Record API (2026-07-18)

**Implementer:** Cursor  
**Status:** ✅ Complete — awaiting Architect acceptance  
**Tests:** 161 passed (`python3 -m pytest tests/htr/ -v`)

### Delivered

- `verification_result` schema with `passed|failed|heal_required` outcomes and check entries
- `make_verification_result()` with None-only defaults (`summary`, `checks`, `metadata`)
- `verification_fingerprint()` — stable JSON with `sort_keys=True`, `separators=(",", ":")`
- `submit_manual_verification()` — `result_submitted → verification_passed|verification_failed|heal_required`
- `manual_verification_submitted` event + replay-only path for terminal verification states
- Minimal state transition update: `result_submitted → heal_required` for manual shortcut outcome

### Non-goals confirmed

- No verification execution, HEAL execution, Runtime/delegate_task integration
- No task_status updates, no task completed, no new attempts
- No SQLite, scheduler, or event replay from log

### Note

`htr/state.py` updated (one transition) — required for `heal_required` outcome from `result_submitted`; outside nominal allowed-file list but necessary for acceptance tests.

**Awaiting:** Architect acceptance before Task 6.

---

## Task 5 — Manual Task Completion API (2026-07-18)

**Implementer:** Cursor  
**Status:** ✅ Complete — awaiting Architect acceptance  
**Tests:** 194 passed (`python3 -m pytest tests/htr/ -v`)

### Delivered

- `task_completion_record` schema + `make_task_completion_record()` with None-only defaults
- `task_completion_fingerprint()` — stable canonical JSON
- `complete_task_manually()` — requires `verification_passed`, updates `task_status` only
- `manual_task_completed` event + replay-only path for completed tasks
- `_find_task_event_by_id` scoped to task_id for replay lookup

### Non-goals confirmed

- No task execution, verification runner, HEAL, Runtime/delegate_task
- No attempt_status / run_status updates
- No SQLite, scheduler, event replay from log

**Awaiting:** Architect acceptance before Task 6.

---

## Task 6 — Manual Run Completion API (2026-07-18)

**Implementer:** Cursor  
**Status:** ✅ Complete — awaiting Architect acceptance  
**Tests:** 228 passed (`python3 -m pytest tests/htr/ -v`)

### Delivered

- `run_completion_record` schema + `make_run_completion_record()` with None-only defaults
- `run_completion_fingerprint()` — stable canonical JSON
- `complete_run_manually()` — requires every listed task already `completed`, updates `run_manifest` only
- `manual_run_completed` event + replay-only path for completed runs
- Run-level event helpers: `make_run_event`, `append_run_event`, `_find_run_event_by_id`
- Run status constants + `assert_valid_run_transition()` in `state.py`
- Event schema: `task_id` optional (run-level events omit it)

### Non-goals confirmed

- No task execution, verification runner, HEAL execution, Runtime/delegate_task
- No task_status / attempt_status updates
- No automatic task discovery or completion
- No SQLite, scheduler, event replay from log

**Awaiting:** Architect acceptance before Task 7.

---

## Task 7 — Manual Run Review API (2026-07-18)

**Implementer:** Cursor  
**Status:** ✅ Complete — awaiting Architect acceptance  
**Tests:** 265 passed (`python3 -m pytest tests/htr/ -v`)

### Delivered

- `run_review_record` schema + `make_run_review_record()` with None-only defaults
- `run_review_fingerprint()` — stable canonical JSON
- `review_run_manually()` — requires completed run + existing `run_completion_record.json`
- `manual_run_reviewed` event + replay-only when review record exists
- Decision constants: `accepted`, `rejected`, `needs_followup`
- Does not update `run_manifest`, `task_status`, or `attempt_status`

### Non-goals confirmed

- No task execution, verification runner, HEAL execution, Runtime/delegate_task
- No artifact/result/verification content inspection
- No automatic task discovery or completion
- No SQLite, scheduler, event replay from log

**Awaiting:** Architect acceptance before Task 8.

---

## Task 8 — Review-Gated Follow-up Planning API (2026-07-18)

**Implementer:** Cursor  
**Status:** ✅ Complete — awaiting Architect acceptance  
**Tests:** 331 passed (`python3 -m pytest tests/htr/ -v`)

### Delivered

- `run_followup_plan_record` schema + `make_run_followup_plan_record()`
- `run_followup_plan_fingerprint()` — stable canonical JSON
- `plan_run_followup()` — review-gated planning after completion + review records exist
- `manual_run_followup_planned` event + replay-only when follow-up plan record exists
- Plan status constants: `open`, `cancelled`
- `planner` may be human, assistant, tool, or mixed process
- `followup_items` are planning notes only — not tasks

### Design principle

Automate safe bookkeeping (schema, fingerprint, idempotency, replay, audit events).
Do not automate execution, scheduling, delegation, or lifecycle mutation.

### Non-goals confirmed

- No task/attempt creation from follow-up items
- No Runtime/delegate_task/DECO/HEAL/scheduler/queue/database
- No artifact/result/verification content inspection
- No run_manifest/task_status/attempt_status updates

**Awaiting:** Architect acceptance before Task 9.

---

## Task 9 — Review-Gated Execution Request API (2026-07-18)

**Implementer:** Cursor  
**Status:** ✅ Complete — awaiting Architect acceptance  
**Tests:** full HTR suite (`python3 -m pytest tests/htr/ -v`)

### Delivered

- `run_execution_request_record` schema + `make_run_execution_request_record()`
- `run_execution_request_fingerprint()` — stable canonical JSON
- `request_run_execution()` — review-gated execution request after completion + review + follow-up plan records exist
- `run_execution_requested` event + replay-only when execution request record exists
- Request status constants: `pending`, `cancelled`
- `execution_items` are approved future actions — not performed actions
- `requester` may be human, assistant, tool, or mixed process

### Design principle

Automate safe bookkeeping (schema, fingerprint, idempotency, replay, audit events).
Execution requests prepare controlled automation; they do not execute work.

### Non-goals confirmed

- No actual execution, Runtime/delegate_task/DECO/HEAL/scheduler/queue/database
- No task/attempt creation from execution items
- No artifact/result/verification content inspection
- No run_manifest/task_status/attempt_status updates
- Task 10 not started

**Awaiting:** Architect acceptance before Task 10.

---

## Task 10 — Controlled One-Shot Execution Adapter (2026-07-18)

**Implementer:** Cursor  
**Status:** ✅ Complete — awaiting Architect acceptance  
**Tests:** 488 passed (`python3 -m pytest tests/htr/ -v`)

### Delivered

- `run_execution_result_record` schema + `make_run_execution_result_record()`
- `run_execution_result_fingerprint()` — stable canonical JSON
- `process_execution_items()` — controlled per-item processing without external side effects
- `execute_run_execution_request()` — one-shot adapter after full review chain + pending execution request
- `run_execution_completed` event + replay-only when result record exists
- Result status constants: `completed`, `partial`, `failed`
- Item status constants: `completed`, `skipped`, `failed`, `unsupported`

### Execution behavior

- Manually triggered only; no scheduler, queue, or daemon
- Loads approved `run_execution_request_record.json` from disk
- `command` dict is data, not executable instructions
- No Runtime/delegate_task/subprocess/HTTP/browser/docs mutation

### Non-goals confirmed

- No task/attempt creation or lifecycle mutation
- No artifact/result/verification content inspection
- No HEAL/DECO/scheduler/queue/database integration
- Task 11 not started

**Awaiting:** Architect acceptance before Task 11.
