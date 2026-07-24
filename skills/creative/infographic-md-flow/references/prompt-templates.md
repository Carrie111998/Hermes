# Prompt Templates

Fill every `<placeholder>` from the asset manifest. Never leave a placeholder
literal in a submitted prompt, and never widen `allowed_text` beyond what the
user supplied.

## Moodboard prompt (Stage A, when no foundation image exists)

```text
Create a 4-up motion-design moodboard sheet for a data-as-subject infographic reel.
Four distinct directions, one per quadrant. No tiny text. No invented metrics.
Subject structure: <variant>. Audience: <audience>. Tier: <tier>.
Use placeholder data shapes only, with no readable fictional numbers.
Show visual language for charts, nodes, process states, and panels; do not create final video frames.
```

## Storyboard prompt skeleton (Stage B)

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

## Video prompt skeleton (Stage C)

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
