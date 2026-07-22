---
author: Agent
date: 2026-07-17
project: hermes-agent
status: Exploration Complete
scope: Agent, subagent, child-agent spawning/invocation and Hermes Desktop visibility
---

# Hermes Agent / Subagent / Child-Agent Lifecycle and Desktop Visibility Exploration

## 1. Mission and boundary

This exploration maps how Hermes launches agents and child agents, how the model invokes delegation, how synchronous and detached work differ, and how live subagent execution becomes visible in Hermes Desktop.

This is an exploration artifact, not an implementation proposal. No runtime or frontend code is changed by this artifact. The repository already contained unrelated/staged changes; the only uncommitted runtime change observed during this exploration was the pre-existing `tui_gateway/server.py` change that bypasses the ordinary tool-progress gate for native `subagent.*` events, plus `tests/test_subagent_observability.py`. That bypass preserves the native lifecycle contract, but it is not by itself the root cause of every missing Desktop execution-log symptom: Desktop also has a fallback path that derives synthetic subagent rows from ordinary `delegate_task` tool events.

Evidence rule:

- **FACT**: directly observed in current source/tests/history.
- **INFERENCE**: derived from multiple observed facts.
- **RISK**: failure mode demonstrated by code or test/history.
- **UNKNOWN**: not proven in this exploration.

## 2. Executive conclusion

Hermes has one child-agent spawn engine and several invocation surfaces. The model-facing operation is `delegate_task`; there is no separate current `function.delegate` primitive in the repository. The invocation surfaces converge on either `AIAgent._dispatch_delegate_task()` or the registry handler, which then converges on `tools.delegate_tool.delegate_task()`.

For a top-level model agent, `_dispatch_delegate_task()` forcibly selects background execution. The child agent is built immediately, receives a stable identity, and executes on a daemon executor. A single task produces one detached async unit. A batch produces **one `delegation_id`** whose worker runs all children in parallel and emits **one consolidated async completion event** after the batch aggregate finishes. Nested work from an orchestrator subagent remains synchronous so that the orchestrator can consume its workers' results during its own turn. The executable branch in `tools/delegate_tool.py:2800-2965` is the source of truth for this one-unit/aggregate behavior; older comments near the input-normalization and registry-fallback code describe an N-handle batch model but are stale relative to the branch that actually dispatches the work.

Desktop live observability is not driven by the detached `async_delegation` completion event. It is driven by relayed `subagent.*` events emitted during child execution and reduced into the process-local nanostore `$subagentsBySession`. The Agents panel renders `allSubagents($subagentsBySession)`.

The confirmed Desktop retention defect is in `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts`: every `message.start` calls `clearSessionSubagents(sessionId)` before the next child events arrive. A new parent chat turn therefore deletes the live Agents rows even though the detached backend delegation can remain alive and continue emitting/completing. This is a frontend state-retention failure, not proof that the child process died.

## 3. Corrected vocabulary

| Term | Current meaning |
|---|---|
| `delegate_task` | Model-facing tool/function name and Python delegation operation. |
| `function.name = "delegate_task"` | OpenAI-compatible tool-call representation in an assistant message. |
| `AIAgent._dispatch_delegate_task()` | Single `AIAgent` call site used by the live model path. |
| `tools.delegate_tool.delegate_task()` | Spawn/validation/orchestration engine. Also callable directly by Python. |
| `delegation_id` | Async registry identity for one detached async unit. A batch occupies one async unit. |
| `subagent_id` | Stable identity for one child branch, used by progress events, registry targeting, and the Desktop tree. |
| `child_session_id` | The child's own SessionDB session id, allowing a UI to open/watch the child session. |
| `origin_ui_session_id` | The live TUI/Desktop session that commissioned detached work. |
| `parent_session_id` | Durable session id of the spawning parent agent. |
| `Agents` / `Spawn tree` | Desktop UI surface backed by `$subagentsBySession`, not by the async completion queue directly. |
| `function.delegate` | **Not found as a distinct current primitive.** Treat references to it as a wrapper/provider terminology issue unless a future branch introduces that symbol. |

## 3A. Model routing and independent-review boundary

**FACT:** The current active configuration was later inspected with secret fields redacted:

