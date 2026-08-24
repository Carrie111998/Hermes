# Research spike: absorbing DeepSeek Harness patterns into Hermes

**Status:** Phase 0/1 implementation candidate; not merged, later phases remain proposed
**Audited source:** `deepseek-ai/deepseek-harness` at commit `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`
**Audited package:** `@deepseek-ai/dsh@0.1.1-rc.2`
**Method:** static source, documentation, manifest, workflow, package metadata, dependency, and secret-pattern inspection. No DeepSeek Harness CLI, Web application, provider login, plugin, installer, shell runtime, model call, server, MCP connection, lifecycle script, or hook was executed.

## Decision

Hermes remains the runtime. DeepSeek Harness is an untrusted reference implementation, not a dependency, embedded runtime, or replacement.

The useful ideas are translated into Hermes-native contracts and tests. Source is not copied. The design must preserve Hermes's existing multichannel delivery, profile isolation, approvals, prompt-cache stability, toolset routing, durable memory, plugin ownership ledger, and fail-closed security controls.

## Evidence classification

| Classification | Meaning |
|---|---|
| Verified implementation | Confirmed in the pinned source tree or package metadata. |
| First-party claim | Documented by the project but not runtime-certified here. |
| Video/third-party claim | Secondary evidence; not authoritative. |
| Auditor inference | A reasoned design conclusion, explicitly not a source claim. |

Notable corrections:

- The project is a large modular runtime, not a thin DeepSeek wrapper. Its profiles compose Cordis plugins for models, tools, sessions, persistence, credentials, telemetry, sandboxing, UI, and extensions (`docs/architecture.md`, `packages/bundle/base/cordis.patch.yml`).
- It is not DeepSeek-model-only; its provider guide covers Anthropic, OpenAI, Bedrock, Vertex, Azure, Codex authentication, and OpenAI-compatible gateways (`docs/user/guide/providers.md`).
- The trajectory UI records provider-returned reasoning blocks, messages, tool activity, timing, and tokens. That is not evidence of access to hidden private chain-of-thought (`packages/client/ui-trajectory/README.md`).
- Worker-thread separation is an execution mechanism, not an OS security boundary. The project says its code-mode and dynamic Cordis paths must not be treated as security sandboxes (`packages/code-runtime/code-runtime/README.md`, `packages/extensions/tool-cordis/README.md`).
- Current popularity is real, but the video claim that the project reached 100K stars in two days was not independently verified.

## Current Hermes baseline

Hermes already contains more of the desired architecture than a superficial comparison suggests:

- a central, generation-tracked, profile-scoped tool registry with plugin override authorization and reversible restoration (`tools/registry.py`);
- deferred tool loading and toolset gating (`model_tools.py`, `tools/tool_search_tool.py`);
- lifecycle hooks, observer-first monitoring, plugin ownership ledgers, profile scoping, and fail-closed security-adjacent paths (`hermes_cli/plugins.py`, `hermes_cli/lifecycle.py`);
- rich desktop tool timelines and progress events (`apps/desktop/src/components/assistant-ui/thread/timeline.tsx`);
- durable session storage, local search, compaction, trace export/upload, and child-agent live transcripts (`hermes_state.py`, `agent/trace_upload.py`, `tools/delegation_live_log.py`);
- bounded delegation with depth/concurrency limits, pause/steer/stop, schema-constrained child output, cancellation propagation, and parent/child identity (`tools/delegate_tool.py`);
- MCP transport, native plugins, skills, cron, Kanban, background processes, browser/CDP, typed browser, desktop control, and multichannel delivery;
- monitoring that intentionally excludes prompts, messages, tool arguments/results, usage analytics, and traces by default (`hermes monitoring status`).

Therefore the absorption program should close specific gaps rather than re-platform Hermes.

### Independent comparison synthesis

Three source-grounded comparison tracks converged on the same boundary:

1. **Capability reporting is the safest first foundation.** Hermes already has enough registry, toolset, plugin ownership, profile isolation, and capability-consent metadata to derive a control-plane report. It should add no model tool, prompt content, dispatch path, approval behavior, or plugin loading side effect (`tools/registry.py`, `toolsets.py`, `model_tools.py`, `hermes_cli/plugins.py`, `hermes_cli/plugin_capabilities.py`).
2. **The smallest observability slice is durable tool-result presentation metadata, not a second session store.** Persist bounded `status`, `duration_ms`, and `is_error` facts through existing session rows and gateway hydration, then render them consistently after resume in Desktop and TUI. A canonical run-event envelope can follow in shadow mode after this compatibility seam is proven (`hermes_state_common.py`, `hermes_state.py`, gateway session hydration, Desktop timeline, TUI message rendering).
3. **Hermes is ahead on durable autonomous operation but can tighten lifecycle precision.** Preserve restart-recoverable delegation, Kanban, cron, and background work; add common first-wins terminal settlement, owner-scoped teardown, bounded quiescence, and a distinction between wait timeout and execution cancellation rather than copying DeepSeek's process-local job model (`tools/delegate_tool.py`, background process registries, Kanban workers, cron runner).

This ranks the work as: capability facts first; durable tool-execution metadata second; lifecycle/quiescence contracts third; broader trajectory/event and permission-policy work only after those seams are stable.

## Adopt / adapt / isolate / reject

| DeepSeek Harness pattern | Hermes decision | Rationale and Hermes-native form |
|---|---|---|
| Event-sourced trajectory model | **Adapt** | Hermes persists conversations and emits rich live events, but does not use one canonical durable event envelope for every model/tool/subagent transition. Add an additive run-event schema and adapters; do not rewrite message storage first. |
| Trajectory inspector | **Adopt** | Extend the existing desktop timeline into a privacy-preserving inspector with turn/tool/subagent spans, search, timing, tokens, status, and local export. Do not persist hidden reasoning or expose content by default. |
| Generated capability/config catalog | **Adopt now** | Generate a machine-readable, deterministic catalog from Hermes's registry. It becomes the basis for tool-diff gates, docs, delegation routing, and security review. Phase 1 must be read-only and must not run availability probes unless explicitly requested. |
| Declarative profile/plugin composition | **Adapt** | Hermes already has toolsets, profiles, plugins, MCP, and scoped overlays. Add validated composition snapshots and diffs rather than importing Cordis or a second DI container. |
| Reversible plugin effects | **Adopt/continue** | Extend Hermes's existing registration ownership ledger and CAS restoration to every plugin-owned hook, command, provider, and UI contribution. |
| Provider-neutral adapters | **Continue** | Hermes already has runtime provider resolution and fallback routing. Focus on conformance tests and capability negotiation, not another adapter layer. |
| Permission presets | **Adapt** | Use capability-based, resource-scoped policies: workspace paths, network destinations, subprocess, credentials, MCP, browser profile, persistence, and external actions. Avoid a single `danger-full-access` mode as a normal profile. |
| Sandbox enforcement level reporting | **Adopt** | Report `enforced`, `partial`, `brokered`, `none`, and the dimensions covered. Never label worker threads or approval prompts as containment. |
| Persistent interruption recovery | **Adopt/continue** | Hermes already recovers durable sessions and background work. Add explicit interrupted/cancelled/abandoned terminal states and orphan-resource checks to the run ledger. |
| Dynamic self-modifying plugin generation | **Investigate only in isolation** | If explored, generate source into a review-only staging directory; no auto-load, no real credentials, no inherited environment, and no host execution. |
| Worker thread as a sandbox | **Reject** | It is not a security boundary. Use brokered subprocess/container/microVM isolation for untrusted execution. |
| Broad inherited environment and repository `.env` discovery | **Reject** | Preserve Hermes profile-scoped secret resolution and explicit credential routing. Child agents and plugins receive only declared capabilities. |
| Public Web control surface without independently verified authentication | **Reject** | Existing Hermes gateway pairing, session authorization, and transport boundaries remain mandatory. |
| Raw trajectory telemetry by default | **Reject** | Local-first, content-free health metrics by default; explicit opt-in export, schema allowlists, redaction, retention, deletion, and destination preview. |
| Automatic plugin install/execute from model output | **Reject** | Separate generation, static review, operator approval, installation, and activation. |

## Prioritized absorption roadmap

### Phase 0 — immutable evidence and threat model

Deliverables:

1. This RFC and pinned source/package identifiers.
2. A source-to-Hermes gap matrix.
3. Explicit non-goals and security invariants.
4. No runtime dependency on DeepSeek Harness.

Gate: maintainer review confirms that the proposal complements rather than duplicates the existing plugin/event-bus roadmap.

### Phase 1 — capability catalog and composition snapshot

Build a deterministic, read-only Hermes tool catalog from the active profile's registry:

- tool name, toolset, bounded registration-time origin class, validated environment-variable names (never values), async/sync, result bound, structural schema digest, and availability state; schema annotations/defaults, free-form descriptions, and suspicious environment declarations are omitted because plugin-controlled strings can contain paths, operator values, or secrets;
- `available`, `unavailable`, or `unknown` availability, with `unknown` as the deterministic default for unprobed dynamic checks regardless of process-global probe-cache state;
- platform selection status, explicitly distinct from dynamic availability, per-session policy, progressive disclosure, and final model exposure;
- canonical JSON output suitable for CI diffing;
- no absolute profile paths, secrets, credential values, tool arguments, or tool results.

The command records plugin discovery honestly: `included` when explicitly requested, `preloaded` when plugin-origin tools were already present in the process-global registry, and `skipped` only when neither condition applies.

Acceptance tests:

- deterministic ordering and structural schema digest, independent of annotation text, defaults, set-like schema ordering, and profile-local paths;
- default catalog generation invokes no `check_fn`;
- explicit probe mode invokes each unique `check_fn` at most once and records failures without crashing the catalog;
- profile-scoped plugin registrations do not leak into another profile snapshot;
- MCP and plugin origins are distinguishable from built-ins without exposing implementation paths; MCP origin requires registration through the trusted native MCP boundary rather than a caller-controlled `mcp-` toolset label;
- MCP selections use canonical `mcp-<server>` IDs so a server name cannot be confused with a built-in toolset;
- credential-shaped identifier components are redacted even inside composed names such as `mcp-<server>`;
- catalog generation never imports or executes an unrequested post-setup hook.
- catalog generation does not mutate registry generation or invoke dynamic schema overrides;
- default catalog startup does not create profile files, open persistent delegation state, restore queues, load external secrets, initialize file logging, clean quarantined executables, or sweep stale bytecode;
- `selected_for_platform` is never presented as proof that a tool is model-exposed or authorized.

Gate: catalog can be generated locally and in CI; snapshot diff is stable across identical configurations.

### Phase 2 — durable tool-execution metadata, then canonical run events

Start with a compatibility slice in the existing session model:

- persist bounded tool execution `status`, `duration_ms`, and `is_error` metadata;
- preserve it through session replay and gateway hydration;
- render the same facts after resume in Desktop and TUI;
- keep tool arguments/results and exception bodies outside the new metadata fields.

Gate: old rows hydrate safely, interrupted/error/success states remain distinguishable after restart, and both presentation surfaces agree without requiring a storage rewrite.

After that gate, add an additive versioned envelope for observable run transitions:

```json
{
  "schema_version": 1,
  "event_id": "sortable-id",
  "session_id": "opaque-id",
  "run_id": "opaque-id",
  "parent_run_id": null,
  "sequence": 42,
  "timestamp": "RFC3339",
  "kind": "tool.completed",
  "status": "ok",
  "duration_ms": 318,
  "attributes": {},
  "content_ref": null
}
```

Rules:

- event metadata and content are separate;
- no hidden chain-of-thought field exists;
- content is local-only and omitted from monitoring/export by default;
- event IDs are deterministic within a persisted run and sequences are monotonic;
- observer backpressure cannot block the model/tool hot path;
- security/approval decisions fail closed and are durably distinguishable from errors.

Acceptance tests:

- model call, tool call/result, approval, compression, delegation spawn/steer/stop/complete, background process, cron/Kanban handoff, interrupt, and recovery all produce valid transitions;
- duplicate completion is idempotent;
- crash recovery closes in-flight spans as `interrupted` without inventing success;
- content-free export contains no prompts, tool arguments/results, credentials, or filesystem payloads;
- bounded queues drop/coalesce observer events according to a documented policy without changing agent behavior.

