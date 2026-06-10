---
name: workflow-router
description: "Use at the start of Ultra Studio creative sessions to classify a user's media request, choose the right video/image workflow skill, detect missing inputs, classify uploaded assets, and avoid triggering unrelated Hermes skills."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative, routing, video-generation, image-generation, atlas, skill-selection, ultra-studio]
    related_skills: [infographic-md-flow, media-qa, prompt-repair]
---

# Workflow Router

## Purpose

`workflow-router` is the first skill for an Ultra Studio creative agent. It
decides what the user is asking for, what assets are available, which workflow
skill should execute next, and whether one blocking question is required before
generation.

It does not create media by itself. It produces a routing decision, then hands
off to a specific workflow or tool.

## Core Rule

Route before generating. Do not start image or video generation just because the
user mentions "video", "image", or uploads media. First determine intent,
required inputs, and the smallest viable workflow.

## Routing Output

Keep this object in working notes before execution:

```yaml
intent: chat | image_generate | image_edit | video_generate | video_from_image | qa | repair | planning
execution_mode: answer_only | ask_once | generate_now | inspect_then_generate | repair_then_retry
workflow_skill: null | infographic-md-flow | media-qa | prompt-repair
primary_tool: null | image_generate | video_generate
aspect_ratio: null | 16:9 | 9:16 | 1:1 | 4:3 | 3:4
asset_roles:
  foundation: []
  image_reference: []
  logo: []
  style_reference: []
  source_video: []
  ignore: []
missing:
  - exact item still required from the user
handoff:
  brief: concise normalized user brief
  constraints: []
  allowed_text: []
```

Only expose the pieces that help the user. Do not dump internal YAML unless the
user asks for the routing trace.

## Intent Classes

`chat`
: User is asking a question, greeting, or discussing options. Answer normally.
  Do not generate media.

`image_generate`
: User asks to create a new still image from text and gives enough content to
  proceed. Use `image_generate`.

`image_edit`
: User supplies an image and asks for a changed still. Use the image tool only
  if the active tool supports editing; otherwise explain the missing capability
  and offer a text-to-image alternative using the uploaded image as style only
  if supported.

`video_generate`
: User asks for a video from text. Use `video_generate` only after the brief has
  enough subject, style, and aspect-ratio signal to avoid a random result.

`video_from_image`
: User supplies a still image and asks to animate it. Use `video_generate` with
  one `image_url` when the current backend supports image-to-video.

`qa`
: User asks whether an existing output is good, why it failed, or what to fix.
  Hand off to `media-qa`.

`repair`
: A generation failed, output is wrong, or user asks to revise. Hand off to
  `prompt-repair`.

`planning`
: User asks for workflow, architecture, implementation, docs, or prompts. Do not
  call media generation tools.

## Workflow Selection

Use `infographic-md-flow` when data is the visual subject:

- KPI animation, dashboard reel, investor update, annual report
- process-flow animation, system-diagram motion, chart reveal
- exact metrics, steps, nodes, or relationships are needed

Stay in the router for ordinary chat or vague creative ideation. If no specific
workflow skill exists yet, compile a direct Atlas-friendly prompt and use the
available `image_generate` or `video_generate` tool. Do not load unrelated
legacy Hermes skills such as ASCII video, ComfyUI, Manim, music, YouTube, or
general productivity skills for Ultra Studio routing.

## Ask-Once Policy

Ask at most one blocking question, and only when guessing would change the
workflow or produce a bad artifact.

Ask when missing:

- `aspect_ratio` for deliverables where layout matters
- exact metrics, steps, or nodes for infographic work
- which uploaded image is source, logo, or style reference when ambiguous
- whether the user wants image or video when the prompt explicitly supports both

Do not ask:

- which low-level provider/model to use
- whether to make a storyboard for a workflow that requires one
- whether to generate multiple variants when the workflow defines a default
- for secrets or API keys in chat

## Asset Classification

Classify uploaded files before tool use:

- `foundation`: dashboard, chart, scene, product, character, or image to animate
- `logo`: mark, wordmark, brand icon
- `style_reference`: visual direction only
- `source_video`: clip to inspect, cut, or transform
- `ignore`: duplicates, unrelated screenshots, or unsupported files

If a backend accepts only one visual input, say which asset will be used. Do not
pretend all references were passed to the provider.

## Atlas Tool Discipline

Atlas credentials and provider settings are server-side. Never ask the user to
paste provider keys. Use Hermes tools as the abstraction layer:

- `image_generate` for text-to-image when the user wants a still.
- `video_generate` for text-to-video or image-to-video when supported.

When generation fails because configuration or backend capability is missing,
report the real missing capability and route to `prompt-repair` only if a retry
can change the outcome.

## Handoff Format

When handing off to another skill or a generation tool, include:

- normalized creative brief
- selected aspect ratio and duration if known
- asset roles and the exact asset URL/path used
- hard constraints from the user
- any allowed text that must appear exactly
- forbidden assumptions, especially invented metrics or fake outputs

## Failure Guards

- No hardcoded demo response.
- No fake job ID, fake asset URL, or fake completion state.
- No hidden switch to unrelated provider workflows.
- No invented metrics, brand names, logos, filenames, or model capabilities.
- No media generation for greetings, questions, or planning requests.
