# TokenRouter 零信任凭证流

状态：实现设计补充
来源：`docs/notion-source/hermes/pages/06-tokenrouter.md`

## 范围

TokenRouter is the control-plane boundary for model/provider calls. In the Hermes cloud MVP it should protect provider credentials, enforce tenant policy, meter usage, and route requests without exposing real upstream keys to the sandbox.

The Notion page uses `[LITERAL PROMPT]` for several Higgsfield-specific statements. This document treats them as source evidence for a target Hermes design, not as verified proof of Higgsfield production internals.

## 核心合约

- Sandboxes never receive real provider API keys.
- Sandboxes receive only short-lived Hermes tokens such as `HF_JWT_TOKEN`.
- TokenRouter verifies the token, extracts claims, checks quota/concurrency, exchanges through a vault-backed credential backend, then proxies the upstream request.
- Missing, expired, invalid, or under-scoped tokens must fail closed. No local-dev credential fallback is allowed in cloud mode.

## 四阶段流程

```text
Sandbox VM
  | request with HF_JWT_TOKEN
  v
TokenRouter edge validation
  | verify signature, tenant, workspace, user, chat, tool scope
  v
Quota and policy check
  | plan, balance, boost, concurrency, model allowlist, asset ACL
  v
Vault-backed key exchange
  | fetch/use upstream credential without exposing it to sandbox
  v
Provider or compute backend
```

## 必需 claims

| Claim | Purpose |
|---|---|
| `sub` | User identity. |
| `tenant_id` | Tenant isolation and billing boundary. |
| `workspace_id` | Workspace quota and asset scope. |
| `project_id` | Project asset, memory, and file boundary. |
| `chat_id` or `session_id` | Traceability and per-session tool scope. |
| `tool_scopes` | Which tool families the request can use. |
| `budget` | Per-run or per-session spending limit. |
| `exp` / `nbf` | Token lifetime. |

## 策略输入

TokenRouter policy should evaluate:

- Tenant/workspace membership.
- Plan, credits, and current quota state.
- Model/provider allowlist.
- Tool family and requested operation.
- Asset ACL for `image_job`, `video_job`, `media_input`, `soul_id`, and `element_id`.
- Concurrency caps such as text-to-image and image-to-video limits.
- Request size, media count, and timeout.
- Redaction and audit requirements.

## 存储与集成

| Concern | Recommended component |
|---|---|
| Credential vault | OpenBao or equivalent vault backend. |
| Realtime quota/concurrency | Redis or Redis-compatible KV. |
| Durable usage/audit | PostgreSQL plus append-only audit store. |
| Policy evaluation | OPA for deterministic allow/deny. |
| Service identity | Istio mTLS or equivalent internal identity. |

## 失败行为

- Invalid token: return 401/403, do not call provider.
- Missing quota state: fail closed for paid/generation calls.
- Vault access failure: return sanitized provider-unavailable error; do not fall back to sandbox keys.
- Provider failure: preserve provider error class internally, return sanitized user-facing error.
- Audit write failure: fail closed for high-risk calls unless an explicit break-glass path is approved.

## 可观测性

Every proxied request should log:

- `request_id`, `run_id`, `tool_call_id`, `tenant_id`, `workspace_id`, `project_id`.
- Token decision result and policy reason.
- Provider/model route, without logging secret material.
- Quota delta and usage event ID.
- Sanitized upstream status and latency.

## MVP 验收检查

- Sandbox environment and mounted files contain no real provider key.
- Expired `HF_JWT_TOKEN` is rejected before provider routing.
- Tenant A token cannot access Tenant B assets or sessions.
- A model outside the workspace allowlist is denied.
- A failed media job can be traced from `job_id` to TokenRouter decision and worker log.
