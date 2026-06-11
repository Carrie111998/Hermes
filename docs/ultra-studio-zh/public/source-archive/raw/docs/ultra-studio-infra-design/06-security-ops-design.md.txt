# Security and Operations Design

Status: infrastructure design  
Scope: zero-trust isolation, secrets, egress, guardrails, audit, observability, service mesh, GitOps.

## Security Objective

Assume the agent can be prompt-injected, tools can fail, providers can be unavailable, and sandboxed code can be hostile. The infrastructure must still protect tenants, credentials, assets, and protected skill internals.

## Trust Boundaries

| Boundary | Must protect |
|---|---|
| Browser to Edge | session token, CSRF, upload limits, stream subscription |
| Edge to services | service identity, route authorization, rate limits |
| Agent to TokenRouter | provider credentials, model allowlist, budget |
| Sandbox to network | metadata service, internal services, forbidden domains |
| Sandbox to filesystem | project volume, protected skills, host paths |
| Worker to object storage | scoped asset objects, upload/download tokens |
| Skill registry to model | protected skill internals and references |
| Browser context to agent | authenticated cookies, account actions |

## Secrets Policy

Rules:

- no hardcoded keys.
- no provider keys in sandbox environment.
- no provider keys in prompt, logs, mounted files, or UI events.
- short-lived scoped Hermes tokens only.
- vault-backed credential exchange through TokenRouter.
- secret redaction in traces and audit views.

Secret classes:

| Class | Storage | Access |
|---|---|---|
| provider key | OpenBao/vault | TokenRouter only |
| service credential | External Secrets to Kubernetes Secret | service account only |
| user OAuth token | encrypted credential store | connector/browser service only |
| scoped job token | minted by control plane | specific worker/job only |
| signed asset URL | object storage signer | browser/user for short TTL |

## Egress Policy

Sandbox egress must be deny-by-default.

Allowed P0 egress:

- TokenRouter endpoint.
- object upload/download endpoint through scoped signer.
- approved public package mirrors if tool needs installation.
- explicitly approved external URL resolver path.

Denied:

- cloud metadata service.
- internal Kubernetes services.
- vault/OpenBao.
- direct provider APIs.
- cross-tenant object storage paths.
- arbitrary private IP ranges.

## Guardrails and Exfiltration Defense

NVIDIA NeMo Guardrails can help at the LLM app layer, but deterministic boundaries remain OPA and egress filters.

Protect:

- internal prompts.
- skill `references/`.
- provider credentials.
- user private uploads.
- browser cookies/session state.
- system architecture secrets.

High-risk outputs:

- request to print full skill instructions.
- bulk export of skill internals.
- generated command that exfiltrates mounted files.
- browser action that posts/deletes/sends money.
- upload of private media to unknown external service.

Required response:

- deny with safe reason.
- log policy decision.
- do not replace denial with fake data.
- ask for approval only when policy allows human override.

## Service Mesh / Internal mTLS

Recommended base: Istio.

Use service identity to distinguish:

- browser user identity.
- agent runtime service identity.
- TokenRouter service identity.
- sandbox workload identity.
- media worker identity.
- CometAPI identity.

Rules:

- internal service calls require mTLS.
- sandbox cannot call vault or DB directly.
- egress gateway enforces allowed domains.
- AuthorizationPolicy aligns with OPA decisions for high-risk routes.

## Observability

Recommended base: OpenTelemetry plus Grafana LGTM stack.

Required correlation IDs:

```text
tenant_id
workspace_id
project_id
session_id
run_id
tool_call_id
job_id
asset_id
provider_request_id
usage_event_id
```

Signals:

- Edge request latency and denial counts.
- TokenRouter decision latency, allow/deny reasons, quota errors.
- provider route latency and error class.
- Temporal workflow retry/timeout counts.
- event bus publish/replay lag.
- sandbox startup/termination time.
- GPU queue depth and worker utilization.
- object storage upload/download errors.
- asset publish failures.

## Audit Events

Audit events are append-only and separate from normal logs.

Events:

```text
session.create
prompt.submit
tool.call
provider.request
tokenrouter.deny
asset.download
asset.delete
browser_context.attach
approval.request
approval.resolve
policy.bundle.deploy
secret.rotate
```

Every deny should be auditable. Every provider request should be traceable to a user, session, tool call, and usage event.

## GitOps and Secrets Delivery

Recommended base: Argo CD + External Secrets Operator.

GitOps owns:

- Kubernetes manifests.
- service routes.
- OPA policy bundle deployment.
- worker deployments.
- event bus and Temporal configs.
- service mesh policy.

Git does not store:

- provider keys.
- user OAuth tokens.
- raw browser cookies.
- signed URLs.

## Operational Runbooks

P0 runbooks:

- provider outage: disable route in TokenRouter, keep jobs recoverable.
- quota store outage: fail closed for generation, show visible error.
- event bus lag: UI falls back to projection polling, alert operations.
- sandbox pool exhaustion: queue work, do not run in unsafe local fallback.
- asset publish failure: mark job failed or retry publish, no fake artifact.
- policy bundle bad deploy: rollback through GitOps, fail closed for sensitive actions.

## Security Tests

- Prompt asks for protected skill references; output must deny.
- Sandbox tries `169.254.169.254`; blocked.
- Sandbox tries direct Atlas/provider endpoint; blocked unless via TokenRouter.
- Tenant A uses Tenant B asset ID; denied by service and RLS.
- Expired scoped token calls TokenRouter; denied before provider route.
- Browser context from Project A attached to Project B; denied.
- Logs and traces contain no provider key substrings.

