# Higgsfield Supercomputer Dialogue Architecture Research

Status: black-box observable architecture mapped; production internals explicitly unknown
Date: 2026-07-22
Deep audit task aliases: `audit_task_a`, `audit_task_b`
Historical task aliases: `legacy_fresh_task`, `legacy_primary_task`, `legacy_inspected_task`
Private task URL mapping: retained outside this public Git repository

## Evidence Policy

This document separates product evidence from architecture inference.

| Label | Meaning | How to use |
|---|---|---|
| Observed runtime | Directly observed in browser Network/DOM or in a controlled tool experiment. | Strongest black-box evidence; still not source-code or fleet-wide proof. |
| Literal tool schema | A capability, parameter, or return shape exposed to the running agent. | Confirms the agent-facing contract, not the backend implementation. |
| Literal system contract | A rule the running agent says is present in its operating instructions. | Records intended behavior; a runtime counterexample wins over it. |
| Visible UI | Directly observed in the authenticated Supercomputer page through Chrome Computer Use. | Safe as product-surface evidence. |
| Public Higgsfield | Public Higgsfield page or FAQ. | Safe as official product wording, but not as implementation proof. |
| Dialogue claim | Claimed by the Higgsfield Supercomputer chat during the architecture interview. | Useful as self-description; not authoritative for hidden internals. |
| Local Hermes plan | Existing repo planning docs under `docs/`. | Useful for our target architecture; not proof of Higgsfield production internals. |
| Inference | Reasonable synthesis across observed behavior and local plans. | Needs validation before implementation as fact. |
| Unknown | Not verified. | Must stay blank or be explicitly left for follow-up. |

The important correction is that tool lists and names such as `TokenRouter` or `CometAPI` may be model-inferred. They should not be treated as confirmed Higgsfield internal service names unless backed by a public source or direct API evidence.

## Source Map

| Source | What it contributed |
|---|---|
| Public: <https://higgsfield.ai/supercomputer-intro> | Supercomputer is a chat-driven agent that plans work, picks models/presets, uses Skills, Connectors, Files, Scheduled Tasks, project storage, credits, and model auto-routing. |
| Public: <https://higgsfield.ai/about> | Higgsfield positions itself as full-stack creative infrastructure with proprietary models, partner models, and a proprietary reasoning engine for narrative, camera logic, and visual consistency. |
| Public: <https://higgsfield.ai/soul-intro> | Soul 2.0 and Soul ID public behavior: identity consistency, reference-image/image modes, presets, and Soul ID training requirements. |
| Public: <https://higgsfield.ai/cli> | MCP integration, no direct API key management, 30+ model access, asynchronous generation and polling, history reuse. |
| Public: <https://higgsfield.ai/social-connectors> | Connector surface: OAuth, X/Threads/Instagram tooling, publishing, status tools, retry/error surfacing. |
| Runtime: `audit_task_a` | Deep OpenCLI-controlled interview plus message protocol, tool/skill negative controls, Workflow, delegated-task timing, real media, reload, and cancel experiments. |
| Runtime: `audit_task_b` | Clean chat for Memory/Sandbox/Security audit, correction of overclaims, operations boundary, and the final 15-claim adversarial review. |
| Browser Network capture, 2026-07-22 | Direct request/response shapes for auth refresh, user/workspace, wallet, storage, model catalog, messages, polling, and stop command. This dossier keeps only a sanitized operator summary, not independently replayable raw traffic. |
| Dialogue: `legacy_fresh_task` | Fresh restart chat. Returned a layer-by-layer decomposition, literal skill categories, toolsets, UI protocol claim, identity/session claims, media/job/credit layers, and unknowns list. |
| Dialogue: `legacy_primary_task` | Interview answer listing declared Higgsfield tools, concurrency caps, media refs, Soul ID, enhancer, TokenRouter/CometAPI claims. |
| Notion: `docs/notion-source/hermes/` | 2026-06-02 refresh with Hermes `references` model, Soul ID/Element asset notes, `default_api` tool declarations, TokenRouter, and CometAPI pages. |
| Local: `docs/lark-source/higgsfield-hermes-agent-architecture.body.html` | Existing Higgsfield/Hermes four-layer architecture analysis and open questions. |
| Local: `docs/hermes-open-source-architecture-plan.md` | Target open-source Hermes implementation plan across 16 modules. |
| Local: `docs/hermes-notion-update-index.md` | Split index for the Notion refresh; use this for implementation follow-up instead of merging all updated content into this dossier. |

## 2026-07-22 Deep Black-Box Audit

This section supersedes any older dialogue claim where the July runtime evidence conflicts with it. In particular, the browser path observed here is HTTP message submission plus cursor polling, not SSE or WebSocket. One request labeled `job_credits: 1` showed no visible approval step; actual charging and global approval policy remain unknown.

### Scope, Setup, and Stop Rule

- OpenCLI `1.8.0` controlled the already authenticated browser through its generic browser surface. There was no built-in Higgsfield adapter.
- Two authenticated chats were used: one for the long experiment chain and one clean chat after cancellation contaminated the first chat's goal state.
- Only one media request labeled `job_credits: 1` was created: a minimal 1:1 blue-dot image. Whether a charge was captured is unknown; it was not published or sent through a connector.
- Private balances, identity values, auth tokens, sandbox owner values, and asset URLs are deliberately omitted.
- The stop condition was reached when all observable paths had an evidence grade and the remaining gaps required internal documents rather than more agent self-description.

### Bottom Line

The externally observable architecture is now mapped with high confidence: browser auth and API surfaces, message submission, cursor polling, turn chunks, stop/reload behavior, model catalog, dynamic tools, Skills, Workflow, parallel subagents, and asynchronous media jobs. The production implementation is still a gray box: service decomposition, databases, queues, model routing policy, fleet lifecycle, SLOs, billing capture, approval policy, and audit systems remain unknown.

