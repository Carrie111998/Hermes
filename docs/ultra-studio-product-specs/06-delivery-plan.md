# Ultra Studio Delivery Plan

Status: execution plan  
Date: 2026-06-10

## Principle

Ship the smallest real creative agent loop first:

```text
chat -> route -> upload/asset -> Atlas job -> status stream -> asset card -> inspector
```

Do not build a fake demo path. If a provider, upload, or job fails, the UI should
show the real blocker.

## P0: Real Loop

Goal: one real image/video generation flow from web chat.

Build:

1. Ultra profile/allowlist bootstrap.
2. Left nav shell with Tasks, Files, Memory, Marketplace placeholders.
3. Real chat upload into typed `media_input`.
4. `workflow-router` runtime connection.
5. `ultra_media_job_create/status/finalize`.
6. Atlas image/video provider integration through real tools.
7. Streaming job status in chat.
8. Inspector for selected job/asset.
9. Asset registration and download.
10. Typed errors.

Acceptance:

- User asks for an image and receives a real generated asset.
- User asks for a video and gets a real job or a typed blocker.
- Uploaded image can be used as a reference asset.
- Refreshing the page does not fake job completion.
- Inspector shows job/model/input/output details.

## P1: Production Creative Workflows

Goal: turn the real loop into reusable creative workflows.

Build:

1. Product photoshoot skill.
2. InfographicMD workflow runtime.
3. ProductMD / UGC / cinematic workflow docs and first implementation.
4. Prompt compiler and provider constraints registry.
5. Media QA and prompt repair flow.
6. Asset library detail view.
7. Element and Character creation from selected assets.
8. Marketplace local catalog.
9. Memory page with visible/revocable entries.

Acceptance:

- Vague requests ask one useful question.
- Clear workflow requests create structured plans.
- Generated assets can become Elements or Characters.
- Marketplace shows available workflows and status.
- Memory can influence a follow-up request and can be inspected.

## P2: Task Computer

Goal: make the product behave like a real creative task computer.

Build:

1. Sandbox lifecycle manager.
2. Task file browser.
3. Artifact bundle export.
4. Browser context store.
5. Local browser/desktop bridge.
6. Durable workflow engine.
7. Human approval gateway.
8. Observation/provenance ledger.
9. Collaboration/share privacy boundary.

Acceptance:

- Running jobs survive worker/session interruption.
- Files created during work are browseable.
- Browser/downloaded artifacts are captured with provenance.
- Cost/private/publish actions require approval.
- Shared sessions do not leak credentials or sandbox files.

## P3: Platform

Goal: make the system extensible and operable.

Build:

1. Skill eval harness.
2. Marketplace publishing flow.
3. Model benchmarking and model recipe quality reports.
4. Team permissions and project policies.
5. Observability dashboards.
6. Billing/quota integration.
7. Scheduled recurring creative tasks.

## Launch Gates

Do not launch a public demo until:

- No fake media URLs.
- No hardcoded job results.
- No accidental FAL/Comfy fallback.
- Atlas credential path is explicit.
- User uploads are real files.
- Assets have real ids and download paths.
- Failed provider calls remain visible.
- The visible skill list is focused.

## Test Commands / Checks

Minimum checks for doc-only changes:

- line count under 800 per file
- markdown link scan
- no duplicate doc for same surface

Minimum checks for runtime changes:

- Python tests for tool contracts
- frontend build/typecheck for UI
- gateway event smoke test
- one real upload smoke test
- one real Atlas job smoke test, or typed missing-credential blocker

## Open Questions

- Should Marketplace be local-only for MVP or backed by a server catalog?
- Should Memory be project-scoped by default, or user-scoped with project filters?
- Should Task Files and Asset Library share storage keys or use separate buckets?
- Which Atlas video model is the P0 default?
- Which actions require explicit approval before first launch?

