---
title: "Trajectory Quality Routing"
status: approved-design
date: 2026-07-29
type: feature
target_repo: hermes-agent
origin: SageRoute due diligence — non-duplicative Hermes gap
related:
  - agent/tool_guardrails.py
  - agent/tool_result_classification.py
  - agent/verification_evidence.py
  - agent/verification_stop.py
  - agent/chat_completion_helpers.py (try_activate_fallback)
  - agent/agent_runtime_helpers.py (switch_model)
  - hermes_cli/config.py (tool_loop_guardrails, fallback_providers)
---

# Trajectory Quality Routing — Design

## 1. End goal and user-visible outcome

Hermes already recovers from **transport/provider** failures (`fallback_providers` /
`try_activate_fallback`) and already **nudges or blocks repeated tool loops**
(`tool_loop_guardrails`). It does **not** yet route on **trajectory quality**:
repeated authoritative tool failures that indicate the current model is thrashing,
failed verification streaks after edits, or a long stretch of tool calls with no
verified progress.

This design adds an **opt-in, Hermes-native, deterministic reducer + policy** that:

1. Consumes **authoritative structured tool/result events** already produced by the
   agent loop (failure flags, signatures, verification ledger outcomes).
2. Escalates one-way through:
   `continue` → `recommend_stronger_model` → `recommend_clean_restart` → `stop`.
3. Emits **durable, redacted, explainable decision records** under the active
   profile home.
4. Leaves behavior **byte-identical when disabled** (default).
5. Does **not** mutate prompts, toolsets, or conversation history, and does **not**
   call external classifiers or proxies.

**User-visible outcome when enabled:** after thrash signals, the CLI/gateway/TUI
surfaces a clear recommendation (and, at the final ladder step, can soft-stop the
turn). The first implementation slice is **recommendation-only** for model changes;
it does not auto-call `switch_model`.

## 2. Problem frame (what exists vs the gap)

| Existing system | What it solves | What it does **not** do |
|---|---|---|
| `fallback_providers` / `try_activate_fallback` (`agent/chat_completion_helpers.py`) | API/auth/rate-limit/upstream failures; swaps backend mid-retry | Quality of the *agent trajectory* while tools still return |
| `tool_loop_guardrails` (`agent/tool_guardrails.py`) | Per-turn repeated identical failures, same-tool failures, idempotent no-progress; warn into tool result; optional hard block | Recommend a stronger model or clean session restart; durable routing decisions |
| `agent/display._detect_tool_failure` + `agent/tool_result_classification.py` | Authoritative success/failure classification for display and guardrails | Aggregate turn-level quality / routing |
| `agent/verification_evidence.py` | Passive ledger of proof commands (`status=passed/failed/...`) under `get_hermes_home()` | Escalate model or restart on failed-verification streaks |
| `agent/verification_stop.py` | Bounded “verify before finish” nudge after code edits | Mid-trajectory thrash routing |
| `IterationBudget` | Cap API/tool iterations | Quality-aware early exit reasons |
| `smart_model_routing` config key | Setup wizard writes `enabled: false`; **no runtime consumer today** | Not a substitute; leave untouched |
| ShareGPT `agent/trajectory.py` save path | Optional training JSONL dump to CWD | Not profile-aware decision storage; not live routing |

**Gap (SageRoute diligence):** detect repeated authoritative tool failures,
rewrite/retest thrash, and lack of verified progress, then apply a one-way
hysteretic routing policy — **without** a proxy and **without** an external
classifier.

## 3. Goals

- G1. Deterministic, pure reducer/policy over structured events (unit-testable
  without LLM, network, or agent runtime).
- G2. Two-identical-failure circuit breaker aligned with existing signature
  hashing (`ToolCallSignature` / `args_hash` + failure class).
- G3. One-way ladder: `continue` → `recommend_stronger_model` →
  `recommend_clean_restart` → `stop`, with hysteresis so levels do not chatter.
- G4. Durable explainable decision records; profile-aware path via
  `get_hermes_home()`; no raw tool args/output persisted.
- G5. Disabled by default; config.yaml only (no new `HERMES_*` behavioral env vars).
- G6. Preserve current transport fallback and tool-loop guardrail behavior
  unchanged when this feature is off **and** when it is on (orthogonal).
- G7. No prompt/toolset/history mutation; no new model tool; no vendor SDK;
  no outbound telemetry.
