---
name: pptx-author
description: Use when creating arbitrary .pptx decks. Generic authoring.
version: 1.0.0
author: hermes-fleet
platforms: [linux]
metadata:
  hermes:
    tags: [pptx, powerpoint, slides, presentation, authoring, creative]
    category: creative
    related_skills: [powerpoint]
---

# PPTX authoring

Use this skill when a task needs a PowerPoint deck (`.pptx`) built from arbitrary source material rather than a finance-specific template or caller.

## Inputs

- Presentation brief: title, audience, purpose, tone, and desired outcome.
- Optional section list that should be expanded into slides.
- Optional slide list, each item able to describe:
  - title and subtitle
  - section grouping
  - layout or slide type
  - speaker notes / talk track
  - styling or theme hints
- Optional content elements per slide:
  - text blocks and bullets
  - tables
  - charts
  - images
  - callouts, labels, and simple shapes
- Optional assets such as screenshots, logos, diagrams, or chart exports.
- Output path for the finished deck.

## Output contract

- Write a polished `.pptx` file.
- Keep the deck round-trippable: save, reopen, and verify the file still opens cleanly.
- Register the artifact only when the surrounding delivery flow expects it.

## Dependencies

- `python-pptx` for the downstream generation path when a deck is actually being built.
- `Pillow` only if images need preprocessing.
- Shared creative theme helpers when the environment provides them.
- Artifact registration helpers only if the deck should be discoverable in the Hermes artifact stack.

Do not require optional presentation packages at skill-discovery or review time; the skill must remain readable even when generation dependencies are not installed.

## Usage

1. Normalize the brief into a deck plan: title slide, sections, and the slide-by-slide story.
2. Prefer a single message per slide; move supporting detail into notes or appendix slides.
3. Map the source into the generic contract above. If the caller provides sections, treat them as slide-grouping hints rather than finance-specific chapters.
4. Use standard layouts and placeholders when available; fall back to explicit placement for mixed media.
5. Keep slide text short and leave enough whitespace for a polished result.
6. If a template file exists, load it first so typography, colors, and masters stay consistent.
7. Reopen the saved deck and verify that tables, charts, notes, and images survived the round trip.

## Pitfalls

- Do not assume placeholder indexes are the same across templates.
- Do not hard-code finance language, valuation slides, or model-specific defaults into the generic path.
- Do not stretch images unless distortion is explicitly requested.
- Do not cram multiple unrelated ideas onto one slide.
- Use the broader `powerpoint` skill when the user wants a live editing session, rich notes, or heavy animation.