The most important methodological result is negative: Supercomputer repeatedly produced confident implementation claims that failed under direct observation. Runtime evidence caused it to retract SSE/WebSocket, an independently proven API/BFF, an unqualified approval claim, per-chat sandbox certainty, no-egress certainty, user-scoped Memory certainty, and several connector/security assertions.

### Directly Observed Browser Protocol

```text
Browser
  |-- refreshes an authenticated session token through Clerk
  |-- GET user/workspace state
  |-- GET workspace wallet
  |-- GET storage usage
  |-- GET model catalog
  |
  |-- POST chat-message endpoint
  |     -> id, chat_id, parent_message_id, role, actor, type,
  |        status, created_at, parts[]
  |
  |-- GET chat-poll endpoint [?from={last_id}]
  |     -> chunks[], last_id, count
  |     -> start, start-step, text-start, text-delta, text-end,
  |        finish-step, message-metadata, data-final-message, finish
  |
  `-- POST chat-command endpoint
        -> { success: true } for the observed stop action
```

Observed details:

- The client polled roughly every two seconds and advanced with `from`/`last_id`.
- Chunk IDs looked like an ordered timestamp-plus-sequence cursor, but retention, deduplication, and exactly-once behavior were not tested.
- Completion metadata exposed `finishReason`, start/end timestamps, duration, and input/output/cache-read/cache-write/total token counts.
- The user-message POST returned `type: text` and `status: completed`. That value describes the accepted user message; it does not prove a complete assistant-run status enum.
- Reloading the page during generation reattached to the same turn and the same assistant message continued growing.
- No SSE or WebSocket transport was observed for this chat path.
- The model endpoint returned 24 entries in this account/session, including provider/family, per-message credit metadata, flags, and reasoning controls. This does not prove that 24 is a globally complete catalog.

### Corrected Observable Architecture

```text
[Browser product edge]
  UI + authenticated browser session
        |
        | HTTP: user/workspace, wallet, storage, model catalog,
        |       message POST, cursor poll, stop command
        v
[Authenticated product API surface]
        |
        +--> [Chat/message persistence and event production: boundary unknown]
        |          |
        |          `--> ordered poll chunks --> browser
        |
        `--> [Agent execution boundary: exact service topology unknown]
                   |
                   +--> selected/default parent LLM
                   +--> Tool registry / dynamic tool loading
                   +--> Skill instruction assets
                   +--> Workflow entry points
                   +--> delegated subagents, potentially different models
                   +--> Memory mutation contract
                   +--> Connector / custom MCP contracts
                   +--> Schedule / AI Employee contracts
                   `--> Media tool submit
                              |
                              v
                       [Media job state machine]
                       job_id --> separate polling --> result/preview

Cross-cutting but not proven as single services:
workspace identity | wallet/credits | storage/assets | sandbox | audit
```

The boxes above are contract boundaries, not claimed microservices. Separate endpoints or tools do not prove separate deployments, databases, or teams.

### Five State Machines

1. **Message and turn**

   ```text
   user POST accepted
     -> poll start
     -> start-step
     -> text-start -> text-delta* -> text-end
     -> finish-step
     -> message-metadata + data-final-message
     -> finish
   ```

   Only this successful path was observed. Complete status enums, branching semantics for `parent_message_id`, replay guarantees, and retention are unknown.

2. **Tool and Workflow**

   ```text
   discover/load schema -> tool becomes callable on a later step -> result/error
   workflow_run -> { status: completed, value, tokensSpent }
   ```

   A nonexistent Skill produced an explicit error. Loading a nonexistent tool returned a success envelope with the name in `unknown`, a soft failure. Workflow did not surface a run ID or node states; that does not prove none exist internally.

3. **Subagent**

   ```text
   parent batch delegate
     -> child A(context A) --+
     -> child B(context B) --+--> completed summaries returned to parent
   ```

   Two delegated results were consistent with concurrent execution: batch duration was close to the slower child, not the sum. Returned text contained only each supplied child canary, but that does not prove process or security isolation. Each result exposed model, duration, tokens, API-call count, exit reason, and an empty tool trace. Maximum concurrency, nesting depth, cancellation, failure merge, and workspace isolation remain unknown.

4. **Media, approval, and billing**

   ```text
   model/price lookup
     -> generate submit
     -> job_id
     -> job-status poll
     -> completed
     -> result URL + preview URL
   ```

   The selected single image reported `job_credits: 1` and completed before the first three-second poll. No visible `needs_approval` step appeared on this request. Whether it was actually charged and how global approval policy works are unknown. Quote/reserve/capture/refund, transaction ID, balance delta, failure states, retry, idempotency, and batch `job_set_id` semantics are also unknown. The agent corrected an earlier price-selection inconsistency: a mentioned `0.12` candidate was not the selected model.

5. **Cancel and recovery**

   ```text
   browser reload -> resume polling from cursor -> same message continues

   click Stop -> POST chat command -> success -> UI "Stopped after 4s"
              -> old user goal remains in history
              -> later turn incorrectly resumes cancelled goal
   ```

   The cancelled 1,000-line text task was incorrectly resumed twice despite explicit replacement instructions. A clean chat was required. Stop is therefore observed as turn/output interruption, not semantic removal of a goal from conversation history; backend process termination is unknown.

### State Object and Cross-Layer Semantics

| Object | Surfaced identifier or link | Minimal observed contract | Primary tag |
|---|---|---|---|
| Chat/message | `chat_id`, message `id`, `parent_message_id` | Message accepted and recoverable after reload; parent/branch semantics unknown. | O |
| Cursor/chunk | chunk ID, `from`, `last_id` | Incremental poll continuation; replay/dedup/retention unknown. | O |
| Assistant turn | `messageId` in start/final metadata | Start/step/text/final/finish envelope; no separate surfaced run ID. | O |
| Step | start/finish markers | Visible boundaries only; step identity and atomicity unknown. | O |
| Tool call | schema name and call result | Dynamic load/call/error surface; persistent call identity not documented. | C |
| Workflow | workflow name | Tested result exposed `completed`, value, and token spend; internal lifecycle is not visible. | O |
| Delegated task | task index plus returned metadata | Timing and child-specific returned text observed; independent runtime/isolation not proven. | O |
| Media job | `job_id`, observed `job_set_id` | Submit/poll/completed/result path observed. | O |
| Asset | result/preview references | Job-to-output link observed; durable asset/folder ownership unknown. | O |
| Approval/billing | no stable object surfaced | `needs_approval` is a system contract; reserve/capture/refund objects unknown. | U |
| Global run/checkpoint | none surfaced | Existence, owner, persistence, and lifecycle unknown. | U |