- G8. First ship slice is **recommendation-only** for model escalation (see §8).

## 4. Non-goals / deferred scope

- **N1.** SageRoute, HTTP proxy, or any third-party routing product in-tree.
- **N2.** LLM-as-judge / aux-model quality classifier mid-loop.
- **N3.** Auto `switch_model` / auto fallback activation driven by quality signals
  in slice 1 (see §8 seam analysis). Config flag reserved for a later slice.
- **N4.** Replacing or folding into `tool_loop_guardrails` (different product job:
  tool block vs trajectory routing). They share primitives; they stay separate
  config sections.
- **N5.** Changing `smart_model_routing` (unrelated stub / setup key).
- **N6.** New core tools, plugins that patch core, or dashboard UI in slice 1
  (decision DB is readable later by insights/dashboard if desired).
- **N7.** Persisting raw tool output, args, stdout, or secrets.
- **N8.** Cross-session learning that changes default model selection globally.
- **N9.** Subagent-orchestrator policy beyond “each agent instance has its own
  reducer state” (parent does not merge child quality into parent ladder in v1).
- **N10.** Production code in the shaping task (this document + plan only).

## 5. Alternatives considered

### A. External proxy / SageRoute-class product

- **Pros:** Off-the-shelf routing UX.
- **Cons:** Vendor coupling, privacy (tool streams leave the host), latency,
  contradicts AGENTS.md “no third-party product plugins in core,” and the
  diligence finding that Hermes only needs a **native** gap fill.
- **Decision:** Rejected.

### B. Mid-loop LLM quality judge (aux client)

- **Pros:** Flexible language about “thrash.”
- **Cons:** Non-deterministic, burns tokens every N steps, risks prompt-cache
  pressure if anything is injected into the main transcript, hard to test.
- **Decision:** Rejected for v1 (and likely forever as the primary signal).

### C. Only extend `tool_loop_guardrails` thresholds

- **Pros:** Minimal new surface.
- **Cons:** Guardrails intentionally act on **tool execution** (warn/block/halt
  calls). Routing wants **session/turn advice** (stronger model, clean restart)
  and durable decision audit. Mixing ladders confuses operators and tests.
- **Decision:** Rejected as sole solution. **Reuse** signatures, hashes, and
  failure flags from the guardrail path.

### D. Plugin-only implementation

- **Pros:** Zero core footprint.
- **Cons:** Needs reliable structured events; `post_tool_call` is observational
  and easy to miss sequential/parallel paths; quality routing is a core loop
  concern like guardrails. Plugin remains a valid *consumer* of decision records
  later, not the primary home.
- **Decision:** Core opt-in module (same tier as `tool_loop_guardrails`).

### E. Auto-execute model switch on recommendation (v1)

- **Pros:** Hands-free recovery.
- **Cons:** See §8 — `switch_model` rebuilds system prompt and client; mid-turn
  provider format shifts are high risk; product wants user-visible recommend first.
- **Decision:** Deferred behind `execute_model_switch: false` (default). Slice 1
  never calls `switch_model` or `try_activate_fallback` for quality reasons.

**Chosen approach:** Hermes-native pure reducer + one-way policy + redacted
decision log + recommendation surface, wired at the existing tool-result
observation seam.

## 6. Architecture

```
                    ┌─────────────────────────────────────┐
                    │  tool_executor (seq + parallel)     │
                    │  _detect_tool_failure → failed bool │
                    │  _append_guardrail_observation      │
                    └──────────────┬──────────────────────┘
                                   │ structured observation
                                   ▼
                    ┌─────────────────────────────────────┐
                    │ TrajectoryQualityController         │
                    │  (pure; agent/trajectory_quality.py)│
                    │  reduce(event) → optional Decision  │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
     DecisionRecordStore   RecommendationSink      Optional soft-stop
     (profile SQLite)      (status / UI only)      (stop level only)
```

### 6.1 Module boundaries

