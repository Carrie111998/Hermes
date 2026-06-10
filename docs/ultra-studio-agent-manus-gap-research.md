# Ultra Studio Agent vs Manus: Gap Research

Date: 2026-06-10

Scope: compare the current Ultra Studio / Hermes fork architecture with Manus-style general agents, then identify what should be added for a real creative production agent.

## Sources Checked

- Manus Sandbox: `https://manus.im/blog/manus-sandbox`
- Manus Cloud Browser docs: `https://manus.im/docs/features/cloud-browser`
- Manus Desktop / My Computer docs: `https://manus.im/docs/features/desktop`
- Manus Skills docs: `https://manus.im/docs/features/skills`
- Manus Agent Skills blog: `https://manus.im/blog/manus-skills`
- OpenManus GitHub: `https://github.com/FoundationAgents/OpenManus`
- Codex Agent Skills docs: `https://developers.openai.com/codex/skills`
- Microsoft Agent Skills docs: `https://learn.microsoft.com/en-us/agent-framework/agents/skills`

## Core Finding

Manus is not differentiated mainly by model access. Its product architecture is a task computer:

1. A persistent sandbox computer per task.
2. Browser, file system, shell, artifacts, and long-running task lifecycle.
3. Skill packages loaded progressively.
4. Human takeover / approval paths for sensitive actions.
5. Artifact visibility and sharing rules.

Ultra Studio already has the right creative/media direction, but it still needs stronger task-computer infrastructure around the creative agent.

## Gap 1: Sandbox Lifecycle Is Too Shallow

Manus documents a sandbox lifecycle:

- create on demand
- sleep / awake
- recycle / recreate
- restore important files after recycle
- keep task artifacts visible

Ultra Studio currently marks `Sandbox VM` and `Hermes Cloud Attach`, but does not define lifecycle semantics.

Required design:

- `sandbox.create`
- `sandbox.attach`
- `sandbox.sleep`
- `sandbox.wake`
- `sandbox.recycle`
- `sandbox.restore_artifacts`
- persisted manifest of restored vs non-restored files

P0 because long video jobs, asset generation, and local file workflows cannot be reliable without task lifecycle.

## Gap 2: Artifact Browser / Task File System

Manus exposes files created during task execution through a "view all files" style entry. This is different from an asset gallery: it includes intermediate files, code, downloaded media, generated reports, and final artifacts.

Ultra Studio has Asset Library direction, but should also include task workspace files.

Required design:

- `task_files` panel
- file tree by session / job / sandbox
- downloadable artifact bundle
- distinction between `task_file`, `asset`, `reference`, `protected_skill_reference`
- one-click "promote to asset library"

P0 because users need to inspect intermediate videos, prompt JSON, storyboards, scripts, and logs.

## Gap 3: Cloud Browser and Local Browser Split

Manus has Cloud Browser for autonomous web work and a path to use local/desktop context when cloud browser is blocked or sensitive.

Ultra Studio should not treat browser access as a generic web tool. Creative workflows need:

- competitor ad research
- product page extraction
- social media trend lookup
- account-authenticated source retrieval
- bot-detection sensitive browsing

Required design:

- `cloud_browser` for general web research
- `my_browser` / local browser bridge for logged-in or bot-sensitive sites
- explicit user takeover state
- per-site session controls
- browser artifacts captured into the asset library

P1 for MVP, P0 if product/UGC workflows need real ecommerce/social-media scraping.

## Gap 4: Local Desktop Bridge

Manus Desktop can operate inside user-approved local folders and use local CLI tools, with explicit approvals for commands.

Ultra Studio currently focuses on cloud/runtime. But creative users will have local media folders, design files, and GPU tools.

Required design:

- folder-scoped local connector
- command approval policy: allow once / always allow / deny
- local file import into project asset library
- local render/export handoff
- local GPU optional route, never assumed

P1. This should not block cloud MVP, but it is important for a serious production agent.

## Gap 5: Skill Progressive Disclosure and Skill Packaging

Manus and Agent Skills use progressive loading:

- Level 1: tiny metadata loaded at startup
- Level 2: `SKILL.md` instructions loaded when triggered
- Level 3: scripts, references, assets loaded only on demand

Ultra Studio already created first skills, but still needs packaging discipline.

