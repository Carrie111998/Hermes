---
name: prompt-repair
description: "Use when an Atlas image/video generation fails, produces a poor result, violates the brief, or needs revision; converts failure evidence into a safer retry prompt and provider-aware retry plan."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative, prompt-engineering, repair, video-generation, image-generation, atlas, retry]
    related_skills: [workflow-router, media-qa, infographic-md-flow]
---

# Prompt Repair

## Purpose

`prompt-repair` turns a failed or weak media generation into a targeted retry.
It uses the original brief, tool error, and QA findings to change only what is
necessary.

It does not claim a retry succeeded. After repairing the prompt, call the
appropriate generation tool only when the user asked for a retry or the current
workflow permits an automatic retry.

## Inputs

Collect:

```yaml
original_brief: ""
artifact_type: image | video
tool_name: image_generate | video_generate
tool_error: null
qa_findings: []
used_assets: []
requested_aspect_ratio: null
requested_duration: null
provider_limits: []
```

If the only problem is missing credentials or unavailable backend capability,
do not rewrite the prompt as if wording will fix it. Report the configuration
blocker.

## Repair Strategy

1. Preserve the user's subject and goal.
2. Identify the smallest failing constraint.
3. Remove ambiguity, decorative clutter, and contradictory style language.
4. Make action, camera, composition, and timing explicit.
5. For exact text or metrics, list allowed strings and forbid new text.
6. For image-to-video, anchor the source image before adding motion.
7. For video, reduce motion complexity before changing concept.
8. Return a retry prompt and the changed parameters.

## Atlas-Aware Constraints

Hermes tools abstract provider details, but current Atlas media backends have
capability limits that must shape retries:

- Do not ask the user for provider API keys in chat.
- Use top-level Hermes tool fields, not provider internals like
  `input.img_url`.
- If the active video backend accepts one `image_url`, use one source image and
  do not claim multiple references are enforced.
- If `reference_image_urls` is unsupported, collapse references into the prompt
  or ask the user to choose the single source image.
- Avoid exact generated text in video unless it is essential; if essential, keep
  it short and large.
- If a backend returns a missing-configuration error, stop and report it.

## Common Repairs

Provider/config error:

- Do not retry blindly.
- Explain the missing provider, key, model route, or unsupported input mode.
- Suggest the exact config surface if known.

Prompt too vague:

- Add subject, setting, style, composition, camera, motion, and finish state.
- Keep the concept narrow.

Motion is chaotic:

- Slow the camera.
- Use one main movement.
- Define start frame, transformation, and end frame.

Image-to-video identity drift:

- Start with "preserve the uploaded image as the exact subject".
- Limit motion to background, light, camera push, or small subject movement.
- Avoid changing species, product shape, logo, clothing, or scene era.

Infographic unreadable:

- Put one metric per scene.
- Use only allowed text.
- Make numbers headline-sized.
- Hold final frame.

Text hallucination:

- Remove nonessential text.
- For required text, list exact allowed strings.
- Avoid tiny UI labels and decorative metadata.

## Output Shape

```markdown
Repair target: motion_quality

Changed:
- reduced camera movement
- locked source image identity
- removed extra background actions

Retry prompt:
...

Tool parameters:
- tool: video_generate
- aspect_ratio: 9:16
- duration: 5
- image_url: <single selected asset>
```

If generation should not be retried, use:

```markdown
Blocked: provider configuration
The prompt is not the problem. The backend reported missing configuration.
```

## Failure Guards

- No fake success after a rewrite.
- No unrelated concept change unless user asks.
- No hidden switch to a different provider.
- No multi-reference claims when the backend cannot enforce them.
- No invented metrics, brand names, logos, or text.
