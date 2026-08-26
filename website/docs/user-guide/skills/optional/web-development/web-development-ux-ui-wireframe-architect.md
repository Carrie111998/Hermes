---
title: "Ux Ui Wireframe Architect — Plan low-fidelity wireframes for product interfaces"
sidebar_label: "Ux Ui Wireframe Architect"
description: "Plan low-fidelity wireframes for product interfaces"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Ux Ui Wireframe Architect

Plan low-fidelity wireframes for product interfaces.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/web-development/ux-ui-wireframe-architect` |
| Path | `optional-skills/web-development/ux-ui-wireframe-architect` |
| Version | `1.0.0` |
| Author | Gawaru. (@duong141001), Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `ux`, `ui`, `wireframe`, `low-fidelity`, `information-architecture`, `mobile-first`, `dashboard`, `forms` |
| Related skills | [`sketch`](/docs/user-guide/skills/bundled/creative/creative-sketch), [`claude-design`](/docs/user-guide/skills/bundled/creative/creative-claude-design), [`adversarial-ux-test`](/docs/user-guide/skills/optional/dogfood/dogfood-adversarial-ux-test) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# UX/UI Wireframe Architect Skill

Plan low-fidelity product interfaces around user goals, information hierarchy,
interaction states, and task flow. This skill produces an approval-ready
structural blueprint; it does not choose a brand aesthetic or silently turn the
wireframe into production UI.

## When to Use

Use this skill when the user asks to:

- wireframe a screen, feature, workflow, dashboard, or data-entry form;
- turn product requirements into information architecture and user flow;
- define mobile-first behavior before visual design or implementation;
- compare layout structures without color, imagery, or brand styling;
- document states, permissions, responsive behavior, or edge cases for handoff;
- create a text blueprint, Mermaid flow, or grayscale Tailwind skeleton.

Do not use it as the primary skill when the requested output is:

- a polished visual direction or high-fidelity prototype — use `claude-design`;
- two or three interactive visual alternatives — use `sketch`;
- a post-build hostile UX evaluation — use `adversarial-ux-test`;
- production code after the layout is already approved.

When the request combines wireframing and implementation, finish the wireframe
and obtain explicit approval before loading an implementation skill or editing
product code.

## Prerequisites

No external service or dependency is required.

Before designing, inspect any supplied requirements, screenshots, routes,
component inventory, design system, analytics summary, policy, or existing UI.
Use `read_file`, `search_files`, and `vision_analyze` when those sources exist.
Do not invent unavailable backend behavior, permissions, metrics, or content.

Load the relevant reference files completely before producing the wireframe:

- Always read `${HERMES_SKILL_DIR}/references/universal-rules.md`.
- Read `${HERMES_SKILL_DIR}/references/specialized-patterns.md` for mobile,
  B2B dashboards, complex forms, tables, or high-risk operations.
- Read `${HERMES_SKILL_DIR}/references/output-templates.md` before generating
  text, Mermaid, or Tailwind output.

## How to Run

Give the screen or flow, target users, platform, known constraints, and desired
output format. If a missing choice materially changes the structure, ask a
focused question; otherwise state the assumption and continue.

Examples:

- "Wireframe the mobile checkout flow for first-time buyers."
- "Create a desktop B2B operations dashboard with filters and bulk actions."
- "Turn this onboarding form into a low-fidelity Mermaid flow."
- "Produce a responsive Tailwind skeleton after I approve the text blueprint."

Default behavior:

1. design mobile-first unless the task is explicitly desktop-only;
2. produce a text blueprint first;
3. cover relevant non-happy states;
4. stop at an approval gate before high-fidelity design or implementation.

## Quick Reference

| Decision | Default |
|---|---|
| Fidelity | Structural low-fidelity only |
| Palette | White, black, and approved grays only |
| Media | `[X]` or `[Image Placeholder: purpose]` |
| Typography | Title/header and body/placeholder levels |
| Grid | 4 columns mobile, 8 tablet, 12 desktop |
| Primary action | One dominant CTA per task state |
| Output | Text blueprint |
| Optional outputs | Mermaid flow or Tailwind skeleton |
| State coverage | Loading, empty, error, success, disabled as relevant |
| Accessibility | Target WCAG 2.2 AA behavior |
| Approval | Required before visual styling or implementation |

### Fidelity contract

Use only:

- `#FFFFFF`, `#000000`, `#F3F4F6`, `#E5E7EB`, `#9CA3AF`, and `#4B5563`;
- solid or dashed grayscale borders;
- spacing, alignment, labels, and placeholder geometry to express hierarchy;
- explicit text labels for icons, images, charts, maps, video, and avatars.

Do not add brand colors, gradients, shadows for decoration, illustration style,
font branding, visual polish, or unapproved marketing copy. Never use color as
the only carrier of state or priority.

## Procedure

### 1. Frame the problem

State, in no more than five lines:

- **Primary actor:** who is using the interface now;
- **User goal:** the outcome they need, not merely the control they click;
- **Primary CTA:** one core action for the current state;
- **Platform and context:** mobile, tablet, desktop, environment, and frequency;
- **Constraints and assumptions:** known rules plus only material assumptions.

Separate facts from assumptions. Flag contradictions, missing permissions,
unresolved business rules, and dependencies that block a reliable structure.

