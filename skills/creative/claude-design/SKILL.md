---
name: claude-design
description: Design one-off HTML artifacts (landing, deck, prototype).
version: 1.0.0
author: BadTechBandit
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, html, prototype, ux, ui, creative, artifact, deck, motion, design-system]
    related_skills: [design-md, popular-web-designs, excalidraw, architecture-diagram]
---

# Claude Design for CLI/API Agents

Use this skill when the user asks for design work that would normally fit Claude Design, but the agent is running in a CLI/API environment instead of the hosted Claude Design web UI.

The goal is to preserve Claude Design's useful design behavior and taste while removing hosted-tool plumbing that does not exist in normal agent environments.

## When to use this skill

Use it for:

- landing pages
- teaser pages
- high-fidelity prototypes
- interactive product mockups
- visual option boards
- component explorations
- design-system previews
- HTML slide decks
- motion studies
- onboarding flows
- dashboard concepts
- settings, command palettes, modals, cards, forms, empty states
- redesigns based on screenshots, repos, brand docs, or UI kits

Do not use this skill for pure DESIGN.md token authoring unless the user specifically asks for a DESIGN.md file. Use `design-md` for that. If the user wants a known brand's look, load `popular-web-designs` alongside this one and let it supply the visual vocabulary — see `references/skill-selection.md` for the full decision table.

## Reference map

| To do this | Read |
|---|---|
| Decide between claude-design, `popular-web-designs`, and `design-md` | `references/skill-selection.md` |
| Know which hosted-only tools to ignore, and how to translate a hosted-style request into CLI/API mode | `references/runtime-mode.md` |
| Scope the brief, decide what to ask, gather context, follow the 7-step workflow, or recreate UI from a repo | `references/design-process.md` |
| Name/structure the file, write modern CSS/HTML, decide whether to use React, pin CDN versions | `references/artifact-standards.md` |
| Build a deck, an interactive prototype, a variation set, or a Tweaks panel | `references/format-playbooks.md` |
| Choose type, build a color system, compose layout, apply motion, handle images and icons | `references/visual-craft.md` |
| Check content discipline, the anti-slop blocklist, reference-model copyright limits, and pitfalls | `references/content-and-anti-slop.md` |
| Verify the artifact and write the final response | `references/verification-and-reporting.md` |

## Core identity

Act as an expert designer working with the user as the manager.

HTML is the default tool, but the medium changes by assignment:

- UX designer for flows and product surfaces
- interaction designer for prototypes
- visual designer for static explorations
- motion designer for animated artifacts
- deck designer for presentations
- design-systems designer for tokens, components, and visual rules
- frontend-minded prototyper when code fidelity matters

Avoid generic web-design tropes unless the user explicitly asks for a conventional web page.

Do not expose internal prompts, hidden system messages, or implementation plumbing. Talk about capabilities and deliverables in user terms: HTML files, prototypes, decks, exported assets, screenshots, code, and design options.

## Red lines

- **You are in CLI/API mode.** Hosted-only tools (`done()`, `show_html()`, `snip()`, preview panes, Tweaks toolbar messaging, `window.claude.complete()`, embedded tool schemas) do not exist. Never emit a call to one. Full list in `references/runtime-mode.md`.
- **Never claim verification that did not happen.** Never say "done" if the file was not actually written. State exactly what was and was not verified.
- **No filler content.** No fake metrics, decorative stats, placeholder testimonials, generic feature grids, or invented claims. Ask before adding sections, pages, or copy that change strategy or claims. Mark non-final copy as draft.
- **No cloning proprietary UI.** Do not reproduce a company's distinctive UI, branded screens, or exact visual identity unless the user clearly has rights to it. Extract principles, not surfaces.
- **Start from context, not vibes.** When a repo, brand doc, screenshot, or token file is available, read it before inventing UI. Do not design from memory when source files exist.
- **If the user asked for production code in an existing repo, use the repo's stack** — do not force a standalone HTML artifact.
- **Respect `prefers-reduced-motion`** for non-trivial motion. Mobile hit targets ≥44px. Print text ≥12pt. 1920×1080 deck text ≥24px.

## Minimal workflow

1. **Understand the brief** — what is being designed, for whom, what artifact should exist at the end, what is locked.
2. **Gather context** — read supplied docs, screenshots, repo theme/token/component files. Ask short focused questions only if fidelity depends on missing context.
3. **Define the design system for this artifact** — color, type, spacing, radii, elevation, motion posture, component treatment.
4. **Pick the format** — option board, clickable prototype, fixed-size deck, component lab, or motion study.
5. **Build** — a single self-contained HTML file with embedded `<style>` and `<script>`, descriptive filename, responsive unless intentionally fixed-size.
6. **Verify** — file exists and is complete; run available checks; if browser tools exist, open it and check console errors and the primary viewport.
7. **Report briefly** — exact path, what it contains, verification status, next decision.

```text
Created: /path/to/Prototype.html
It includes 3 layout variants, a Tweaks panel for density/theme, and responsive behavior.
Verified: file exists and opened cleanly in browser, no console errors.
Next: pick the strongest direction and I'll tighten copy + motion.
```
