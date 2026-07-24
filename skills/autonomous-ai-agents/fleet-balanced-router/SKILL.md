---
name: fleet-balanced-router
description: Automatically route each new substantive bounded task through the subscription-only fleet when fleet.enabled is true; also use when the user explicitly requests fleet routing.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [agents, routing, subscriptions, fleet]
---

# Fleet-balanced task routing

When `fleet.enabled` is true, use this skill automatically for each new
substantive bounded task. Explicit fleet requests also activate it. Do not
admit casual conversation, status questions, tiny edits, or a continuation as
a new task.

1. Save a self-contained UTF-8 task file and pass an explicit workspace with
   `hermes fleet plan --task-file <path> --cwd <workspace> --json`.
2. Treat `NO_ELIGIBLE_LANE` as final for this attempt. Never bypass failed
   auth, billing, qualification, capacity, reserve, or cooldown gates.
3. If the plan is eligible, call `hermes fleet run` with the same task file and
   working directory. Retain its `task_id`; pass `--task-id <id>` for every
   follow-up belonging to that task so its lane pin cannot migrate.
4. Report the selected lane, adapter kind, capacity source/hash, captured time,
   freshness, and confidence.
5. Verify claimed file or external side effects independently before relaying
   them as facts. Never describe `planned`, `pinned`, or `started` as
   `completed`.

`status`, `doctor`, `plan`, and `audit` are read-only. Do not edit credentials,
capacity evidence, or fleet state to manufacture eligibility.

Fleet admission happens once per new substantive task. It does not wrap every
LLM call, alter the current conversation model, or reroute an active task.