### 2. Map the flow

Describe the shortest successful path:

```text
Entry → Understand → Act → Validate → Confirm → Recover/continue
```

Add alternate and recovery paths only when they affect layout or decisions.
For each transition, identify trigger, system response, visible feedback, and
exit. Do not hide irreversible or high-risk actions inside the happy path.

### 3. Build the information architecture

List regions from highest to lowest priority. For every region, define:

- purpose;
- content or data required;
- primary and secondary actions;
- relationship to adjacent regions;
- whether it is persistent, conditional, collapsible, or deferred.

Prefer a clear reading and action order over equal-weight card grids. Group by
user task and meaning, not by backend table or organizational ownership.

### 4. Choose the surface pattern

Name the primary surface before arranging blocks:

- **Monitor:** observe changing status;
- **Operate:** act on records or queues;
- **Compare:** evaluate alternatives on aligned dimensions;
- **Configure:** enter or change settings/data;
- **Decide/Learn:** understand information or make a decision;
- **Explore:** browse and filter an open result space;
- **Command/Inspect:** work quickly on one object or via keyboard.

A screen may support a secondary pattern, but one pattern must lead. Do not use
a marketing hero composition for an operations dashboard.

### 5. Draft the text blueprint

Use the syntax in `references/output-templates.md`. Include:

- screen name, platform, viewport assumption, and grid;
- regions in reading order;
- column spans or full-width behavior;
- controls with explicit labels and hierarchy;
- media placeholders and aspect ratios where relevant;
- sticky, scroll, overflow, overlay, and keyboard behavior;
- responsive transformation notes at the end of each region or screen.

For a multi-screen task, draw every named screen and connect them with a user
flow. Do not collapse a requested flow into one representative screen.

### 6. Add the state and rule matrix

Cover only states relevant to the task, but explicitly consider:

- first use and returning use;
- loading, progressive loading, and stale data;
- empty, no-results, partial data, and offline states;
- field, section, page, and system errors;
- success, confirmation, undo, and retry;
- disabled, read-only, insufficient permission, and expired session;
- destructive confirmation, conflict, duplicate submission, and partial failure.

For data-rich surfaces, add sort, filter, pagination or virtualization,
selection, bulk-action, export, and refresh behavior where relevant.

### 7. Review the structure

Run these checks before delivery:

1. Can a first-time user identify the purpose and next action quickly?
2. Does reading order match visual and keyboard order?
3. Is one CTA dominant in each task state?
4. Are labels concrete and actions distinguishable?
5. Can the user recover without losing valid work?
6. Does the layout survive narrow width, long text, large data, and localization?
7. Are permission, audit, tenant, privacy, and destructive-action boundaries visible?
8. Does the wireframe remain understandable without color or imagery?

Revise any failed check before presenting the result.

### 8. Deliver and pause for approval

Return, in this order:

1. **User Goal & Key CTA**
2. **Assumptions / Open Decisions**
3. **Information Architecture**
4. **User Flow**
5. **Text-based Wireframe Blueprint**
6. **State & Rule Matrix**
7. **Responsive and Accessibility Notes**
8. **Risks / Trade-offs**
9. **Approval Gate**

At the approval gate, ask the user to approve the structure or identify a
specific revision. Do not proceed to colors, visual design, or production code
unless the user has already approved the wireframe or explicitly waived the
gate.

## Pitfalls

- **Claiming completeness:** no checklist contains every interface rule. Apply
  the smallest relevant set and disclose omitted or unresolved concerns.
- **Decoration disguised as hierarchy:** use layout, spacing, borders, and text
  labels; do not introduce brand styling into low-fidelity work.
- **Desktop shrunk to mobile:** reprioritize, stack, defer, and transform rather
  than scaling a twelve-column page down.
- **Happy-path-only design:** show states that change decisions, recovery, or
  component geometry.
- **Backend-shaped IA:** expose concepts and tasks users understand, not raw
  schemas or service boundaries.
- **Placeholder ambiguity:** every `[X]` states purpose, aspect ratio, and
  optional/required status when it matters.
- **Dense dashboard theater:** every KPI, chart, filter, and column must support
  a real decision or action.
- **Validation noise:** validate at the right time, preserve entered data, focus
  the first invalid field, and summarize page-level errors.
- **Hidden risk:** destructive, financial, permission-changing, cross-tenant,
  or irreversible actions need explicit consequence and confirmation.
- **Premature implementation:** a Tailwind skeleton is still a wireframe; do
  not let it become an unreviewed production component.

## Verification

A complete result must satisfy all of the following:

- the primary actor, goal, CTA, platform, and assumptions are explicit;
- IA order and user flow agree with the blueprint;
- every requested screen or step is represented;
- only the approved grayscale palette is used;
- media is represented by labeled placeholders;
- grid and responsive transformations are stated;
- relevant states, permissions, recovery, and accessibility are covered;
- complex forms or dashboards use the specialized reference rules;
- the final section contains an approval gate;
- no brand treatment, fake backend state, or unsupported product claim appears.

If Mermaid or Tailwind was requested, validate syntax or renderability with the
available tools before claiming completion. If the user requested text only,
verify internal consistency directly and return the blueprint in Markdown.
