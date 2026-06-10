---
name: media-qa
description: "Use after image or video generation, or when the user asks to inspect a media result, to score creative output quality, catch visible failures, and produce a repair direction without fabricating observations."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative, qa, video-generation, image-generation, atlas, review, repair]
    related_skills: [workflow-router, prompt-repair, infographic-md-flow]
---

# Media QA

## Purpose

`media-qa` reviews generated or uploaded media against the user's brief. It
decides whether the result is ready to deliver or needs a targeted repair.

This skill does not generate media. If a retry is needed, hand off to
`prompt-repair` with concrete failure evidence.

## Evidence Rule

Do not invent visual observations. Use the media preview, file metadata, tool
result, frame analysis, or user-provided screenshot. If you cannot inspect the
asset, say what evidence is missing and review only the available metadata.

## QA Inputs

Track:

```yaml
brief: original user request
artifact:
  type: image | video
  url_or_path: null
  provider: null
  model: null
  aspect_ratio: null
  duration: null
tool_result: {}
known_constraints: []
```

## Scorecard

Use 0-5 scores. A score below 4 needs a repair note.

| Field | Question |
|-------|----------|
| `instruction_match` | Does the artifact match the user's subject, action, style, and format? |
| `artifact_integrity` | Is the file present, playable/viewable, non-empty, and not broken? |
| `visual_readability` | Are the main subject and important text readable? |
| `motion_quality` | For video, is the motion coherent, stable, and intentional? Use `n/a` for still images. |
| `asset_consistency` | Are uploaded references, logos, characters, and products preserved as requested? |
| `text_fidelity` | Did visible text stay exact when exact text matters? |
| `delivery_readiness` | Can this be shown to the user as the final result? |

`text_fidelity` must be strict. Generated text in images/videos often fails; do
not excuse wrong brand names, wrong metrics, or unreadable required labels.

## Verdicts

`pass`
: Artifact is ready. Include the media link/path and one concise note.

`minor_fix`
: Artifact is usable but one improvement would help. Offer a single repair
  direction.

`retry_required`
: Core subject, asset, file integrity, motion, or required text failed. Hand off
  to `prompt-repair`.

`blocked`
: The media cannot be inspected or required backend data is missing. State the
  missing evidence.

## Video-Specific Checks

For video outputs, verify:

- file or URL exists and loads
- duration roughly matches request
- first frame establishes the subject
- motion has an interpretable direction
- no severe flicker, melt, jump, or unwanted camera chaos
- final frame does not cut off the main result
- uploaded source image remains recognizable for image-to-video

For data or infographic videos, also verify:

- only user-provided metrics, labels, steps, or nodes appear
- numbers are headline-readable
- charts and motion express data relationships
- final one to two seconds hold a stable resolve

## Image-Specific Checks

For still images, verify:

- image URL/path exists and is renderable
- subject is centered or intentionally composed
- style matches the request
- important details are not cropped
- visible text is absent unless requested or exact enough to trust
- brand/logo use is not hallucinated

## Output Shape

Keep user-facing QA concise:

```markdown
Verdict: retry_required

Main issue: The source product is not recognizable in the generated video.
Repair: Retry with one source image, slower camera motion, and a prompt that
locks the product silhouette before adding background motion.
```

For internal handoff to `prompt-repair`, include the scores and the exact failed
constraints.

## Failure Guards

- Do not call a generation result "done" if the file is missing.
- Do not call uninspected media "looks good".
- Do not hide provider errors behind vague creative language.
- Do not rewrite the user's goal during QA.
- Do not broaden a retry into a new concept unless the user asks.
