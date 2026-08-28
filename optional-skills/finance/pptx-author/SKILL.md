---
name: pptx-author
description: Compatibility wrapper for the generic pptx-author skill.
version: 1.0.0
author: Anthropic (adapted by Nous Research)
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [powerpoint, pptx, presentation, finance, wrapper]
    related_skills: [excel-author, powerpoint]
---

# pptx-author

This entry point remains for finance callers that already reference `official/finance/pptx-author`, but the reusable presentation logic now lives in `creative/pptx-author`.

Use this wrapper when a finance workflow still wants the legacy install path. For new non-finance or cross-domain decks, prefer the generic skill directly.

## Compatibility contract

- Accept the same legacy finance-style deck brief the old skill used to receive.
- Forward generic slide structure, text, tables, charts, images, notes, and styling into the generic contract used by `creative/pptx-author`.
- Preserve the existing `./out/<name>.pptx` output convention for callers that already depend on it.
- Keep finance-specific defaults out of the shared path; only translate them when a caller explicitly supplies finance-oriented material such as model-backed tables or valuation slides.

## Delegation

1. If the incoming brief already fits the generic presentation contract, pass it through unchanged.
2. If the caller uses finance-flavored shorthand, normalize it into the generic slide/section structure before handing it off.
3. If the deck depends on workbook citations or model outputs, keep those as slide notes, table footnotes, or source annotations rather than baking finance assumptions into the base skill.
4. Do not require optional presentation packages during discovery or review; the wrapper must remain readable even when generation dependencies are absent.

## Pitfalls

- Do not let finance terminology leak into the generic implementation.
- Do not keep hard-coded pitch-deck defaults as the only supported shape.
- Do not duplicate the reusable slide-building logic here; keep this file as a compatibility and translation layer only.
- Use the generic skill for new work and this wrapper only for existing finance entry points.

## Attribution

Conventions adapted from Anthropic's Claude for Financial Services plugin suite, Apache-2.0 licensed. Original: https://github.com/anthropics/financial-services/tree/main/plugins/agent-plugins/pitch-agent/skills/pptx-author
