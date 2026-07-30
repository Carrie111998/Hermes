---
title: "Implement Trajectory Quality Routing"
status: ready
date: 2026-07-29
type: feature
target_repo: hermes-agent
spec: docs/superpowers/specs/2026-07-29-trajectory-quality-routing-design.md
branch_for_impl: cf/trajectory-quality-routing
tdd: strict RED→GREEN→REFACTOR per slice
---

# Implementation Plan — Trajectory Quality Routing

## Summary

Implement the approved design in
`docs/superpowers/specs/2026-07-29-trajectory-quality-routing-design.md` as an
opt-in, disabled-by-default Hermes-native quality router. Pure reducer/policy
first; persistence second; thin runtime wiring third; docs last. **No auto
`switch_model`.** No prompt/toolset mutation. No production shortcuts that skip
RED→GREEN evidence.

**Depends on:** this plan + design committed on shaping branch
`cf/trajectory-quality-routing-spec`. Implementation branch should
fetch/cherry-pick that commit so authorship of the specs is preserved.

## Requirements traceability

| ID | Requirement | Primary slices |
|---|---|---|
| R1 | Consume authoritative structured tool/result events | S3, S6 |
| R2 | Two-identical-failure circuit breaker | S1, S6 |
| R3 | Failed verification streak + stagnation (no raw-output heuristics) | S1, S6 |
| R4 | One-way ladder + hysteresis | S2 |
| R5 | Durable redacted decision records under profile home | S4, S5 |
| R6 | Recommendation-only model escalation; optional soft-stop | S7, S8 |
| R7 | Disabled default; config.yaml only; no behavior change when off | S5, S9 |
| R8 | Preserve transport fallback + tool_loop_guardrails | S9 |
| R9 | No new model tool / vendor / telemetry / raw output persistence | all |
| R10 | Tests via `scripts/run_tests.sh` | every slice |

## Out of scope (do not implement)

- Auto `switch_model` / consuming `_fallback_chain` for quality reasons
- LLM judge, proxy, SageRoute, dashboard UI
- SessionDB schema migration
- Changing `smart_model_routing` stub
- Parent←child quality merge
- Speculative plugin hooks

## Pre-flight (every function edit)

1. Re-read the design §11 seams.
2. If GBrain MCP is configured: `code_blast` / `code_callers` on the symbol.
   Otherwise: `rg -n "symbol" -g '*.py'` and list callers in the commit message
   notes.
3. Touch only listed files per slice.

## Target files (expected)

| Path | Role |
|---|---|
| `agent/trajectory_quality.py` | **New** — config, events, controller, policy |
| `agent/trajectory_quality_store.py` | **New** — SQLite decision store |
| `hermes_cli/config.py` | `DEFAULT_CONFIG["trajectory_quality_routing"]` |
| `agent/agent_init.py` | Construct controller + store |
| `agent/tool_executor.py` | Observe after guardrail observation (seq + parallel) |
| `run_agent.py` | Thin `_observe_trajectory_quality`, recommendation emit, stop flag |
| `agent/turn_context.py` | `reset_for_turn` |
| `website/docs/user-guide/configuration.md` | Operator docs (near tool_loop_guardrails) |
| `tests/agent/test_trajectory_quality.py` | **New** pure tests |
| `tests/agent/test_trajectory_quality_store.py` | **New** store + HERMES_HOME |
| `tests/run_agent/test_trajectory_quality_runtime.py` | **New** runtime integration |

Optional read-only reuse (import, do not fork logic):

- `agent/tool_guardrails.py` — `ToolCallSignature`, `_result_hash` (export if needed),
  `canonical_tool_args`
- `agent/tool_result_classification.py` — `file_mutation_result_landed`
- `agent/display.py` — `_detect_tool_failure` (already called upstream)
- `agent/verification_evidence.py` — read helpers for verify status if a clean
  public function exists; otherwise pass verification fields from the executor
  only when already known

## Architecture reminder

```text
tool_executor (failed, args, result)
    → run_agent._observe_trajectory_quality
        → TrajectoryQualityController.observe(event)
            → optional Decision
                → store.persist(decision)   # if enabled
                → status recommendation     # if action != continue
                → set stop flag             # if stop && execute_stop
```

