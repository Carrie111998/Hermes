# P0 Agent, Skill, Tool, and Media Contracts

Status: canonical P0 runtime contract  
Date: 2026-06-11

## Canonical Endpoint Namespace

Use `/api/...` for web-facing P0 routes.

| Endpoint | Purpose |
|---|---|
| `POST /api/sessions` | Create a session. |
| `POST /api/sessions/{session_id}/resume` | Resume messages, active media jobs, and selected assets. |
| `POST /api/sessions/{session_id}/prompt` | Submit text and attachment asset IDs. |
| `GET /api/sessions/{session_id}/events` | SSE replay and live events. |
| `POST /api/chat/uploads` | Upload media and create an asset. |
| `POST /api/media-jobs` | Create image/video generation job. |
| `GET /api/media-jobs/{job_id}` | Read job status. |
| `POST /api/media-jobs/{job_id}/cancel` | Cancel if provider supports it. |
| `GET /api/assets/{asset_id}` | Read asset metadata. |
| `GET /api/assets/{asset_id}/download` | Get signed or local download URL. |

Existing `/api/ws` can remain the transport for session calls. The contract names above are canonical for implementation and tests.

## Canonical Events

| Event | Meaning |
|---|---|
| `message.delta` | Streaming assistant text. |
| `message.complete` | Assistant text finished. |
| `tool.start` | Tool call started. |
| `tool.complete` | Tool call finished. |
| `tool.error` | Tool call failed. |
| `media_job.created` | Durable media job created. |
| `media_job.updated` | Job status/progress changed. |
| `asset.ready` | Output asset is persisted and previewable. |
| `asset.failed` | Asset publish failed. |
| `approval.requested` | Human input is required. |
| `error` | Structured user-visible error. |

Do not mix `job.created`, `asset.created`, and `job.succeeded` in P0 docs. Use the names above.

## Media Job Schema

```json
{
  "job_id": "mj_...",
  "session_id": "sess_...",
  "run_id": "run_...",
  "tool_call_id": "tool_...",
  "kind": "image_generate",
  "provider": "atlas",
  "model": "nano-banana-2",
  "status": "queued",
  "prompt": "cat image",
  "input_asset_ids": [],
  "output_asset_ids": [],
  "progress": 0,
  "error_code": null,
  "error_message": null,
  "created_at": "2026-06-11T00:00:00Z",
  "updated_at": "2026-06-11T00:00:00Z"
}
```

Allowed statuses:

```text
queued
running
polling
publishing
complete
failed
cancelled
blocked
```

## Asset Schema

```json
{
  "asset_id": "asset_...",
  "session_id": "sess_...",
  "media_job_id": "mj_...",
  "kind": "image",
  "source": "generated",
  "mime_type": "image/png",
  "path": "/local/path/or/object/key",
  "preview_url": "/api/assets/asset_.../preview",
  "download_url": "/api/assets/asset_.../download",
  "width": 1024,
  "height": 1024,
  "duration_sec": null,
  "provider": "atlas",
  "model": "nano-banana-2",
  "prompt_hash": "sha256:...",
  "created_at": "2026-06-11T00:00:00Z"
}
```

P0 may store local paths. The key point is that UI and router use `asset_id`, not raw paths as product identity.

## Router Output

P0 router output must be persisted or attached to the run:

```yaml
intent: chat | image_generate | video_generate | video_from_image | qa | repair | planning
execution_mode: answer_only | ask_once | generate_now | inspect_then_generate | repair_then_retry
workflow_skill: null | infographic-md-flow | media-qa | prompt-repair
primary_tool: null | image_generate | video_generate
asset_roles:
  foundation: []
  image_reference: []
  logo: []
  style_reference: []
  source_video: []
  ignore: []
missing: []
handoff:
  brief: ""
  constraints: []
  allowed_text: []
```

Rules:

- greeting or planning requests cannot create media jobs.
- router asks at most one blocking question.
- router never asks for provider API keys.
- router cannot hand off to `ultra_media_job_create` unless that tool exists.
- if no specialized skill exists, it can compile a direct Atlas prompt.

## P0 Skill Allowlist

P0 visible skills:

- `workflow-router`
- `infographic-md-flow`
- `media-qa`
- `prompt-repair`

Not P0:

- `product-photoshoot`
- `product-md-flow`
- `ugc-flow`
- `amazon-product-listing`
- unrelated legacy skills such as ASCII video, ComfyUI, Manim, music, YouTube, productivity, GitHub, and general devops.

## Atlas Tool Contract

P0 wraps the existing tools:

| Tool | Provider path | P0 wrapper |
|---|---|---|
| `image_generate` | `plugins/image_gen/atlas/` | `ultra_media_job_create(kind=image_generate)` |
| `video_generate` | `plugins/video_gen/atlas/` | `ultra_media_job_create(kind=video_generate)` |

Wrapper requirements:

- create `media_job` before provider call;
- emit `media_job.created`;
- update status while provider runs or polls;
- create `asset` only from real provider output;
- emit `asset.ready` after persistence;
- return typed errors instead of fake output.

## Typed Errors

| Code | Meaning |
|---|---|
| `missing_credential` | `ATLAS_API_KEY` or allowed server credential is unavailable. |
| `invalid_prompt` | Prompt is blank or invalid. |
| `invalid_asset_ref` | Asset ID is unknown or not usable for this request. |
| `unsupported_modality` | Tool/model does not support requested input mode. |
| `provider_api_error` | Provider returned HTTP/API error. |
| `provider_timeout` | Provider did not finish within timeout. |
| `empty_response` | Provider completed without usable output. |
| `asset_publish_failed` | Output existed but could not be persisted. |
| `cancelled` | User or system cancelled the job. |

## Verification

P0 contract tests should assert:

- `你好` does not create `media_job`;
- `image_generate` success creates one job and one asset;
- missing credential creates `missing_credential`;
- Atlas video prediction ID is recorded in job metadata;
- refresh/resume returns active jobs and assets;
- no FAL fallback is used when Atlas is missing.

