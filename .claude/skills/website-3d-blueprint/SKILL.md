---
name: website-3d-blueprint
description: >
  Premium animated 3D website blueprint generator. Runs a structured discovery-and-design
  workflow: analyze the brand, the audience, the competitors, and the emotional impact the
  site should create — then deliver a complete build blueprint (concept, sitemap, per-page
  3D/animation direction, tech stack, asset list, performance budget, build phases).
  Use this skill whenever Dan asks for a website blueprint, a new site concept, a landing
  page with 3D or animation, a site redesign, "make my site feel premium", "3D website",
  "animated website", "website concept", "site blueprint", "immersive web experience",
  or wants to plan a web presence for any brand, product, company, or project. Also trigger
  when Dan shares a brand/company and asks "what should the website look like". The output
  is always a blueprint (a plan a developer/agency could execute), not code — unless Dan
  explicitly asks to build it.
---

# Premium Animated 3D Website Blueprint

Source prompt (adapted from @agentprompts.ai on Threads,
threads.com/@agentprompts.ai/post/DbIFDDbIDyZ):

> "Build a complete blueprint for a premium animated 3D website. Analyze my brand, my
> audience, my competitors, and the emotional impact I want visitors to feel."

Your job is to turn that one-liner into a rigorous, executable design workflow. The
deliverable is a **blueprint document** — something Dan could hand to a developer, an
agency, or a future Claude Code session and get the intended site built without further
explanation.

## Step 0 — Intake (only ask what's missing)

Collect these four inputs. Pull them from context first (the conversation, the Obsidian
vault's Companies/Projects notes, the brand's existing site) and ask only for what you
cannot infer:

1. **Brand** — name, what it does, positioning, existing visual identity (colors, type,
   logo), maturity (startup vs. established).
2. **Audience** — who visits and why; their sophistication, device mix, patience for
   loading/motion; what action the site must drive (the one conversion that matters).
3. **Competitors** — 2–4 sites the brand is judged against. If Dan doesn't name them,
   research or propose them and say so.
4. **Emotional impact** — the feeling a first-time visitor should have in the first
   5 seconds and the impression they should leave with. Push past vague answers
   ("professional") to specific ones ("quiet confidence, like a private bank").

Never stall the workflow on missing inputs: state your assumption, mark it clearly, and
continue.

## Step 1 — Analysis (the thinking, before any design)

Produce four short analyses. Each ends in design implications, not just observations:

- **Brand analysis** → what the visual language must express and must avoid; how far the
  3D/motion treatment can go before it fights the brand.
- **Audience analysis** → tolerance for motion and load time; accessibility needs
  (including `prefers-reduced-motion`); device/bandwidth reality; where delight helps vs.
  where it obstructs the conversion.
- **Competitor scan** → what the judged-against sites do visually; where the ceiling is;
  the specific move that makes this site feel a tier above them (the "premium gap").
- **Emotional arc** → map the target feeling to concrete craft: pacing, color, depth,
  easing curves, sound (if any), scroll rhythm. Name the arc scene by scene
  (arrive → explore → convert).

## Step 2 — The Blueprint (the deliverable)

Deliver these sections, in order:

1. **Concept statement** — 2–3 sentences: the site's big idea and its emotional promise.
2. **Sitemap & narrative flow** — pages/sections and the story order a visitor moves
   through; where the conversion moment sits.
3. **Scene-by-scene 3D & animation direction** — for each key section: what's on screen,
   what's 3D vs. flat, what animates, what triggers it (scroll, hover, load), and the
   easing/duration character. Be specific enough to storyboard.
4. **Art direction** — palette, typography, lighting/material language for the 3D
   elements, texture and depth rules, dark/light behavior.
5. **Tech stack recommendation** — default to Three.js / React Three Fiber + GSAP (or
   Rive/Lottie/WebGL shaders where lighter-weight fits), with rationale and alternatives.
   Note where pre-rendered video beats real-time 3D.
6. **Asset inventory** — every model, texture, animation, and copy block to produce, with
   suggested sourcing (custom, library, generated).
7. **Performance & accessibility budget** — target Core Web Vitals, max payload for the
   3D bundle, progressive-loading strategy, `prefers-reduced-motion` fallback, and the
   graceful no-WebGL experience.
8. **Build plan** — phased milestones (static skeleton → motion pass → 3D pass → polish),
   each independently shippable, with a rough effort scale per phase.

## Step 3 — Delivery

- Default: render the blueprint as a well-structured document in chat; offer an Artifact
  version when its length or visual nature warrants it.
- If the work relates to a company or project in the vault, offer to file the blueprint
  under the matching `Projects/` folder per vault conventions (obsidian-vault-manager
  skill governs the write).
- Close with the three decisions Dan must make before build starts (typically: budget
  tier, custom vs. library 3D assets, and the single conversion metric).

## Rules

- Blueprint, not build: write code only if Dan explicitly asks.
- Every recommendation traces back to one of the four analyses — no decoration for its
  own sake. If a 3D element doesn't serve the brand, audience, or emotional arc, cut it.
- State assumptions loudly; never present guessed brand facts as research.