When `enabled: false`, `_observe_trajectory_quality` returns immediately.

---

## Slice S0 — Scaffold test module (RED infrastructure)

**Goal:** Create empty test modules that import the future public API so later
slices have a home.

**Files:**

- `tests/agent/test_trajectory_quality.py` (start with one skipped or failing import test)
- Do **not** create production module yet if you prefer classic TDD — first test
  fails on `ImportError`.

**Test:**

```python
def test_public_api_importable():
    from agent.trajectory_quality import (
        TrajectoryQualityConfig,
        TrajectoryQualityController,
        TrajectoryObservation,
    )
    assert TrajectoryQualityConfig().enabled is False
```

**RED:** ImportError  
**GREEN:** minimal stubs in `agent/trajectory_quality.py` with defaults only  
**Verify:**

```bash
scripts/run_tests.sh tests/agent/test_trajectory_quality.py::test_public_api_importable -q
```

---

## Slice S1 — Reducer: identical failure + counters (pure)

**Goal:** `TrajectoryQualityController` counts identical failures and emits a
decision at threshold 2.

**Files:** `agent/trajectory_quality.py`, `tests/agent/test_trajectory_quality.py`

**API sketch:**

```python
@dataclass(frozen=True)
class TrajectoryQualityConfig:
    enabled: bool = False
    execute_stop: bool = True
    execute_model_switch: bool = False  # must be ignored by runtime
    allow_deescalate_on_progress: bool = False
    persist_decisions: bool = True
    retention_days: int = 30
    max_decisions_per_session: int = 200
    identical_failure: int = 2
    same_tool_failure: int = 4
    failed_verification: int = 2
    stagnation_window: int = 8
    hysteresis_progress_needed: int = 2
    stronger_provider: str | None = None
    stronger_model: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> TrajectoryQualityConfig: ...

@dataclass(frozen=True)
class TrajectoryObservation:
    tool_name: str
    args_hash: str
    result_hash: str | None
    failed: bool
    progress_kind: str = "none"
    verification_status: str | None = None
    api_call_count: int = 0
    session_id: str = ""
    model: str = ""
    provider: str = ""

@dataclass(frozen=True)
class TrajectoryQualityDecision:
    action: str
    reason_code: str
    level_before: str
    level_after: str
    tool_name: str
    args_hash: str
    result_hash: str | None
    count: int
    explain: str
    # ... plus ids filled by store/runtime
```

**Behavior:**

- `observe()` no-ops decisions when `not config.enabled` (still may no-op entirely).
- On `failed=True`, increment exact `(tool_name, args_hash)` and `tool_name` counts.
- When exact count reaches `identical_failure`, escalate toward
  `recommend_stronger_model` with `reason_code=two_identical_failures`.
- On success with `progress_kind in {file_mutation_landed, verification_passed}`,
  clear that signature’s failure count; do **not** de-escalate level unless
  `allow_deescalate_on_progress`.
- `reset_for_turn()` clears counts and level.

**Tests (each RED then GREEN):**

1. Two identical failures → decision action `recommend_stronger_model`,
   reason `two_identical_failures`, count==2.
2. Two failures with different `args_hash` do **not** trip identical breaker
   (may still count same_tool separately).
3. Success after one failure clears exact counter (third failure later needs 2 again).
4. Disabled config → `observe` returns `None` always.

**Verify:**

```bash
scripts/run_tests.sh tests/agent/test_trajectory_quality.py -q
```

---

## Slice S2 — Policy ladder + hysteresis

**Goal:** Monotonic escalation and duplicate suppression.

**Files:** same pure module + tests

**Behavior:**

- Levels ordered: `continue < recommend_stronger_model < recommend_clean_restart < stop`.
- `same_tool_failure` streak → at least level 1.
- `failed_verification` streak (observations with
  `verification_status == "failed"` or `progress_kind == "verification_failed"`)
  → at least level 1.
- `stagnation_window` without progress kinds, after any failure/verification_failed
  in the turn → at least level 2.
