---
name: infographic-md-flow
description: "Motion-design reel where data is the subject: KPI animation, dashboard reel, annual report video, chart reveal, process-flow or system-diagram motion. Use when numbers, charts, or nodes are the visual subject, not decoration."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative, motion-design, infographic, data-visualization, video-generation, atlas, storyboard, kpi, dashboard]
    related_skills: [baoyu-infographic, manim-video, comfyui, design-md, architecture-diagram]
---

# Infographic MD Flow

## Overview

`infographic-md-flow` creates a short motion-design reel where data is the
subject. It is for metrics, charts, dashboards, process steps, architecture
nodes, and system relationships that need to become a readable animated visual
system.

This is a clean-room execution spec. It describes observable workflow behavior
and production constraints; it is not original hidden prompt text and should not
be treated as a clone of any private workflow.

The workflow has a fixed production line:

1. Stage A: resolve a visual foundation.
2. Stage B: generate a six-panel 3x2 storyboard.
3. Stage C: animate the storyboard into video variants and pick the cleanest.

## When to Use

Use this skill when removing the data would remove the video's main subject.

Good triggers:

- KPI animation, stats reel, chart reveal, dashboard reel
- annual report, investor update, SaaS metric reveal
- process flow, pipeline, state transition, how-it-works diagram
- system architecture, node graph, modules, relationships, data flow
- prompts where numbers, charts, nodes, or steps must be visually dominant

Do not use this skill for:

- generic brand ads with one decorative metric
- product commercials, marketplace listings, or product hero shots
- UGC, talking-head, CEO-in-office, interview, or office cinematic report
- slogan, wordmark, or letterform as the visual subject
- photoreal lifestyle scenes, human drama, or cinematic story treatment
- high-energy chaotic brand reveals where data lines are just decoration

Adjacent routing, worked trigger examples, and boundary cases:
`references/routing-examples.md`.

## Cost and Tool Safety

Use live media generation only when the user is actually asking to generate
assets. For planning, review, or spec-writing tasks, stop at the plan or prompt.

Do not ask the user to paste `ATLAS_API_KEY` or any media-provider secret into
chat. Hermes keeps provider credentials server-side. If generation fails because
the provider is unavailable, report that configuration is missing and do not
fake a job, URL, or output asset.

Never invent a final video URL, task ID, or completed generation result. Never
claim reference-image conditioning or multi-reference input happened unless the
active backend actually supports it. Exact tool surface and parameter defaults:
`references/stage-workflow.md`.

## Hard Invariants

- Use only user-provided metric strings, labels, steps, nodes, and relationships.
- Never add invented quarters, statuses, percentages, dates, callouts, or labels.
- Key numbers must be headline-tier and visually dominant.
- Each storyboard panel has one main information subject.
- Critical labels must be readable; if labels would be tiny, remove them.
- Motion must encode meaning: growth, comparison, connection, flow,
  transformation, hierarchy, or state change.
- Charts, nodes, and numbers are not decorative background texture.
- Keep the palette to two or three core colors.
- Camera supports data. Data does not support camera.
- Avoid photoreal humans, office scenes, product hero shots, and cinematic
  lifestyle treatment.
- Avoid highMD chaos: smash cuts, whip-pan overload, fast flying data lines with
  no readable data.
- Final one to two seconds must hold a stable resolve frame.
- Do not fade to black over the final insight, logo, or wordmark.

## Minimal End-to-End

1. **Lock inputs.** Pick exactly one variant — `n-stats-sequence`,
   `process-flow`, or `system-diagram` — plus aspect ratio and style tier. Copy
   the user's exact metric/step/node strings into an `allowed_text` set. Ask only
   for material that is genuinely missing.
2. **Stage A — foundation.** Classify any supplied images as `foundation` /
   `logo` / `style_ref` / `ignore`. If there is no foundation image, generate a
   four-up moodboard with `image_generate` and have the user pick a frame.
3. **Stage B — storyboard.** Generate one six-panel 3x2 storyboard sheet:
   one information subject per panel, P06 a stable final resolve.
4. **Stage C — video.** `video_generate` with the storyboard as `image_url`,
   10s / 720p / no audio by default, 2 candidates varying only motion phrasing.
5. **QA and deliver.** Score each candidate; `metric_fidelity` must be 5. Repair
   and regenerate on failure, then return video URL, storyboard URL, variant, QA
   scores, provider/model, and limitations.

## References

Load on demand with `skill_view(name="infographic-md-flow", file_path="references/...")`.

| To do this | Read |
|------------|------|
| Pick the variant, collect the required inputs, or plan the six-panel sequence | `references/variants.md` |
| Choose between the premium 3D and illustrated tiers | `references/style-tiers.md` |
| Execute Stage A/B/C — tool surface, aspect-ratio mapping, asset manifest, foundation, logo placement, video defaults, delivery format | `references/stage-workflow.md` |
| Write the moodboard, storyboard, or video prompt | `references/prompt-templates.md` |
| Score a candidate, repair a specific failure, or run the final checklist | `references/qa-and-repair.md` |
| Decide whether this skill applies at all, or where to route instead | `references/routing-examples.md` |
