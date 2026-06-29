---
name: higgsfield-content-factory
description: "Use when building a product content campaign pipeline across UGC entertainment, street interview, unboxing, product review, and ASMR formats; plans prompts, batch generation, image asset packs, publishing, and cost reporting."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative, content-pipeline, higgsfield, marketing-studio, ugc, video-generation]
    related_skills: [marketing-studio-director, gpt-image-2-director, workflow-router, media-qa, prompt-repair]
---

# Higgsfield Content Factory

Use this skill to run a product content campaign pipeline across Higgsfield
Marketing Studio and GPT Image 2.0 assets.

The pipeline has five stages:

1. Research and idea generation.
2. Video content plan.
3. Higgsfield video generation plus image asset pack.
4. Meta Ads scheduling or exportable calendar.
5. Cost comparison report.

## Required References

Read these files before acting:

- `references/pipeline-stages.md` for onboarding, Marketing Studio capability
  constraints, the five UGC format definitions, Stage 1 trend research,
  Stage 2 planning, and Stage 3 generation.
- `references/publishing-reporting-guidelines.md` for Stage 4 scheduling,
  Stage 5 cost reporting, and general behavioral rules.

## Operating Rules

- Ask every onboarding question in one button-driven AskUserQuestion round.
- Keep user-facing language plain. Do not expose MCP tool names, UUID plumbing,
  upload internals, slug mismatch details, or parallel-search mechanics.
- Default to UGC-first campaign composition: UGC Entertainment, Street
  Interview, Unboxing, Product Review, and ASMR.
- Every video idea must fit a live Marketing Studio preset and its 4-15 second
  limit, unless explicitly labeled outside Marketing Studio.
- Ask permission before each Stage 3 generation batch. Never generate the full
  campaign without explicit user approval.
- Never put social captions, subtitles, watermarks, lower thirds, or other
  rendered text into video prompts.
- Use `gpt-image-2-director` for still-image prompt construction and
  `marketing-studio-director` for individual Higgsfield video prompt routing
  when a focused director pass is needed.
- Log failed job IDs and offer retry, skip, or pause. Do not silently skip
  failed generations.
