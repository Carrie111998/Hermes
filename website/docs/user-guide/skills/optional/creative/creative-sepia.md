---
title: "Sepia — De-AI writing: fix narrative structure, not just style"
sidebar_label: "Sepia"
description: "De-AI writing: fix narrative structure, not just style"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Sepia

De-AI writing: fix narrative structure, not just style.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/creative/sepia` |
| Path | `optional-skills/creative/sepia` |
| Version | `1.0.0` |
| Author | Nanako0129 (upstream sepia), ported by Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `writing`, `de-ai`, `humanize`, `fiction`, `prose`, `storyscope` |
| Related skills | [`humanizer`](/docs/user-guide/skills/bundled/creative/creative-humanizer) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Sepia — de-AI writing

Make AI-generated writing read as human-written, in fiction and in professional
prose. Repairs the narrative architecture of fiction and stories (based on
StoryScope, arXiv:2604.03136); routes professional text through domain rules for
release notes, announcements, PR and issue replies, code-review comments,
incident postmortems, tickets, work orders, technical articles, and blog posts.
Four operations — write, review (diagnose AI tells without editing), refactor
(minimal in-place edits), recreate (full rewrite). Use when asked to humanize,
de-AI, unslop, or strip AI flavor from any text; when writing or revising any of
these document types; or whenever output must not read as machine-written.

*Ported from upstream sepia v0.2.0. The upstream `research/` directory (evidence
notes behind each rule) is not vendored in this port — see
https://github.com/Nanako0129/sepia/tree/main/research for provenance.*

Every rule here is backed by a measured human-vs-AI gap. The load-bearing facts: in fiction, a classifier using only **narrative-structure features** detects AI at 93.2% macro-F1 and style editing barely moves it — so structure is fixed before style, always. In professional prose, the measured tells are different — filler density, missing stance, chatbot residue, register mismatch, format uniformity — and the fix is domain-specific. Route first, then operate.

## Sepia vs humanizer

Hermes also bundles **humanizer** (Wikipedia signs-of-AI-writing): a fast,
style-level de-AI pass for any text — word swaps, tone, surface tells.
**Sepia** is structural repair: fiction narrative architecture (StoryScope —
structure alone detects AI at 93.2% macro-F1; style editing barely moves it),
domain-routed professional prose rules (release notes, PR replies, postmortems,
tickets, tech articles), and four operation contracts (write / review /
refactor / recreate). Recommendation: quick de-slop → humanizer; fiction,
domain documents, or text that *still reads as AI after style edits* → sepia.

## Loading references in Hermes

Load the pass files on demand with
`skill_view(name='sepia', file_path='references/narrative-pass.md')` (and
likewise for any path in the routing table below).

## Routing

| Text type | Load, in order |
|---|---|
| Fiction / stories / narrative essays | `references/narrative-pass.md` → `references/discourse-pass.md` → `references/style-pass.md`; diagnose with `references/rubric.md` |
| Release notes, changelogs, announcements | `references/professional-pass.md` + `references/domains/release-notes.md` |
| PR replies, issue replies, review comments | `references/professional-pass.md` + `references/domains/dev-replies.md` |
| Incident postmortems / RCA | `references/professional-pass.md` + `references/domains/postmortems.md` |
| Tickets, work orders, bug reports | `references/professional-pass.md` + `references/domains/tickets.md` |
| Technical articles, blog posts, tutorials | `references/professional-pass.md` + `references/domains/tech-articles.md` + `references/discourse-pass.md` §1–3 |
| Any other prose | `references/professional-pass.md` + `references/style-pass.md` |

Every non-fiction route ends with the vocabulary/syntax scan in `references/style-pass.md` §2–3, and long professional pieces take the whole style pass — in both cases skipping its fiction-slop table. If the text was produced by a known model, add `references/model-fingerprints.md` (fiction-centric; use as priors).

## Operations

Any request maps to one of four operations:

| Operation | Contract |
|---|---|
| **write** | New content. Read the domain file *before* drafting — architecture and register decisions come first, they cannot be retrofitted cheaply. For fiction, follow Workflow A below. |
| **review** | Diagnose only — no edits. Produce the defect list (fiction: rubric report; professional: checklist findings with quoted evidence) and stop. Report findings; apply nothing until asked. |
| **refactor** | Minimal in-place revision preserving structure, voice, and intent. Two-stage: full defect list first, then fix item by item, deepest layer first. Skew replace/delete over insert (measured editor ratio 74/18/8). |
| **recreate** | Full rewrite. Extract the facts, claims, and intent from the original into a bare list; verify nothing invented; write fresh under the domain rules. Use when defects are structural and the text is short enough that surgery costs more than rebuilding. |

The two-stage protocol is not optional for refactor/recreate: paraphrasing without a defect list makes AI fingerprints *more* visible, not less (measured on expert detectors).

## Fiction workflows

**A — writing new fiction:** (1) premise, genre, length — genre sets calibration targets; (2) fill the architecture sheet in `references/narrative-pass.md`; (3) select 3–5 human-leaning moves + one rarity move; (4) outline, run the outline/QUD checks in `references/discourse-pass.md` and the echo test in `references/narrative-pass.md` §2; (5) draft; (6) self-diagnose with `references/rubric.md`, one group at a time; (7) style pass last.

**B — revising existing fiction:** (1) diagnose completely first (rubric → discourse → style), no edits; (2) triage — architecture defects need scene-level surgery, tell the user how deep before cutting; (3) fix deepest first; (4) verify: re-run changed rubric groups, read key passages aloud, echo-test any added twist.

## Calibration — the rule that governs all rules

| Principle | Meaning |
|---|---|
| Aim at the band, not the opposite pole | Human values are moderate (chronological discontinuity 2.4/5, not 5). Inverting every AI tell creates a new fingerprint. In professional prose the equivalent: match the venue's register, don't overshoot into forced casualness — informality alone fools no trained reader. |
| Select, don't accumulate | Human writing is diverse. Fiction: 3–5 moves per story, chosen for the premise, varied across works. Professional: fix what the checklist actually flags, nothing more. |
| Leave slack | Ordinary sentences, an underdeveloped thought, a plain paragraph. Do not sand every surface. |

## Hard guardrails

- **Never invent specifics.** Fiction: intertextual references, brands, places must be real and correct. Professional: versions, numbers, timestamps, benchmarks, quotes come from the actual change/incident/data — missing info means ask the user or leave an explicit TODO, never fill. Confident wrong facts are themselves a top-tier tell.
- **Deletion beats addition** (74% replace / 18% delete / 8% insert). The only additive fix is real specificity.
- **Respect the author's voice and the venue's corpus.** Extract habits from the user's samples or the venue's recent artifacts before editing; edit toward *that* profile. Do not remove a mannerism they actually use.
- **Dialogue quotes and quoted material are load-bearing** — do not regularize them.
- **Check the whitelists** (`references/style-pass.md` §7, `references/professional-pass.md` last section) before flagging: clean grammar, formal tone in formal venues, and conventional templates are not evidence of AI.
