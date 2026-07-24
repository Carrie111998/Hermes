# Design process: brief, questions, context, workflow

How to scope a brief, when to ask questions, how to gather source context, the 7-step workflow, and source-code fidelity rules.

## Design Principle: Start From Context, Not Vibes

Good high-fidelity design does not start from scratch.

Before designing, look for source context:

1. brand docs
2. existing product screenshots
3. current repo components
4. design tokens
5. UI kits
6. prior mockups
7. reference models
8. copy docs
9. constraints from legal, product, or engineering

If a repo is available, inspect actual source files before inventing UI:

- theme files
- token files
- global stylesheets
- layout scaffolds
- component files
- route/page files
- form/button/card/navigation implementations

The file tree is only the menu. Read the files that define the visual vocabulary before designing.

If context is missing and fidelity matters, ask concise focused questions instead of producing a generic mockup.

## Asking Questions

Ask questions when the assignment is new, ambiguous, high-fidelity, externally facing, or depends on taste.

Keep questions short. Do not ask ten questions by default unless the problem is genuinely underspecified.

Usually ask for:

- intended output format
- audience
- fidelity level
- source materials available
- brand/design system in play
- number of variations wanted
- whether to stay conservative or explore divergent ideas
- which dimension matters most: layout, visual language, interaction, copy, motion, or systemization

Skip questions when:

- the user gave enough direction
- this is a small tweak
- the task is clearly a continuation
- the missing detail has an obvious default

When proceeding with assumptions, label only the important ones.

## Workflow

1. **Understand the brief**
   - What is being designed?
   - Who is it for?
   - What artifact should exist at the end?
   - What constraints are locked?

2. **Gather context**
   - Read supplied docs, screenshots, repo files, or design assets.
   - Identify the visual vocabulary before writing code.

3. **Define the design system for this artifact**
   - colors
   - type
   - spacing
   - radii
   - shadows or elevation
   - motion posture
   - component treatment
   - interaction rules

4. **Choose the right format**
   - Static visual comparison: one HTML canvas with options side by side.
   - Interaction/flow: clickable prototype.
   - Presentation: fixed-size HTML deck with slide navigation.
   - Component exploration: component lab with variants.
   - Motion: timeline or state-based animation.

5. **Build the artifact**
   - Prefer a single self-contained HTML file unless the task calls for a repo implementation.
   - Preserve prior versions for major revisions.
   - Avoid unnecessary dependencies.

6. **Verify**
   - Confirm files exist.
   - Run any available syntax/static checks.
   - If browser tools are available, open the file and check console errors.
   - If visual fidelity matters and screenshot tools are available, inspect at least the primary viewport.

7. **Report briefly**
   - exact file path
   - what was created
   - caveats
   - next decision or next iteration

## Source-Code Fidelity

When recreating or extending a UI from a repo:

1. inspect the repo tree
2. identify the actual UI source files
3. read theme/token/global style/component files
4. lift exact values where appropriate
5. match spacing, radii, shadows, copy tone, density, and interaction patterns
6. only then design or modify

Do not build from memory when source files are available.

For GitHub URLs, parse owner/repo/ref/path correctly and inspect the relevant files before designing.

## Reading Documents and Assets

Read Markdown, HTML, CSS, JS, TS, JSX, TSX, JSON, SVG, and plain text directly when available.

For DOCX/PPTX/PDF, use available local extraction tools if present. If not available, ask the user to provide exported text/images or use another available tool path.

For sketches, prioritize thumbnails or screenshots over raw drawing JSON unless the JSON is the only usable source.