| Piece | Location | Responsibility |
|---|---|---|
| Config dataclass + parser | `agent/trajectory_quality.py` | `TrajectoryQualityConfig.from_mapping` |
| Event types | same | Frozen dataclasses; no raw content fields |
| Reducer + policy | same | `TrajectoryQualityController.observe` / `.snapshot` |
| Persistence | `agent/trajectory_quality_store.py` | Profile-aware SQLite under `get_hermes_home()` |
| Runtime thin adapter | `run_agent.py` methods + call from `agent/tool_executor.py` | Build events from existing failed/signature data; emit status; optional halt |
| Init | `agent/agent_init.py` | Construct controller from config when present |
| Turn reset | `agent/turn_context.py` | Reset per-turn counters with guardrails |
| DEFAULT_CONFIG | `hermes_cli/config.py` | `trajectory_quality_routing` section, `enabled: false` |
| Docs | `website/docs/user-guide/configuration.md` | Operator section (implementation slice) |

**Do not** grow `run_agent.py` with policy logic — mirror the
`ToolCallGuardrailController` extraction pattern.

### 6.2 Relationship to tool-loop guardrails

- Guardrails remain the **tool execution** circuit breaker (warn into result /
  block call).
- Quality routing is a **parallel observer** of the same `failed` + signature
  inputs (and verification ledger summaries).
- When both are enabled, a repeated identical failure may **both** warn in the
  tool result (guardrails) **and** escalate the routing ladder (quality). That
  is intentional and not double-mutation of prompts: guardrail warnings already
  append to tool results today; quality routing must **not** append to tool
  results or system prompt.

### 6.3 Cache, alternation, and fallback invariants

- Quality routing **must not** rewrite `messages`, system prompt, tool schemas,
  or toolsets.
- Quality routing **must not** call `try_activate_fallback` (transport fallback
  stays owned by API error paths).
- Quality routing **must not** inject synthetic user/assistant messages.
- Recommendation text goes only through existing **status / notice** channels
  (`_emit_status`, tool-progress callbacks, gateway status), never into the
  model transcript.
- Soft-stop at ladder `stop` may reuse the guardrail halt pattern
  (`_set_tool_guardrail_halt` / controlled halt response) **or** a sibling
  `_trajectory_quality_halt_decision` flag checked at the same loop points —
  without inserting extra user-role turns.

## 7. Data model

### 7.1 Observation event (in-memory only)

```text
TrajectoryObservation:
  tool_name: str
  args_hash: str          # sha256 of canonical args (ToolCallSignature)
  result_hash: str | None # sha256 of canonicalized structured result; never raw
  failed: bool            # from _detect_tool_failure / executor is_error
  progress_kind: str      # none | file_mutation_landed | verification_passed
                          # | verification_failed | idempotent_repeat | other_success
  verification_status: str | None  # from verification_evidence when applicable
  api_call_count: int
  session_id: str
  model: str
  provider: str
  ts_mono: float
```

**Authoritative failure:** the same boolean the executor already computes via
`agent.display._detect_tool_failure` (and passes as `failed=` into
`_append_guardrail_observation`). Do not re-parse free text with new heuristics
in the reducer. Optional structured enrichments:

- `file_mutation_result_landed` → `progress_kind=file_mutation_landed`
- `verification_evidence` last status for session/root when tool is `terminal`
  and classified as a verify command → `verification_passed` / `verification_failed`
- Idempotent same `result_hash` while `failed=False` → `idempotent_repeat`
  (stagnation signal, not failure)

**Explicitly forbidden in events and records:** raw args, raw result strings,
stdout, env, paths beyond optional **hashed** path digests if needed later.

### 7.2 Reducer state (per agent instance, per turn)

```text
TrajectoryQualityState:
  level: enum continue | recommend_stronger_model | recommend_clean_restart | stop
  exact_failure_counts: map[(tool_name, args_hash)] -> int
  same_tool_failure_counts: map[tool_name] -> int
  consecutive_no_progress: int
  failed_verification_streak: int
  last_decision_id: str | None
  last_reason_code: str | None
```

Reset on new user turn (`turn_context` path that already calls
`agent._tool_guardrails.reset_for_turn()`). Session-level persistence is for
**audit records only**, not for carrying ladder level across `/new` (fresh
session clears runtime state).

### 7.3 Decision record (durable)

```text
TrajectoryQualityDecision:
  id: str                 # ulid/uuid
  created_at: iso8601
  session_id: str
  api_call_count: int
  action: str             # continue | recommend_stronger_model | ...
  reason_code: str        # two_identical_failures | same_tool_failure_streak |
                          # failed_verification_streak | stagnation_no_progress |
                          # progress_reset (only if de-escalate enabled)
  level_before: str
  level_after: str
  tool_name: str
  args_hash: str
  result_hash: str | null
  count: int
  model: str
  provider: str
  recommended_model: str | null
  recommended_provider: str | null
  config_sha256: str      # hash of normalized config thresholds
  explain: str            # short operator-facing sentence; no secrets
```

