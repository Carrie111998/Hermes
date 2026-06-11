# Skill, Tool, And Prompt Contract

Status: workflow specification  
Date: 2026-06-10

## Goal

Prevent the agent from behaving like a generic assistant with 87 unrelated
skills. Ultra Studio should expose a focused creative skill system with
progressive loading, typed tools, and provider-aware prompt compilation.

## Skill Layers

```text
skill metadata
  -> SKILL.md
  -> references/
  -> scripts/
  -> assets/
```

Startup should load only skill metadata. `SKILL.md` loads after routing.
Large schemas, prompt compilers, rubrics, and examples belong in `references/`.

## Required Skill Runtime Objects

| Object | Purpose |
|---|---|
| `Skill Registry` | Discovery, enable/disable, version, profile filtering. |
| `Skill Allowlist Profile` | Ultra Studio visible skill set. |
| `Skill Trigger Eval` | Tests whether routing picks the right skill. |
| `Skill Output Contract Eval` | Tests handoff schema and missing fields. |
| `Skill Resource Loader` | Loads references/scripts/assets only when needed. |

## Router Output

`workflow-router` should produce a structured object:

```json
{
  "intent": "image_generate | video_generate | edit | asset_search | chat | unknown",
  "execution_mode": "answer | ask | tool | workflow",
  "workflow_skill": "infographic-md-flow",
  "primary_tool": "ultra_media_job_create",
  "asset_roles": [],
  "missing": [],
  "handoff": {}
}
```

The router must not generate media itself. It chooses the next workflow or asks
for missing fields.

## Clarification Rules

Ask only when the answer changes:

- output type
- aspect ratio
- asset role
- workflow
- cost/concurrency
- model capability

Do not ask generic questions before routing. A vague request like "make a video"
should route to `video_generate` intent, then ask one useful question about type
or source material.

## Tool Groups

### Asset Tools

- `ultra_asset_upload`
- `ultra_asset_list`
- `ultra_asset_inspect`
- `ultra_asset_download`
- `ultra_asset_promote`

### Media Job Tools

- `ultra_media_job_create`
- `ultra_media_job_status`
- `ultra_media_job_cancel`
- `ultra_media_job_retry`
- `ultra_media_job_finalize`

### Model / Prompt Tools

- `ultra_model_catalog`
- `ultra_media_constraints_get`
- `ultra_prompt_compile`
- `ultra_prompt_enhance`

## Prompt Compiler Boundary

The agent should collect structured intent and asset roles. A prompt compiler
turns that into provider-specific payloads.

```text
user request
  -> route
  -> workflow plan
  -> asset role manifest
  -> provider constraints
  -> prompt compile
  -> job create
```

The compiler should know:

- target media type
- model family
- aspect ratio
- duration
- reference assets
- negative constraints
- workflow skill
- provider input limits

## Required Creative Skills

P0:

- `workflow-router`
- `infographic-md-flow`
- `media-qa`
- `prompt-repair`
- `product-photoshoot`
- `product-md-flow`

P1:

- `ugc-flow`
- `cinematic-flow`
- `typography-md-flow`
- `amazon-product-listing`
- `character-consistency`

## Non-Goals

- Do not expose unrelated general-purpose skills by default.
- Do not delete upstream skills before disable/allowlist verification.
- Do not let plugins replace workflow skills.
- Do not bypass Atlas constraints in prompts.

## Acceptance

- The visible skills list is Ultra-focused.
- A generic video request does not trigger random ASCII/Comfy/Manim skills.
- A clear image request calls the image path without asking irrelevant questions.
- A clear video request creates a real media job or returns a typed blocker.
- Skill output is machine-readable enough for tools and UI.