```yaml
delegation:
  provider: "ai-router"
  model: "cb-gpt-sol"
  max_iterations: 50
  max_concurrent_children: 8
  max_spawn_depth: 1
```

The reviewer dispatch itself did not include a model/provider override, and its returned handle/summary did not record the effective model. No durable record for `deleg_4167c87d` was available afterward. Therefore the exact model/provider used by that reviewer is **UNKNOWN**; it is not valid to claim from the summary alone that it used either the parent model or the current `delegation.*` pair.

The runtime rule is nevertheless proven: when `delegation.provider` and `delegation.model` are empty, `_resolve_delegation_credentials()` returns null overrides and `_build_child_agent()` inherits the parent's provider, model, endpoint, and applicable credential-pool behavior (`tools/delegate_tool.py:3053-3178`). When those keys are configured, the child resolves the configured delegation provider/model instead. The reviewer had a fresh isolated context, but its model independence was not verified.

**FACT:** Hermes supports a different provider/model for child delegations through `config.yaml`, but the setting applies to the delegation's children as a group:

```yaml
delegation:
  provider: "openrouter"
  model: "<review-or-child-model>"
```

`delegation.base_url`, `delegation.api_key`, and `delegation.api_mode` provide the direct-endpoint alternative. Provider overrides use the same runtime provider resolver as CLI/gateway startup; missing credentials fail the delegation rather than silently pretending that a different model is active (`tools/delegate_tool.py:3064-3072`, `3148-3178`).

**FACT:** The model-facing `delegate_task` schema exposes `goal`, `context`, `tasks[].goal`, `tasks[].context`, `role`, and deprecated `background`; it does not expose a per-call/per-task `model`, `provider`, or `skills` selector (`tools/delegate_tool.py:3402-3478`). The live dispatch passes `toolsets=None` and resolves one `creds["model"]` for all children in that call (`tools/delegate_tool.py:2527-2546`).

**INFERENCE:** A genuinely independent review therefore requires one of these boundaries:

1. Configure `delegation.provider/model` differently before spawning the reviewer; this changes the child model for that delegation but also applies to sibling children in the same call.
2. Run the reviewer as a separately configured Hermes profile/process when it needs a different model, skills, memory, or tool policy.
3. Regardless of model choice, provide only evidence/diff/acceptance criteria and an adversarial rubric; do not provide the implementer's self-assessment or hidden reasoning. Fresh context reduces anchoring but does not remove same-model correlation.

**UNKNOWN:** This exploration does not establish a supported per-task model override inside one model-facing `delegate_task(tasks=[...])` call. The current schema and dispatch path provide no such contract.

## 4. Invocation surfaces and convergence

### 4.1 Top-level model function-call path

```text
LLM response
  assistant_message.tool_calls[].function.name == "delegate_task"
        |
        v
agent/agent_runtime_helpers.py
  function_name == "delegate_task"
        |
        v
AIAgent._dispatch_delegate_task(function_args)
        |
        v
  tools.delegate_tool.delegate_task(..., background=True, parent_agent=self)
```

Evidence:

- `agent/agent_runtime_helpers.py:2407-2410` dispatches the `delegate_task` branch to `agent._dispatch_delegate_task()`.
- `run_agent.py:5959-5989` defines the single dispatch point and forces `background=(not _is_subagent)`.
- `run_agent.py:5979-5980` identifies nested subagents by `_delegate_depth > 0`.

The `background` field in the model schema is deprecated/ignored for the live model path. The parent runtime, not the model, decides whether the top-level operation is detached.

### 4.2 Tool executor path

`agent/tool_executor.py:1370-1407` provides the display/middleware wrapper around `delegate_task`:

1. Builds a delegation spinner label.
2. Sets `agent._delegate_spinner`.
3. Runs `_run_agent_tool_execution_middleware()`.
4. Calls `agent._dispatch_delegate_task(next_args)`.
5. Clears the spinner in `finally`.

This is not a second spawn implementation. It is a display/middleware wrapper around the same dispatch point.

### 4.3 Registry fallback path

`tools/delegate_tool.py:3482-3539` registers `delegate_task` in the tool registry. Its handler calls `delegate_task(...)` and computes the background value using `_model_background_value()`.

