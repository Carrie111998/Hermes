# Ultra Studio Agent Complete Skill / Tool / Prompt Specification

Status: complete implementation specification
Date: 2026-06-10
Scope: magic-mod Hermes Agent into an Atlas-first video creation agent

## Objective

Turn this fork into a focused creative video agent instead of a general-purpose
Hermes install with dozens of unrelated skills.

The target is not "one more plugin." The target shape is:

1. A narrow visible skill catalog for video creation.
2. A router skill that loads detailed workflows only when needed.
3. Atlas-backed image and video tools with real job, artifact, and error
   contracts.
4. Prompt templates that compile a user request into a production plan, then
   into Atlas-compatible media prompts.
5. A deletion/disable strategy that removes unrelated Hermes skills without
   breaking the runtime.

## Done When

This document is complete when an implementer can make the fork behave as a
focused video agent without asking for hidden architecture decisions.

Completion criteria:

- The visible skill list is video-focused.
- Vague video requests route through `workflow-router` and ask one useful
  question instead of auto-generating.
- Clear image requests call Atlas image generation directly.
- Clear video requests call the selected workflow skill, then real Atlas media
  tools.
- Uploaded images/videos are treated as typed assets, not prompt text only.
- The UI can show job state, progress, errors, and final media artifacts.
- Missing provider credentials or unsupported model features produce typed
  errors, never fake success.
- Unrelated Hermes skills are disabled first and deleted only after verification.

## Non-Goals

- Do not preserve upstream Hermes' general-purpose skill catalog.
- Do not contribute this product direction back to `NousResearch/hermes-agent`.
- Do not build a fake demo site or fake job runner.
- Do not call FAL, ComfyUI, or any non-Atlas backend unless explicitly enabled
  by this fork's configuration.
- Do not promise multi-reference video conditioning until the active Atlas video
  provider reports it as supported.
- Do not hide provider failures behind generic chat apologies.

## Evidence

Local Hermes facts:

- `skills_list` and `skill_view` already implement progressive disclosure:
  metadata first, then `SKILL.md`, then referenced files.
- `skills.disabled` and `skills.platform_disabled` already hide skills from
  discovery without deleting files.
- `skills.external_dirs` already supports alternate skill roots.
- Plugin skills registered with `PluginContext.register_skill()` are namespaced
  and explicit-load only; they do not enter the flat global skill index.
- `image_generate` and `video_generate` are unified tools backed by provider
  plugins.
- Atlas image/video providers already exist under:
  - `plugins/image_gen/atlas/`
  - `plugins/video_gen/atlas/`
- Atlas video currently supports text-to-video and image-to-video, but reports
  `max_reference_images: 0`; do not promise multi-reference conditioning yet.
- The real chat UI contract requires gateway events, real uploads, real media
  references, and no fake jobs or hardcoded video briefs.

Current repo state:

- `plugins/image_gen/atlas/` and `plugins/video_gen/atlas/` are already present.
- `skills/creative/infographic-md-flow/` exists but is still untracked in the
  current worktree.
- No unrelated Hermes skills have been physically deleted in the current
  worktree.
- The correct remote for this fork is the user's repo; upstream should remain a
  fetch-only reference.

External design references:

- Claude skill docs: skills are `SKILL.md` packages with frontmatter used for
  invocation; supporting files keep the main skill concise.
- MCP tool docs: tools should have schema-defined inputs and may return
  structured content and resource links.
- MCP client guidance: loading too many tools/definitions upfront wastes
  context, increases latency, and degrades model behavior.
- Higgsfield public product pages are useful as product-shape references:
  creative agent, skills, reusable history, async generation, and project assets.

## Notion Alignment

Notion page inspected:

- Page: `Hermes`
- ID: `36e0837e-a8a7-80ee-b368-e66611362836`
- Last edited: `2026-06-09T10:58:00.000Z`
- Relevant child pages: `Skill`, `Prompt`, `Prompt enhance`,
  `工具调用说明文档`, `tokenrouter`, `CometAPI`, `soulID`,
  `资产管理 Element Management`.

Important constraints to carry into this fork:

- `references` has three separate meanings and the implementation must not mix
  them: skill-internal `references/`, user-uploaded attached references, and
  reusable media references such as `image_job`, `video_job`, `media_input`,
  `soul_id`, and `element_id`.
