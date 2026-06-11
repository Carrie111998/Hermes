# Control Plane Design

Status: infrastructure design  
Scope: request ingress, identity, authorization, TokenRouter, quota, approvals, Gateway/Session APIs.

## Objective

The control plane decides who can do what, with which tools, models, assets, and budget. It must be deterministic, auditable, and independent from prompt wording.

## Request Flow

```text
Browser
  -> Edge Gateway
  -> session/auth middleware
  -> OPA policy check
  -> Gateway / Session service
  -> Agent Runtime
  -> TokenRouter for provider/model calls
  -> usage and audit ledgers
```

## Edge Gateway Contract

Recommended base: Envoy Gateway + Envoy Proxy from the open-source architecture plan.

Responsibilities:

- TLS termination.
- route admission.
- JWT/session token verification.
- request size limits for uploads.
- WebSocket/SSE idle timeout handling.
- per-tenant and per-route rate limiting.
- external authorization hook to OPA or auth service.
- stream reconnect token validation.

Non-responsibilities:

- no prompt understanding.
- no provider credential access.
- no asset lineage mutation.
- no long-running workflow state.

P0 routes:

```text
POST /api/sessions
POST /api/sessions/{session_id}/resume
POST /api/sessions/{session_id}/prompt
GET  /api/sessions/{session_id}/events
POST /api/uploads
GET  /api/jobs/{job_id}
GET  /api/assets/{asset_id}
```

## Identity and Tenant Access

Recommended base: Keycloak or equivalent OIDC provider.

Token claims required by Hermes:

| Claim | Required | Purpose |
|---|---:|---|
| `sub` | yes | user identity |
| `tenant_id` | yes | tenant boundary |
| `workspace_id` | yes | quota and asset boundary |
| `project_id` | yes for project work | files, assets, memory scope |
| `roles` | yes | admin/member/viewer controls |
| `session_id` | stream token only | event stream binding |
| `exp` / `nbf` | yes | token lifetime |

Membership truth remains in Hermes Postgres, not only in the IdP. The IdP authenticates; Hermes authorizes project/workspace access.

## OPA Policy Contract

OPA receives structured input and returns structured decisions.

Input shape:

```json
{
  "actor": {"user_id": "...", "tenant_id": "...", "roles": ["member"]},
  "resource": {"type": "asset", "id": "...", "project_id": "..."},
  "action": "asset.download",
  "request": {"route": "/api/assets/...", "ip_class": "user"},
  "plan": {"tier": "team", "limits": {"video_jobs": 2}}
}
```

Decision shape:

```json
{
  "allow": false,
  "reason": "asset_project_mismatch",
  "redactions": ["provider_key", "internal_prompt"],
  "audit_level": "security"
}
```

Policy rules:

- default deny.
- missing policy bundle denies sensitive operations.
- deny reason is short and safe for logs.
- internal policy details are not exposed to the model or user.

## TokenRouter Contract

TokenRouter is the credential, quota, and provider routing boundary.

Responsibilities:

- validate scoped Hermes token.
- evaluate model/provider allowlist.
- enforce credits, budget, and concurrency.
- verify asset ACL for media inputs.
- exchange through vault-backed provider credential.
- proxy or sign provider request.
- emit usage event.
- emit sanitized audit event.

Non-responsibilities:

- no browser UI state.
- no prompt routing.
- no binary media preprocessing; CometAPI owns that future boundary.
- no direct user-facing provider key exposure.

Request envelope:

```json
{
  "run_id": "run_...",
  "tool_call_id": "tool_...",
  "tenant_id": "ten_...",
  "project_id": "proj_...",
  "operation": "video.generate",
  "provider": "atlas",
  "model": "wan-2.6-flash",
  "input_asset_ids": ["asset_..."],
  "budget": {"max_credits": 1000}
}
```

Failure policy:

- invalid/expired token: 401/403, no provider call.
- quota store unavailable: fail closed for generation.
- vault unavailable: provider unavailable, no fallback key.
- provider error: return sanitized error plus internal error class.
- audit write failure on high-risk calls: fail closed unless break-glass is active.

## Gateway / Session API

The Gateway/Session service is the web equivalent of the local TUI boundary.

Session creation response:

```json
{
  "session_id": "sess_...",
  "event_cursor": "0",
  "allowed_models": ["grok-4.3", "atlas-video-default"],
  "upload_policy": {"max_files": 8, "max_bytes": 104857600}
}
```

Prompt submission request:

```json
{
  "message": "帮我做一个产品短视频",
  "model": "grok-4.3",
  "attachment_asset_ids": ["asset_..."],
  "client_message_id": "client_..."
}
```

Prompt submission response:

```json
{
  "run_id": "run_...",
  "accepted": true,
  "event_cursor": "123"
}
```

The response is immediate. The UI watches the event stream for model deltas, tool progress, job events, and final completion.

## Approval Boundary

Some actions must stop for approval:

- posting to external accounts.
- spending above budget.
- downloading from private URL contexts.
- deleting assets, sessions, browser contexts, or memory.
- using a persistent browser context with authenticated accounts.
- sharing or exporting generated media outside the project.

Approval records must include:

```text
approval_id
session_id
run_id
requested_action
risk_level
policy_reason
expires_at
approved_by
resolved_at
```

## Current Implementation Truth

| Contract | Current truth | Required next step |
|---|---|---|
| local TUI prompt.submit | exists in local architecture | expose web-compatible session API |
| session resume/list | exists locally | add cloud auth, cursor replay, tenant scoping |
| TokenRouter | design doc only | implement service boundary and tests |
| OPA | selected in architecture docs | add policy bundle, input/output contract tests |
| Edge Gateway | selected in architecture docs | add deployable route/auth/limit config |
| Approvals | TUI prompt concepts exist | persist approval records and event stream |

