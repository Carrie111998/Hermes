---
name: nocode
description: "Use when /nocode is typed: answer only, no code or installs."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [answer-only, read-only, question-answering, agent-behavior, no-code]
    related_skills: [plan]
---

# Answer-Only Mode (/nocode)

## Overview

`/nocode` is an answer-only mode for the agent: when the user types it (CLI
or messaging platform) followed by a question, the agent answers directly —
no code, no file writes, no installs, no state changes. Read-only research
is allowed and encouraged; mutations are not.

It is the inverse of plan mode: `/plan` produces a plan document, `/nocode`
produces an answer. Users reach for it when they want a question answered
without the agent "helpfully" scaffolding projects, installing packages, or
editing files along the way.

## When to Use

- The user types `/nocode <question>` on the CLI or a messaging platform.
- The user asks for "answer only", "just answer, don't do anything",
  "só responde", "sem código", or an equivalent phrase.
- You are about to reach for a mutating tool (write, install, config change)
  in order to answer a question — stop and answer instead.

Don't use for:

- Requests that explicitly ask for action (edits, installs, deploys, tests) —
  those are normal turns.
- Planning work — use the `plan` skill instead.

## How This Differs from /plan

`/plan` and `/nocode` are sibling mode skills that both restrain the agent
from running off and doing things, but they produce different deliverables:

| | `/plan` | `/nocode` |
|---|---|---|
| Deliverable | A markdown plan document saved under `.hermes/plans/` | A direct answer in the conversation |
| Purpose | Prepare a task for later implementation | Answer a question now, without side effects |
| File writes | Writes exactly one file — the plan | Writes nothing, ever |
| Typical trigger | "Plan how to build X" | "Just answer, don't do anything" |
| After the turn | The plan is meant to be executed later | The answer is the end state; nothing remains |

Choose `/plan` when the user wants a roadmap for work that will happen later.
Choose `/nocode` when the user wants information and explicitly does not want
anything done. `/nocode` is the stricter mode: it permits only read-only
research, while `/plan` may inspect the repo and writes the plan file as its
deliverable.

## Core Behavior

1. **Answer directly in the same turn.** No preamble, no plan, no
   "I'll set that up for you".
2. **Research is allowed and encouraged** when it improves the answer —
   anything read-only: `web_search`, `web_extract`, `read_file`,
   `session_search`, `skills_list`, and read-only terminal inspection.
3. **Forbidden unless the user explicitly asks in the same message:**
   - `write_file`, `patch`, or any file creation/editing.
   - Mutating terminal commands: installs (`pip`, `apt`), `git commit/push`,
     `rm`, `mv`, config changes, service restarts.
   - `execute_code` with side effects (pure read-only computation is fine).
   - `cronjob` create/update/remove, memory writes, skill management.
   - Any "let me just test it / install it / set it up" behavior.

## When Action Is Genuinely Required

If answering accurately requires an action (e.g., inspecting a live server or
checking a running service):

1. Explain briefly what you would do and why.
2. Ask permission and wait.

## Common Pitfalls

1. **Treating "answer only" as "no research".** Read-only research is the
   point of the mode — don't answer from memory when a quick `web_search`
   would verify the facts.
2. **Slipping in a "helpful" action.** The deliverable is the answer;
   artifacts, installs, and "I went ahead and..." are failures of this mode.
3. **Asking permission unnecessarily.** If you can answer, just answer —
   only gate when an action is truly required.

## Verification Checklist

- [ ] The user has a complete, direct answer.
- [ ] No files were created or edited.
- [ ] No packages were installed.
- [ ] No config, cron, memory, or service state changed.
- [ ] If an action was required, permission was requested before doing anything.