Required design:

- each production workflow has `SKILL.md`
- `references/` for schema, prompt compiler rules, rubrics, examples
- `scripts/` for deterministic compilers / validators
- `assets/` for templates
- skill trigger tests
- skill eval fixtures
- skill versioning and changelog

P0 for reliable creative routing. Without this, the agent will keep overloading the main prompt.

## Gap 6: Human Approval / Takeover Controls

Manus exposes human takeover for browser actions and explicit approval for local desktop commands. Ultra Studio needs the same idea for expensive or risky actions.

Required approval gates:

- spend credits / boost concurrency
- submit long GPU video job
- use paid external model
- upload local/private media
- access logged-in browser session
- run local command
- publish/share/export externally

P0 for cost, privacy, and trust.

## Gap 7: Sharing and Collaboration Privacy

Manus distinguishes conversation sharing from sandbox sharing. Recipients can see messages/artifacts but not the full private sandbox. Connectors are disabled in collaboration.

Ultra Studio needs this because creative sessions will contain private product images, brand references, and paid-model outputs.

Required design:

- share conversation
- share selected artifacts only
- never share sandbox filesystem by default
- connector/session credentials disabled in collaboration mode
- asset ACL and redaction review before sharing

P1 for MVP, P0 before team/collaboration launch.

## Gap 8: Provenance and Anti-Fake Output

External tests of autonomous agents often fail because agents fabricate data, simulate completed work, or hide weak evidence. Ultra Studio already has the principle "no fake demo", but it needs product-level enforcement.

Required design:

- every media card shows source job id, model, provider, prompt hash, inputs, status
- every claim in research mode links to a source or asset
- no markdown-only generated URL as final proof
- failed job has trace: request -> TokenRouter decision -> worker log -> asset state
- QA report must separate observed output from inferred quality

P0. This is a core trust layer.

## What This Adds to the Current Architecture

Add these boxes to the architecture roadmap:

1. Sandbox Lifecycle Manager
2. Task File Browser
3. Artifact Bundle Export
4. Cloud Browser / Local Browser split
5. Local Desktop Bridge
6. Skill Registry with progressive disclosure
7. Skill Eval Harness
8. Human Approval Gateway
9. Collaboration Privacy Boundary
10. Provenance / Evidence Ledger

## Recommended Build Order

P0:

1. Skill Registry + progressive disclosure structure.
2. Sandbox lifecycle semantics, even if first implementation maps to local process/session.
3. Task File Browser and artifact promotion into Asset Library.
4. Human Approval Gateway for credits, uploads, browser auth, external publish, local command.
5. Provenance ledger for every media and research result.

P1:

1. Cloud Browser with artifact capture.
2. Local Browser bridge for logged-in/bot-sensitive websites.
3. Local Desktop folder connector.
4. Collaboration/share privacy model.
5. Skill eval harness for route correctness and output contract.

P2:

1. Full local GPU route.
2. Public project sharing.
3. Scheduled recurring creative tasks.
4. Automated skill packaging from successful sessions.

## Bottom Line

Ultra Studio should not become only an Atlas media generator with a chat UI. To compete with Manus-style agents, it needs a task-computer layer:

chat + sandbox + files + browser + approvals + skills + artifacts + provenance.

The current architecture has the media/provider side mostly identified. The missing product depth is task lifecycle, workspace filesystem, approval, provenance, and progressive skill packaging.

## Round 2: Broader Agent Infrastructure Research

This round expands beyond Manus. The goal is to identify missing product and infrastructure layers from adjacent agent systems: sandbox providers, browser-agent platforms, durable execution systems, skill standards, and media-generation APIs.

Additional sources checked:

