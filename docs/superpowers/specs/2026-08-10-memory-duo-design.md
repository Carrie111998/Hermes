# Memory Duo v1 — Hermes + Obsidian durable memory design

**Status:** approved design for implementation planning  
**Date:** 2026-08-10  
**Target profile:** Hermes Autopilot  
**Primary topology:** Hermes Desktop on Windows connected to a remote Hermes backend

## 1. Goal

Build a strong two-tier durable memory architecture for the Autopilot Hermes profile before the orchestration layer is strengthened.

The design keeps Hermes' existing bounded hot memory and session/Graphify history, while adding Obsidian as a large, human-readable durable knowledge store behind a controlled Memory Broker.

The system must be lightweight, remote-backend-first, provider-agnostic, resilient to outages, and ready to become a shared memory service for future multi-agent orchestration without paying that service overhead today.

## 2. Operating constraints

The user's current client machine is Windows 11 Pro with a 14th-generation Intel Core i5, 16 GB DDR5 RAM, and no dedicated GPU. Hermes Desktop connects to a remote Hermes backend. No local model inference, local embedding model, local reranker, CUDA stack, or GPU-dependent component is required.

Memory Duo must therefore put operational memory work beside the remote Hermes backend. The Windows client should require no additional memory daemon beyond the existing Hermes Desktop and Obsidian applications.

## 3. Core model

Memory has three complementary layers:

1. **Hermes hot memory** — `MEMORY.md` and `USER.md`; tiny, high-value facts needed frequently.
2. **Hermes episodic memory** — session DB + Graphify/raw-evidence history; records what actually happened.
3. **Obsidian deep memory** — curated, structured, long-lived knowledge representing what was learned.

Mental model:

> Hermes remembers what it constantly needs. Graphify remembers what happened. Obsidian remembers what was learned. The Memory Broker controls what agents may retrieve or change.

Obsidian does not replace built-in Hermes memory or Graphify.

## 4. Architecture boundary

Use a hybrid of a Hermes-native memory provider and a service-oriented broker core.

```text
Hermes Autopilot
      |
ObsidianDuo MemoryProvider     # thin Hermes adapter
      |
MemoryBrokerClient            # typed stable contract
      |
EmbeddedMemoryBroker          # v1 runtime
      |
+-- SQLite + FTS5
+-- ObsidianVaultAdapter
+-- Retrieval / ranking
+-- Memory policy
+-- provenance/evidence
+-- conflict/supersession
+-- journal/recovery
+-- secret filtering
+-- optional inherited-session LLM assistance
```

The broker is **service-ready but embedded now**. Business logic must not be scattered through Hermes core or tied directly to SQLite call sites. A later `IPCBrokerClient` or remote broker implementation must be possible without changing caller contracts.

Do not run a separate broker daemon in v1 unless a concrete runtime requirement appears.

### Plugin placement

Respect the repository's narrow-core/third-party integration policy. Obsidian-specific implementation should use Hermes' existing memory-provider plugin discovery and should be suitable for installation as a user/standalone plugin rather than requiring Obsidian-specific branches throughout core Hermes. Widen generic memory-provider interfaces only where a real missing generic capability is proven by tests.

## 5. Single write authority

The Memory Broker is the only autonomous authority allowed to promote durable agent knowledge into the Hermes-managed Obsidian memory area.

Future subagents do not write directly to the vault. They may request context from the broker and submit memory candidates. The broker validates, deduplicates, verifies, checks conflicts and decides whether a candidate is discarded, staged or promoted.

This rule must hold when there is one agent and when there are hundreds.

## 6. Obsidian access boundary

Hermes may read the whole configured Obsidian vault.

Autonomous durable memory writes are concentrated under a dedicated managed area:

```text
Hermes Memory/
├── Projects/
├── Decisions/
├── Research/
├── People/
├── Preferences/
├── Lessons/
├── Workflows/
├── Tasks/
├── Entities/
├── Conflicts/
└── Inbox/
```

Hermes may modify notes outside the managed area only when the active task explicitly requires editing those notes. Memory promotion alone never grants authority to reorganize the rest of the vault.

## 7. Memory types

Keep the initial ontology small:

- `preference`
- `person`
- `project`
- `decision`
- `research`
- `fact`
- `lesson`
- `workflow`
- `task`
- `entity`
- `candidate`

Folder location is presentation. Frontmatter `type` and stable `memory_id` are authoritative.

## 8. Durable note schema

Each managed memory is an ordinary readable Markdown note with structured frontmatter. Filenames are not identities.

Required/expected fields include:

