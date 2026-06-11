# TokenRouter

Status: spec-only — zero TokenRouter code exists (rg for
`tokenrouter|token_router` over source returns no hits, verified this
session); the design is complete in the credential-flow doc. Today's
credential reality: provider keys are server-side static config (e.g.
`ATLAS_API_KEY` resolved in provider clients), with LLM-credential pooling
machinery in `agent/`.
Date: 2026-06-11

Sources:

- Docs: `docs/hermes-tokenrouter-credential-flow.md` (entire doc; itself
  sourced from `docs/notion-source/hermes/pages/06-tokenrouter.md` — the
  Notion `[LITERAL PROMPT]` statements are treated as target-design
  evidence, not verified Higgsfield production fact),
  `docs/ultra-studio-product-specs/02-agent-runtime-contract.md`
  (§Sandbox Lifecycle, §Error Contract), `03-media-asset-contract.md`
  (`tokenrouter_decision_id`), `docs/hermes-cometapi-media-gateway.md`
  (§与 TokenRouter 的关系)
- Code (adjacent, verified this session): `plugins/video_gen/atlas/client.py`
  (`resolve_credentials` — server-side static key resolution),
  `agent/credential_pool.py`, `agent/credential_sources.py`,
  `agent/credential_persistence.py` (LLM provider credential machinery,
  not a policy/exchange proxy), `agent/secret_sources/`

## Purpose & Scope

TokenRouter is the control-plane boundary for model/provider calls: protect
provider credentials, enforce tenant policy, meter usage, and route
requests without exposing real upstream keys to the sandbox
(`hermes-tokenrouter-credential-flow.md` §范围).

Core contract (§核心合约):

- Sandboxes never receive real provider API keys.
- Sandboxes receive only short-lived Hermes tokens such as `HF_JWT_TOKEN`.
- TokenRouter verifies the token, extracts claims, checks
  quota/concurrency, exchanges through a vault-backed credential backend,
  then proxies the upstream request.
- Missing/expired/invalid/under-scoped tokens fail closed; no local-dev
  credential fallback in cloud mode.

Scope: token verification, policy evaluation, credential exchange,
proxying, usage metering, and decision logging. Media preprocessing is
CometAPI (`18-cometapi-media-gateway.md`); asset ACL data is owned by the
Asset Service and consumed here as policy input.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Specified, not built | Four-phase flow: edge validation -> quota/policy -> vault key exchange -> provider | `hermes-tokenrouter-credential-flow.md` §四阶段流程 |
| Specified, not built | Claim set: `sub`, `tenant_id`, `workspace_id`, `project_id`, `chat_id/session_id`, `tool_scopes`, `budget`, `exp/nbf` | §必需 claims |
| Specified, not built | Policy inputs: membership, plan/credits, model allowlist, tool family, asset ACL, concurrency caps, request size, redaction/audit requirements | §策略输入 |
| Specified, not built | Storage/integration picks: vault backend (OpenBao-class), Redis-class quota KV, PostgreSQL + append-only audit, OPA policy, mTLS service identity | §存储与集成 (recommendations, not decisions) |
| Specified, not built | Fail-closed behaviors incl. audit-write fail-closed for high-risk calls | §失败行为 |
| Specified, not built | Per-request decision logging with ids and policy reason | §可观测性 |
| Specified, not built | `tokenrouter_decision_id` on MediaJob envelopes | `03-media-asset-contract.md` §Media Job Envelope |
| Implemented (adjacent, different concern) | Server-side static provider key resolution for media calls | `plugins/video_gen/atlas/client.py` (`resolve_credentials`) — keys never reach the browser, but they are static and process-wide, not scoped tokens |
| Implemented (adjacent, different concern) | LLM credential pooling/persistence for agent model calls | `agent/credential_pool.py`, `credential_sources.py`, `credential_persistence.py`, `agent/secret_sources/` |
| Gap | No tenancy model exists in the deployment today (single-operator self-host); tenant/workspace claims have no enforcement substrate yet | — |

## User Entry Points

None — TokenRouter is invisible infrastructure. Its effects surface as:

- Typed errors in chat/tools: `missing_credential`, `quota_exceeded`,
  model-denied messages (`02-agent-runtime-contract.md` §Error Contract).
- Budget/quota state in account surfaces (future pricing/account nav,
  `01-product-surface.md` IA).