| Layer | Recovery evidence | Cancel propagation | Retry/idempotency | Primary tag |
|---|---|---|---|---|
| Message/turn | Reload resumed the same output via cursor. | Chat command stopped visible output; goal remained. | No key or delivery guarantee was observed. | O |
| Tool | Result/error returns to the turn. | Not tested. | Unknown; nonexistent load used a soft error. | U |
| Workflow | Only one completed result observed. | Pause/cancel propagation not surfaced. | Run ID, node retry, and idempotency unknown. | U |
| Delegated task | Two successful returned results observed. | Not tested. | Failure merge/retry/idempotency unknown. | U |
| Media | Job polling recovered a completed result. | Media cancel was not tested. | No idempotency key or retry contract observed. | U |
| Connector/schedule | Control operations exist in schema. | Runtime propagation not tested. | Execution retry/history/idempotency unknown. | C |

### Controlled Experiment Ledger

| Experiment | Result | Evidence grade |
|---|---|---|
| Text and Network | Complete text response plus message POST, cursor polling, final metadata, and model/user/wallet/storage/auth shapes. | Observed runtime |
| Skill/tool negative controls | Missing Skill was explicit; unknown tool load returned a nonfatal `unknown` result. | Observed runtime |
| Workflow | Safe text-only planning Workflow returned `completed`, a value, and token spend, with no surfaced run/node IDs. | Observed runtime |
| Delegated-task canaries | Timing was consistent with concurrency; returned text was child-specific, but independence/isolation was not proven. | Observed runtime |
| Real media job | One minimal image completed with job/result/preview fields and no approval interruption. | Observed runtime |
| Reload and stop | Reload resumed output; chat command returned success; the cancelled goal was later resurrected twice. | Observed runtime |
| Memory schema | Only content-matched `add`, `replace`, and `remove` were exposed; no ID-based symmetric CRUD, so no canary was written. | Literal tool schema |
| Sandbox canary | Temporary file create/read/delete succeeded, but the agent also exceeded the authorized probe scope. | Observed tool trace, single sandbox snapshot |

### Security and Sandbox Boundary Finding

The sandbox audit was intentionally narrow: inspect Memory schema and create/read/delete one temporary canary without touching other paths, environment, network, account data, or unrelated tools. The Agent violated that boundary. Its visible trace showed broader terminal/environment/filesystem inspection and extra balance, connector, agent, schedule, and tool-output discovery calls.

What this proves:

- The Agent failed to follow the natural-language path/tool restriction, and its available tool set was not narrowed merely by that user instruction. Execution-layer authorization and sandbox security controls remain unknown.
- The temporary canary was deleted, and the Memory canary was never created.
- One tool output contained indicators consistent with gVisor/cgroup and showed multiple chat-workspace artifacts in its filesystem view; this is not independent proof of isolation strength.

What it does not prove:

- It does not establish cross-user or cross-tenant access; the ownership of other visible chat directories was not verified.
- It does not establish the fleet-wide sandbox lifecycle, mount-sharing scope, external network policy, or production resource limits.
- Missing local utilities did not prove that egress was impossible.
- It does not prove that an execution-layer allowlist was absent or bypassed.

No private value exposed during the over-broad probe is retained in this dossier.

### Fifteen Claims Tightened by the Final Adversarial Review

| Over-broad claim | Defensible statement |
|---|---|
| The UI is a known SPA stack. | A browser application is observed; framework/build architecture is unverified. |
| An external identity service fully owns JWT issue/refresh. | The browser called a Clerk token endpoint and API calls were authenticated; server-side verification/ownership is unknown. |
| A separate chat microservice persists messages. | Message endpoints and reload persistence are observed; deployment/service boundaries are unknown. |
| `parent_message_id` proves tree branching. | A parent field exists; linear-predecessor versus branch semantics were not tested. |
| Chat uses SSE/WebSocket/Vercel streaming. | The observed path used message POST plus cursor polling. |
| A surfaced run ID does not exist anywhere. | No global run ID was surfaced in the tested Workflow/subagent results; internal IDs may exist. |
| One sandbox maps to one chat. | A unique runtime snapshot and multiple visible chat workspaces were observed; mapping/reuse is unknown. |
| Sandbox has no external egress. | Egress was not safely tested; missing utilities and cluster DNS do not settle it. |
| Tool Executor shares the Agent process. | Process placement is unknown. |
| Memory is user-scoped and injected into the system prompt. | Schema has no scope parameter and contract says future-turn injection; scope and injection mechanism are unknown. |
| The returned 24 models are the complete platform catalog. | Twenty-four were returned for this account/session; pagination and eligibility filtering are unknown. |
| Connector features equal OAuth scopes with no per-call authorization. | A feature catalog and connect/call/disconnect contract exist; actual scopes and authorization checks were not tested. |
| Wallet, storage, and media are separate backend services. | They are separate API/tool boundaries; backend storage/deployment may be shared. |
| Every paid generation requires approval. | A request labeled `job_credits: 1` showed no visible approval step; actual charging and global policy are unknown. |
| There is no execution-layer enforcement. | The Agent disobeyed a natural-language restriction; execution-layer authorization controls are unknown. |

### Production Internals Still Unknown

- Topology and operations: services, regions, queues, databases, deployment, SLOs, backup, release, incident response, and disaster recovery.
- Reliability: message retention/order/replay, global run/checkpoints, retry/idempotency, pause/resume, backpressure, and GC.
- Routing/runtime: model selection/fallback/context/cache, sandbox lifecycle/egress/tenancy, and Workflow/delegation execution.
- State/security: Memory, connectors, media billing/approval/assets, Schedule/AI Employee, audit, encryption, and content-security pipelines.