```yaml
hermes_memory: true
memory_id: mem_<stable-id>
type: decision
status: active
project: hermes-desktop
entities: []
confidence: 0.97
verification: directly_observed
importance: 0.8
created_at: <timestamp>
updated_at: <timestamp>
created_by: hermes
authority: agent
source:
  kind: session
  session_id: <id>
  task_id: <optional>
evidence: []
supersedes: []
superseded_by: null
sensitivity: normal
tags: []
scope:
  profile: autopilot
  workspace: null
  project: null
  task: null
  session: null
```

Stable IDs survive note renames and moves.

## 9. Confidence, verification and authority

Do not collapse belief into evidence.

`confidence` means how strongly the producing agent believes the proposition.

`verification` describes evidential support, using an initial ordered vocabulary such as:

- `unverified`
- `inferred`
- `source_supported`
- `directly_observed`
- `user_confirmed`

Authority is separate. Manual user corrections outrank agent-generated memories. A high-confidence inference is never silently treated as a directly observed fact.

## 10. Provenance and evidence

Every important durable memory must answer where it came from.

Supported provenance should include user statements, Hermes sessions, files, tool observations, web/research sources, subagent output, manual Obsidian edits, and links to prior memories.

Evidence receives its own stable identity (`evidence_id`). Multiple memories can reference the same evidence. Large raw evidence should be referenced rather than copied into every note.

For future swarms, preserve evidence lineage so forty agents repeating one underlying source cannot masquerade as forty independent confirmations.

## 11. Conflict and supersession

Use append-first history for important durable claims.

Do not silently overwrite a conflicting durable memory. Create a new version/memory and mark the prior one `superseded`, or represent a live unresolved conflict explicitly.

Initial states should support at least:

- `active`
- `unverified`
- `disputed`
- `superseded`
- `archived`
- `needs_attention`

Retrieval must surface unresolved conflicts rather than hiding the lower-ranked side.

## 12. Manual Obsidian edits

Manual user edits are authoritative.

Track note hashes and broker write transaction IDs. If content changes without a matching broker write, treat it as a probable user edit:

1. reparse;
2. record a user-correction event/version;
3. invalidate relevant retrieval caches;
4. supersede conflicting agent-generated state where appropriate;
5. never silently recreate the agent's previous text.

If a manual edit creates malformed managed frontmatter, do not overwrite the file to repair it. Mark it `needs_attention` in broker state and retain the prior valid indexed representation until corrected.

## 13. Promotion policy

Use confidence-gated, event-driven consolidation.

Continuously notice candidates, but commit at meaningful boundaries rather than after every conversational sentence.

Immediate promotion candidates include explicit `remember this` requests, direct user corrections, confirmed preferences and confirmed important decisions.

Deferred candidates include research findings, coding lessons, workflows, project facts and tool workarounds. Promote after verification, milestone completion, task completion or session end.

Never persist chain-of-thought/private reasoning, transient plans, raw tool dumps, temporary paths, unsupported guesses, or credentials as durable memory.

## 14. Secrets policy

Credentials must never become memory.

Reject/redact API keys, passwords, access/refresh tokens, authentication cookies, private keys, authorization headers and comparable secrets. It is acceptable to remember references such as `GitHub token is available through GITHUB_TOKEN`, but never the value.

Run deterministic secret scanning both before LLM/policy transformation and immediately before final persistence. Redact logs and diagnostics as well.

## 15. Retrieval — deterministic first

No vector database or local embedding stack is required in v1.

Start with:

1. metadata/scope filtering;
2. SQLite FTS5 lexical retrieval;
3. Obsidian links/relationships;
4. verification/authority weighting;
5. type-aware recency and importance;
6. optional LLM reranking only when deterministic retrieval is insufficient.

The broker should classify recall into approximately:

- `NONE`
- `EXACT`
- `STRUCTURED`
- `SEMANTIC`
- `DEEP`

Trivial prompts and obvious exact retrieval must not invoke an auxiliary model.

The system must be able to return `NO VERIFIED MEMORY FOUND` rather than forcing a vaguely related memory into context.

## 16. Memory packets

Agents receive bounded, ephemeral memory packets, not unrestricted vault dumps.

Packets include stable memory IDs, concise content/summary, type, confidence, verification, authority, relevance, evidence references, relationships, conflicts and uncertainties.

Packets are views, not durable memories. They may be short-lived cached objects but must always be rebuildable.

Every request accepts a context/token budget. Future orchestrators may set different budgets for a main commander, research manager, coder or cheap worker.

## 17. Inference and zero-cost routing invariant

By default, all LLM-assisted Memory Duo operations use the **current live Hermes session runtime**, not a separately configured paid model and not merely the profile default.

The inherited runtime includes, where compatible:

- provider;
- active model;
- base URL;
- credential pool/API credential resolution;
- API mode;
- compatible request overrides;
- relevant prompt-cache opportunities.

This mirrors the existing background-review approach that inherits the parent's live runtime.