### 7.4 Persistence

Follow the **verification_evidence** pattern (separate profile-scoped SQLite),
not the CWD JSONL trajectory dump:

- Path: `get_hermes_home() / "trajectory_quality.db"`
- Tables: `meta(schema_version)`, `decisions(...)` matching §7.3
- Indexes: `(session_id, created_at DESC)`, `(created_at)`
- Retention: delete rows older than `retention_days` (default 30) on write
  opportunistically (same spirit as verification_evidence caps)
- Cap: `max_decisions_per_session` (default 200) — drop oldest in session if exceeded
- **Never** store raw tool output columns

Rationale vs SessionDB migration: quality decisions are an agent-ops ledger like
verification evidence; avoiding `SCHEMA_VERSION` bumps on `hermes_state.py`
reduces blast radius. Session id is still stored for join-by-hand.

### 7.5 Privacy / redaction

- Args and results enter storage **only** as hex sha256 hashes (reuse
  `agent.tool_guardrails._sha256` / `ToolCallSignature` / `_result_hash`).
- `explain` strings use tool **names** and counts only
  (e.g. `terminal failed 2× with identical args_hash`).
- Apply `agent.redact.redact_sensitive_text` defensively on any string field
  before write.
- Logs at INFO may include `action`, `reason_code`, `tool_name`, `count`; never
  result previews from this subsystem (executor already logs truncated errors).

## 8. Model-switch seam analysis (why recommendation-only in slice 1)

**Existing seams:**

| Seam | File/symbol | When used | Cache impact |
|---|---|---|---|
| `AIAgent.switch_model` → `agent.agent_runtime_helpers.switch_model` | User `/model`, TUI/gateway model pickers | Rebuilds client, clears `_cached_system_prompt`, re-resolves reasoning + compressor, updates `_primary_runtime` | Intentionally invalidates cached system prompt; different model cannot reuse prior provider cache anyway |
| `try_activate_fallback` | API error retry loop | Temporary backend swap for transport failures; may rewrite model identity lines | Transport recovery path; **must stay owned by FailoverReason** |
| `update_session_model` / billing route | SessionDB after user switch | Dashboard consistency | Nulls stored system_prompt snapshot |

**Verdict for slice 1:**

- Auto-invoking `switch_model` mid-turn from quality policy is **unsafe to claim
  as “cache-preserving”**: it always clears `_cached_system_prompt` and may change
  `api_mode` / message shaping. That is acceptable for an **explicit user**
  `/model` action; it is not acceptable as a silent side effect of a new opt-in
  subsystem until a dedicated between-turns execute path is designed and tested.
- Quality routing must **not** consume or advance `_fallback_chain` /
  `_fallback_index` (would corrupt transport fallback).
- Therefore slice 1 **only recommends**. Config reserves:

```yaml
execute_model_switch: false   # slice 1 hard-ignores true if set; document as unsupported
```

A future slice may execute **between turns only** (after turn finalizer, before
next user message) by calling the same `switch_model` path the UI uses, with
explicit user config and tests for provider format continuity. That work is
**out of scope** here.

## 9. Policy specification

### 9.1 Ladder (one-way within a turn)

| Level | Action | Meaning |
|---|:---:|---|
| 0 | `continue` | No quality intervention |
| 1 | `recommend_stronger_model` | Surface: try a stronger model (from config) |
| 2 | `recommend_clean_restart` | Surface: `/new` or clean session; context thrash |
| 3 | `stop` | Soft-stop further tool thrash this turn (if `execute_stop: true`) |

Escalation is **monotonic** within a turn: level only increases, never decreases,
unless `allow_deescalate_on_progress: true` (default **false**).

### 9.2 Triggers (defaults)

| Reason code | Default threshold | Escalates to at least | Notes |
|---|---:|---|---|
| `two_identical_failures` | `identical_failure: 2` | level 1 | Same `(tool_name, args_hash)` with `failed=True` twice |
| `same_tool_failure_streak` | `same_tool_failure: 4` | level 1 | Same tool_name failed N times (any args) |
| `failed_verification_streak` | `failed_verification: 2` | level 1 | Consecutive verification_evidence failures after edits |
| `stagnation_no_progress` | `stagnation_window: 8` | level 2 | N tool observations without any `progress_kind` in `{file_mutation_landed, verification_passed}` **and** at least one prior failure or verification_failed in the turn |
| Second distinct reason while already ≥1 | — | level 2 | Hysteresis: compounding thrash → clean restart |
| Any trigger while already level 2 | — | level 3 | Stop |

