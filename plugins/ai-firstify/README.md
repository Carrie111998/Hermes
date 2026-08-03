# ai-firstify

Audit, re-engineer, or bootstrap a project so it aligns with **AI-first design
principles** — the 9 principles and 7 design patterns from the
[TechWolf AI-First Bootcamp](https://ai-first.techwolf.ai). This is a Hermes
plugin port of the [`ai-firstify`](https://github.com/techwolf-ai/ai-first-toolkit)
skill (MIT).

## What it does

The plugin bundles a single skill, registered as `ai-firstify:ai-firstify`,
that operates in three modes:

| Mode | Trigger words | Behaviour |
|---|---|---|
| **Audit** | review, audit, analyze, check, assess | Read-only. Scores the project across 7 dimensions (project structure, agent architecture, skill usage, scope & complexity, context hygiene, safety, workflow design) and emits a report with prioritized recommendations. Changes nothing. |
| **Re-Engineer** | ai-firstify, fix, improve, re-engineer, transform | Runs the audit, then actively fixes issues in 7 phases: foundation, de-agentification, skill extraction, complexity reduction, context hygiene, safety hardening, workflow optimization. |
| **Bootstrap** | start, new project, bootstrap, set up, build from scratch | Interactive scaffolding of a new AI-first project via discovery questions. |

Reference material (principles, patterns, anti-patterns, skill architecture,
project structure, assessment rubric, and the per-mode playbooks) lives under
`skills/ai-firstify/references/` and is loaded progressively — the skill reads
only the reference file relevant to the dimension it is currently working on.

## Enabling

Plugins are opt-in. Add it to your allow-list:

```bash
hermes plugins enable ai-firstify
# or edit ~/.hermes/config.yaml manually:
plugins:
  enabled:
    - ai-firstify
```

## Using it

Once enabled, either invoke the slash command or just ask:

```
/ai-firstify audit             # scored report for the current project
/ai-firstify reengineer .      # audit, then apply fixes
/ai-firstify bootstrap         # scaffold a new AI-first project
/ai-firstify help              # mode reference
```

The slash command points the agent at the bundled skill. The agent loads the
overview with `skill_view(name="ai-firstify:ai-firstify")` and then reads the
per-mode playbook and reference files directly from disk under
`plugins/ai-firstify/skills/ai-firstify/references/` with `read_file`.

> **Note:** `skill_view` only serves a plugin skill's `SKILL.md` — its
> `file_path` argument is ignored for plugin-provided skills. The reference
> files (`references/*.md`) are therefore read with `read_file` using their
> on-disk paths, which the `/ai-firstify` command prints for you.

Because plugin-provided skills are explicit opt-in loads (they are not listed
in the system prompt's skill index), the slash command is the intended entry
point — but you can also ask directly, e.g. "ai-firstify this repo" or "audit
this project for AI-first design", and point the agent at the
`ai-firstify:ai-firstify` skill.

The `scripts/validate-report.sh` helper checks that a generated audit report
has all required sections, all 7 dimensions scored, and valid priority tags.

## Attribution and licensing

* Everything under `skills/ai-firstify/` is a verbatim fork of
  [`techwolf-ai/ai-first-toolkit`](https://github.com/techwolf-ai/ai-first-toolkit)
  (`plugins/ai-firstify/skills/ai-firstify/`, commit `ac797fb`, 2026-07-10,
  version 1.1.0), Copyright (c) 2026 TechWolf, under the
  [MIT License](./LICENSE). See [NOTICE](./NOTICE) for full attribution.
* `plugin.yaml`, `__init__.py`, and `README.md` are original work by
  NousResearch, MIT-licensed alongside the rest of hermes-agent.