Example: if the active Hermes session uses DeepSeek V4 Flash through a free OpenCode Zen route, Memory Duo's candidate classification/reranking/consolidation should use that same route by default. If the session uses a Mistral free tier, it should inherit that route instead.

### Cost policy

Default configuration must behave like:

```yaml
memory_duo:
  inference:
    mode: inherit_session
    cost_policy: no_paid_fallback
```

Requirements:

- no mandatory dedicated inference provider;
- no mandatory embedding provider;
- no automatic transition from an inherited free route to a paid provider;
- if the inherited runtime is unavailable/rate-limited, deterministic retrieval continues and deferrable AI work is queued;
- a paid/specialist override is allowed only when explicitly configured by the user;
- Memory Duo never persists duplicate API credentials.

Provider pricing/free-tier status itself is external; the invariant is that Memory Duo does not create an independent paid route without explicit opt-in.

## 18. Resource constraints

On the Windows client, Memory Duo adds zero required background services, zero local model processes, zero vector DBs and zero embedding daemons.

On the remote Hermes host, v1 should use:

- one embedded broker;
- one SQLite DB with FTS5;
- no Redis;
- no PostgreSQL requirement;
- no Qdrant/Chroma/Weaviate/Milvus requirement;
- no Neo4j requirement;
- no local LLM/embedding process;
- no permanent Obsidian headless process requirement.

Idle work should be effectively negligible. Expensive maintenance must be event-driven or idle-priority.

## 19. SQLite and derived state

Use SQLite as the broker state/index store, with WAL mode, foreign keys, sensible busy timeout and transactional writes.

Expected logical tables include memories, memory_versions, evidence, memory_evidence, relationships, conflicts, candidates, note/index state, sync events, broker journal, and bounded retrieval cache.

The Obsidian managed knowledge remains human-readable durable source material. Derived indexes must be rebuildable from the vault and other authoritative Hermes history. Deleting the derived index must not destroy knowledge.

## 20. Vault indexing

Index Markdown first. Do not deeply index binary attachments by default.

Initial indexing default should be `lazy`:

1. index `Hermes Memory/` first;
2. cheaply catalogue the wider vault;
3. index wider-vault content on demand or during idle work.

Maintain a manifest of path, mtime, size and content hash. After initial indexing, process changed notes only.

Prefer filesystem notifications where reliable; fall back to low-frequency polling. Exclude temporary/cache directories, `.git`, `.trash`, `node_modules`, binary attachments and configured ignored paths.

## 21. Obsidian synchronization

Support the vault being available to the remote Hermes machine through a headless/synchronised mechanism, but do not make continuous sync a core requirement.

Default to event/debounced synchronization where available:

- sync before a task when freshness is important;
- debounce multiple broker writes into one sync;
- sync after important task completion;
- final best-effort flush on graceful shutdown.

Sync failure never invalidates a successful local memory commit. Record `sync_dirty` and retry later.

The broker's storage adapter must remain usable with an ordinary filesystem-mounted/synchronised vault; do not hardwire the core to one sync vendor.

## 22. Runtime lifecycle and degradation

Startup must be cheap and should not require network access. Initialize by loading config, opening/validating SQLite, checking the vault/index manifest and inspecting the recovery journal.

Expose states such as:

- `READY`
- `DEGRADED`
- `SYNCING`
- `REINDEXING`
- `RECOVERING`
- `UNAVAILABLE`

Deep-memory failure must not stop Hermes chat/agent operation. If Obsidian or optional semantic inference is unavailable, Hermes retains built-in hot memory and session/Graphify recall.

## 23. Non-blocking writes and backpressure

Respect Hermes' `sync_turn()` non-blocking contract.

Candidate extraction/promotion work occurs asynchronously using a bounded in-process queue. Do not create unbounded memory jobs during long Autopilot runs.

When pressure rises, deduplicate/coalesce candidates, discard low-value ephemera and journal important deferred work.

## 24. Compression and session lifecycle

Use existing provider hooks instead of special-casing the main loop where possible.

Before context compression, `on_pre_compress()` must ensure important pending candidates/evidence references are durably represented before live context is dismissed.

Use `on_session_switch()` to keep profile/session lineage correct across resume/branch/reset/compression transitions.

At session/task completion, perform a bounded Memory Consolidation Pass over task summaries, artifacts, verification results, failures, decisions and evidence rather than replaying enormous raw histories unnecessarily.

## 25. Delegation and future swarms

Use parent-side `on_delegation(task, result, child_session_id, ...)` to observe completed delegated work.

Subagent output becomes a candidate, never automatically a verified durable memory.

Every memory operation should accept optional orchestration identifiers from day one:

- `mission_id`
- `task_id`
- `agent_id`
- `parent_agent_id`
- `workspace_id`
- `project_id`