- `skill_view(name)` and `skill_view(name, file_path=...)` are intentionally
  separate loads. A workflow cannot assume loading a reference file also loaded
  the parent `SKILL.md`.
- `SKILL.md` is the warm layer; `references/` is the cold, precise layer. Long
  API schemas, prompt compiler contracts, QA rubrics, and flow-specific inputs
  belong in `references/`, not the global prompt.
- The media tool group in the reference architecture contains generation,
  status polling, upload, balance/limits, asset list, reusable elements,
  Soul ID, and prompt enhancer. Ultra Studio should mirror this shape with
  `ultra_media_job_*`, `ultra_asset_*`, `ultra_model_catalog`, and
  `ultra_prompt_compile`.
- Prompt enhancement is a compiler boundary: the agent collects structured JSON
  such as shot plans and asset roles; a backend compiler/enhancer turns it into
  model-specific production prompts.
- TokenRouter and CometAPI imply a control-plane/data-plane split. In this fork,
  Atlas credential and model routing stay in the provider/backend layer, while
  media files and reusable assets are handled through asset/job tools.
- Communication policy matters for UI: long jobs need phase updates, but final
  answers should not expose raw job IDs, internal paths, or template markers.
- Clarification must happen after route/skill selection. Do not ask broad
  questions before deciding whether this is image, video, UGC, product,
  infographic, or cinematic work.
- Costly or high-variance generation paths should require one structured
  confirmation only when the choice changes cost, workflow, or output shape.

## Source Of Truth Map

| Contract | Source of truth | Notes |
|---|---|---|
| Skill discovery | `agent/skill_utils.py`, `tools/skills_tool.py` | Existing discovery supports disabled skills and external dirs. |
| Skill toggling | `hermes_cli/skills_config.py` | Use `skills.disabled` before deleting files. |
| Image generation provider | `plugins/image_gen/atlas/` | Atlas image payload stays top-level. |
| Video generation provider | `plugins/video_gen/atlas/` | Atlas video payload stays top-level and polls prediction status. |
| Chat/media UI | `docs/hermes-real-chat-agent-ui.md` | UI must use real gateway events and uploads. |
| Workflow skill content | `skills/creative/*/SKILL.md` | This is the creative workflow layer, not plugins. |
| High-level media jobs | new `tools/ultra_media_*` files | This layer is missing and must be implemented. |
| Product prompt identity | agent system prompt addendum | Should be fork-specific and Atlas-first. |

## Chosen Architecture

Treat Hermes as an agent runtime with four separate layers:

```text
chat/session UI
  -> prompt + skill router
  -> workflow skills
  -> Atlas media tools
  -> provider plugins / storage / gateway events
```

Do not collapse these layers into one plugin.

### Layer 1: Agent Identity

Rename the product-facing agent to something like:

- `Ultra Studio Agent`
- public tagline: `Bringing it to life`
- role: Atlas-first creative video production assistant

Core identity prompt:

```text
You are Ultra Studio Agent, an Atlas-first creative video production agent.
You help users make images, videos, edits, reels, ads, and motion-design
assets through real Hermes tools.

Do not fake media outputs, job ids, URLs, assets, provider status, or model
decisions. If a provider is unavailable, report the exact missing capability.
Ask only workflow-changing questions. Prefer creating real artifacts through
tools when the user asks to generate media.
```

### Layer 2: Tool Surface

Keep the existing provider plugins, but add a higher-level media tool layer
for creative workflows.

Existing low-level provider tools:

| Tool | Role |
|---|---|
| `image_generate` | Generate one image through configured provider. |
| `video_generate` | Generate one video through configured provider. |

New high-level tools:

| Tool | Purpose |
|---|---|
| `ultra_asset_upload` | Register uploaded image/video/audio files and return a typed asset ref. |
| `ultra_asset_list` | List session/project media assets for reuse. |
| `ultra_asset_inspect` | Return media type, dimensions, duration, local path or URL, and role hints. |
| `ultra_media_job_create` | Submit image/video generation with workflow metadata. |
| `ultra_media_job_status` | Poll job state and stream stage-level progress. |
| `ultra_media_job_cancel` | Cancel a queued/running generation if backend supports it. |
| `ultra_media_asset_download` | Materialize remote output to local/project storage. |
| `ultra_model_catalog` | Return Atlas image/video model families and constraints. |
| `ultra_prompt_compile` | Compile workflow plan into provider-specific prompt payloads. |
| `ultra_skill_route` | Structured router output for choosing one workflow skill. |

