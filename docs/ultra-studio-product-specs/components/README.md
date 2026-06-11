# Ultra Studio Component Specs

Status: per-component functional specifications  
Date: 2026-06-11

Chinese reading version: [components-zh/README.html](../components-zh/README.html)

This directory holds the complete per-component functional specs that
complement the contract-level pack in the parent directory. Each doc follows
the same 12 sections: Purpose & Scope, Implementation Status, User Entry
Points, Feature List, State Machine, APIs & Events, Data Model, UI Behavior,
Permissions & Error Handling, Acceptance Criteria, Non-Goals, Open Questions.

Status values: `implemented` (works today, code-cited), `partial` (real
machinery exists, product contract incomplete), `spec-only` (design exists,
no code). Every "implemented" claim in these docs cites a verified file
path; planned behavior is never presented as shipped.

## Component Map

| Doc | Component | Status | Summary |
|---|---|---|---|
| [01-left-nav-shell.md](01-left-nav-shell.md) | Left Nav Shell | partial | Generic dashboard sidebar shipped; all seven Ultra nav entries spec-only. |
| [02-creative-chat-ui.md](02-creative-chat-ui.md) | Creative Chat UI | partial | Real gateway chat at `/chat`; media cards, picker, typed errors spec-only. |
| [03-inspector-live-panel.md](03-inspector-live-panel.md) | Inspector / Live Panel | partial | Generic chat inspector exists; creative-asset inspector spec-only. |
| [04-marketplace.md](04-marketplace.md) | Marketplace | spec-only | No catalog surface; skill install/enable machinery is the substrate. |
| [05-memory.md](05-memory.md) | Memory | partial | Memory store, tool, providers, prompt injection shipped; Memory page spec-only. |
| [06-files-task-file-browser.md](06-files-task-file-browser.md) | Files / Task File Browser | partial | File tools, upload, sync shipped; Files surface and promotion spec-only. |
| [07-tasks-session-history.md](07-tasks-session-history.md) | Tasks / Session History | partial | Session store + sessions browser shipped; task rows and full restore spec-only. |
| [08-asset-library-ui.md](08-asset-library-ui.md) | Asset Library UI | spec-only | Gallery, mention menu, picker fully designed; no code. |
| [09-asset-service.md](09-asset-service.md) | Asset Service | spec-only | Entities/APIs/indexing fully designed; no service code. |
| [10-media-job-service.md](10-media-job-service.md) | Media Job Service | partial | Sync generation tools + Atlas submit/poll shipped; durable jobs spec-only. |
| [11-skill-registry.md](11-skill-registry.md) | Skill Registry | partial | Discovery, progressive loading, install/guard shipped; Ultra profile + evals spec-only. |
| [12-workflow-router.md](12-workflow-router.md) | Workflow Router | partial | Skill package + allowlist helpers exist; runtime wiring spec-only. |
| [13-prompt-compiler.md](13-prompt-compiler.md) | Prompt Compiler | spec-only | Compile/enhance tools undefined in code; payload builders exist below. |
| [14-sandbox-lifecycle.md](14-sandbox-lifecycle.md) | Sandbox Lifecycle | partial | Multi-backend environments shipped; product lifecycle verbs spec-only. |
| [15-human-approval-gateway.md](15-human-approval-gateway.md) | Human Approval Gateway | partial | Approval + clarify machinery shipped; durable typed decisions spec-only. |
| [16-observation-provenance-ledger.md](16-observation-provenance-ledger.md) | Observation / Provenance Ledger | spec-only | Insights/provenance fragments exist; unified ledger spec-only. |
| [17-tokenrouter.md](17-tokenrouter.md) | TokenRouter | spec-only | Zero-trust credential flow fully designed; no code, keys are static server-side. |
| [18-cometapi-media-gateway.md](18-cometapi-media-gateway.md) | CometAPI Media Gateway | spec-only | Future media data plane; explicitly not an MVP capability. |
| [19-model-catalog-provider-constraints.md](19-model-catalog-provider-constraints.md) | Model Catalog / Provider Constraints | partial | Atlas catalogs + constraint schemas shipped in plugins; queryable registry spec-only. |

## Reading Order

- Building the P0 loop: 01, 02, 12, 10, 19, 03.
- Asset workstream: 09, 08, 06, 03.
- Platform/runtime workstream: 14, 15, 11, 13, 16.
- Cloud-mode security: 17, 18, 14.

Cross-component contracts (events, errors, session state) remain owned by
the parent pack (`../02-agent-runtime-contract.md` and siblings); component
docs cite them rather than redefining them.