This prevents a later migration simply to answer which agent/mission produced a memory.

## 26. Memory scopes

Support retrieval scopes such as user, profile, workspace, project, task and session.

Retrieval starts as narrowly as possible and expands only when necessary. All provider-owned state uses the `hermes_home` passed to `initialize()` so remote profiles remain isolated correctly.

## 27. Adaptive hot/deep memory

Track retrieval/use frequency and importance to enable later safe promotion between tiers.

Stable, small, high-authority knowledge repeatedly needed across sessions can become a candidate for built-in `MEMORY.md`/`USER.md`. Rarely needed hot memory can be demoted to Obsidian while remaining durable.

Do not implement aggressive autonomous churn in the first pass; expose scoring and safe candidate mechanisms first.

## 28. Crash safety

Use a durable broker journal for multi-step mutations. Operations transition through explicit states such as prepared, written, indexed and committed.

Obsidian writes should use temporary-file + atomic-replace semantics where supported. On startup, reconcile incomplete transactions deterministically.

A process crash between note write and index update must be recoverable without guessing or silently losing the memory.

## 29. Caching

Cache deterministic derived work such as parsed frontmatter, lexical candidate sets, link resolution and file hashes.

Avoid long-lived caches of semantic conclusions. If semantic/rerank results are cached, keep TTLs short and key them to a memory-version epoch so any relevant memory change invalidates them.

## 30. CLI/diagnostics

Provide lightweight provider/plugin-native diagnostics such as:

```text
hermes memory-duo status
hermes memory-duo doctor
hermes memory-duo rebuild-index
hermes memory-duo reconcile
hermes memory-duo pending
hermes memory-duo conflicts
hermes memory-duo stats
```

`doctor` should validate vault reachability, DB integrity/schema, journal state, duplicate IDs, broken supersession, malformed managed notes, secret leakage indicators and sync state.

Do not require a separate metrics server. Store inexpensive counters/diagnostics locally.

## 31. Backups

State under `HERMES_HOME` naturally participates in Hermes backup. If managed vault data is outside `HERMES_HOME`, use `backup_paths()` or an equivalent safe plugin mechanism where appropriate.

Default to backing up broker metadata and the Hermes-managed Obsidian memory area, not blindly copying the user's entire unrelated vault/attachment archive.

## 32. Required tests and acceptance gates

At minimum prove these behaviors:

1. A durable preference/decision can be retrieved in a later session.
2. Unrelated queries do not inject irrelevant memory.
3. Exact/structured retrieval works without any LLM call.
4. An inherited-session inference operation uses the actual active session provider/model/base URL/runtime, including remote-backend sessions.
5. A fake free provider test proves Memory Duo never contacts an unapproved paid endpoint.
6. Rate-limited/unavailable inherited inference degrades to deterministic retrieval or deferred work rather than paid fallback.
7. Manual Obsidian edits override conflicting agent-generated state.
8. Malformed manual notes are not destructively auto-repaired.
9. Conflicting memories are surfaced, not silently ranked away.
10. Secret-like values are blocked before persistence and do not leak into logs.
11. Broker/Obsidian failure does not take Hermes down.
12. Crash during a multi-step write recovers from the journal.
13. Deleting the derived SQLite/index state permits a successful rebuild.
14. Incremental indexing touches only changed notes after initial indexing.
15. Bounded queues prevent runaway RAM use in a long task.
16. `on_pre_compress` preserves important pending knowledge/evidence before dismissal.
17. Subagent/delegation observations become candidates, not trusted facts.
18. Profile isolation works across desktop remote backend, gateway channels and direct remote CLI for the same/different `HERMES_HOME` values.
19. No GPU/local-model dependency is pulled into the required install path.
20. Startup succeeds without contacting the vault sync service or model API.

Add retrieval fixtures with known relevant and irrelevant notes so retrieval changes can be evaluated rather than judged by anecdotes.

## 33. Non-goals for v1

Do not build:

- a heavyweight distributed memory platform;
- a permanently running multi-process broker unless proven necessary;
- a mandatory vector database;
- a mandatory embedding model/API;
- local LLM inference;
- automatic arbitrary edits across the whole Obsidian vault;
- autonomous paid-model fallback;
- direct durable-memory write authority for subagents;
- a replacement for Hermes session DB/Graphify;
- orchestration itself.

The orchestration architecture is the next project and should consume Memory Duo through the stable broker contracts defined here.

## 34. Implementation principle

Prefer existing Hermes provider/plugin infrastructure over core changes. Preserve prompt caching and keep the model tool footprint narrow. Verify the current code path before widening any hook.

The implementation should optimize for the smallest runtime footprint that still preserves durability, auditability, provenance, failure recovery and future swarm compatibility.