Exact numeric defaults are knobs; tests lock **ordering and monotonicity**, not
magic product taste beyond the mandated **two-identical-failure** breaker.

### 9.3 Hysteresis rules

1. **No chatter:** emitting the same `(action, reason_code, tool_name, args_hash)`
   decision more than once per turn is suppressed (still update counts).
2. **Monotonic levels** when `allow_deescalate_on_progress` is false (default).
3. **Turn boundary reset** of runtime ladder state (new user turn).
4. Optional future: de-escalate one step after
   `hysteresis_progress_needed` verified progress events (off by default).

### 9.4 Recommendation payload (not in transcript)

```text
TrajectoryQualityRecommendation:
  action: recommend_stronger_model | recommend_clean_restart | stop
  reason_code: str
  explain: str
  recommended_model: str | null
  recommended_provider: str | null
  decision_id: str
```

Surfacing:

- CLI: one status line via existing buffered/status emission patterns (similar to
  fallback notices — user visible, not model visible).
- Gateway: status event / notice if a channel already supports non-transcript
  notices; otherwise log + decision DB only (do not DM the user a fake assistant
  message).
- TUI/desktop: status channel parity if a hook exists; else log + DB.

## 10. Config schema and defaults

Add to `DEFAULT_CONFIG` in `hermes_cli/config.py` (deep-merge; **no**
`_config_version` bump required — new keys only):

```yaml
trajectory_quality_routing:
  enabled: false                 # master switch; default off
  execute_stop: true             # honor ladder stop with soft-halt when enabled
  execute_model_switch: false    # unsupported in slice 1; must remain no-op
  allow_deescalate_on_progress: false
  persist_decisions: true
  retention_days: 30
  max_decisions_per_session: 200
  # Optional explicit stronger model for recommendations (not auto-applied).
  # If null, recommendation explain points the user at /model and fallback_providers.
  stronger_model:
    provider: null
    model: null
  thresholds:
    identical_failure: 2         # mandated two-identical-failure breaker
    same_tool_failure: 4
    failed_verification: 2
    stagnation_window: 8
  hysteresis_progress_needed: 2  # only if allow_deescalate_on_progress
```

Also add `"trajectory_quality_routing"` to known root keys if required by the
config validator’s extra-root allowlist pattern (mirror `tool_loop_guardrails`
which lives in `DEFAULT_CONFIG` proper).

**No new behavioral env vars.** Secrets stay in `.env`; this feature is not secret.

## 11. Integration seams (exact symbols)

| Seam | Symbol | Change |
|---|---|---|
| Config defaults | `hermes_cli/config.py` :: `DEFAULT_CONFIG` | Add section |
| Agent init | `agent/agent_init.py` (~tool_guardrails block ~1560) | Build `TrajectoryQualityController` + store |
| Tool sequential + parallel completion | `agent/tool_executor.py` after `_append_guardrail_observation` (~916, ~1621) | `agent._observe_trajectory_quality(...)` |
| Guardrail observation helper | `run_agent.py` :: `_append_guardrail_observation` **or** sibling `_observe_trajectory_quality` | Prefer sibling called next to guardrail observe so concerns stay split |
| Failure detection (read-only use) | `agent.display._detect_tool_failure` | No change; consume `failed` already computed |
| Signatures / hashes | `agent.tool_guardrails.ToolCallSignature`, `_result_hash` | Import/reuse; do not fork hash algorithms |
| File mutation progress | `agent.tool_result_classification.file_mutation_result_landed` | Set `progress_kind` |
| Verification failures | `agent.verification_evidence` (read APIs used by `verification_stop`) | On terminal verify commands, set verification fields |
| Turn reset | `agent/turn_context.py` where `_tool_guardrails.reset_for_turn()` runs | Also `reset_for_turn` on quality controller |
| Soft stop | `run_agent.py` / conversation loop halt checks for `_tool_guardrail_halt_decision` | Also honor quality stop if `execute_stop` |
| Status emit | Existing `_emit_status` / quiet-mode status paths | Emit recommendation once per decision |
| Persistence home | `hermes_constants.get_hermes_home` | DB path only |