- E2B docs: `https://e2b.dev/docs`
- Daytona sandbox docs: `https://www.daytona.io/docs/en/sandboxes/`
- Browserbase Contexts: `https://docs.browserbase.com/platform/browser/core-features/contexts`
- Browserbase AI browser-agent platform: `https://www.browserbase.com/industry/ai`
- OpenAI Operator: `https://openai.com/index/introducing-operator/`
- Anthropic computer use: `https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool`
- LangChain HITL middleware: `https://docs.langchain.com/oss/python/langchain/human-in-the-loop`
- Temporal durable execution: `https://temporal.io/blog/what-is-durable-execution`
- Inngest AI orchestration: `https://www.inngest.com/ai`
- Runway API docs: `https://docs.dev.runwayml.com/api/`
- Runway input asset docs: `https://docs.dev.runwayml.com/assets/inputs/`
- fal queue API docs: `https://fal.ai/docs/documentation/model-apis/inference/queue`
- Codex skills docs: `https://developers.openai.com/codex/skills`
- Microsoft Agent Skills docs: `https://learn.microsoft.com/en-us/agent-framework/agents/skills`

## New Finding A: Sandbox Should Be a Product Primitive, Not an Implementation Detail

E2B describes a sandbox as a fast secure Linux VM created on demand for an agent. Daytona goes further: a sandbox is a composable computer with its own kernel, filesystem, network stack, vCPU, RAM, and disk.

Implication for Ultra Studio:

- `Sandbox VM` should become a user-visible execution surface, not just a backend box.
- Users should see what machine/session a job ran in.
- Files, logs, generated code, prompt JSON, and outputs should be browseable from the session.
- Sandbox state needs a policy: temporary, restorable, promotable to assets, or disposable.

New required component:

`Task Computer Service`

Responsibilities:

- sandbox lifecycle
- task filesystem manifest
- process log capture
- browser/session attachment
- artifact bundle export
- promotion from task file to asset library

## New Finding B: Browser Sessions Need Persistent Contexts

Browserbase Contexts persist cookies, authentication, localStorage, IndexedDB, and other site data across sessions. Browserbase also separates browser sessions, search/fetch APIs, runtime sandboxes, agent identity, and gateway.

Implication for Ultra Studio:

- Browser is not one tool. It is a set of primitives.
- A creative agent needs persistent browser contexts for ecommerce, social-media, ads libraries, inspiration boards, and logged-in tools.
- Each context must be workspace-scoped and revocable.

New required components:

1. `Browser Context Store`
2. `Browser Session Manager`
3. `Browser Artifact Capture`
4. `Agent Identity / Credential Boundary`

Browser outputs should be first-class:

- screenshots
- downloaded media
- extracted product metadata
- page HTML / markdown snapshots
- source URL and timestamp
- authentication context id

## New Finding C: Computer Use Requires Stronger Safety Than Tool Calling

OpenAI Operator and Anthropic computer use both emphasize human takeover or confirmation for sensitive actions. Anthropic's docs explicitly call out dedicated virtual machines/containers, domain allowlists, avoiding sensitive data exposure, and human confirmation for real-world consequences.

Implication for Ultra Studio:

- A normal `tool_call` permission model is too weak for browser and desktop control.
- We need action categories and approval policies.

Required approval policy:

| Action type | Default |
|---|---|
| Read public page | allow |
| Download public asset | allow with provenance |
| Use logged-in browser context | ask |
| Upload private media | ask |
| Spend credits / Boost | ask |
| Start long GPU job | ask |
| Run local command | ask |
| Publish / post / email / checkout | require explicit approval |
| Financial / legal / high-stakes decision | block or manual-only |

New required component:

`Human Approval Gateway`

It should support `approve`, `edit`, `reject`, and `respond` decisions, matching common HITL patterns.

## New Finding D: Durable Execution Is the Missing Backbone for Long Media Jobs

Temporal defines durable execution as crash-proof execution. Inngest positions AI orchestration around tool loops, sub-agents, human-in-the-loop, reliable orchestration, observability, and external tools/models.

Implication for Ultra Studio:

- Chat should not hold long-running creative work in memory.
- Media generation should be a durable workflow with checkpoints.
- Human approval should pause a job without losing state.
- Worker crashes should resume from the last durable step.

Required workflow shape:

```text
message received
  -> route skill
  -> compile prompt
  -> request approval if needed
  -> upload/resolve assets
  -> create provider job
  -> poll/webhook result
  -> register asset
  -> run QA
  -> notify chat/session
```

New required components:

1. `Durable Workflow Engine`
2. `Media Job Event Log`
3. `Workflow Checkpoints`
4. `Resume / Retry / Cancel / Compensate`
5. `Approval Pause State`

## New Finding E: Media APIs Converge on Queue + Status + Webhook + Upload

