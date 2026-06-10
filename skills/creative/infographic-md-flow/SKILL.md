---
name: infographic-md-flow
description: "Use when creating a short data-as-subject motion-design reel: KPI animation, dashboard reel, annual report video, investor data update, process-flow animation, system-diagram motion, chart reveal, or any video where numbers, charts, nodes, processes, or data flow are the visual subject."
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

Adjacent routing:

- Generic motion brand ad: use a classic motion-design workflow.
- Hyperkinetic AI or tech reveal: use a high-energy motion workflow.
- Wordmark or kinetic typography subject: use typography motion.
- Real product as subject: use product/commercial workflows.
- Person talking about data: route to cinematic/UGC, with this skill only for
  the infographic insert.

## Cost and Tool Safety

Use live media generation only when the user is actually asking to generate
assets. For planning, review, or spec-writing tasks, stop at the plan or prompt.

Do not ask the user to paste `ATLAS_API_KEY` or any media-provider secret into
chat. Hermes keeps provider credentials server-side. If generation fails because
the provider is unavailable, report that configuration is missing and do not
fake a job, URL, or output asset.

Hermes' current tool surface matters:

- `image_generate` accepts `prompt` and `aspect_ratio`; provider/model are
  controlled by the user's Hermes configuration.
- `video_generate` accepts `prompt`, optional `image_url`, optional
  `reference_image_urls` when the active backend supports them, `duration`,
  `aspect_ratio`, `resolution`, `audio`, `seed`, and optional `model`.
- For Atlas image generation, the configured default is expected to be a Nano
  Banana route.
- For Atlas video generation, common models include `wan-2.6-flash`,
  `wan-2.6`, and `seedance-1.5-pro-fast`.
- Do not pretend multiple visual references were used if the active backend
  only honors one `image_url`.

## Input Contract

Infer what is unambiguous, but never invent facts.

Required by variant:

| Variant | Required user-provided information |
|---------|------------------------------------|
| `n-stats-sequence` | Exact metric strings, such as `ARR +34%`, `84% retention`, `12K active users` |
| `process-flow` | Exact steps, such as `Upload -> Clean -> Analyze -> Export` |
| `system-diagram` | Exact nodes and relationships, such as `App -> API -> Model -> Database -> Dashboard` |

Ask only for missing material that changes the workflow:

- aspect ratio: `16:9`, `9:16`, `1:1`, `4:3`, or `3:4`
- variant: `n-stats-sequence`, `process-flow`, or `system-diagram`
- exact metrics, steps, nodes, or relationships
- which image is foundation, logo, style reference, or ignored when multiple
  images are supplied
- logo upload or skip when the user mentions a logo but does not provide one
- moodboard frame choice when no foundation image exists

Do not ask whether to create the storyboard, which provider/model to use, or
whether to generate multiple candidates. Those are workflow defaults unless the
user explicitly overrides them.

## Primary Variants

Choose exactly one primary structure. Dashboard is a composition form, not a
fourth variant.

### n-stats-sequence

Use for two or more metrics, KPI sequences, growth stats, investor highlights,
dashboard numbers, market-report data, and annual-report data.

If the user gives only one metric, this variant can still be used, but do not
invent supporting comparisons. Decompose only facts the user supplied.

Typical sequence:

1. stat or hook metric
2. second metric
3. third metric or supplied context
4. dashboard, comparison, or aggregate reveal
5. synthesis or insight using only supplied text
6. wordmark, final insight, or stable brand resolve

### process-flow

Use for steps, workflow, pipeline, state transition, and how-it-works requests.

Typical sequence:

1. step 1
2. step 2
3. step 3
4. step 4 or transformation if supplied
5. resolved system state
6. wordmark or final resolve

### system-diagram

Use for architecture, system maps, entities, modules, relationships, networks,
and data flow.

Typical sequence:

1. entry node
2. second module appears
3. connections form
4. data moves through edges
5. full system reveal
6. wordmark or final resolve

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

## Style Tier

Choose one tier.

`premium-3d-data-viz`:

- Best for investor, SaaS, enterprise, AI platform, dashboard, annual report,
  and high-end data reveal.
- Use 3D numbers, glass/metal/chrome surfaces, luminous data lines, controlled
  parallax, dark or neutral background, strong foreground, and one accent color.

`illustrated-friendly-data-viz`:

- Best for education, wellness, onboarding, consumer explainers, and internal
  training.
- Use softer illustrated nodes, cards, icons, simplified shapes, and lower
  technical intimidation.

## Asset Manifest

Maintain this manifest while working and summarize it when delivering generated
assets:

