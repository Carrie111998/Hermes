# Data Plane Design

Status: infrastructure design  
Scope: CometAPI, asset library, object storage, Postgres RLS, media job lineage, memory/files/marketplace.

## Objective

The data plane stores, transforms, searches, and serves creative artifacts. It must preserve provenance: what prompt, model, inputs, job, and user action produced each asset.

## Data Plane Overview

```text
Upload / Atlas output / browser download / sandbox file
  -> object storage
  -> asset record
  -> lineage edge
  -> collection / smart group / character / element
  -> preview, download, reuse in next job
```

## Core Entities

| Entity | Purpose |
|---|---|
| `media_job` | durable generation/edit/analyze job record |
| `asset` | typed media object: image, video, audio, 3D, document, prompt artifact |
| `asset_object` | object storage pointer, size, checksum, mime, thumbnail |
| `asset_lineage` | input/output relation between jobs and assets |
| `collection` | manual static list of asset IDs |
| `smart_group` | saved query rule, not static membership |
| `character` | identity-preserving creative asset, separate from collection |
| `element` | reusable visual/object/style asset |
| `memory_item` | durable knowledge attached to project/user, not raw file |
| `marketplace_item` | packaged skill/template/asset listing |

## Media Job Contract

```json
{
  "job_id": "job_...",
  "job_type": "video_generate",
  "provider": "atlas",
  "model": "wan-2.6-flash",
  "status": "queued",
  "session_id": "sess_...",
  "run_id": "run_...",
  "tool_call_id": "tool_...",
  "input_asset_ids": ["asset_..."],
  "output_asset_ids": [],
  "prompt_hash": "sha256:...",
  "parameter_json": {},
  "usage_event_id": null,
  "error_class": null
}
```

Allowed statuses:

```text
queued
validating
submitted
running
uploading_result
complete
failed
cancelled
expired
```

No final media response can be `complete` without at least one output asset or an explicit empty-result reason.

## Asset Contract

```json
{
  "asset_id": "asset_...",
  "tenant_id": "ten_...",
  "project_id": "proj_...",
  "asset_type": "video",
  "source": "generated",
  "status": "ready",
  "object_id": "obj_...",
  "preview_object_id": "obj_thumb_...",
  "created_by_run_id": "run_...",
  "created_by_job_id": "job_...",
  "metadata": {
    "duration_sec": 5,
    "aspect_ratio": "9:16",
    "model": "wan-2.6-flash"
  }
}
```

Rules:

- asset ID is the app authorization handle.
- object key is never enough to authorize access.
- object URLs are short-lived signed URLs.
- all generated media carries provider/model/prompt/parameter provenance.

## CometAPI Boundary

CometAPI is a future media data-plane gateway, not the default MVP path.

It owns:

- external URL resolution.
- large video/audio download.
- trim and time-window extraction.
- frame sampling.
- transcoding.
- audio/subtitle extraction.
- native multimodal packaging.
- tenant-safe cache.

It does not own:

- provider credentials.
- quota.
- policy.
- user identity.
- final asset authorization.

Flow:

```text
Agent video_analyze tool
  -> TokenRouter policy/quota
  -> CometAPI delegated media request
  -> resolver/cache/preprocess
  -> model payload packaging
  -> provider route through TokenRouter
  -> result asset/analysis record
```

## Upload Flow

```text
Browser selects files
  -> Edge checks upload policy
  -> object storage multipart upload with scoped token
  -> asset row status=uploaded
  -> media inspection worker
  -> thumbnail/metadata ready
  -> event: asset.created
```

Validation:

- MIME sniffing, not extension only.
- file size and duration limits.
- virus/malware scan for high-risk file types.
- image/video metadata extraction.
- reject or quarantine unsupported files.

## Asset Library Behaviors

Collection:

- manually created.
- stores explicit asset IDs.
- future assets do not auto-enter.

Smart Group:

- stores query JSON.
- dynamically resolves assets.
- can filter by prompt, endpoint/model, source, time, type, status, tags, character, and similarity.

Character:

- first-class entity for identity consistency.
- can be created from upload or generated image.
- should store reference assets, face/body descriptors, allowed use, and lineage.

Element:

- reusable object/style/prop/background/brand visual.
- can be selected as input to future image/video jobs.

## Memory / Files / Marketplace

Memory:

- stores durable user/project knowledge.
- not a replacement for asset storage.
- cannot contain provider keys or protected skill internals.

Files:

- represent workspace filesystem artifacts.
- can be promoted into assets when published.
- should support browser downloads and sandbox outputs.

Marketplace:

- packages skills, templates, and curated assets.
- installation creates project-scoped references.
- protected internals are not bulk-exportable.

## Postgres RLS

Every control record should carry:

```text
tenant_id
workspace_id
project_id
created_by
created_at
```

RLS is a second line of defense. Application code must still pass tenant/project filters. Tests must verify cross-tenant denial at both service and DB layers.

## Required Indexes

P0 indexes:

```text
media_job(session_id, created_at)
media_job(run_id)
asset(project_id, created_at)
asset(asset_type, status)
asset_lineage(input_asset_id)
asset_lineage(output_asset_id)
usage_event(tenant_id, created_at)
audit_event(tenant_id, created_at)
```

P1 search:

- prompt text search.
- embedding search for visual/text semantic grouping.
- endpoint/model filters.
- time-window filters.

## Validation Checks

- Tenant A cannot download Tenant B object even with guessed object key.
- Failed media job has no fake ready asset.
- Smart Group shows a newly generated asset if it matches the query.
- Collection membership does not change when a matching future asset is created.
- Character creation records source image and consent/allowed-use metadata.
- Deleting an asset revokes signed URLs and hides it from smart groups.