These tools are not replacement providers. They are workflow orchestration
tools that may call `image_generate`, `video_generate`, or provider adapters
internally.

The high-level tools should return structured JSON plus a short text summary.
For media outputs, include resource-style references:

```json
{
  "success": true,
  "job_id": "atlas:prediction-id",
  "state": "completed",
  "provider": "atlas",
  "model": "wan-2.6-flash",
  "modality": "image-to-video",
  "artifacts": [
    {
      "type": "video",
      "url": "https://...",
      "local_path": "/Users/.../cache/video.mp4",
      "role": "final_video"
    }
  ],
  "workflow": {
    "skill": "infographic-md-flow",
    "stage": "stage_c_render"
  }
}
```

Errors must be typed:

```json
{
  "success": false,
  "error_type": "auth_required | unsupported_parameter | provider_error | timeout | empty_response | invalid_asset",
  "message": "human-readable reason",
  "recoverable": true,
  "next_action": "configure_atlas | remove_reference_images | retry_status | ask_user"
}
```

### Tool Contract Details

Keep schemas compact and strict:

| Tool | Required input | Output contract |
|---|---|---|
| `ultra_asset_upload` | `path` | `asset_id`, media type, dimensions/duration, local path, role. |
| `ultra_asset_inspect` | `asset_id` or `path` | normalized media metadata and role hints. |
| `ultra_media_job_create` | `kind`, `prompt` | `job_id`, state, provider, model, artifacts or typed error. |
| `ultra_media_job_status` | `job_id` | current state, elapsed seconds, provider status, artifacts when complete. |
| `ultra_model_catalog` | optional `kind` | Atlas model families and constraints. |
| `ultra_prompt_compile` | workflow plan | provider-ready prompt and payload summary. |

Job creation rules:

- `kind=image` calls `image_generate`.
- `kind=video` without `image_url` calls text-to-video.
- `kind=video` with `image_url` or `asset_id` calls image-to-video.
- Local image assets must pass through the existing Atlas normalization path.
- Provider errors return `success=false`; no synthesized artifact.

Stable job state machine:

```text
queued -> preparing -> submitting -> generating -> polling -> downloading -> completed
queued -> preparing -> failed
generating -> cancelled
polling -> timeout
```

Recommended UI labels: `thinking`, `routing`, `planning`,
`preparing_assets`, `creating_image`, `storyboarding`, `creating_video`,
`polling`, `reviewing`, `complete`, `failed`.

### Gateway Event Contract

Long media jobs must not freeze the chat UI. The backend should emit:

```text
status.update      state/routing/planning labels
tool.start         tool name + workflow stage
tool.progress      provider status, poll count, elapsed seconds
tool.complete      structured output or typed error
message.delta      assistant natural-language progress text
message.complete   final answer with artifact references
```

The frontend should render media artifacts from structured fields first. Raw
Markdown links are fallback only.

## Atlas API Contract

Atlas should be the only media generation backend for this fork unless the user
explicitly enables another backend.

Public request payloads should stay top-level:

Image:

```json
{
  "model": "google/nano-banana-2/text-to-image",
  "prompt": "text prompt",
  "aspect_ratio": "16:9",
  "output_format": "png",
  "enable_sync_mode": true,
  "num_images": 1
}
```

Video:

```json
{
  "model": "alibaba/wan-2.6/image-to-video-flash",
  "prompt": "motion prompt",
  "image": "https://... or data:image/...",
  "duration": 5,
  "resolution": "720P",
  "enable_sync_mode": false
}
```

Do not expose downstream provider fields like `input.img_url` to skills or UI.
Those belong inside Atlas workflow/router/provider mapping.

Default model policy:

| Need | Default |
|---|---|
| image foundation / storyboard | `nano-banana-2` |
| fast basic video | `wan-2.6-flash` |
| higher-quality standard video | `wan-2.6` |
| faster social reel alternative | `seedance-1.5-pro-fast` |
| premium cinematic experiment | `veo3.1` or `sora-2`, explicit user or config choice |