```yaml
infographic_variant: n-stats-sequence | process-flow | system-diagram
aspect_ratio: 16:9 | 9:16 | 1:1 | 4:3 | 3:4
tier: premium-3d-data-viz | illustrated-friendly-data-viz
allowed_text:
  metrics: []
  steps: []
  nodes: []
  brand: []
assets:
  foundation:
    generation_ref: null
    visual_url: null
    role: foundation | style_ref | generated_moodboard_frame
  storyboard:
    generation_ref: null
    visual_url: null
    role: storyboard
  final_video:
    generation_ref: null
    visual_url: null
    role: final_video
logo:
  visual_url: null
  placement: opener | closer | both | skip
```

`generation_ref` is for the generation chain. `visual_url` is for visual review
and user-facing delivery. Do not confuse them.

## Stage A: Foundation

If the user supplies dashboard, chart, system, brand, or infographic reference
images:

1. Classify every supplied image as `foundation`, `logo`, `style_ref`, or
   `ignore`.
2. Use the foundation as the visual basis.
3. If the current image tool cannot condition on reference images, analyze the
   foundation visually and compile its structure into the storyboard prompt.
4. Do not claim reference-image conditioning happened unless the active tool
   actually supports it.

If the user supplies no foundation image:

1. Generate a four-up moodboard sheet with `image_generate`.
2. Ask the user to choose frame 1, 2, 3, 4, or regenerate.
3. Treat the selected frame as the foundation style.

Moodboard prompt template:

```text
Create a 4-up motion-design moodboard sheet for a data-as-subject infographic reel.
Four distinct directions, one per quadrant. No tiny text. No invented metrics.
Subject structure: <variant>. Audience: <audience>. Tier: <tier>.
Use placeholder data shapes only, with no readable fictional numbers.
Show visual language for charts, nodes, process states, and panels; do not create final video frames.
```

Aspect ratio mapping for `image_generate`:

- `16:9` or `4:3`: use `landscape`
- `9:16` or `3:4`: use `portrait`
- `1:1`: use `square`

## Logo and Wordmark

Logo is optional but must be handled explicitly.

Sources:

- uploaded logo image
- user mentions a logo but does not upload it
- one of several uploaded images is clearly the logo

Placement options:

- `opener`
- `closer`
- `both`
- `skip`

Default to `closer`. Infographic reels usually establish the data system first
and resolve to the brand at the end.

If logo rendering is not reliable in the active model, prefer a simple supplied
logo asset when supported; otherwise use a plain brand wordmark or skip logo.
Do not generate garbled pseudo-logo text.

## Stage B: Six-Panel Storyboard

Always create a six-panel 3x2 storyboard before video unless the user explicitly
asks for a rough one-shot test. The storyboard is the final video's structure,
not a loose image gallery.

Storyboard requirements:

- exact allowed text only
- final aspect ratio target
- selected style tier
- foundation or moodboard description
- logo placement if any
- six panels with one subject each
- P06 stable final resolve

Storyboard prompt skeleton:

```text
Design a six-panel 3x2 storyboard sheet for a short data-as-subject motion-design reel.
Infographic variant: <variant>.
Aspect ratio target for final video: <aspect_ratio>.
Visual tier: <tier>.
Foundation/style basis: <foundation summary or selected moodboard frame>.
Allowed text only: <exact metrics/steps/nodes/brand/tagline>.

Hard rules:
- Data is the subject, not decoration.
- Use only allowed text. Do not invent numbers, dates, quarters, labels, or statuses.
- Numbers must be headline-sized and readable.
- Each panel has one main information subject.
- Critical labels are large; remove tiny nonessential labels.
- Palette max 3 colors.
- No photoreal humans, office scenes, product hero shots, or cinematic lifestyle treatment.
- No high-energy camera chaos.

Panels:
P01: <panel plan>
P02: <panel plan>
P03: <panel plan>
P04: <panel plan>
P05: <panel plan>
P06: stable final resolve with <brand/logo/final insight>, held cleanly as a cover frame.
```

## Stage C: Video Render

Use the Stage B storyboard as the primary visual reference.

Recommended defaults:

- `image_url`: storyboard visual URL, when available
- `duration`: 10 seconds for normal runs, 5 seconds for smoke tests, 15 seconds
  only when the storyboard genuinely needs more time
- `resolution`: 720p by default, 1080p only when quality matters and the active
  model supports it
- `audio`: false unless the user asks for sound
- variants: 2 unless the user asks for exactly one or more; cap at 4

If the active backend can only accept one image reference, pass the storyboard
as `image_url` and keep foundation/logo information in the prompt text. Do not
pass multiple upstream references and then act as if all were honored.

Video prompt skeleton:

```text
Animate the provided six-panel storyboard into one coherent short motion-design reel.
The storyboard is the primary visual reference. Preserve the six-panel information order.
Infographic variant: <variant>. Aspect ratio: <aspect_ratio>. Tier: <tier>.
Allowed text only: <exact allowed strings>.

Motion semantics:
<bar rise / line draw-on / node connect / data flow / panel unfurl / state transform>

Hard rules:
- Data remains the visual subject throughout.
- Use only allowed text. No invented numbers, labels, quarters, dashboards, or status tags.
- Headline numbers stay large and readable.
- One main information subject per moment.
- Camera supports data with controlled parallax or subtle drift only.
- No smash-cut chaos, whip-pan overload, photoreal people, product hero shots, or cinematic office scenes.
- Final 1-2 seconds holds a stable resolve frame with <brand/logo/final insight>.
```

Generate repeated candidates only by varying motion phrasing, not facts.

## QA Gate

Score each candidate from 0 to 5:

| Field | Question |
|-------|----------|
| `data_as_subject` | Are data, charts, nodes, or process states the visual subject? |
| `metric_fidelity` | Does it only use user-provided facts? |
| `readability` | Are headline numbers and critical labels readable? |
| `semantic_motion` | Does movement express growth, connection, flow, or transformation? |
| `six_panel_structure` | Does the order follow the storyboard? |
| `style_alignment` | Is the selected premium 3D or illustrated tier consistent? |
| `cognitive_load` | Is information density controlled? |
| `final_resolve` | Is the final frame stable and cover-ready? |

Thresholds:

- `metric_fidelity` must be 5.
- `readability`, `semantic_motion`, and `data_as_subject` should be at least 4.

If a candidate fails, repair the plan or prompt and regenerate. Do not explain
away invented metrics, tiny labels, or unstable ending frames.

## Repair Rules

| Failure | Repair |
|---------|--------|
| Numbers too small | Reduce text density and make the main number headline-tier |
| Invented metric appears | Rewrite allowed text and regenerate storyboard/video |
| Chart moves decoratively | Bind motion to a specific semantic change |
| Panel too busy | Split information; one subject per panel |
| Colors too varied | Reduce to two or three core colors |
| Diagram unclear | Establish nodes first, then edges, then data flow |
| Flow unclear | Make each step a separate state |
| Too highMD | Reduce camera motion and strengthen chart build/internal choreography |
| Too cinematic or photoreal | Remove people, office, product hero, and lens language |
| Ending unstable | Rebuild P06 as a stable final resolve |
| Logo or text garbled | Use uploaded logo if supported, simple wordmark, or no logo |

## Black-Box Examples

Should trigger:

- `做一个 KPI animation: ARR +34%, retention 84%, 12K active users`
  -> `n-stats-sequence`
- `做一个 dashboard reel, 展示 $420M revenue, +28% margin, 3.2x growth`
  -> `n-stats-sequence`
- `做流程图动画: Upload -> Clean -> Analyze -> Export`
  -> `process-flow`
- `做系统架构动画: App, API, Model, Database, Dashboard 的数据流`
  -> `system-diagram`
- `把这张 dashboard 截图做成 motion reel`
  -> foundation image plus likely `n-stats-sequence`

Should not trigger:

- `做一个高能 AI brand reveal, 很多数据线飞`
  -> high-energy motion unless real data/system is the subject
- `做一个产品广告, 显示 30% off`
  -> product/ad workflow
- `做一句 slogan 的 kinetic typography`
  -> typography motion
- `做一个 CEO 讲年度报告`
  -> cinematic/UGC, optional infographic insert

Boundary checks:

- `做一个 SaaS dashboard-looking video`: ask for concrete metrics if no data
  was supplied; otherwise route away from this skill.
- `做一个 annual report reel`: ask for the report metrics, sections, or flow
  if absent.
- `做一个 AI architecture motion`: use `system-diagram` only when there are
  nodes and relationships; otherwise route to a generic AI motion workflow.

## Verification Checklist

- [ ] Variant is one of `n-stats-sequence`, `process-flow`, or `system-diagram`.
- [ ] Aspect ratio is known.
- [ ] All metrics, steps, nodes, labels, brand text, and tagline are from the
      user.
- [ ] Asset roles are classified: foundation, logo, style reference, or ignore.
- [ ] Foundation exists, either uploaded or selected from a four-up moodboard.
- [ ] Storyboard has exactly six panels in a 3x2 sheet.
- [ ] Each panel has one main information subject.
- [ ] P06 is a stable final resolve.
- [ ] Stage C uses storyboard as the primary reference.
- [ ] QA scores pass the required thresholds before final delivery.

## Delivery

For generated assets, return:

1. final video URL or path
2. storyboard URL or path
3. selected variant and QA scores
4. provider/model information visible from the tool result or config
5. limitations, especially when only one visual reference was supported

Keep the user-facing summary concise. Never invent a final video URL, task ID,
or completed generation result.