This fallback exists for paths where the direct `AIAgent` intercept is bypassed. It still converges on the same engine. Direct Python callers retain the historical synchronous default unless they explicitly pass `background=True`.

### 4.4 Other wrappers

The repository contains ACP, CLI, gateway, plugin-context, and MCP-facing tool dispatch wrappers. The repository-wide search shows these paths reference the same `delegate_task` name; they do not define a separate child-agent runtime. The distinction is transport/adapter behavior, not child semantics.

Important boundary:

- `delegate_task` is not a Kanban task.
- Kanban worker spawning is board-level orchestration and has its own lifecycle.
- A Kanban worker may itself receive the delegation toolset, but board dispatch and in-conversation child delegation must not be conflated.

## 5. Spawn engine: `tools.delegate_tool.delegate_task`

### 5.1 Input normalization and guardrails

`tools/delegate_tool.py:2381-2503` performs the following before any child runs:

1. Requires a `parent_agent`.
2. Rejects new spawns when the delegation pause kill switch is active.
3. Normalizes the requested role.
4. Normalizes `background`.
5. Enforces `delegation.max_spawn_depth`.
6. Loads delegation config and resolves child credentials/provider runtime.
7. Normalizes either one `goal` or a `tasks` list.
8. Enforces `delegation.max_concurrent_children` for a batch.
9. Validates every task object and required goal.

The model cannot narrow child toolsets through a model-facing `toolsets` argument in the live path; the child inherits the parent's effective toolsets (`tools/delegate_tool.py:2527-2536`).

### 5.2 Child construction and identity

`tools/delegate_tool.py:2521-2553` builds all children on the parent thread, preserving the parent's resolved tool-name global after child construction.

`_build_child_agent()` creates a real `AIAgent` with:

- `platform="subagent"`;
- fresh iteration budget;
- inherited or explicitly resolved provider credentials;
- `skip_context_files=True` and `skip_memory=True`;
- parent SessionDB handle and `parent_session_id`;
- child progress callback;
- child `_delegate_depth`, `_delegate_role`;
- stable `_subagent_id` and `_parent_subagent_id`;
- `_subagent_goal` and parent turn id;
- `_delegate_from` session marker to keep child sessions out of normal session pickers;
- optional shared credential pool.

The child is appended to the parent's `_active_children` for synchronous interrupt propagation. It immediately emits `subagent.spawn_requested` after construction (`tools/delegate_tool.py:1360-1407`), so a queued child can appear before execution starts.

### 5.3 Child execution

`_run_single_child()` at `tools/delegate_tool.py:1746`:

1. Gets the child's progress callback.
2. Acquires a credential lease where applicable.
3. Starts a heartbeat thread that touches parent activity while the child works.
4. Registers the live child by `subagent_id` for UI controls/status.
5. Emits `subagent.start`.
6. Runs `child.run_conversation(...)` on a daemon timeout executor.
7. Relays child streamed text as `subagent.text`.
8. Relays tool/thinking/progress events through the callback.
9. Builds structured result metadata: status, summary, duration, model, token counts, tool trace, files read/written, output tail, cost.
10. Emits `subagent.complete`.
11. Cleans up child registry/credential/heartbeat state in the surrounding lifecycle.

The child itself is a normal `AIAgent`; “subagent” is a role/lifecycle marker plus inherited execution context, not a different reasoning engine.

## 6. Execution modes and exact semantics

### 6.1 Synchronous direct Python call

A direct call such as `tools.delegate_tool.delegate_task(goal=..., parent_agent=parent)` defaults to synchronous execution. The caller blocks until `_execute_and_aggregate()` returns.

This is mainly relevant to tests/internal callers and nested orchestration. Do not infer top-level Desktop behavior from this mode.

### 6.2 Top-level single task

`run_agent.py:_dispatch_delegate_task()` sets `background=True` for a top-level parent. `delegate_tool.py:2800-2965` dispatches the whole operation through `tools.async_delegation.dispatch_async_delegation_batch()`.

The call returns immediately with:

```json
{
  "status": "dispatched",
  "mode": "background",
  "count": 1,
  "delegation_id": "deleg_<id>",
  "goals": ["..."],
  "note": "Subagent is running in the background..."
}
```

The detached worker later executes the child and publishes one `async_delegation` completion event. The result is injected back into the owning conversation by the gateway/CLI completion mechanism.

