---
name: web-design-extractor
description: "Web Design Extractor: Analyze and extract brand guidelines, colors, fonts, and design principles from a given website."
version: 1.0.0
author: System
license: MIT
platforms: [linux, macos, windows]
required_credential_files: []
tags: [design, styling, css, typography, brand, web-design]
---

# Web Design Extractor Skill

A methodology and script collection for forensic extraction of a client's design language, brand guidelines, typography, and styling systems from their live site or a static provided HTML/CSS environment.

## Linked Resources
- `references/web-design-extractor.md` - Core extraction methodology and fallback processes.
- `references/design-principles.md` - Autonomous aesthetic judgment criteria and anti-AI-artifact constraints.
- `references/a11y-checklist.md` - WCAG 2.2 AA actionable checklist.
- `references/design-systems-reference.md` - Rules of arbitration among design systems (shadcn, Material, Radix, etc.).
- `references/wmh-brand.md` - WMH internal brand schema (secondary fallback).
- `scripts/extract_web_design.py` - Core extraction script capable of fetching and parsing live CSS via `tinycss2`.

## Using the Extractor
To run the automated extraction script against a live website:

```bash
uv run /opt/data/.hermes/knowledge/web-design/extract_web_design.py https://example.com --out /tmp/client-brand
```
This extracts colors and fonts natively.

For heavily dynamic SPA/Client-side sites, a browser tool screenshot alongside a visual analysis task via `vision_analyze` or `delegate_task(role='orchestrator')` may be required to cross-verify the parsed styling variables.

Always defer to `design-principles.md` when proposing new UI layouts to stay grounded in non-artificial, human-centric aesthetic standards.