- Compounding: if already level ≥1 and a **new** reason fires → level 2;
  if already level 2 and any trigger → level 3 `stop`.
- Suppress duplicate decision when `(action, reason_code, tool_name, args_hash)`
  unchanged since last emitted decision this turn.
- `allow_deescalate_on_progress=False`: progress never lowers level.
- Optional test with `allow_deescalate_on_progress=True` +
  `hysteresis_progress_needed=2` lowers one step after 2 progress events (keeps
  flag honest).

**Verify:** same test file.

---

## Slice S3 — Event builder helpers (pure)

**Goal:** Build `TrajectoryObservation` from tool name/args/result/failed without
storing raw content.

**Files:** `agent/trajectory_quality.py` (helpers), tests

**Helpers:**

```python
def build_observation(
    *,
    tool_name: str,
    args: Mapping[str, Any] | None,
    result: str | None,
    failed: bool,
    **meta,
) -> TrajectoryObservation:
    # args_hash via ToolCallSignature.from_call
    # result_hash via tool_guardrails._result_hash (export _result_hash if private)
    # progress_kind:
    #   file_mutation_landed if file_mutation_result_landed(...)
    #   verification_failed / verification_passed if verification_status passed in
    #   idempotent_repeat only if caller supplies prior hash match — keep simple:
    #   default none/other_success/file_mutation_landed
```

**Tests:**

1. Args hash stable under key reordering; secret values not present in
   `Observation` `repr`/asdict strings except inside irreversible hash.
2. `file_mutation_result_landed` sets progress_kind.
3. Failed terminal result sets failed True when caller passes failed=True
   (builder does not re-detect — trust caller).

**Note:** Prefer exporting a public `result_content_hash(result: str | None) -> str`
from `tool_guardrails` rather than importing `_result_hash` if reviewers object to
private imports. Smallest change: add alias in tool_guardrails `__all__` only if
needed.

---

## Slice S4 — Decision store (temp HERMES_HOME)

**Goal:** Persist redacted decisions.

**Files:**

- `agent/trajectory_quality_store.py`
- `tests/agent/test_trajectory_quality_store.py`

**API:**

```python
class TrajectoryQualityStore:
    def __init__(self, path: Path | None = None): ...
    def record(self, decision: TrajectoryQualityDecision) -> str:  # returns id
    def list_for_session(self, session_id: str, *, limit: int = 50) -> list[dict]:
    def purge_expired(self) -> int: ...
```

Default path: `get_hermes_home() / "trajectory_quality.db"`.

**Tests:**

1. Write + read roundtrip under `monkeypatch.setenv("HERMES_HOME", tmp)`.
2. Row JSON/dict contains no raw substring from a planted secret in args
   (only hashes).
3. Retention purge removes old `created_at`.
4. Session cap trims oldest.

**Verify:**

```bash
scripts/run_tests.sh tests/agent/test_trajectory_quality_store.py -q
```

---

## Slice S5 — Config wiring

**Goal:** `DEFAULT_CONFIG` + `from_mapping` parity; agent_init constructs when
loading config.

**Files:**

- `hermes_cli/config.py` — add full default tree under
  `trajectory_quality_routing`
- `agent/agent_init.py` — next to tool_guardrails block (~1560)
- tests: extend pure config tests; optional
  `tests/hermes_cli/test_config_trajectory_quality.py` if config load tests are
  preferred separate

**Init behavior:**

```python
agent._trajectory_quality = TrajectoryQualityController(
    TrajectoryQualityConfig.from_mapping(
        _agent_cfg.get("trajectory_quality_routing", {})
    )
)
agent._trajectory_quality_store = (
    TrajectoryQualityStore()
    if agent._trajectory_quality.config.enabled
    and agent._trajectory_quality.config.persist_decisions
    else None
)
```

Fail-open: exceptions constructing store log warning and set store None.

**Tests:**

1. Default config has `enabled is False`.
2. Nested thresholds parse.
3. Agent with empty config has controller disabled (runtime test can cover).

**Config version:** do **not** bump `_config_version` (new keys only).

---