fal exposes queue submit, status, streaming updates, results, cancellation, and webhooks. Runway's API surface includes generation endpoints, task management, uploads, and strict input asset constraints such as URL/data URI/ephemeral upload formats and size limits.

Implication for Ultra Studio:

- Atlas provider tools should not be exposed raw to the agent as the only abstraction.
- We need a provider-neutral media job envelope.
- Provider-specific constraints must be known before prompt compilation.

Required schema:

```yaml
MediaJob:
  job_id:
  provider:
  model:
  media_type:
  mode:
  status:
  inputs:
  provider_constraints:
  prompt:
  negative_prompt:
  seed:
  created_by_session:
  tokenrouter_decision_id:
  output_assets:
  error:
```

Required tools:

- `ultra_media_job_create`
- `ultra_media_job_status`
- `ultra_media_job_cancel`
- `ultra_media_job_retry`
- `ultra_media_job_finalize`
- `ultra_media_constraints_get`

## New Finding F: Skill Standards Require Registries, Not Just Folders

Codex, Microsoft Agent Skills, and Manus converge on the same shape:

- `SKILL.md`
- optional `scripts/`
- optional `references/`
- optional `assets/`
- progressive disclosure
- concise trigger descriptions
- focused skills

Implication for Ultra Studio:

- The current first skills are a good start but not enough.
- We need a `Skill Registry` that can filter, route, version, evaluate, and disable skills.
- Skill descriptions must be short enough to survive truncation but precise enough for routing.

Required components:

1. `Skill Registry`
2. `Skill Allowlist Profile`
3. `Skill Version Manifest`
4. `Skill Trigger Eval`
5. `Skill Output Contract Eval`
6. `Skill Resource Loader`

Recommended folder shape:

```text
skills/creative/product-md-flow/
  SKILL.md
  references/
    route.md
    inputs.schema.json
    prompt-compiler.md
    qa-rubric.md
    examples.md
  scripts/
    compile_prompt.py
    validate_inputs.py
  assets/
    storyboard-template.json
```

## New Finding G: Browser/Computer Agents Need Observation Loops

Anthropic computer use recommends checking after each step and not assuming success. Browser-agent platforms similarly rely on screenshot/DOM observation, logs, and recovery.

Implication for Ultra Studio:

- Every action-heavy tool should return an observation, not just a success flag.
- UI should show observation deltas: screenshot, DOM summary, stdout, file diff, media preview, job status.

Required component:

`Observation Ledger`

Records:

- action requested
- action executed
- observation before/after
- result evidence
- error evidence
- retry decision

This complements the earlier `Provenance Ledger`; provenance is about asset lineage, observation is about agent action truth.

## Revised Architecture Additions

Add these to the roadmap:

1. `Task Computer Service`
2. `Browser Context Store`
3. `Browser Session Manager`
4. `Human Approval Gateway`
5. `Durable Workflow Engine`
6. `Media Job Event Log`
7. `Provider Constraint Registry`
8. `Skill Registry`
9. `Skill Eval Harness`
10. `Observation Ledger`
11. `Provenance Ledger`
12. `Artifact Promotion Pipeline`

## Revised P0 Build Order

1. Define `MediaJob` and provider constraint schema.
2. Add durable media job event log with `create/status/cancel/retry/finalize`.
3. Add human approval gateway for cost, private media, browser auth, local commands, and publish actions.
4. Add task file browser and artifact promotion into Asset Library.
5. Add skill registry with allowlist, version manifest, and trigger evals.
6. Add observation/provenance ledger to prevent fake completion.

## Revised P1 Build Order

1. Browser Context Store and cloud browser session manager.
2. Durable workflow engine integration if the first event log is not enough.
3. Local desktop bridge with folder-scoped access and command approval.
4. Skill scripts for prompt compilation and input validation.
5. Collaboration privacy boundary and share artifact flow.

## Design Principle After Round 2

Ultra Studio should be a creative task computer:

```text
chat
  + sandbox/task filesystem
  + browser contexts
  + durable media jobs
  + human approvals
  + skill registry
  + provider constraints
  + artifact/provenance ledgers
  + asset library
```

The agent should never be allowed to claim completion without an artifact, observation, or ledger record that proves what happened.