## Skill Catalog Target

The final visible catalog should not contain all general Hermes skills.

Keep P0 video-focused skills:

| Skill | Role |
|---|---|
| `workflow-router` | First skill loaded for ambiguous creative/video requests. |
| `infographic-md-flow` | Data-as-subject motion reel. |
| `product-photoshoot` | Product image/foundation generation and product video setup. |
| `ugc-flow` | UGC-style ad generation, hook/body/CTA, optional avatar/voice later. |
| `app-sizzle` | App/site teaser from screenshots or product URL. |
| `cinematic-trailer` | Generic cinematic brief -> shot plan -> video generation. |
| `media-qa` | Evaluate outputs for artifact, text, motion, identity, and instruction match. |
| `prompt-repair` | Repair failed Atlas prompts based on provider error/visual QA. |

P0 `SKILL.md` frontmatter should be precise because Hermes uses metadata for
selection:

```yaml
---
name: workflow-router
description: "Route creative image/video requests to the right Ultra Studio workflow. Use for vague video requests, uploaded media, or when deciding between image generation, video generation, infographic motion, product ad, UGC, app teaser, or cinematic trailer."
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ultra-studio, video, router, atlas]
---
```

Avoid vague descriptions like "helps make videos"; those cause broad and noisy
activation.

Keep P1 adjacent skills only if they directly support video production:

| Skill | Keep condition |
|---|---|
| `ascii-video` | Only if you still want retro/ASCII video mode. Otherwise remove. |
| `pixel-art` | Only if pixel-art video is an intended creative mode. |
| `songwriting-and-ai-music` | Only if music/audio generation becomes part of video workflows. |
| `youtube-content` | Only if ingesting/analyzing YouTube source videos is in scope. |
| `touchdesigner-mcp` | Only if realtime/installation visuals are in scope. |
| `manim-video` | Usually remove; it is code-rendered explainer video, not Atlas media generation. |
| `comfyui` | Remove unless ComfyUI is a supported backend in this fork. |

Disable or delete all unrelated categories:

- `apple`
- `autonomous-ai-agents`
- `data-science`
- `devops`
- `email`
- `gaming`
- `github`
- `mcp` except if needed for media connectors
- `mlops` except media model utility you intentionally keep
- `note-taking`
- `productivity`
- `red-teaming`
- `research`
- `smart-home`
- `social-media`
- generic `software-development`

### Final Visible Catalog

The target default catalog should be small enough that the agent can reason
about it without listing categories to the user:

```text
creative/
  workflow-router
  infographic-md-flow
  product-photoshoot
  ugc-flow
  app-sizzle
  cinematic-trailer
  media-qa
  prompt-repair
```

Optional, only if explicitly retained:

```text
creative/
  ascii-video
  pixel-art
media/
  youtube-content
```

Everything else should be disabled by default for this fork.

## Progressive Disclosure Strategy

The user should not see or trigger 87 skills.

Use this flow:

```text
User request
  -> global prompt says "creative video agent"
  -> metadata exposes only 6-10 visible skills
  -> workflow-router loads first for vague requests
  -> router returns structured route JSON
  -> selected skill loads
  -> selected skill reads only needed references
  -> tools execute real media jobs
  -> media-qa / prompt-repair load only after an output or failure
```

`workflow-router` should be short. Its job is not to teach video production;
its job is to classify and ask at most one blocking question.

Router output schema:

```json
{
  "intent": "chat | image | video | edit | analysis | unsupported",
  "workflow": "infographic-md-flow | product-photoshoot | ugc-flow | app-sizzle | cinematic-trailer | none",
  "confidence": 0.0,
  "execution_mode": "answer | ask | generate_image | generate_video | multi_stage",
  "missing": ["aspect_ratio", "source_image", "metrics"],
  "asset_roles": [
    {"asset_id": "upload:...", "role": "foundation | logo | style_ref | source_video | audio | unknown"}
  ],
  "recommended_tools": ["image_generate", "video_generate"],
  "reason": "short reason"
}
```

Routing rules:

| User request | Route |
|---|---|
| "做一个视频" | Ask one question: type/purpose, unless attached media makes it clear. |
| "帮我生成一个猫的图片" | Direct `image_generate`; no video skill. |
| "把这张图做成视频" | `cinematic-trailer` or `product-photoshoot` depending asset role. |
| KPI/dashboard/report/data flow | `infographic-md-flow`. |
| Product photo, product ad, Amazon/TikTok listing | `product-photoshoot` then `ugc-flow` if ad script needed. |
| App screenshots/site demo | `app-sizzle`. |
| "像广告口播/UGC" | `ugc-flow`. |
| Need critique/retry | `media-qa` then `prompt-repair`. |

## Skill Package Structure

Use one `SKILL.md` plus shallow references.

```text
skills/creative/workflow-router/
  SKILL.md
  references/router-schema.md

skills/creative/infographic-md-flow/
  SKILL.md
  references/prompt-templates.md
  references/qa-rubric.md

skills/creative/product-photoshoot/
  SKILL.md
  references/shot-types.md
  references/prompt-templates.md
  references/qa-rubric.md

skills/creative/ugc-flow/
  SKILL.md
  references/script-structure.md
  references/prompt-templates.md
  references/qa-rubric.md

skills/creative/app-sizzle/
  SKILL.md
  references/storyboard.md
  references/prompt-templates.md
  references/qa-rubric.md

skills/creative/media-qa/
  SKILL.md
  references/scoring.md

skills/creative/prompt-repair/
  SKILL.md
  references/provider-failures.md
```

`SKILL.md` body should stay under roughly 300-500 lines. Put long examples,
model-specific prompting, and rubrics under `references/`.

### Required Skill Contents

Every workflow skill needs these sections:

```text
Overview
When to Use
Do Not Use
Input Contract
Blocking Questions
Asset Handling
Workflow Stages
Prompt Compile Rules
Tool Calls
Failure Handling
Delivery Format
QA / Repair
```

Every workflow skill must include this line:

```text
Do not present generated media unless a Hermes media tool returned a real artifact.
```

`workflow-router` should include no model-specific prompt lore. It only routes.

`prompt-repair` should be loaded only after one of these:

- provider error
- empty response
- user says the output is wrong
- QA score fails threshold
- user asks for another version based on a concrete failure

`media-qa` should not generate media. It scores and recommends repair.

## Prompt Design

### Global Agent Prompt Addendum

```text
Ultra Studio Agent is a focused creative media agent.

Primary rule: use Atlas-backed Hermes media tools for real generation.
Never present generated media unless a tool returned it.
Never invent job ids, media URLs, model names, metrics, or source assets.

For vague creative requests, route first. Ask at most one blocking question.
For clear image requests, generate an image directly.
For clear video requests, choose a workflow skill, build a short production
plan, then execute with real tools.
For expensive or ambiguous multi-stage generation, explain the stages before
the first media job.
```

### Router Prompt

```text
Classify the user request for Ultra Studio Agent.
Return JSON only.
Do not generate content in this step.
Choose exactly one workflow or `none`.
Only include missing fields that block correct execution.
```

### Workflow Skill Prompt Pattern

Every workflow skill should follow the same internal sequence:

```text
1. Intake: summarize user-provided facts and attachments.
2. Route guard: confirm this skill is appropriate.
3. Missing gate: ask only if required facts are absent.
4. Asset manifest: classify uploads and generated assets.
5. Stage plan: foundation -> storyboard/shot plan -> render.
6. Prompt compile: provider-ready prompt with constraints.
7. Tool call: create real image/video job.
8. Delivery: show artifact, provider/model, limitations, and next options.
9. QA: optional scoring and repair path.
```

### No-Fake Prompt Clause

Every media workflow skill should include this invariant:

```text
If a media tool does not return success and an artifact URL/path, do not say
the image or video was created. Report the typed error and recovery path.
```

## Disabling and Deletion Plan

Do this in two phases.

### Phase A: Disable First

Add a generated allowlist and disable everything else through config or a
repo-owned bootstrap.

Example allowlist:

```yaml
ultra_studio:
  skill_allowlist:
    - workflow-router
    - infographic-md-flow
    - product-photoshoot
    - ugc-flow
    - app-sizzle
    - cinematic-trailer
    - media-qa
    - prompt-repair
```

Then compute:

```text
disabled = all_installed_skill_names - skill_allowlist
```

Write to:

```yaml
skills:
  disabled:
    - apple-notes
    - github-pr-workflow
    - ...
```

Why disable first:

- It preserves rollback.
- It proves the runtime works with a narrow skill index.
- It avoids breaking tests/docs that assume bundled skill files exist.
- It lets us measure startup/selection behavior before irreversible deletion.

### Phase B: Physical Delete or Archive

After the narrowed agent works:

1. Create a snapshot of the skill tree.
2. Move unrelated bundled skills to `archived-skills/` or delete them in one
   explicit commit.
3. Update docs/tests that count skills or categories.
4. Keep only video-related skills in `skills/creative/` and optionally
   media-analysis skills in `skills/media/`.

Do not delete provider plugins just because their skills are gone. Provider
plugins are runtime infrastructure; skills are routing/prompt packages.

## Implementation Roadmap

### P0: Make It Coherent

- Track the new `infographic-md-flow` skill.
- Add `workflow-router`.
- Add `media-qa` and `prompt-repair` minimal skills.
- Add a config/bootstrap mechanism for a video-only skill allowlist.
- Set image/video provider defaults to Atlas in the fork's default config.
- Add tests proving disabled skills do not appear in `skills_list`.

### P1: Real Media Job Layer

- Add `ultra_media_job_create/status/cancel`.
- Preserve Atlas `prediction_id` and provider/model metadata.
- Stream `status.update` and `tool.progress` events for long video jobs.
- Add artifact materialization and UI rendering for image/video URLs.
- Add tests for typed error responses and no fake success.

### P2: Workflow Skills

- Add `product-photoshoot`.
- Add `ugc-flow`.
- Add `app-sizzle`.
- Add `cinematic-trailer`.
- Split prompt templates and QA rubrics into references.

### P3: Delete/Archive Legacy Skills

- Generate the full unrelated-skill deletion list.
- Disable first, run tests, then physically delete/archive.
- Update README/docs that advertise general Hermes skills.
- Commit as a fork-specific product direction, not an upstream contribution.

## Validation Matrix

Run before claiming the fork works:

```bash
uv run pytest \
  tests/plugins/image_gen/test_atlas_provider.py \
  tests/plugins/video_gen/test_atlas_plugin.py \
  tests/agent/test_external_skills.py \
  tests/test_plugin_skills.py
```

Add new tests:

- `test_ultra_skill_allowlist_hides_unrelated_skills`
- `test_workflow_router_routes_image_request_without_video_skill`
- `test_workflow_router_asks_for_vague_video_request`
- `test_infographic_skill_uses_only_user_metrics`
- `test_ultra_media_job_create_requires_real_provider_success`
- `test_video_tool_rejects_reference_images_for_atlas_until_supported`

Manual acceptance:

1. User says `帮我生成一个猫的图片`.
   - Agent calls image generation only.
   - Frontend shows the real image.
2. User says `我要做一个视频`.
   - Agent asks one useful question; it does not auto-generate.
3. User uploads an image and says `把这张图做成5秒视频`.
   - Agent classifies upload as foundation/source image.
   - Agent calls Atlas image-to-video.
   - Frontend streams status and shows the final video.
4. User asks KPI/dashboard/data flow video.
   - Agent routes to `infographic-md-flow`.
   - It does not invent metrics.
5. Skill list is video-focused.
   - Unrelated Hermes skills do not appear.

## Main Risks

- Deleting bundled skills too early can break docs, tests, skill counts, and
  user rollback.
- If non-video skills remain visible, the agent will keep loading irrelevant
  routes such as ASCII/video/music/devops.
- If Atlas tool errors are not typed, the agent will hallucinate success or
  give generic provider advice.
- If multiple references are promised before Atlas supports them, generated
  videos will not match the user's asset expectations.
- If router and workflow prompts both classify intent, routing will drift.
  Router owns classification; workflow skills own execution.

## Decision

Use Hermes' existing skill architecture, not plugin skills, for the creative
workflow layer.

Use provider plugins only for low-level Atlas image/video integration.

Use a high-level media job tool layer for status, assets, progress, and UI
contracts.

Disable unrelated skills first, then delete/archive after the narrow catalog
is verified.
