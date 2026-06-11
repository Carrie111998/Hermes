# P0 Security and Credential Boundaries

Status: minimal P0 security contract  
Date: 2026-06-11

## Goal

Prevent the most damaging failures without building the full cloud security stack:

- no provider key leakage;
- no fake media artifacts;
- no accidental fallback to FAL/Comfy/other providers;
- no protected skill reference dumping;
- no cross-session asset confusion;
- no silent provider failure.

## P0 Credential Model

P0 can use environment-backed credentials:

```text
ATLAS_API_KEY
ATLAS_API_BASE
ATLAS_BASE_URL
```

Rules:

- keys are server-side only;
- never ask user to paste keys in chat;
- never return keys to UI events;
- never write keys to media job metadata;
- redaction must cover logs and tool errors;
- missing credentials return `missing_credential`.

Full TokenRouter/vault is later. P0 still needs a TokenRouter-shaped seam so the later migration does not rewrite every tool.

## Provider Boundary

P0 provider policy:

- Atlas is the default and only creative media provider for Ultra P0.
- FAL must not be selected silently.
- XAI/OpenAI image providers must not be used unless explicitly configured for a non-Ultra profile.
- Provider model names are internal; UI can show friendly names.
- Provider failure is visible and typed.

## Asset Boundary

P0 does not need full tenant RLS, but it does need session/project ownership.

Each asset must store:

```text
asset_id
session_id
project_id or profile_id
source
path
mime_type
created_by_job_id
created_at
```

Rules:

- UI uses `asset_id`.
- tools receive validated asset references.
- raw local paths are compatibility fields, not product identity.
- deleting or failing to publish an output means no `asset.ready`.

## Skill Reference Boundary

Protected:

- full internal `SKILL.md` bodies unless selected by routing;
- skill `references/`;
- hidden prompts;
- provider recipes;
- raw tool-chain scripts not needed by the selected skill.

Allowed:

- public skill name;
- description;
- selected workflow summary;
- routing trace when user asks, with internals redacted.

## Approval Boundary

P0 approvals should be small:

| Action | P0 behavior |
|---|---|
| Spend more credits than configured budget | block or ask confirmation |
| Use authenticated browser context | not P0, show unsupported |
| Publish/post externally | not P0, show unsupported |
| Delete asset | confirm |
| Retry failed paid job | confirm if it may spend again |

## Audit Minimum

Each media job should be traceable:

```text
session_id
run_id
tool_call_id
media_job_id
asset_id
provider
model
status
error_code
created_at
updated_at
```

This can be ordinary DB rows in P0. It does not require a full audit pipeline.

## Required Failure Behavior

| Failure | Behavior |
|---|---|
| missing Atlas key | typed error, no fallback, no fake output |
| provider HTTP error | typed `provider_api_error`, retain internal detail server-side |
| timeout | typed `provider_timeout`, job remains inspectable |
| no output URL | typed `empty_response`, no asset |
| output save failure | typed `asset_publish_failed`, no ready asset |
| user asks for hidden skill internals | deny or summarize safely |
| unknown asset ID | `invalid_asset_ref` before provider call |

## Later Security Stack

Move these to P1/P2:

- OpenBao/vault-backed TokenRouter.
- OPA policy bundle.
- service mesh.
- sandbox egress policy.
- Keycloak or enterprise IdP.
- Postgres RLS.
- append-only audit store.

These are important for cloud multi-tenant deployment, not for proving P0 media loop correctness.

