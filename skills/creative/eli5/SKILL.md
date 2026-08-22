---
name: eli5
description: Explain any topic as a simple visual HTML page.
version: 1.0.0
author: Neutize (Minji)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [eli5, explainer, html, visualization, education, artifact]
    related_skills: [claude-design, concept-diagrams, architecture-diagram, sketch]
---

# ELI5 Skill

Turn a topic into a beginner-friendly visual explanation delivered as one self-contained HTML file. The output reads like a picture book for adults: large headings, big diagrams, very few words.

This skill does not produce prose essays, slide decks, or production web pages. For designed one-off artifacts use the design-taste sibling skill; for dense educational SVG diagrams use `concept-diagrams`; for software architecture use `architecture-diagram`.

## When to Use

Load this skill when the user:

- types `/eli5 <topic>`;
- asks to "explain X like I'm 5" or "explain X simply and visually";
- wants a concept made obvious at a glance after reading code, docs, or an incident.

Do not load it for short chat answers, API references, or when the user asks for normal prose.

## Prerequisites

None. The skill uses only native Hermes tools and produces a single offline HTML file with no external dependencies.

## How to Run

1. Load the topic from the slash-command arguments or the current conversation.
2. Follow the Procedure below.
3. Reply with a two-to-four sentence summary plus the absolute `.html` path on its own line so the gateway can deliver the file natively.

## Quick Reference

| Input | Output |
|-------|--------|
| `/eli5 how does DNS work` | `dns-eli5.html`: resolver chain drawn as labeled boxes |
| `/eli5 why did this deploy fail` | Timeline of the incident with cause highlighted |
| `/eli5 this module` | Data-flow diagram of the module's real functions |

## Procedure

1. **Ground the facts.** Read the supplied files with `read_file`, search with `search_files`, or fetch sources with `web_extract`. Do not invent architecture, commands, names, causes, or numbers that are not in the conversation or sources.
2. **Outline first.** Before writing HTML, list 3 to 7 sections, each carrying exactly one idea, in the order a beginner needs them.
3. **Preserve identifiers exactly.** Code, commands, URLs, file paths, error messages, and names must appear verbatim. Never paraphrase a command or "fix" an identifier while explaining it.
4. **Write the artifact.** Create one self-contained `.html` file at an absolute path (for example `/opt/data/eli5/dns-explainer.html`) using `write_file`.
5. **Verify, then report** per Verification below, quoting the path in the final reply.

The bare absolute path is enough: gateway deliverable mode detects `.html` paths in replies and uploads the file as a native attachment on messaging platforms.

### Artifact contract

The generated file must satisfy all of these:

- Single self-contained `.html` file; inline CSS and inline SVG only.
- No external assets: no CDN, no remote fonts, no images fetched over the network, no JavaScript required to understand the page.
- Opens correctly via double-click in any modern browser, fully offline.
- A TL;DR strip at the top: what this is, why it matters, in two sentences maximum.
- 3 to 7 numbered sections following the outline; each has one large heading, one visual (SVG diagram, flow arrows, timeline, comparison boxes), and captions under 25 words each.
- Sentence-case text everywhere; body text at least 16px; high-contrast colors; meaningful `alt`/`aria-label` text on visuals; no decorative animation.
- Any claim you could not ground in step 1 is marked visibly in the page as `[unverified]`.

## Pitfalls

- Do not pad sections to look complete; cut a section rather than dilute it.
- Do not invent plausible architecture when evidence is missing; label the gap `[unverified]` instead.
- Do not reword commands, paths, or error strings into friendlier forms; quote them exactly.
- Do not rely on CDNs or webfonts; if typography matters, use system font stacks.
- Do not dump the whole HTML into the chat reply; give the path and a summary.

## Verification

Before claiming success, confirm with `read_file` or `search_files` that:

1. the file exists at the stated absolute path;
2. the file contains no `http://` or `https://` resource references inside markup attributes (`src=`, `href=` pointing to remote assets), meaning it truly renders offline;
3. every outlined section appears in the file;
4. identifiers quoted from sources match the originals character-for-character.

If browser tools are available, additionally open the file and visually check layout; otherwise state plainly that visual verification was skipped. Never claim visual verification that did not happen.