- Decision ids on job records for failure tracing
  ("A failed media job can be traced from `job_id` to TokenRouter decision
  and worker log", §MVP 验收检查).

## Feature List

| Feature | Status |
|---|---|
| JWT verification (signature, lifetime, scope claims) at the edge | Planned |
| Tenant/workspace/project isolation enforcement | Planned |
| Plan/credit/quota checks with realtime concurrency caps (e.g. t2i, i2v limits) | Planned |
| Model/provider allowlist enforcement | Planned (catalog declares, TokenRouter decides — `19-model-catalog-provider-constraints.md`) |
| Asset ACL checks for `image_job`/`video_job`/`media_input`/`soul_id`/`element_id` refs | Planned (data from Asset Service) |
| Vault-backed key exchange; upstream keys never serialized to sandbox | Planned |
| Request proxying with sanitized upstream errors | Planned |
| Usage events with quota deltas | Planned |
| Decision logging (`request_id`, `run_id`, `tool_call_id`, tenant ids, policy reason, route, latency — no secret material) | Planned |
| Budget claims per run/session | Planned |
| Break-glass path for audit-store outage | Planned (explicit approval required, §失败行为) |

## State Machine

Per-request decision pipeline (stateless per request; quota state is
external):

```text
received
  -> token_verified        | -> rejected(401/403)  invalid/expired/under-scoped
  -> policy_evaluated      | -> denied(policy_reason)
  -> key_exchanged         | -> failed(provider_unavailable)  vault outage; no fallback
  -> proxied -> upstream_ok | upstream_error(sanitized)
  -> usage_recorded        | audit-write failure -> fail closed for high-risk calls
```

Token lifecycle (issued elsewhere, validated here):

```text
issued (short-lived, scoped) -> active -> expired
                                      -> revoked
```

Rules: every terminal state produces a decision record with a reason; no
path skips logging except total ledger outage, which itself fails closed
for paid/generation calls.

## APIs & Events

Planned surface (shape per §四阶段流程 and §与 TokenRouter 的关系):

- Proxy endpoint(s) that accept provider-neutral requests with
  `Authorization: Bearer <HF_JWT_TOKEN>`; internal routes per tool family.
- Delegation: CometAPI receives verified scoped requests or validates a
  delegated token; emits usage signals back.
- Decision/usage records exposed to the ledger by id join
  (`16-observation-provenance-ledger.md`), not by API in MVP.

Required claims (verbatim table, §必需 claims): `sub`, `tenant_id`,
`workspace_id`, `project_id`, `chat_id`/`session_id`, `tool_scopes`,
`budget`, `exp`/`nbf`.

No gateway events; failures map to the typed error contract
(`missing_credential`, `quota_exceeded`, provider errors sanitized).

## Data Model

Planned (storage recommendations from §存储与集成):

```text
tokenrouter_decisions (append-only)
- decision_id (= tokenrouter_decision_id on MediaJob)
- request_id, run_id, tool_call_id
- tenant_id, workspace_id, project_id, sub
- decision: allow | deny | fail_closed
- policy_reason
- provider/model route        (no secret material)
- quota_delta, usage_event_id
- upstream_status (sanitized), latency_ms
- created_at

quota_state (KV, realtime)
- scope keys (tenant/workspace/user) -> credits, concurrency counters

vault: upstream credentials       (never in app DB)
policy: OPA bundles or equivalent (versioned)
```

## UI Behavior

None owned. Obligations to other surfaces:

- Denials must reach the user as actionable typed errors ("model not in
  your workspace allowlist", "quota exceeded — resets …"), not vague
  apologies (`02-agent-runtime-contract.md` §Error Contract).
- The inspector shows the decision id on failed jobs for support/debug.
- No UI ever displays exchanged upstream credentials or vault paths.

## Permissions & Error Handling

Failure behavior is the heart of the component (verbatim, §失败行为):

- Invalid token: 401/403, provider never called.
- Missing quota state: fail closed for paid/generation calls.
- Vault access failure: sanitized provider-unavailable error; never fall
  back to sandbox keys.
- Provider failure: preserve provider error class internally, return
  sanitized user-facing error.
- Audit write failure: fail closed for high-risk calls unless an explicit
  break-glass path is approved.

Logging redaction: decisions log routes and reasons, never secret material
(§可观测性).

## Acceptance Criteria

Verbatim MVP checks (`hermes-tokenrouter-credential-flow.md`
§MVP 验收检查):

- Sandbox environment and mounted files contain no real provider key.
- Expired `HF_JWT_TOKEN` is rejected before provider routing.
- Tenant A token cannot access Tenant B assets or sessions.
- A model outside the workspace allowlist is denied.
- A failed media job can be traced from `job_id` to TokenRouter decision
  and worker log.

Plus integration checks: `tokenrouter_decision_id` is present on MediaJob
records once both exist; CometAPI usage is traceable into usage/billing
records (`hermes-cometapi-media-gateway.md` §验收检查).

## Non-Goals

- Media data-plane work (fetch/transcode/packaging — CometAPI).
- Being the asset ACL source of truth (Asset Service owns ACL data;
  TokenRouter evaluates it).
- Replacing the existing LLM credential pool for agent chat models in MVP
  (`agent/credential_pool.py` machinery is a separate concern until cloud
  multi-tenancy requires unification).
- Billing UI/pricing pages (out of pack scope per `00-index.md`).
- Local self-host hard requirement: the fail-closed cloud rules apply to
  cloud mode; the single-operator local deployment keeps server-side static
  keys until TokenRouter ships.

## Open Questions

1. Token issuer: which component mints `HF_JWT_TOKEN` (gateway at
   session/sandbox create?), and what is the rotation/lifetime policy per
   claim scope?
2. MVP cut: full vault + OPA + Redis stack vs a minimal in-process policy
   checker with the same contract — what is the smallest honest version
   that keeps fail-closed semantics?
3. Tenancy substrate: tenant/workspace ids exist in specs but not in the
   current deployment model — what creates them, and how do self-host
   single-tenant installs degrade?
4. Concurrency cap taxonomy (t2i, i2v, …) — where are the cap values
   declared (plan config?) and how do they interact with provider-side
   limits?
5. Budget claim semantics: per-run hard stop vs soft warning; who decides
   the budget at `prompt.submit` time?
6. Migration: when TokenRouter ships, do the static `ATLAS_API_KEY` paths
   in provider clients get replaced outright (repo no-backward-compat rule)
   or gated by deployment mode?