## Slice S6 — Runtime observe seam

**Goal:** After every real tool result observation, feed the controller.

**Files:**

- `run_agent.py` — add:

```python
def _observe_trajectory_quality(self, tool_name, function_args, function_result, *, failed: bool) -> None:
    ctrl = getattr(self, "_trajectory_quality", None)
    if ctrl is None or not ctrl.config.enabled:
        return
    ...
```

- `agent/tool_executor.py` — call immediately after
  `_append_guardrail_observation` in **both** parallel completion path (~916)
  and sequential path (~1621). Skip when `blocked` (tool never ran) — same as
  guardrail after_call skip.

**Progress enrichment:**

- If `file_mutation_result_landed(tool_name, result)`: progress_kind set.
- Optional: if tool_name == `"terminal"` and verification evidence API can return
  last status cheaply, pass `verification_status`. If that requires heavy DB work,
  defer verification streak to a narrow helper already used by verification_stop
  snapshot — keep fail-open and never block tool completion on store errors.

**On decision with action != continue:**

1. Persist via store if present (swallow errors).
2. Set `agent._pending_trajectory_quality_recommendation = decision`.
3. Emit status once via existing status helper (mirror fallback notice style).
4. If action == `stop` and `execute_stop`: set
   `agent._trajectory_quality_halt_decision = decision` (new attribute).

**Hard rule:** never call `switch_model` or `try_activate_fallback` here.
If `execute_model_switch` is True, log debug “unsupported; recommendation only”.

**Tests** (`tests/run_agent/test_trajectory_quality_runtime.py`), patterned on
`test_tool_call_guardrail_runtime.py`:

1. Disabled: after two identical failures, no recommendation attr, no DB file.
2. Enabled: two identical failures → recommendation action
   `recommend_stronger_model`; messages list has no new system/user rows from
   router; tool result text does **not** gain quality-routing suffix
   (guardrail warn suffix may still exist — assert quality-specific marker absent).
3. `switch_model` not called (patch and assert).

**Verify:**

```bash
scripts/run_tests.sh tests/run_agent/test_trajectory_quality_runtime.py -q
```

---

## Slice S7 — Recommendation surfacing

**Goal:** User-visible notice without transcript mutation.

**Files:** `run_agent.py` (emit helper), runtime tests

**Behavior:**

- When a new decision is produced, call the same family of user status APIs used
  for fallback notices (`_emit_status` / quiet print). Message example:

  `Trajectory quality: recommend stronger model (two_identical_failures on terminal×2). Try /model <stronger> or start a clean session.`

- Include configured `stronger_model` if set.
- Gateway: do not inject assistant messages; status/log is enough in slice 1
  unless an existing non-transcript notice path is one line away.

**Tests:** capture status callback / mock `_emit_status` called once per unique
decision; not called again on suppressed duplicates.

---

## Slice S8 — Soft-stop on ladder `stop`

**Goal:** When level reaches `stop` and `execute_stop`, halt further thrash like
guardrail halt.

**Files:**

- `run_agent.py` — halt flag + response text helper
- `agent/conversation_loop.py` **only if** halt is checked solely via
  `_tool_guardrail_halt_decision` — extend the check to OR quality halt.
  Prefer a small helper `agent._should_halt_tools()` to avoid scattering.

**Behavior:**

- Soft-stop means: stop scheduling more tool iterations / break loop with a
  controlled final response explaining quality stop (similar to
  `_toolguard_controlled_halt_response`).
- Must not leave dangling tool_calls without results (reuse existing halt paths).

**Tests:**

1. Force controller to stop level → conversation ends without infinite tools.
2. `execute_stop: false` → recommendation only, loop can continue (guardrails may
   still block independently).

**Caller blast (required before edit):**

```bash
rg -n "_tool_guardrail_halt_decision|should_halt" agent/ run_agent.py
```

---

## Slice S9 — Turn reset + disabled parity + neighbor regressions

**Goal:** Reset with guardrails; prove zero impact when disabled.

**Files:** `agent/turn_context.py` (reset), runtime tests

**Tests:**

