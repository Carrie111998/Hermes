# Complete System Perspective

Status: complete-view design, not P0 implementation scope  
Date: 2026-06-11

## Purpose

P0 must stay thin, but the system model must be complete. This document defines
the whole target shape so later additions do not become accidental patches.

The rule is:

```text
build the P0 vertical slice now
reserve the ownership seams for the full product now
defer heavy infrastructure until a trigger makes it necessary
```

## Complete Product Surface

| Surface | P0 | Later |
|---|---|---|
| Chat | real Hermes web chat, upload, streaming, media cards | multi-run workspace, advanced approvals, collaborative sessions |
| Inspector | selected job/asset details, error, download | QA scores, repair plans, create Element, create Character |
| Assets | upload asset, generated image/video asset | smart groups, semantic search, collections, lineage graph |
| Skills | workflow-router, infographic, media QA, prompt repair | product shoots, UGC, listing, campaigns, marketplace skills |
| Browser | not required for generation | persistent contexts, take-over, authenticated reference gathering |
| Sandbox | not required for Atlas-only generation | isolated tools, file transforms, render pipelines, code execution |
| Memory | not P0 unless already available | project memory, brand memory, preference memory, source-backed recall |
| Marketplace | not P0 | skill/template/asset packages with permissions and provenance |

## Target Architecture

```text
Experience Layer
  chat, uploads, inspector, assets, files, marketplace

Agent Runtime Layer
  session, run, workflow-router, skill runtime, prompt compiler

Media Execution Layer
  media_job, Atlas provider adapters, polling, publish, retry

Asset Data Layer
  asset, object/file, lineage, collection, smart_group, character, element

Control Boundary Layer
  credential shim -> TokenRouter, policy shim -> OPA, audit, quota

Expansion Layer
  browser contexts, sandbox runtime, CometAPI, durable workflows, GPU fabric
```

P0 implements the first three layers minimally and creates the first asset data
layer. It should not implement the full expansion layer, but it must not block it.

## State Ownership

| State | Owner | P0 representation | Later upgrade |
|---|---|---|---|
| Session | Hermes session store | existing session/message store | multi-user workspace session store |
| Run | Agent runtime | run_id attached to prompt/tool/job | durable run event log |
| Router decision | Agent runtime | stored route JSON | deterministic router service/tool |
| Tool call | Agent runtime | existing tool event + tool_call_id | complete tool ledger |
| Media job | Media execution | new `media_jobs` table | worker queue / Temporal workflow |
| Asset | Asset data layer | new `media_assets` table | object store, lineage graph, search index |
| Provider credential | Credential boundary | server env credential shim | TokenRouter + vault |
| Browser context | Browser service | none | scoped context store |
| Sandbox workspace | Sandbox service | none | workspace volume and sandbox lifecycle |
| Usage/billing | Control boundary | optional usage row | quota, credits, audit, billing ledger |

## Complete Capability Map

| Capability | First usable version | Final owner |
|---|---|---|
| text-to-image | P0 Atlas wrapper | Media execution |
| text-to-video | P0 Atlas wrapper | Media execution |
| image-to-video | P0 if current Atlas model supports it | Media execution |
| video analysis | later simple worker, then CometAPI | CometAPI / media data plane |
| asset reuse | P0 by `asset_id` | Asset data layer |
| character consistency | later | Character service over asset data |
| element reuse | later | Element service over asset data |
| skill routing | P0 prompt-level router | Skill runtime |
| skill marketplace | later | Marketplace service |
| browser reference gathering | later | Browser context service |
| sandbox file transforms | later | Sandbox runtime |
| provider quota | P0 shim | TokenRouter |
| multi-tenant policy | later | OPA / auth boundary |

## Design Invariants

These must hold from P0 onward:

- UI references media by `asset_id`, not raw file path.
- Any generated media must pass through `media_job`.
- A final response cannot claim an artifact unless `asset.ready` was emitted.
- Router decides before generation.
- Missing credential is a typed blocker, not a provider fallback.
- Skills are progressively disclosed.
- Provider details are behind a media/provider adapter.
- Later browser/sandbox work cannot bypass job, asset, audit, or policy seams.

## Full Research Implications

The official references point to the same architecture direction:

- Manus Skills emphasizes file-based, reusable, composable skills and progressive disclosure.
- Manus Cloud Browser shows authenticated browser action needs isolated sessions, take-over, and user-controlled account access.
- E2B shows sandbox lifecycle should be explicit when code/file execution becomes necessary.
- Browserbase contexts show browser session state is its own managed object.
- OpenAI/Anthropic computer-use patterns show GUI control should be tool-like, observable, and constrained.
- LangGraph and Temporal both justify durable state for long-running workflows, but only after P0 proves the loop needs more than DB-backed jobs.

## Complete View Acceptance

The design is complete enough when future features have a named slot:

| Future feature | Slot already reserved |
|---|---|
| TokenRouter | provider credential seam behind media job wrapper |
| CometAPI | media data-plane seam before provider call |
| browser contexts | browser service seam, not chat UI state |
| sandbox | execution service seam, not direct local shell |
| smart groups | asset query layer, not collection mutation |
| characters/elements | first-class asset-derived entities |
| marketplace | skill/template package layer with permissions |
| Temporal/NATS | job/event implementation upgrade, not contract rewrite |

