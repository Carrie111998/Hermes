---
name: hermes-agent
description: "Configure, extend, or contribute to Hermes Agent."
version: 2.2.0
author: Hermes Agent + Teknium
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, setup, configuration, multi-agent, spawning, cli, gateway, development]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [claude-code, codex, opencode]
---

# Hermes Agent

## Overview

Hermes Agent is the local/runtime operator surface for CLI, gateway platforms, tools, skills, profiles, cron, TTS, browser automation, plugins, and developer contributions. This SKILL.md is now a router; load focused references only when the task requires detail.

## When to Use

Use this skill before answering or changing Hermes Agent itself: CLI setup, config, providers, models, tools/toolsets, gateway platforms, skills, voice/TTS, cron, profiles, plugins, runtime services, or code contributions in the Hermes Agent repo.

Do not use this for Scaffolde projection/canonical-source questions unless the Hermes runtime is the target surface; pair with `scaffolde-platform-operations` for Scaffolde-owned projection work.

## Fast Path

- Setup/status: `hermes setup`, `hermes setup tools`, `hermes status`.
- Config: prefer `hermes config set ...` and `hermes config get ...` over hand-editing YAML.
- Skills: built-in skill source lives under `skills/<category>/<name>/SKILL.md`; user-local skills live under `~/.hermes/skills/`.
- Logs: `hermes logs --follow` or inspect `~/.hermes/logs/` when troubleshooting runtime behavior.
- Repo tests: from the Hermes Agent checkout, prefer `scripts/run_tests.sh` or focused pytest targets.

## Scope & Verification

This skill is a concise operating guide, not the complete source of truth for every Hermes feature. If a Hermes feature, command, or setting is not mentioned here, do not treat that absence as evidence that it does not exist. Check the live repository and official docs before giving a negative answer.

Good verification targets:

- CLI commands: `hermes --help`, `hermes <command> --help`, and `hermes_cli/main.py`
- User documentation: https://hermes-agent.nousresearch.com/docs/
- Source tree: https://github.com/NousResearch/hermes-agent

## References

- `references/full-cli-runtime-guide.md` — full preserved CLI/runtime/developer guide: slash commands, config paths, privacy toggles, voice, spawning agents, cron/background systems, Windows quirks, troubleshooting, and contributor map.
- `references/context-budget-and-surface-audit-2026-05-07.md` — Scaffolde/Hermes context-budget and wrong-surface instruction audit pattern, when present in the installed skill copy.
- `references/scaffolde-agent-profiles-2026-05.md` — Scaffolde-named Hermes profile projection pattern, when present in the installed skill copy.

## Project Context Files

Hermes injects project-level instructions from context files in first-match-wins order:

1. `.hermes.md` / `HERMES.md` — walks parent directories up to the git root.
2. `AGENTS.md` / `agents.md` — current working directory only.
3. `CLAUDE.md` / `claude.md` — current working directory only.
4. `.cursorrules` / `.cursor/rules/*.mdc` — current working directory only.

Each context file is capped at 20,000 characters with head+tail truncation. Context file content passes through the threat-pattern scanner before prompt injection. `hermes --ignore-rules` skips these project context files, `SOUL.md`, user config, plugins, and MCP servers for one isolated session.

## Operating Rules

1. Use canonical Hermes CLI commands first; do not invent config file edits when a command exists.
2. For gateway/platform issues, verify the active platform/session path, not just CLI behavior.
3. For repo changes, edit the repo-bundled skill/source, run focused tests, and keep user-local runtime copies as projections unless explicitly live-patching.
4. For Scaffolde-owned Hermes projection parity, repair canonical Scaffolde source and sync the Hermes surface rather than patching `~/.hermes` directly.

## Verification Checklist

- [ ] `hermes status` or the relevant focused command was run when runtime behavior is claimed.
- [ ] File edits were made in the canonical repo/source for the requested scope.
- [ ] Focused tests or validators passed, or blockers are reported plainly.
- [ ] Any live/runtime patch has a canonical follow-up path.
