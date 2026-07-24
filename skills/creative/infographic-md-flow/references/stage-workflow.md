# Stage Workflow

The fixed production line: Stage A resolves a visual foundation, Stage B
generates the six-panel storyboard, Stage C animates it. Prompt text for every
stage lives in `references/prompt-templates.md`.

## Tool Surface

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

Aspect ratio mapping for `image_generate`:

- `16:9` or `4:3`: use `landscape`
- `9:16` or `3:4`: use `portrait`
- `1:1`: use `square`

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

Generate repeated candidates only by varying motion phrasing, not facts.

## Delivery

For generated assets, return:

1. final video URL or path
2. storyboard URL or path
3. selected variant and QA scores
4. provider/model information visible from the tool result or config
5. limitations, especially when only one visual reference was supported

Keep the user-facing summary concise. Never invent a final video URL, task ID,
or completed generation result.