Gate: shadow-write only, no model-loop or UI dependency; compare against existing messages/progress events for parity.

### Phase 3 — desktop trajectory inspector

Extend the existing desktop timeline instead of creating a second Web application:

- filterable turn/tool/subagent/event timeline;
- parent/child run graph;
- latency, tokens, retries, cost where already available;
- approval and policy decisions;
- local redacted export and deletion controls;
- clear labels for provider-returned reasoning summaries versus ordinary messages—never “complete thinking process.”

Gate: inspector reads Phase 2 events but the agent remains fully functional if the inspector is absent or broken.

### Phase 4 — orchestration contracts

Use the run ledger to strengthen existing delegation/Kanban/cron contracts:

- explicit parent/child lineage and ownership;
- quiescence definition: no running child, process, pending delivery, or unresolved approval;
- cancellation acknowledgement and bounded teardown;
- orphan-resource detection;
- budget envelopes for iterations, time, tokens, spend, subprocesses, and toolsets;
- least-tool child routing derived from operator policy and capability catalog—not model choice.

Gate: adversarial tests cover stuck children, late completions, duplicate wakeups, gateway restart, stale authority, and partial batch failure.

### Phase 5 — permission policy and enforcement facts

Introduce declarative policy evaluation while preserving existing approvals:

- workspace/filesystem scopes;
- subprocess and command classes;
- network egress destinations and private-network denial;
- credential aliases rather than values;
- MCP server/tool allowlists;
- browser profile and typed-browser trust class;
- persistence and external-action scopes;
- enforcement report per dimension: `enforced`, `partial`, `brokered`, or `none`.

Gate: fail-closed negative tests and platform-specific containment tests. Documentation may not call a control a sandbox unless the asserted dimensions are actually enforced.

### Phase 6 — review-gated extension generation

Optional, isolated developer workflow only:

1. generate source and manifest into staging;
2. run static analysis and tests with scripts/network disabled;
3. show capability diff and provenance;
4. require operator approval;
5. install disabled;
6. activate in a disposable profile;
7. support deterministic unload/rollback.

Gate: no model-generated artifact can become executable or gain credentials in the same step that created it.

## Security invariants

1. DeepSeek Harness remains absent from Hermes runtime dependencies and plugin paths.
2. No real repository, transcript, browser profile, cloud account, or production credential is used in comparative experiments.
3. Trajectory data is local-first, bounded, redactable, deletable, and excluded from monitoring export by default.
4. Reasoning visibility is limited to provider-returned content; hidden chain-of-thought is neither requested nor claimed.
5. Tool/plugin capability grants are deny-by-default and profile-scoped.
6. Worker threads, permission prompts, and path checks are not represented as OS containment.
7. Public/LAN control surfaces require authenticated, authorized, session-bound transports.
8. Generated extensions cannot install, load, execute, or persist themselves.
9. Static inspection and unit tests do not certify runtime isolation; containment claims require adversarial platform tests.

## Comparative evaluation

Any isolated pilot must run identical synthetic tasks in Hermes and the pinned DeepSeek artifact and measure:

- task completion and verification rate;
- human intervention count;
- tool calls, model calls, latency, tokens, and cost;
- cancellation and restart recovery;
- unauthorized access attempts and actual reachability;
- event completeness, privacy leakage, and trajectory usefulness;
- operational complexity and upgrade/rollback behavior.

Immediate stop conditions: unexpected egress, host filesystem access outside the synthetic workspace, inherited real credentials, approval bypass, uncontrolled subprocess survival, raw telemetry, or cross-session/plugin leakage.

## Deferred decisions

- Whether run events live in a new append-only table or are derived from the existing message/event stores during the shadow phase.
- Whether the canonical envelope should align directly with OpenTelemetry spans or remain a smaller Hermes schema with an OTLP adapter.
- Which policy engine format, if any, should represent resource-scoped grants.
- Whether content references should use encrypted blob storage, existing message rows, or no separate storage.
- Whether capability metadata belongs on each tool registration, each action variant, or a separate policy registry. Static tool-level risk labels alone are too coarse for multi-action tools.

These decisions must be made from Hermes requirements and tests, not copied from DeepSeek Harness.