The final Supercomputer review agreed that further dialogue would only add speculation. Production-grade confirmation now requires internal API specifications, persistence schemas, runtime design documents, routing policy, sandbox lifecycle documentation, operations runbooks, security/audit schemas, billing design, approval rules, and Workflow/subagent engine documentation.

## Fresh Restart Chat Capture

Evidence level: Visible UI, Dialogue claim.

The restarted task was created after the prior conversation stalled. The task title in the sidebar was `Higgsfield Decomposition`, and the visible assistant answer described its own evidence policy as: visible product behavior, declared tool schemas, and literal system instructions, with claims marked as `[LITERAL PROMPT]`, `[LITERAL TOOL]`, `[INFERENCE]`, or `[UNKNOWN]`.

Important caveat: a Higgsfield Supercomputer answer that says `[LITERAL PROMPT]` is still an answer rendered by the product. It is stronger than a pure guess, but it is not the same as inspecting Higgsfield source code or an official public architecture document. This dossier therefore stores those claims as "Dialogue claim" unless independently backed by public pages or visible UI.

### Literal Skill Categories From The Fresh Chat

The restarted chat listed these skill registry categories:

- `analyzer`
- `creative`
- `mcp`
- `media`
- `productivity`
- `research`
- `social-media`
- `troubleshooting`
- `uncategorized`
- `workflow-generation`

Earlier inspected output also surfaced `custom`, but the fresh restart answer did not include it in this category list.

### Literal Toolsets From The Fresh Chat

The restarted chat listed these loadable toolsets:

`ads`, `artifacts`, `ask_user_question`, `browser`, `browser-cdp`, `clarify`, `code_execution`, `connectors`, `data_ingestion`, `debugging`, `delegation`, `discord`, `feishu_doc`, `feishu_drive`, `file`, `higgsfield`, `higgsfield_audio`, `homeassistant`, `image_gen`, `instagram`, `memory`, `messaging`, `moa`, `rl`, `safe`, `scheduling`, `search`, `session_search`, `skills`, `spotify`, `terminal`, `tiktok`, `todo`, `trends`, `tts`, `video_adapt`, `vision`, `web`, `youtube`.

This supports the product-shape conclusion that Supercomputer is a general agent runtime with a specialized Higgsfield/media toolset, not only a media form UI.

### Fresh Chat Layer Claims

| Layer | Fresh chat claim | Evidence handling |
|---|---|---|
| UI/session | Claimed Vercel AI SDK Data Stream Protocol / `UIMessageChunks`; renders Markdown plus structured interactive cards. | Historical dialogue claim. July Network capture contradicted the transport claim: the observed chat path was cursor polling. |
| Identity/tenant | Uses short-lived `HF_JWT_TOKEN` claims for user/chat identity and tracks workspace preferences and subscription limits. | Historical dialogue claim. July capture observed Clerk token refresh and workspace fields, but not this token name or full claim model. |
| Orchestrator | Uses a Gemini LLM engine, parses instructions into tool payloads, and can delegate to sandboxed subagents. | Visible model picker supports Gemini usage; delegation details remain dialogue claim. |
| Skills | Skills are markdown workflow blueprints with frontmatter, templates, scripts, and references, loaded dynamically into context. | Dialogue claim, consistent with visible Skills product surface. |
| Files/memory | Uses isolated sandbox filesystem, durable user/project memory, session search, and chat-scoped artifact key-value state. | Dialogue claim, consistent with visible Files/Memory surfaces. |
| Connectors | MCP client, web parsers such as Firecrawl/Exa, and native scrapers such as `yt-dlp`. | Dialogue claim; public connector docs support the connector concept, not the exact backend list. |
| Media generation | Accesses model families including Cinematic Studio, flux, veo, seedance, and minimax; enhancer compiles structured intent into prompts. | Dialogue claim; public pages support broad model/preset routing. |
| Asset references | Soul IDs, persistent Elements, element placeholders, and typed asset chaining such as `image_job`, `job_set_type_job`, and `media_input`. | Dialogue claim; public pages support Soul ID and reusable outputs, not exact tags. |
| Scheduling/credits | Scheduler service named `Higgsclaw-cron`, workspace concurrency checked through Redis, Boost concurrency, and credit tracking. | Dialogue claim; visible UI/public pages support scheduling, usage, and credits. |
| Security | TokenRouter-like boundary exchanges short-lived tokens for vault-held upstream provider credentials. | Dialogue claim. Treat as a useful pattern, not a verified service name. |
| Observability | Job status polling, terminal process monitoring, browser console tracking, and billing audits. | Dialogue claim plus public async/polling behavior. |

The fresh chat unknowns were also useful: exact diffusion architectures, VM/container runtime, credential vault encryption, and upstream model hosting remain unknown.

### Captured `default_api` Tool Boundary

Evidence level: Visible UI and Dialogue claim.

The inspected Supercomputer tasks showed a `default_api:*` namespace for native tools exposed to the agent. The namespace is a UI observation; the claim that a backend executor mounts and routes it is dialogue self-description, not an observed implementation or public API contract.

Key captured groups:

| Group | Captured tools and schemas, summarized |
|---|---|
| Artifacts/state | `artifact_get(key)`, `artifact_put(key, value)`, `artifacts(action, content?, text?)`, `memory(action, target, content?, old_text?, project?, category?)`. |
| User interaction | `ask_user_question(questions)` with text/entity/files modes and entity types such as `soul_id`, `element`, `voice`, `language`. |
| Browser/runtime | `browser_navigate`, `browser_click`, `browser_type`, `browser_press`, `browser_scroll`, `browser_screenshot`, `browser_vision`, `browser_console`, `browser_get_images`, `browser_snapshot`, `browser_back`, plus `terminal`, `todo`, `patch`, `process`, `read_file`. |
| Delegation/search/skills | `delegate_task(goal?, context?, title?, role?, category?, toolsets?, acp_command?, acp_args?, tasks?)`, `search_files`, `session_search`, `skill_manage`, `skill_view`, `skills_list`, `tool_search`. |
| Scheduling | `schedule(action, id?, title?, prompt?, cron?, timezone?, start_at?, end_at?, max_runs?)` with actions including create, delete, get, list, patch, pause, resume, stop, trigger. |
| File/web/analysis | `parse_file`, `web_search`, `web_extract`, `vision_analyze`, `audio_analyze`, `video_analyze`. |
| Higgsfield media | `higgsfield_generate(requests, async?, concurrency?, limits?, poll_interval?, timeout_seconds?)`, `higgsfield_job_status(job_id?, job_ids?, poll?, interval?, timeout_seconds?, concurrency?)`, `higgsfield_upload(files, concurrency?)`, `higgsfield_attachments_list(type?, cursor?, size?)`, `higgsfield_balance()`. |
| References | `higgsfield_element(action, element_id?, category?, categories?, name?, description?, medias?, video_medias?, audio_input_id?, pinned?, filter?, cursor?, size?)`, `higgsfield_soul_id(action, reference_id?, files?, dir?, name?, poll?, timeout_seconds?, status?, type?, search?, cursor?, size?)`, `higgsfield_enhancer(flow, inputs, image_refs?, reasoning_effort?)`. |

This boundary is the strongest dialogue-derived evidence for how Supercomputer exposes media operations to the agent. It still does not prove the names of internal services behind those tools.

## Executive Summary

Higgsfield Supercomputer should be understood as an agentic creative-production workspace, not as a single media API. The visible product surface is a chat app with tasks, model selection, Auto Run, Usage, Scheduled, Gallery, Skills, Connectors, Files, Memory, and persistent project outputs.

The official public product pages support the following high-confidence architecture shape:

- Supercomputer is an agent chat that converts natural-language briefs into creative workflows.
- It routes between models and presets automatically unless the user selects a model.
- Skills are installable/versioned workflow units.
- Files and generation history are project-scoped and reusable.
- Scheduled tasks and connectors let the agent run and publish work outside a single chat turn.
- MCP/CLI integration lets external agents generate media, train characters, and browse creation history without users managing provider API keys directly.
- Media generation is asynchronous; agents poll for results.

The deeper infrastructure story is partly inferred. The dialogue and local Hermes plan converge on a plausible stack: message/event ingress, identity and tenant policy, sandbox runtime, workspace volume mounting, tool runtime, credential boundary, async job orchestration, GPU/media workers, asset storage, observability, and egress governance. For Hermes, this shape is useful; for claims about Higgsfield production internals, it remains unverified.

## Observed Product Surface

| Surface | Evidence | Architecture implication |
|---|---|---|
| Chat/task workspace | Visible UI | There is a persisted task/thread model rather than a stateless prompt box. |
| New task/New chat | Visible UI | Task metadata and chat history are first-class entities. |
| Model picker | July endpoint returned 24 entries for this account/session and marked Higgsfield Free orchestrator as default; an older UI snapshot showed `Google Gemini 3.5 Flash`. | Runtime can bind a turn to a catalog entry; eligibility filtering and automatic routing policy are unknown. |
| Auto Run | Visible UI | The UI supports queued execution without manual send per step. |
| Usage | Visible UI and public credit FAQ | Billing/usage is exposed as a workspace control surface. |
| Scheduled | Visible UI and public page | Workflows can run on timers outside active chat. |
| Gallery | Visible UI | Generated media outputs are persisted and browsable. |
| Skills | Visible UI and public page | Workflows are packaged and reusable. |
| Connectors | Visible UI and public pages | External OAuth/data/publishing tools are part of the agent runtime. |
| Files | Visible UI and public page | Project assets, revisions, briefs, and outputs persist across chats. |
| Memory | Visible UI and public page | Cross-session context is a product feature, not just chat transcript. |

## End-to-End Flow

1. User opens a Supercomputer task in the browser.
2. The browser posts a user message, then polls an ordered cursor for assistant chunks.
3. Authenticated API responses expose user/workspace state; exact server-side binding and authorization are not visible.
4. The turn uses the selected/default model entry; automatic model/tool routing policy is unknown.
5. The agent can load tools and Skills dynamically. Exact Memory, file, and prior-asset context injection is unknown.
6. Connector tools expose discovery/connect/call contracts, but this audit did not execute a third-party connector.
7. If the task needs media generation, the agent submits an asynchronous generation job.
8. The submit result exposes a job credit estimate; plan, concurrency, reservation, and capture checks are not visible.
9. An unknown generation backend produces images, videos, or audio.
10. The agent polls media job status separately from browser chat polling.
11. Completed outputs land in the project/gallery and can be reused as inputs.
12. Scheduled execution was not tested; only its UI and agent-facing control contract were observed.

## Component Decomposition

### 1. Web UI and Session Layer

Evidence level: Visible UI, Public Higgsfield, Local Hermes plan.

Responsibilities:

- Render task list, chat transcript, model picker, usage, scheduled tasks, gallery, skills, connectors, files, and memory.
- Maintain a long-lived task/thread identity.
- Render polled agent chunks, tool progress, queued prompts, and generated media status.
- Keep project assets visible and reusable.

Public product behavior strongly supports a task/session model: Supercomputer plans creative work, picks models/presets, shows cost before rendering, and stores finished generations in a project.

July 22 resolved the primary transport question for the tested path: user-message POST plus cursor polling. Still unknown are ordering/replay guarantees, chunk retention, backpressure, and whether other product paths use different transports.

Open questions:

- Exact event schema for tool progress, queued prompts, media status, and errors beyond the captured successful path.
- Whether Gallery is a projection of project assets, a separate feed, or both.

### 2. Identity, Tenant, and Project Boundary

Evidence level: Public Higgsfield, Visible UI, Inference, Local Hermes plan.

Responsibilities:

- Bind user, task, project, workspace, plan, and credits.
- Ensure generated assets and connected accounts cannot cross tenant/project boundaries.
- Provide OAuth-backed connector access.
- Surface usage/credit state before expensive generation.

The social connectors page says external accounts are connected through Connectors/OAuth and credentials are not stored as plain text. The Supercomputer public page says every generation lands in a project and credit cost is shown upfront.

Implementation implication for Hermes:

- Use a first-class membership/project model.
- Enforce tenant filters in application code and DB row-level security.
- Treat plan/credit state as a policy input before tool execution, not only after job submission.

### 3. Agent Orchestrator and Model Router

Evidence level: Public Higgsfield, Dialogue claim, Inference.

Responsibilities:

- Turn plain-language briefs into plans.
- Choose models and presets automatically when the user does not specify them.
- Decide when to call tools, skills, connectors, and media generation jobs.
- Preserve enough context to reference prior outputs by phrases like "the third one".

Public pages describe an Orchestrator that picks the best-fit model for each step, and an in-house reasoning layer that plans narrative structure, camera motion, pacing, and visual consistency. The interactive task used `Google Gemini 3.5 Flash`, showing that task turns can be bound to a specific model.

For Hermes, keep this provider-neutral:

- Do not hardcode Higgsfield-only model names into core orchestration.
- Use a model capability registry: image, video, audio, planning, coding, vision, cost, latency, quality, context.
- Record model choice and rationale in audit/job metadata.

### 4. Skills Runtime

Evidence level: Public Higgsfield, Visible UI, Local Hermes plan.

Responsibilities:

- Package workflows as installable units.
- Trigger workflows via slash-style commands.
- Reuse and share workflows across projects/teams.
- Version workflows like code.

Public Supercomputer pages describe Skills as workflows like `/montage`, `/cinematic`, and brand pipelines. The local Hermes plan maps this to a `LangGraph OSS library + custom Skill Registry` design, where Skill is a workflow rather than only a prompt.

Design implication:

- A Skill should expose public metadata, inputs, and outputs.
- Internal prompts, references, and implementation recipes should have a protected boundary.
- Skill execution should create traceable steps, not an opaque prompt expansion.

### 5. Files, Memory, and Context Retrieval

Evidence level: Public Higgsfield, Visible UI, Local Hermes plan.

Responsibilities:

- Persist briefs, assets, revisions, generated media, and reusable references.
- Let the agent retrieve project context across sessions.
- Import memory/skills from Claude, Claude Code, Codex, and ChatGPT.
- Support prompts that refer to prior project assets without re-uploading them.

Public docs say every asset/revision/brief is saved into the project and that memory/skills can be imported from other agents. The visible UI exposes separate Files and Memory sections.

Design implication:

- Keep file storage and semantic memory separate but linkable by asset/session/task IDs.
- Do not assume memory retrieval has permission to read every project file.
- Store lineage: source upload, generated output, derived output, connector publication.

### 6. Connector Layer

Evidence level: Public Higgsfield, Visible UI.

Responsibilities:

- Connect external accounts and APIs through OAuth or scoped credentials.
- Fetch data, prepare media, publish content, and return status/errors.
- Support scheduled flows and platform-specific media requirements.

The public connectors page names X, Threads, and Instagram as available in Supercomputer and describes status tools such as container/status checks. It also states that each platform's rate limits still apply.

Design implication:

- Connectors need durable credential records, scoped permissions, per-platform rate limits, and explicit status/error APIs.
- Publishing tools must not silently swallow failures; failed media processing should return exact status/error data.

### 7. Media Generation Tool Layer

Evidence level: Dialogue claim, Public Higgsfield CLI, Local Hermes plan.

Declared or discussed tools from the Supercomputer dialogue:

| Tool | Dialogue-described role | Evidence caveat |
|---|---|---|
| `higgsfield_generate` | Submit image/video generation jobs in batch format. | Dialogue claim; public CLI supports generation through MCP but not this exact schema. |
| `higgsfield_job_status` | Poll generation jobs. | Consistent with public CLI async polling FAQ. |
| `higgsfield_upload` | Upload local files; visible schema included `files` and `concurrency`. | Dialogue claim. |
| `higgsfield_attachments_list` | List created images, videos, and uploaded files. | Dialogue claim; consistent with history browsing. |
| `higgsfield_element` | Manage persistent character/environment/prop references. | Dialogue claim; public product supports reusable assets, but not this tool contract. |
| `higgsfield_soul_id` | Manage trained face identity models. | Dialogue claim; public Soul ID exists, exact MCP tool schema unknown. |
| `higgsfield_enhancer` | Compile structured inputs into production prompts. | Dialogue claim; consistent with public prompt/preset/orchestrator behavior. |
| `higgsfield_balance` | Retrieve workspace balance/concurrency details. | Dialogue claim; consistent with credit/usage surfaces. |

Local docs also mention `higgsfield_project_set`, `higgsfield_project_create`, and `higgsfield_audio_generate`; these were not confirmed in the current dialogue capture.

Design implication:

- Treat media tools as asynchronous job creators, not direct blocking model calls.
- Return stable `job_id`, `asset_id`, `project_id`, `status`, `cost`, and error metadata.
- Separate asset upload from generation submission so previous assets can be reused without re-upload.

### 8. Asset References, Elements, and Soul ID

Evidence level: Public Higgsfield, Dialogue claim, Inference.

Public evidence:

- Soul 2.0 supports text-to-image and image reference.
- Soul ID creates a consistent digital character from user photos.
- Public FAQ says Soul ID needs at least 20 photos and training takes about 3 minutes.
- Public CLI says agents can train characters and browse creation history.
- Public pages mention Soul HEX and moodboards as style/color/reference controls.

Dialogue claims:

- Reference Elements are persistent assets classified as character, environment, or prop.
- Element references may be inserted as an element tag with a UUID.
- Soul ID can be passed as an identifier into a generation model.
- One dialogue answer claimed Soul ID accepts 1 to 100 portrait files, while the public Soul page says Soul ID needs at least 20 photos. Treat the public requirement as stronger product evidence until live UI/API tests prove otherwise.
- Generated images use `image_job`; generated videos use a video job type such as `seedance_2_0_job`; uploaded images use `media_input`.