### 6.3 Top-level batch

A batch is not currently N independent async registry records. `delegate_tool.py:2800-2965` dispatches the whole batch as one async unit using `dispatch_async_delegation_batch()`.

Inside that unit:

- all children are built first;
- `_execute_and_aggregate()` submits children to a daemon pool;
- children run in parallel, bounded by `max_concurrent_children`;
- the async registry occupies one slot for the whole batch;
- one `_finalize_batch()` call emits one `type="async_delegation"` event containing the complete per-task `results` list.

`tools/async_delegation.py:645-827` is explicit about this contract.

**Correction to earlier analysis:** describing a background batch as “N independent async dispatches, each with its own handle” is incorrect for the current code. The current implementation has N child branches inside one detached batch unit and one `delegation_id`/completion event.

### 6.4 Nested orchestrator → worker

A child may be granted `role="orchestrator"`, subject to role normalization and depth limits. Nested delegation is synchronous (`background=(not _is_subagent)`), because the orchestrator needs worker results during its own turn and does not own the original Desktop/gateway return route.

Nested identities use `parent_subagent_id` and `parent_id`; the Desktop can reconstruct a tree when events preserve these fields.

### 6.5 Capacity and unsupported delivery fallbacks

The async registry rejects dispatch at capacity. The caller falls back to synchronous execution in the relevant path (`delegate_tool.py:2945-2962`).

Stateless HTTP/API sessions that cannot deliver a detached result after the request ends also fall back to synchronous execution (`delegate_tool.py:2810-2837`).

Therefore “background=true” does not guarantee detached execution on every transport; the delivery capability check is part of the runtime contract.

## 7. Async registry, persistence, ownership, interruption

### 7.1 Registry identity and state

`tools/async_delegation.py`:

- `_new_delegation_id()` creates `deleg_<8 hex chars>` ids (`:416-418`).
- In-memory records track running/finalizing/completed status and retain recent completions (`:420-435`, `:830-840`).
- Durable SQLite records persist dispatch/completion metadata (`:71-384`).
- The completion event includes `delegation_id`, `session_key`, `origin_ui_session_id`, `parent_session_id`, goal(s), status, summary/results, timings, and error metadata (`:587-643`, `:766-827`).

### 7.2 Completion queue and delivery claims

The worker pushes its completion into the shared `process_registry.completion_queue`. The TUI gateway has one poller per live TUI session, so all pollers see the same queue.

`tools/async_delegation.py:325-371` provides durable claim/release/complete delivery operations. `tui_gateway/server.py:9174-9185` claims an event before injecting it into a conversation, and completes the claim only after dispatch succeeds.

This prevents duplicate completion delivery when multiple desktop session pollers race.

### 7.3 Owner routing

`tui_gateway/server.py:8921-9041` applies two gates:

1. `_notification_event_belongs_elsewhere()` defers events owned by another live session.
2. `_session_owns_notification_event()` requires positive ownership before addressed events are injected.

Ownership precedence:

1. `origin_ui_session_id` for the commissioning Desktop/TUI tab;
2. durable `session_key`;
3. compression continuation lineage resolved through SessionDB;
4. addressed orphan is dropped rather than adopted by an unrelated session.

Tests in `tests/test_tui_gateway_server.py:2546-2614` cover origin-session preference, compression continuation, and finalized-origin fallback.

### 7.4 Session-end interruption

`tui_gateway/server.py:645-670` calls `interrupt_for_session()` during TUI session finalization. `tools/async_delegation.py:870-923` interrupts records matching `origin_ui_session_id`, `session_key`, or `parent_session_id`.

This is intentional lifecycle ownership: closing the commissioning session must stop detached work that has no valid return address. A new message/turn is not equivalent to closing/finalizing the session.

The parent-level synchronous path separately propagates interrupts through `_active_children`. Detached async dispatch removes children from that parent list and transfers lifecycle ownership to the async registry (`delegate_tool.py:2875-2902`).

## 8. Live observability event pipeline

