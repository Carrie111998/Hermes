---
name: code-wiki
description: "Generate wiki docs + Mermaid diagrams for any codebase."
version: 0.1.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Documentation, Mermaid, Architecture, Diagrams, Wiki, Code-Analysis]
    related_skills: [codebase-inspection, github-repo-management]
---

# Code Wiki Skill

Generate a comprehensive wiki for any codebase — overview, architecture, per-module deep-dives, Mermaid class and sequence diagrams. Inspired by Google CodeWiki, but works on local repos, private repos, and any language. Uses only existing Hermes tools (`terminal`, `read_file`, `search_files`, `write_file`); no Docker, no external services, no extra dependencies.

This skill produces **reference documentation** (what/how). It does not produce strategic narrative (why — that's a different skill).

## When to Use

- User says "document this codebase", "generate a wiki", "make architecture diagrams"
- Onboarding to an unfamiliar repo and wants a structured reference
- User points at a GitHub URL and asks for documentation
- Need a stable artifact (markdown + Mermaid) that renders on GitHub

Do NOT use this for:
- Single-file or single-function documentation — just answer directly
- API reference for one specific endpoint — use `read_file` and answer inline
- Strategic "why does this exist" narrative — different skill, different purpose
- Codebases the user is actively developing in this session — just answer questions as they come

## Prerequisites

- No env vars required.
- `git` on PATH for repo SHA tracking and remote clones.
- Optional: `pygount` for language-breakdown stats (see the `codebase-inspection` skill).

## How to Run

Invoke through the `terminal` tool from the target repo's root, then use `read_file` / `search_files` / `write_file` to produce the wiki. Default output location is `~/.hermes/wikis/<repo-name>/`. Only write into the repo (`docs/wiki/`) when the user explicitly requests it.

## End-to-End Skeleton (the 12 steps)

| Step | Action |
|---|---|
| 1 | Resolve target — local cwd, given path, or `git clone --depth 50 <url>` to a temp dir |
| 2 | Scan structure — `ls`, `find -maxdepth 3`, manifest files, README |
| 3 | Pick 8–10 modules to document |
| 4 | Write `README.md` (overview + module map) |
| 5 | Write `architecture.md` with Mermaid flowchart |
| 6 | Write per-module docs in `modules/` |
| 7 | Write `diagrams/class-diagram.md` (Mermaid classDiagram) |
| 8 | Write `diagrams/sequences.md` (Mermaid sequenceDiagram, 2–4 workflows) |
| 9 | Write `getting-started.md` |
| 10 | Write `api.md` if applicable, else skip |
| 11 | Write `.codewiki-state.json` |
| 12 | Report paths to user |

Full commands, doc bodies, and Mermaid conventions for every step: `references/procedure.md`.

## Routing — load what the current step needs

| Intent | Read |
|---|---|
| Run any of the 12 steps (exact shell commands, output doc bodies, Mermaid shape semantics, state file, final report) | `references/procedure.md` |
| Repo is huge / decide how much to cover / cost ballpark | `references/scope-and-rerun.md` |
| `.codewiki-state.json` already exists — regenerate or incremental update | `references/scope-and-rerun.md` |
| Quality problems, Mermaid rendering quirks, post-write verification commands | `references/pitfalls-and-verification.md` |
| Fill-in-the-blank overview page | `templates/README.md` |
| Fill-in-the-blank architecture page (+ flowchart) | `templates/architecture.md` |
| Fill-in-the-blank per-module page | `templates/module.md` |
| Fill-in-the-blank setup/first-run page | `templates/getting-started.md` |

## Red Lines

- **No fabricating components.** Every diagram node and claimed function call must be in the source. `read_file` before writing. The single biggest failure mode for auto-generated docs is plausible-sounding fabrication.
- **No in-repo output without asking.** Default is `~/.hermes/wikis/`. Only write into the repo when the user explicitly requests it.
- **No Mermaid diagram over 50 nodes.** They don't render legibly. Split them.
- Before reporting done, run the verification steps in `references/pitfalls-and-verification.md`.