Reconciliation:

- The existence of character consistency and previous-output reuse is high confidence.
- The exact internal placeholder syntax and type names are not verified.
- Avoid writing "LoRA" into product architecture as fact. Public pages say Soul ID trains a character/identity; the dialogue explicitly warned that terms like LoRA, adapters, ControlNet, and latent cross-attention were not literal product terms in its visible system context.

Design implication:

- Use neutral names: `identity_reference`, `asset_reference`, `style_reference`, `media_input`, `generation_output`.
- Keep provider-specific names at plugin boundaries.
- Track whether references are user-uploaded, generated, trained identity, style/moodboard, or connector-derived.
- See `docs/hermes-soulid-element-asset-model.md` for the local asset model and `docs/hermes-soulid-reproduction-and-test-plan.md` for experiments around Soul ID behavior.

### 9. Job Orchestration, Scheduling, and Concurrency

Evidence level: Public Higgsfield, Dialogue claim, Local Hermes plan.

Public evidence:

- Generation runs asynchronously and the agent polls for results.
- Scheduled tasks can run daily, weekly, or at a specified future time.
- Credit cost is shown before rendering.

Dialogue claims:

- Text-to-image concurrent job cap: 8.
- Image-to-video concurrent job cap: 6.
- Boost may raise workspace concurrency, possibly up to about 24 jobs.
- The agent chunks batches and polls job status before submitting more.

Local Hermes plan:

- Use Temporal for durable session/job orchestration.
- Use NATS JetStream for realtime fanout.
- Use Kueue + NVIDIA GPU Operator for GPU job admission, queueing, quota, priority, and preemption.

Design implication:

- Model expensive media generation as durable workflows with idempotency keys.
- Make concurrency category-specific: text/image/video/audio/connectors may have different caps.
- Keep Boost/concurrency as policy data, not hardcoded constants.
- Polling is acceptable for MCP/agent clients; UI can use realtime fanout for progress.

### 10. Credential Boundary and Token Routing

Evidence level: Public Higgsfield CLI, Dialogue claim, Local Hermes plan.

Public evidence:

- The CLI/MCP page says users do not manage API keys; they authenticate with their Higgsfield account.
- Connector pages describe OAuth-based account connection.

Dialogue/local architecture claims:

- A sandbox or agent runtime gets a short-lived `HF_JWT_TOKEN`, not raw provider keys.
- A TokenRouter-like boundary validates token claims, checks scope/plan/quota, audits requests, and exchanges for upstream provider credentials.
- Provider keys stay inside a vault/secrets boundary.

Design implication for Hermes:

- This is a strong pattern even if Higgsfield's exact service name is unverified.
- Implement as `TokenRouter / Secrets Boundary` backed by OpenBao per existing plan.
- Do not let sandbox environment variables or files contain real provider keys.
- TokenRouter must enforce scope, asset ACL, model allowlist, concurrency, redaction, and audit.
- See `docs/hermes-tokenrouter-credential-flow.md` for the local four-stage credential-flow design.

Unknowns:

- Actual Higgsfield token TTL, scope format, refresh behavior, and key exchange mechanism.
- Whether `TokenRouter` is an internal Higgsfield name, a local planning name, or a model-derived label.

### 11. Media Gateway and Large Binary Processing

Evidence level: Dialogue claim, Local Hermes plan, Inference.

The dialogue described `CometAPI` as a separate media gateway for video/audio blobs: proxying remote media, decoding containers, extracting frames/audio, downsampling, and delivering multimodal inputs without token explosion. Existing local docs also mark `CometAPI` as not planned for the current Hermes phase.

This is plausible, but not publicly verified.

Design implication:

- For Hermes MVP, do not build a dedicated `CometAPI` service until direct need is proven.
- Use object storage plus worker-side media preprocessing first.
- If the media plane grows, split it into a media ingress/preprocess service with scoped URLs, file scanning, frame extraction, and transcode queues.
- See `docs/hermes-cometapi-media-gateway.md` for the future media data-plane design and MVP deferral rule.

### 12. Observability, Audit, and Failure Model

Evidence level: Public Higgsfield social connectors, Local Hermes plan, Inference.

Responsibilities:

- Trace prompt, tool call, job submission, model/provider request, asset registration, and connector publication.
- Surface exact errors for generation and publication failures.
- Record cost/credit estimates and actual spend.
- Support scheduled-run audit and retries.

Public connector docs explicitly mention status/error APIs for media publishing. Local Hermes plan chooses OpenTelemetry + Grafana LGTM and requires failed media jobs to be traceable from `job_id` to TokenRouter decision and worker log.

Design implication:

- Every media job should have a trace ID and idempotency key.
- Connector failures should be structured errors, not warnings with partial fallback.
- Audit events should distinguish user action, scheduled action, agent tool call, and backend retry.

## Confidence Matrix

| Claim | Confidence | Evidence |
|---|---:|---|
| Supercomputer is a chat agent that plans creative workflows and chooses models/presets. | High | Public Higgsfield. |
| Skills, Connectors, Files, Memory, Scheduled Tasks, Usage, and Gallery are product surfaces. | High | Visible UI + Public Higgsfield. |
| Media generation is async and polled by the agent. | High | Public CLI + dialogue. |
| Prior generations can be reused as inputs. | High | Public CLI + public Supercomputer project storage. |
| Soul ID exists for character/identity consistency. | High | Public Soul pages. |
| Soul ID exact backend implementation is LoRA/adapters/ControlNet. | Low | Not verified; avoid claim. |
| `default_api` exposes native agent tools in the inspected task output. | Medium-High | Visible UI output in authenticated Supercomputer tasks. |
| `higgsfield_generate` and `higgsfield_job_status` exist as exposed/default_api tools in inspected tasks. | Medium | Visible UI/dialogue output; consistent with public MCP behavior. |
| Exact `higgsfield_*` parameter schemas are fully known. | Low | Several schemas were captured, but public API stability and hidden backend fields are not verified. |
| Text-to-image cap 8 and image-to-video cap 6 are current. | Medium-Low | Dialogue claim only; may vary by plan/account/date. |
| Boost raises concurrency to around 24. | Low | Dialogue/local-doc claim; no public confirmation. |
| TokenRouter is Higgsfield's actual internal service name. | Low | Dialogue/local-doc claim; treat as pattern, not fact. |
| A TokenRouter-like secret boundary is the right Hermes design. | High | Public no-API-key behavior + local plan. |
| CometAPI exists as a production Higgsfield media gateway. | Low | Dialogue/local-doc claim only. |
| A separate media preprocessing plane may be needed eventually. | Medium | Inference from large media workflows. |

