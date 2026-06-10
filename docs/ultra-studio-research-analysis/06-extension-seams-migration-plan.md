# Extension Seams and Migration Plan

Status: future-proofing design  
Date: 2026-06-11

## Purpose

This document defines the seams that P0 must reserve so later infrastructure can
be added without rewriting the product.

## P0 Seams To Reserve Now

| Seam | P0 implementation | Future replacement |
|---|---|---|
| Credential access | env-backed Atlas credential shim | TokenRouter + vault |
| Job execution | synchronous wrapper with DB row | worker queue / Temporal workflow |
| Realtime state | existing web gateway events | durable event stream / NATS replay |
| Asset storage | local file path plus asset row | object storage plus signed URLs |
| Router | prompt-level workflow-router | deterministic route tool / workflow runtime |
| Provider adapter | Atlas image/video provider | provider registry with capability constraints |
| Policy | simple server checks | OPA policy bundle |
| Browser | unsupported or local manual browser | browser context service |
| Sandbox | unsupported | E2B/Kata/Docker-backed execution service |
| Media preprocessing | direct upload/image URL | CometAPI resolver/transcoder/packager |

## Anti-Rewrite Rules

P0 code should not:

- pass raw file paths as the only asset identity;
- return provider output directly to UI without an asset row;
- call Atlas directly from frontend;
- put provider keys into chat messages or tool outputs;
- hardcode `FAL` or other fallback providers;
- make router output transient only in model text;
- make UI depend on provider-specific response shape;
- make upload records separate from asset records forever.

## Migration Steps

### Step 1: Local P0

```text
SQLite media_jobs/media_assets
local upload directory
Atlas provider wrappers
web gateway events
React media cards
```

This proves the loop.

### Step 2: Worker Split

Trigger: video polling blocks chat, refresh recovery is weak, or jobs run longer
than a single request.

Move provider calls to a worker while keeping:

- same `media_job` schema;
- same `media_job.*` events;
- same `asset.ready` event;
- same UI media card contract.

### Step 3: TokenRouter

Trigger: multi-user use, quota, or credential safety pressure.

Replace env credential shim with:

```text
media job wrapper
  -> TokenRouter request
  -> provider route
```

Do not change UI or router output.

### Step 4: Object Storage

Trigger: assets need sharing, retention, or cross-machine access.

Replace local `path` with object metadata:

```text
asset_id
object_key
preview_object_key
signed_download_url
checksum
```

Keep `asset_id` stable.

### Step 5: CometAPI

Trigger: long video/audio analysis, external URL ingestion, repeated frame
sampling, or native multimodal packaging.

Insert CometAPI before provider/model calls:

```text
asset or URL
  -> CometAPI preprocess
  -> packaged media input
  -> provider route
```

Do not let CometAPI own credentials or policy.

### Step 6: Browser And Sandbox

Trigger: user asks for authenticated web references, file transforms, custom
render scripts, or account actions.

Add separate resource types:

```text
browser_context_id
sandbox_id
workspace_mount_id
```

These are not chat messages. They are scoped resources with lifecycle, audit,
and revoke/delete operations.

### Step 7: Full Workflow Runtime

Trigger: workflows need resumable multi-stage execution, approvals, branching,
or retries beyond a simple job row.

Promote:

- router decision to workflow state;
- media job stages to workflow activities;
- approvals to interrupt/resume states;
- QA and repair to explicit stages.

LangGraph or Temporal can fit here, but the product contract should already be stable.

## Interface Stability Matrix

| Interface | Must remain stable across migrations |
|---|---|
| UI media card | reads `media_job` and `asset`, not provider payload |
| Router handoff | emits intent, tool, asset roles, missing fields |
| Provider wrapper | returns typed success/error, not raw provider response |
| Asset API | accepts `asset_id`; implementation can move from local path to object storage |
| Events | `media_job.created`, `media_job.updated`, `asset.ready`, `error` |
| Errors | typed codes remain stable even if backend changes |

## Future Feature Placement

| Feature | Correct placement | Wrong placement |
|---|---|---|
| Character consistency | asset-derived Character service | prompt string only |
| Element library | asset-derived Element service | tags in chat messages |
| Smart Groups | saved asset query | manual collection mutation |
| Marketplace skills | skill package registry | arbitrary hidden prompt text |
| Brand memory | memory service with source links | global system prompt |
| Browser automation | browser context service | direct UI hack in chat component |
| Sandbox transforms | sandbox execution service | local shell from web request |
| Billing | TokenRouter/usage ledger | provider response parsing in UI |

## Validation For Future Additions

Any new capability must answer:

1. What state does it own?
2. What IDs does it create?
3. What events does it emit?
4. What typed errors can it return?
5. What assets does it read or write?
6. Which skill routes to it?
7. Which policy checks apply?
8. How is it disabled without breaking P0?

If it cannot answer these, it is not ready to enter the product architecture.