1. After `reset_for_turn`, identical failure counter starts fresh.
2. Full agent with default config: enable spy on controller.observe — either not
   called or early-returns; **no** `trajectory_quality.db` created under temp home.
3. Re-run neighbor suites green:

```bash
scripts/run_tests.sh \
  tests/agent/test_tool_guardrails.py \
  tests/run_agent/test_tool_call_guardrail_runtime.py \
  tests/agent/test_verification_evidence.py \
  tests/agent/test_trajectory_quality.py \
  tests/agent/test_trajectory_quality_store.py \
  tests/run_agent/test_trajectory_quality_runtime.py -q
```

---

## Slice S10 — Operator docs

**Goal:** Document config next to Tool-Loop Guardrails.

**Files:** `website/docs/user-guide/configuration.md`

**Content:** short section — purpose, default off, YAML block, relationship to
tool_loop_guardrails and fallback_providers, recommendation-only note, privacy
(hashes only).

No `_config_version` migration notes needed.

---

## Implementation constraints checklist

- Extend existing infrastructure; do not reimplement failure detection.
- Do not append quality text into tool results (that is guardrails’ job).
- Do not mutate system prompt or toolsets.
- Do not advance fallback index.
- Do not write secrets or raw outputs to DB.
- Use `get_hermes_home()` only for store path.
- Keep `run_agent.py` changes thin (forwarders already dominate that file).

## Commit strategy (implementation branch)

Prefer small commits per slice:

1. `test(agent): trajectory quality reducer RED/GREEN`
2. `feat(agent): trajectory quality policy ladder`
3. `feat(agent): trajectory quality decision store`
4. `feat(agent): wire trajectory quality observe seam`
5. `feat(agent): trajectory quality stop + status`
6. `docs: trajectory quality routing config`

Or fewer commits if the implementer batches GREEN slices carefully — but each
slice must have been RED then GREEN locally.

## Final verification (definition of done)

```bash
scripts/run_tests.sh \
  tests/agent/test_trajectory_quality.py \
  tests/agent/test_trajectory_quality_store.py \
  tests/run_agent/test_trajectory_quality_runtime.py \
  tests/agent/test_tool_guardrails.py \
  tests/run_agent/test_tool_call_guardrail_runtime.py \
  tests/agent/test_verification_evidence.py -q
```

Manual review of diff:

- [ ] No `switch_model(` calls from quality code paths
- [ ] No `try_activate_fallback` for quality reasons
- [ ] No raw result columns in store schema
- [ ] Default enabled false
- [ ] No new HERMES_* behavioral env vars
- [ ] No prompt/toolset mutation

## Proposed PR title / body (for implementer; do not open PR in shaping)

**Title:** `feat(agent): opt-in trajectory quality routing (recommend-only)`

**Body sketch:**

```markdown
## Summary
Adds disabled-by-default deterministic trajectory quality routing:
identical-failure / verification / stagnation signals → one-way
recommendations (stronger model, clean restart, stop). Decision audit DB
under HERMES_HOME. No auto model switch; no prompt mutation.

## Spec
- docs/superpowers/specs/2026-07-29-trajectory-quality-routing-design.md
- docs/superpowers/plans/2026-07-29-trajectory-quality-routing-plan.md

## Test plan
- [ ] scripts/run_tests.sh <focused modules above>
- [ ] enabled=false: no DB, no status
- [ ] enabled=true: two identical failures → recommendation
```

## Residual risks

| Risk | Mitigation |
|---|---|
| Double UX noise with tool_loop warnings | Quality uses status channel only; different wording |
| Verification streak expensive if naïvely queried | Pass status only when cheap; fail-open |
| Halt interaction with guardrail halt | Single combined halt check helper |
| Future execute_model_switch footgun | Hard no-op + log in slice 1; separate design later |
| GBrain unavailable during shaping | Implementer re-blasts callers on touch points |

## Handoff fields for implementation completion

Implementation task must return:

- `repository`, `branch`, `base`, full 40-char `head_sha`
- concrete check commands + outputs
- `durable_task_id`
- residual risks
- proposed PR title/body
- **Do not** open the PR via GitHub API (per implementer card)