```text
Child AIAgent
  │ child_progress_cb
  ▼
tools.delegate_tool callback
  │ adds identity: subagent_id, parent_id, depth, child_session_id,
  │ goal, task_index/task_count, model, tool_count
  ▼
parent_agent.tool_progress_callback
  ▼
tui_gateway.server._on_tool_progress(sid, event_type, ...)
  │ serializes subagent payload
  │ emits JSON-RPC event on the owning session transport
  ▼
write_json({_jsonrpc: "2.0", method: "event", params: {...}})
  │ session-specific transport if session_id is present
  ▼
Desktop use-message-stream event router
  ▼
$subagentsBySession[sid] / upsertSubagent()
  ▼
AgentsView + status bar + composer status stack
```

### 8.1 Event types

Desktop recognizes (`apps/desktop/src/app/session/hooks/use-message-stream/utils.ts:84-91`):

- `subagent.spawn_requested`
- `subagent.start`
- `subagent.thinking`
- `subagent.tool`
- `subagent.progress`
- `subagent.complete`

The backend emits these from the child progress callback (`tools/delegate_tool.py:883-1003`, `1746-2279`).

### 8.2 Backend event filter

`tui_gateway/server.py:3950-4066` normally gates ordinary progress on `_tool_progress_enabled(sid)`. Current code explicitly bypasses that gate for `event_type.startswith("subagent.")`, so native lifecycle events remain visible to Desktop Agents.

This bypass is necessary for the native path: ordinary tool progress preference is a chat-noise preference, while native subagent lifecycle is an Agents panel data contract. However, it does **not** prove that `display.tool_progress` is the sole cause of missing execution logs. The Desktop has a second, fallback path described in Section 9.2. The evidence-supported statement is narrower: with the bypass, native `subagent.*` events are emitted even when ordinary tool progress is off; ordinary events remain governed by the ordinary progress setting.

`subagent.text` is mirrored to a child watch window but intentionally not emitted on the parent session to avoid flooding the parent stream (`server.py:4058-4066`, `4096-4155`).

### 8.3 Child watch-window mirror

If a child session is opened in watch mode, `_mirror_subagent_to_child()` emits synthetic native stream events on the child session:

- `message.start` once;
- `reasoning.delta` for thinking;
- `message.delta` for child text;
- `tool.start`/`tool.complete` around child tools;
- `message.complete` at child completion.

This is separate from the parent Agents tree. A child watch window can be live even when the parent tree is not retained.

## 9. Desktop state and rendering model

### 9.1 Store

`apps/desktop/src/store/subagents.ts` defines:

```ts
$subagentsBySession: atom<Record<string, SubagentProgress[]>>
```

Each row carries:

- `id` / `subagent_id`;
- `parentId`;
- `goal`;
- `sessionId` / `child_session_id`;
- status;
- task count/index;
- timestamps/duration;
- model and token/cost counters;
- files read/written;
- bounded stream tail;
- summary/current tool.

`upsertSubagent()` merges events by stable id and refuses late updates after a terminal status (`subagents.ts:209-229`). `buildSubagentTree()` connects `parentId` to children and sorts by spawn time/task index (`:231-256`).

### 9.2 Fallback and native event coexistence

Desktop has two producer paths for the same Agents store:

1. **Native path:** the backend emits authoritative `subagent.spawn_requested`, `subagent.start`, `subagent.thinking`, `subagent.tool`, `subagent.progress`, and `subagent.complete` events with child identity and execution metadata.
2. **Fallback path:** `use-message-stream/index.ts:312-321` can synthesize `delegate-tool:<tool_id>:<index>` rows from ordinary `tool.start`/`tool.progress`/`tool.complete` events for `delegate_task` using `delegateTaskPayloads()` (`utils.ts:131-179`). The fallback parser derives goals, status, task index/count, summary, and a bounded summary tail from the parent tool event; it is not equivalent to the native per-child execution stream.

Once native subagent events arrive, `pruneDelegateFallbackSubagents()` removes those fallback rows. This prevents duplicate rows but introduces a sequencing requirement: native `subagent.*` events must not be dropped before the first native event establishes the session as native.

Consequences for diagnosis:

- `display.tool_progress: off` suppresses ordinary fallback source events, but native `subagent.*` events are explicitly allowed through the current gateway bypass.
- A missing **full child execution log** cannot be attributed to `tool_progress` without identifying whether the UI was receiving the native path or only the fallback path.
- A missing **Agents row/list after a new user message** is independently explained by `message.start -> clearSessionSubagents(sessionId)` in Section 10.

