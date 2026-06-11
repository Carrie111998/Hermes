# Ultra Studio Infrastructure Design Research Pack

Status: infrastructure design research  
Date: 2026-06-10  
Scope: Ultra Studio as a cloud creative agent built on the Hermes fork.

## Why This Is Not Component Documentation

你刚才说的是“基建”，不是 UI 组件。这个包只写底座：

- control plane: Edge Gateway, Identity, OPA, TokenRouter, approvals, quota.
- execution plane: sandbox, browser context, filesystem mount, durable jobs, event bus, GPU workers.
- data plane: CometAPI, asset library, object storage, relational state, memory/files/marketplace.
- security and ops: secrets, egress, audit, observability, service mesh, GitOps.

UI 文档只说明用户能看到什么；本包说明这些能力由哪些基础设施承担、谁拥有状态、边界如何防止越权、失败如何验证。

## Status Legend

| Color | Meaning | Evidence standard |
|---|---|---|
| Green | Existing code or already landed behavior. | Repo code path, committed doc, or runnable local path. |
| Yellow | Existing doc, local prototype, or design intent, but not wired into runtime. | Spec/prototype exists; startup/runtime contract incomplete. |
| Red | P0/P1 infrastructure gap. | Needed for real cloud agent but not implemented. |

Do not treat yellow as production capability. It is a routing anchor for implementation.

## Document Map

| Doc | Purpose |
|---|---|
| [01-reference-research.md](01-reference-research.md) | Research notes from Manus, E2B, Browserbase, OpenAI/Anthropic computer use, Temporal, LangGraph, media queue APIs, and existing Hermes. |
| [02-boundary-map.md](02-boundary-map.md) | Full infrastructure layer map, state ownership, source of truth, and current gap status. |
| [03-control-plane-design.md](03-control-plane-design.md) | Edge, identity, policy, TokenRouter, quota, approvals, and Gateway/Session contracts. |
| [04-execution-plane-design.md](04-execution-plane-design.md) | Sandbox, browser, filesystem, session/job orchestration, event bus, and GPU scheduling. |
| [05-data-plane-design.md](05-data-plane-design.md) | CometAPI, asset library, storage, Postgres RLS, media job lineage, memory/files/marketplace. |
| [06-security-ops-design.md](06-security-ops-design.md) | Zero-trust isolation, guardrails, egress, audit, observability, service mesh, GitOps, incident checks. |
| [07-validation-roadmap.md](07-validation-roadmap.md) | P0/P1/P2 roadmap, validation matrix, failure tests, and launch gates. |

## Existing Source Material

- [Open-source architecture selection](../open-source-architecture/00-index.html)
- [Open-source architecture plan](../hermes-open-source-architecture-plan.md)
- [Ultra Studio product specs](../ultra-studio-product-specs/00-index.md)
- [Final research analysis pack](../ultra-studio-research-analysis/00-index.md)
- [Ultra Studio architecture diagram](../ultra-studio-agent-architecture.html)
- [TokenRouter credential flow](../hermes-tokenrouter-credential-flow.md)
- [CometAPI media gateway](../hermes-cometapi-media-gateway.md)
- [Asset library backend design](../hermes-asset-library-backend-design.md)
- [Skill/tool/prompt specification](../ultra-studio-agent-skill-tool-prompt-design.md)
- [Real chat agent UI contract](../hermes-real-chat-agent-ui.md)
- [Manus gap research](../ultra-studio-agent-manus-gap-research.md)
- [Research appendix](../ultra-studio-agent-research-appendix.html)

## Top-Level Shape

```text
Browser UI / Creative Chat
  -> Edge Gateway / Realtime Ingress
  -> Gateway Session API
  -> Agent Runtime and Workflow Router
  -> Control Plane
       Identity / OPA / TokenRouter / Quota / Approvals
  -> Execution Plane
       sandbox / browser context / workspace volume / durable workflows
  -> Media Compute Plane
       Atlas provider routes / CometAPI / GPU workers / async job workers
  -> Data Plane
       Postgres RLS / object storage / asset library / memory / files
  -> Security and Ops
       guardrails / egress / service mesh / audit / observability / GitOps
```

## Primary Architecture Decisions

| Decision | Chosen direction | Why |
|---|---|---|
| State owner | Event log plus durable projections for jobs, sessions, assets, and usage. | Agent tasks are long-running and recoverable; UI should replay state from events. |
| Sandbox boundary | microVM-grade sandbox for untrusted execution; local process only for dev. | Creative agent tools can run code, fetch files, and touch user uploads. |
| Credential boundary | TokenRouter backed by vault; sandbox receives scoped Hermes token only. | Provider keys and Atlas credentials must never appear in prompt, environment, or mounted files. |
| Workflow runtime | Skill router plus staged workflow state machine; Temporal for durable external work. | Skills are workflows, not prompt snippets. Long media jobs need resumable orchestration. |
| Realtime channel | NATS JetStream or equivalent durable fanout behind Gateway. | UI needs streaming status, tool events, job status, and reconnect replay. |
| Media data plane | Atlas-first provider tools; CometAPI only for large media preprocessing and native multimodal packaging. | Keep MVP simple while preserving a future binary/media gateway boundary. |
| Storage | Postgres RLS for control records; object storage for binary media; POSIX workspace volume for sandbox. | Assets, jobs, files, and lineage need different persistence models. |
| Policy | OPA as deterministic allow/deny engine; guardrails do not replace policy. | Prompt-level safety cannot protect mounts, tokens, egress, or provider calls. |

## Current Truth Snapshot

| Area | Status | Notes |
|---|---|---|
| Local Hermes TUI and gateway | Green | Repo has `ui-tui`, `tui_gateway`, `prompt.submit`, session list/resume concepts, and local agent loop. |
| Ultra Studio product specs | Green | Product spec pack exists under `docs/ultra-studio-product-specs/`. |
| Open-source infra selection | Yellow | 16-module selection exists as docs; it is not runtime wiring. |
| Workflow router docs | Yellow | Skill/router contracts exist; runtime integration is still incomplete. |
| Atlas image/video plugins | Yellow | Provider plugin/catalog paths exist; need full skill/tool/prompt runtime contracts and ledger wiring. |
| TokenRouter | Yellow | Credential-flow design exists; cloud runtime service and vault integration are not implemented. |
| CometAPI | Yellow | Future media gateway design exists; not MVP runtime. |
| Cloud sandbox | Red | No production sandbox control plane, microVM runtime, workspace mount, or egress policy is wired. |
| Cloud event stream | Red | Local/TUI events exist; cloud Gateway/NATS replay contract is missing. |
| Asset lineage ledger | Red | Asset concepts exist; complete job->asset->element/character lineage needs implementation. |

## Done-When For This Infra Design

This design is complete enough to implement when:

- Every layer has one owner for state and mutation.
- Every external effect crosses a named boundary: provider call, file mount, egress, credential exchange, media fetch, job scheduling.
- Every red gap maps to a P0/P1 roadmap item with a validation check.
- The web UI can stream real state from the Gateway instead of hardcoded demo state.
- The agent cannot claim media completion unless a job, asset, or ledger record exists.