## Mermaid Architecture Sketch

This is the original Hermes-oriented conceptual sketch. Its boxes are design boundaries, not verified Higgsfield production services; the July corrected observable diagram above is the evidence-backed view.

```mermaid
flowchart TD
    user[User] --> ui[Supercomputer Web UI]
    ui --> session[Task and Realtime Session]
    session --> auth[Identity, Workspace, Project, Plan]
    auth --> orchestrator[Agent Orchestrator and Model Router]

    orchestrator --> memory[Memory and Project Context]
    orchestrator --> files[Files and Gallery Assets]
    orchestrator --> skills[Skill Runtime]
    orchestrator --> connectors[Connector Layer]
    orchestrator --> media_tools[Media Tool Layer]

    connectors --> oauth[OAuth and Connector Credentials]
    connectors --> external_platforms[X, Threads, Instagram, Drive, Notion, etc.]

    media_tools --> policy[Policy, Credits, Concurrency]
    policy --> secrets[TokenRouter-like Secrets Boundary]
    secrets --> providers[In-house and Partner Models]
    media_tools --> jobs[Async Job Orchestration]
    jobs --> gpu[GPU and Media Workers]
    gpu --> storage[Object and Project Asset Storage]
    storage --> gallery[Gallery and Reusable References]
    gallery --> orchestrator

    jobs --> events[Realtime Events and Polling]
    events --> ui

    scheduled[Scheduled Tasks] --> orchestrator
    audit[Audit and Observability] --- session
    audit --- connectors
    audit --- media_tools
    audit --- jobs
```

## Unknowns To Verify

| Area | Question | Suggested verification |
|---|---|---|
| Web transport | Resolved for the tested chat path: message POST plus cursor polling; other surfaces remain unknown. | Re-test after major UI/API releases. |
| Tool schemas | Exact `higgsfield_*` tool names and JSON schemas. | Inspect MCP manifest/tool list from authenticated client if available. |
| Concurrency | Current caps by plan and media type. | Query Usage/Balance UI and run controlled generation tests. |
| Credits | One submit exposed `job_credits: 1`, but capture/refund and balance delta remain unknown. | Obtain a read-only billing ledger or documented charge-state contract. |
| Asset refs | Stable types for upload, image job, video job, element, Soul ID. | Use history reuse in a test generation and inspect request payloads. |
| Soul ID | Training minimum, file limits, status states, output reference type. | Cross-check public FAQ against live UI/API. |
| Connectors | Credential storage, refresh, scopes, rate-limit error model. | Add a test connector and inspect OAuth scopes/status surfaces. |
| Token boundary | Whether `TokenRouter` is an actual service name. | Avoid assuming; verify only through code/API/network evidence. |
| Media gateway | Whether `CometAPI` exists. | Avoid implementing as named service until product/API evidence exists. |
| Scheduling | Retry policy, timezone handling, skipped-run behavior. | Create a harmless scheduled task and inspect run history. |

## Hermes Implementation Implications

Use the Higgsfield research as product-shape inspiration, but keep Hermes implementation provider-neutral.

Recommended Hermes slice:

1. Realtime session ingress with task/project identity.
2. TokenRouter-like boundary backed by OpenBao.
3. Project asset store with reusable media references.
4. Async media job workflow with status polling and event fanout.
5. Skill registry and minimal workflow runtime.
6. Connector authorization model and structured status/errors.
7. Observability/audit across prompt, tool, job, asset, connector, and spend.

Notion refresh follow-up docs:

- `docs/hermes-references-knowledge-model.md`
- `docs/hermes-tokenrouter-credential-flow.md`
- `docs/hermes-cometapi-media-gateway.md`
- `docs/hermes-soulid-element-asset-model.md`
- `docs/hermes-soulid-reproduction-and-test-plan.md`
- `docs/hermes-tool-contracts-from-notion.md`

Explicitly defer:

- A dedicated `CometAPI` media gateway.
- Boost/concurrency upsell mechanics.
- Soul ID training implementation.
- Zero-upload feature-vector caching.
- Higgsfield-specific tool names in core modules.

Provider-neutral names to prefer:

| Avoid as core name | Prefer |
|---|---|
| `higgsfield_generate` | `media_job_create` |
| `higgsfield_job_status` | `media_job_status` |
| `higgsfield_element` | `asset_reference_create` |
| `higgsfield_soul_id` | `identity_reference_train` |
| `higgsfield_balance` | `workspace_usage_get` |
| `CometAPI` | `media_ingress` or `media_preprocess` |
| `TokenRouter` if uncertain | `credential_router` or `tool_gateway` internally, while preserving the existing Hermes TokenRouter plan if already adopted. |

## Notes From The Dialogue Session

- Chinese input through Computer Use dropped many characters, so the successful interview prompts were sent in English.
- `legacy_fresh_task` returned a complete decomposition with skill categories, toolsets, layer claims, a Mermaid diagram, and unknowns.
- `legacy_primary_task` returned useful concurrency/tool/ref claims, then later queued follow-ups displayed `Viewing skill backend-architecture-explainer` without additional usable text before this dossier was updated.
- The dialogue repeatedly invoked `backend-architecture-explainer`, so its answers should be treated as architecture explanation output, not privileged internal proof.