**Plugin hooks:** do **not** require plugins. Optional later: emit an internal
observer hook only if a generic hook already exists for non-mutating notices —
do not invent speculative hooks.

## 12. Rewrite / retest thrash definition

“Rewrite/retest thrash” is **not** NLP over assistant prose. It is structured:

1. **Retest thrash:** ≥ `failed_verification` consecutive
   `verification_failed` observations for the session/root after a file mutation
   in the turn, without an intervening `verification_passed`.
2. **Rewrite thrash:** ≥ `identical_failure` failed `write_file`/`patch` with the
   same `args_hash`, **or** oscillating file mutation failures recorded in
   `_turn_failed_file_mutations` style signals exposed as failed observations
   without `file_mutation_landed`.
3. **Stagnation:** `stagnation_window` observations with no verified progress
   kinds while the turn already saw a failure or failed verification.

## 13. Migration and backward compatibility

- Default `enabled: false` → **zero** runtime calls into the controller beyond a
  cheap disabled check, or controller not installed.
- No schema migration on `state.db` / `hermes_state.SCHEMA_VERSION`.
- No change to `tool_loop_guardrails` defaults.
- No change to `fallback_providers` behavior or activation reasons.
- Existing tests for guardrails, fallback, verification must remain green without
  config changes.
- If `enabled: true` but store cannot open, fail **open** (log warning, disable
  persistence for the session; policy still runs in-memory).

## 14. Testing strategy (summary; details in plan)

- Pure unit tests for reducer/policy (no AIAgent).
- Config parse tests (defaults, bad types clamp/safe).
- Store tests with temp `HERMES_HOME` (real sqlite).
- Runtime tests patterned on `tests/run_agent/test_tool_call_guardrail_runtime.py`:
  disabled no-op; enabled recommendation; stop soft-halt; no transcript mutation;
  no `switch_model` call.
- Redaction tests: raw secret in args never appears in DB row JSON.
- Invariant: prompt messages list unchanged across observe.

## 15. Verification commands (for implementers)

```bash
# Focused pure + runtime
scripts/run_tests.sh tests/agent/test_trajectory_quality.py \
  tests/agent/test_trajectory_quality_store.py \
  tests/run_agent/test_trajectory_quality_runtime.py -q

# Neighbor regressions
scripts/run_tests.sh tests/agent/test_tool_guardrails.py \
  tests/run_agent/test_tool_call_guardrail_runtime.py \
  tests/agent/test_verification_evidence.py -q
```

## 16. Security and AGENTS.md compliance checklist

- [x] No new core model tool
- [x] No vendor dependency
- [x] No outbound telemetry
- [x] No raw tool-output persistence
- [x] config.yaml only for behavior
- [x] Profile-safe paths via `get_hermes_home()`
- [x] Prompt cache: no mid-conversation prompt/toolset mutation
- [x] Message alternation: no synthetic user messages
- [x] Transport fallback preserved
- [x] Disabled by default

## 17. Open questions (resolved for v1)

| Question | Resolution |
|---|---|
| Auto model switch? | No in slice 1 |
| Share state with tool_loop_guardrails controller instance? | Share **primitives** (hash/signature/failed); separate controller instance and config |
| Persist to SessionDB? | No; sidecar SQLite like verification_evidence |
| Gateway user-visible text? | Prefer non-transcript status; never fake assistant message |
| Subagents? | Own controller per AIAgent; no parent merge in v1 |

## 18. GBrain / code-navigation note

Task asked for MCP GBrain `code_blast` / `code_callers` before planning edits.
This environment has **no MCP servers configured** (`hermes mcp list` empty).
Symbol blast was performed with repo search against the live tree at
`origin/main` HEAD used for this branch (`15dc65eeda397a5d4d35edd6779141eeb8139944`
at branch creation). Implementers should re-run callers on touch points if GBrain
is available in their profile.

## 19. Success criteria (acceptance for this design)

1. Spec names exact files/symbols for seams and non-goals.
2. Two-identical-failure breaker and one-way ladder are fully specified.
3. Privacy/redaction and durable record schema are explicit.
4. Config defaults disable the feature.
5. Slice 1 is recommendation-only for model changes; stop may soft-halt when
   configured.
6. Companion implementation plan lists TDD slices and verification commands.