### 9.3 Agents panel and status indicators

`apps/desktop/src/app/agents/index.tsx:80-99` reads all sessions, flattens them with `allSubagents()`, and builds one Spawn tree. It does not filter to the focused session.

`apps/desktop/src/store/background-delegation.ts:1-48` derives the parked-background status from running/queued rows for the active session when the parent is idle.

`apps/desktop/src/store/composer-status.ts:143-168` merges live subagent rows, background process rows, and todos into the status stack.

`apps/desktop/src/app/shell/hooks/use-statusbar-items.tsx` reads the same subagent store scope as the Agents panel. The tests explicitly protect against a count/tree desynchronization (`apps/desktop/src/store/subagents.test.ts:104-123`).

## 10. Confirmed visibility regression

Current handler (`apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts:295-317`):

```text
message.start
  ├─ flushQueuedDeltas(sessionId)
  ├─ clearSessionSubagents(sessionId)       <-- destructive reset
  ├─ reset compaction/native-session refs
  └─ mark parent session busy
```

Subagent events are handled later (`gateway-event.ts:482-495`) and upsert into the store only if the session is not marked interrupted.

### Why the rows disappear

1. Top-level `delegate_task` detaches the child/batch from the parent turn and returns a handle.
2. The Desktop can become idle while child work remains active.
3. The user sends another message, or the completion poller starts a new internal turn.
4. Gateway emits `message.start` for that new turn.
5. Desktop clears all rows for that session.
6. Existing child events may continue, but the UI now depends on a later `subagent.spawn_requested`/`subagent.start` event being accepted as a new row; prior stream/history is lost, and if no further event arrives during a quiet tool call the panel appears empty.

This explains the observed symptom: backend work can continue and complete while the Agents list vanishes.

### Why this is not equivalent to session termination

Backend session finalization is an explicit lifecycle path that calls `interrupt_for_session()`. `message.start` merely starts a turn on a still-live session. Treating the two as the same cleanup boundary is the core semantic mismatch.

### Important second gate

The frontend subagent branch rejects updates when `sessionInterrupted(sessionId)` is true (`gateway-event.ts:482-495`). Therefore any fix to retention must preserve true interrupt semantics: an explicitly interrupted session/turn must not resurrect stale rows unintentionally. The desired contract is not “never clear”; it is “do not clear live rows merely because a new turn started.”

## 11. Risks and invariants for any future implementation

### 11.1 Worst-case unintended side effects

1. **Zombie rows:** retaining terminal rows forever could make the Agents panel accumulate stale historical children and misrepresent active work.
2. **Cross-turn contamination:** reusing a stable `subagent_id` after terminal state could cause a new run to be rejected by `upsertSubagent()` or merge into the old branch.
3. **Wrong-session resurrection:** accepting late events after a real session close/interrupt could show work whose return address is no longer owned.
4. **Duplicate fallback/native rows:** changing clear/prune ordering could retain synthetic `delegate-tool:*` rows alongside authoritative native rows.
5. **Count inconsistency:** changing retention in the focused-session path but not the global Agents/statusbar aggregation would recreate the existing count/tree desync class.

### 11.2 Behavioral invariants

- A running/queued child remains visible across a new parent `message.start`.
- A terminal child remains inspectable for a bounded retention period or until explicit cleanup, but must not count as active.
- A true session finalization/explicit cancellation removes or terminalizes its owned live children according to the chosen UX contract.
- Native `subagent.*` events remain observable even when ordinary `display.tool_progress` is off; ordinary fallback source events remain subject to the ordinary progress setting.
- Event ownership remains fail-closed; no unrelated Desktop session may consume another session's async completion.
- One child branch maps to one stable `subagent_id`; one detached batch maps to one `delegation_id` but multiple child rows.
- The Agents panel and statusbar use the same store scope.
- `subagent.complete` must clear active tool state and produce a terminal row with summary/metrics.

## 12. Historical evidence and intent

Relevant history confirms that this area has been actively hardened for ownership and observability rather than being an accidental unused path:

- `aab351bfa fix(delegation): route async results to origin session` — added origin-session routing.
- `4b27be111 fix(delegation): fail-closed orphan handling + session-scoped delegation lifecycle` — added fail-closed ownership and session interruption.
- `d0e9a42ce fix(delegation): harden durable completion delivery` — added durable completion/claim behavior across async delivery.
- `54d0948d3 fix(tui): route post-turn completions by owner` — hardened shared completion queue routing.
- `51a710e57 refactor(desktop): extract gateway-event dispatcher into its own sub-hook` — explicitly recorded the extraction as behavior-preserving; the current `clearSessionSubagents` line moved with the dispatcher.

Interpretation: ownership and delivery are deliberate design concerns. The retention issue is not evidence that the async architecture is absent; it is a distinct frontend retention-policy mismatch.

## 13. Verification performed

### Passed

Command, executed from `apps/desktop`:

```bash
npm test -- --run src/store/subagents.test.ts src/app/session/hooks/use-message-stream/utils.test.ts src/lib/gateway-events.test.ts
```

Actual result:

```text
Test Files  3 passed (3)
Tests       19 passed (19)
```

This verifies current store tree/upsert/terminal/fallback behavior, native-versus-fallback event utility mapping, and fail-closed Desktop event session routing.

### Failed or blocked, reported honestly

Python targeted suite command:

```bash
scripts/run_tests.sh tests/tools/test_async_delegation.py tests/test_tui_gateway_server.py
```

Actual result:

```text
error: no virtualenv found in /o/workspaces/oss/hermes-agent/.venv or /o/workspaces/oss/hermes-agent/venv,
and HERMES_PYTHON is not a python with pytest
```

No Python pass is claimed. The environment has no usable `.venv`/`venv` at the MSYS-resolved path for the mandatory wrapper.

A first Vitest attempt from repository root failed because Vite did not load the Desktop package alias configuration. It was corrected by rerunning from `apps/desktop`; the package-local command passed as recorded above.

Static probe confirmed that the current `message.start` block contains `clearSessionSubagents(sessionId)` before the subagent event branch. `git diff --check` passed.

## 14. Open questions that must be decided before implementation

1. **Retention UX:** Should terminal rows remain visible until the user closes the Agents panel, for a fixed TTL, until a new root delegation begins, or until explicit “clear completed” action?
2. **Ownership of a new parent turn:** A new prompt should not terminate detached children, but should its new turn appear as a separate root in the same global Spawn tree or be visually grouped with the prior delegation?
3. **Async completion behavior:** When the completion event re-enters the parent conversation and triggers `message.start`, should live rows remain unchanged, become terminal from the existing child events, or be reconciled from durable delegation state if events were missed?
4. **Reconnect/restart:** The current live Agents store is process-local. Should Desktop rehydrate running/completed subagents from durable `async_delegations` after renderer reload or gateway reconnect?
5. **Interrupt semantics:** Which frontend action constitutes true cancellation for retention purposes: `/stop`, `session.interrupt`, closing the Desktop session, or only backend `interrupt_for_session()`?
6. **Batch identity display:** Should Desktop receive/retain `delegation_id` on each child payload so grouping is explicit, rather than using timing/task-count heuristics in `AgentsView.groupDelegations()`?
7. **No-event gap:** What should the panel show during a long child tool call when no progress event arrives for a while? The backend has `_active_child_runs` for watch liveness, but the parent store currently needs a retained row to show activity.

## 15. Recommended next exploration/implementation boundary

The next change should be a narrowly scoped Desktop retention change, not a rewrite of delegation or gateway ownership:

- preserve live `$subagentsBySession[sessionId]` across ordinary `message.start`;
- explicitly separate “new turn reset” from “session finalized/true cancellation cleanup”;
- retain native/fallback pruning logic;
- add behavioral tests for a running row surviving a new `message.start`, terminal rows not counting as active, and true interrupt cleanup;
- add an integration/e2e path proving a detached child remains visible after the user sends another message;
- only then consider durable rehydration or explicit completed-row UX.

No implementation was performed because OpenSpec Explore is discovery-only.

## 16. Decision gate

The system is now understood sufficiently to create a formal OpenSpec change for Desktop subagent retention/observability. The exploration should be treated as the source of truth for that proposal.

Before implementation begins, the Commander must decide the retention UX and interruption boundary from Section 14. The technical root cause is no longer ambiguous.
