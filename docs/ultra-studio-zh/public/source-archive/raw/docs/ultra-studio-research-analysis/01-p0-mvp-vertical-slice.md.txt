# P0 MVP Vertical Slice

Status: final P0 build scope  
Date: 2026-06-11

## Goal

Build one real creative loop before adding platform infrastructure:

```text
user prompt + optional upload
  -> real Hermes web session
  -> workflow-router classification
  -> Atlas image/video generation
  -> media_job record
  -> asset record
  -> streamed status
  -> rendered image/video in chat and inspector
```

This is the first slice. It should be boring, narrow, and testable.

## P0 User Stories

| Story | Required behavior |
|---|---|
| Ask a normal question | Reply normally. No media job is created. |
| Generate an image | Route to Atlas image generation and render the image in the UI. |
| Generate a video | Route to Atlas video generation and render the video in the UI, or return a typed blocker. |
| Upload an image then ask for a video | Upload creates an asset; router uses that asset as image input if supported. |
| Refresh during generation | Session resumes and current job/asset state remains visible. |
| Provider failure | Show structured error; no fake asset or fake completion. |

## P0 Non-Goals

Do not build these in P0:

- Keycloak.
- Istio.
- Kueue.
- Temporal cluster.
- NATS JetStream.
- Kata microVM.
- Rook-Ceph.
- Argo CD.
- full OpenBao TokenRouter.
- full CometAPI.
- browser context fleet.
- marketplace publishing.
- smart groups.
- Character/Element lifecycle.
- full memory UI.
- autonomous posting or account actions.

They are legitimate later architecture, but they block the first real loop if pulled into P0.

## Existing Code To Use

| Area | Path | Use in P0 |
|---|---|---|
| Web chat | `web/src/pages/ChatPage.tsx` | Treat as P0 chat baseline. |
| Gateway hook | `web/src/hooks/useGatewayChat.ts` | Use existing `session.create`, `session.resume`, `prompt.submit` flow. |
| Web routes | `hermes_cli/web_server.py` | Reuse `/api/ws`, `/api/events`, upload route, plugin API route patterns. |
| Uploads | `hermes_cli/dashboard_uploads.py` | Wrap uploads into typed assets. |
| Atlas image | `plugins/image_gen/atlas/` | Use via existing `image_generate` tool path. |
| Atlas video | `plugins/video_gen/atlas/` | Use via existing `video_generate` provider path. |
| Router skill | `skills/creative/workflow-router/SKILL.md` | Use as routing policy until deterministic router tool exists. |
| Skill allowlist helper | `hermes_cli/ultra_studio_skills.py` | Apply Ultra profile allowlist. |

## Required P0 Work

1. Choose React `/api/ws` chat as the baseline.
2. Add `media_jobs` and `media_assets` persistence.
3. Upgrade uploads to create `asset_id` while preserving existing `path`.
4. Add `ultra_media_job_create`, `ultra_media_job_status`, and `ultra_asset_get`.
5. Wrap existing Atlas image/video tools behind those job functions.
6. Emit `media_job.created`, `media_job.updated`, and `asset.ready` events.
7. Render media job cards and final assets in chat.
8. Show selected job/asset details in inspector.
9. Wire `workflow-router` to the job/asset contract.
10. Add smoke tests for real Atlas success or typed missing-credential blockers.

## P0 Acceptance

| Scenario | Pass condition |
|---|---|
| `你好` | Text response only; no `media_job` row. |
| `帮我生成一个猫的图片` | `media_job` row, Atlas image call, `asset.ready`, image rendered. |
| `做一个猫的视频` | `media_job` row, Atlas video call or typed blocker, video rendered if complete. |
| upload image + `把这张图做成视频` | upload creates `asset_id`; job references that asset. |
| refresh while job running | UI restores session and job status. |
| missing `ATLAS_API_KEY` | typed `missing_credential`; no FAL fallback; no fake result. |
| provider returns no output | typed `empty_response`; no ready asset. |

## Build Order

1. Data model and API wrapper.
2. Upload-to-asset.
3. Media job wrapper around Atlas tools.
4. Event emission.
5. Chat/media card rendering.
6. Inspector details.
7. Router handoff.
8. Smoke tests and fixtures.

