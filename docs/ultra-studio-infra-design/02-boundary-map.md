# Infrastructure Boundary Map

Status: architecture boundary map  
Scope: ownership, source of truth, and status of every Ultra Studio infra layer.

## Ownership Principle

Each infrastructure concern must have exactly one state owner. Other layers can read through APIs or subscribe to events, but they do not mutate that state directly.

## Layer Map

| Layer | State owner | Main mutations | Status |
|---|---|---|---|
| Edge Gateway / Realtime Ingress | Gateway service | session stream auth, route admission, upload admission | Red |
| Identity / Tenant Access | Identity service plus Hermes membership DB | login, membership sync, tenant selection | Red |
| Policy Engine | OPA policy bundle | allow/deny decisions, policy versioning | Yellow |
| TokenRouter | TokenRouter service | provider route, quota decision, credential exchange, usage event | Yellow |
| Gateway / Session API | Session service | session.create, session.resume, prompt.submit, stream subscribe | Yellow |
| Workflow Router | Agent runtime | intent, missing fields, asset roles, workflow_skill | Yellow |
| Skill Runtime / Registry | Skill registry plus workflow runtime | skill selection, preflight, staged execution, QA gate | Yellow |
| Sandbox Runtime | Sandbox control plane | VM/pod create, attach, terminate, network policy | Red |
| Workspace Volume | Volume mounter | project/session mount, snapshot, output publish | Red |
| Browser Contexts | Browser context service | create/reuse/delete authenticated browser state | Red |
| Durable Orchestration | Temporal | workflow start, activity result, retry, timeout, approval wait | Red |
| Realtime Events | NATS JetStream or equivalent | event publish, durable consumer, replay cursor | Red |
| GPU / Media Workers | Job manager plus Kueue | queue admission, worker dispatch, cancel, status | Red |
| Atlas Provider Tools | Atlas media tool adapters | image/video submit, status, result, download | Yellow |
| CometAPI | Media data-plane service | resolve, trim, transcode, package, cache media | Yellow |
| Relational Data | Postgres with RLS | sessions, jobs, assets, usage, audit, memberships | Red |
| Object Media Storage | Object storage | uploads, outputs, thumbnails, manifests | Red |
| Observability / Audit | OTel collector plus audit store | traces, metrics, logs, append-only audit events | Red |
| GitOps / Secrets Delivery | Argo CD / External Secrets / vault | deploy, rotate, reconcile, policy bundle rollout | Red |

## Control Plane Boundaries

```text
Browser request
  -> Edge Gateway
  -> Identity and OPA check
  -> Gateway / Session service
  -> Agent Runtime
  -> TokenRouter for provider calls
  -> Usage and audit ledgers
```

Rules:

- Browser never calls Atlas or provider APIs directly.
- Sandbox never receives provider API keys.
- UI-selected model is an input to policy, not a guarantee of route.
- Missing policy or quota state fails closed for paid media generation.

## Execution Plane Boundaries

```text
Agent Runtime
  -> Temporal workflow
  -> sandbox or media worker activity
  -> workspace volume / object storage
  -> event bus
  -> UI stream and projections
```

Rules:

- Long-running work must have durable job/session IDs.
- Tool progress is an event, not the source of truth.
- The final answer must reference produced artifacts or explicit failure evidence.
- Worker logs can be observed, but UI state comes from normalized events and DB projections.

## Data Plane Boundaries

```text
Upload or generated output
  -> object storage object
  -> asset row
  -> lineage edge
  -> collection / smart group / character / element reference
  -> downloadable signed URL
```

Rules:

- Object keys are not authorization.
- Asset IDs and scoped tokens are authorization boundaries.
- Collections are static membership lists.
- Smart Groups are saved queries.
- Characters and Elements are first-class assets, not tags.

## Source-of-Truth Map

| Contract | Source of truth | Consumers |
|---|---|---|
| User identity | Identity provider token plus Hermes membership DB | Edge, Session, TokenRouter, Postgres RLS |
| Tenant/project access | Hermes membership tables | OPA, TokenRouter, asset library, UI |
| Session state | Session DB projection plus event stream | UI, agent runtime, audit |
| Agent transcript | Session message store | UI, memory, audit, skill QA |
| Workflow state | Temporal event history | workers, retry logic, operations |
| Realtime progress | NATS stream | UI, inspector, logs, job projection updater |
| Tool call result | tool_call record plus event | agent runtime, audit, UI |
| Media job | media_job row plus provider status | inspector, asset library, billing |
| Binary object | object storage | downloads, previews, CometAPI, workers |
| Asset metadata | asset table | search, collections, smart groups, lineage |
| Usage and billing | usage_event ledger | TokenRouter, quota, reporting |
| Security decision | OPA decision log plus audit event | incident review, operations |
| Provider credential | OpenBao/vault | TokenRouter only |

## Status Truth For Earlier Questions

### Gateway / Session

The local Hermes TUI has a gateway-style JSON-RPC boundary, including session list/resume and prompt submission concepts. The cloud Gateway/Session API is not fully implemented.

Required cloud endpoints:

```text
POST /sessions
POST /sessions/{id}/resume
POST /sessions/{id}/prompt
GET  /sessions/{id}/events
POST /sessions/{id}/uploads
GET  /jobs/{id}
GET  /assets/{id}
```

### Workflow Router

The workflow-router is documented, but it is not complete runtime infrastructure. It must produce:

```text
intent
workflow_skill
asset_roles
missing_fields
tool_plan
provider_constraints
approval_requirements
```

It cannot auto-create video just because the user says hello or uploads a file.

### TokenRouter

TokenRouter is a separate control-plane service boundary, not a UI panel. It sits between sandbox/agent/media workers and provider credentials:

```text
Sandbox or worker
  -> scoped Hermes token
  -> TokenRouter policy and quota
  -> vault-backed provider credential
  -> Atlas/provider route
```

See [TokenRouter credential flow](../hermes-tokenrouter-credential-flow.md).

### Inspector / Live Panel

The inspector is a UI consumer of infrastructure state. It should display current job, selected asset, QA, downloads, create Element, and create Character. It does not own the job, asset, or security decision state.

