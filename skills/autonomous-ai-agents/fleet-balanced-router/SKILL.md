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

First determine which admission surface owns the work:

- A Desktop session whose authoritative runtime metadata reports
  `model_source=fleet_auto` and `fleet_route_purpose=desktop_parent` is already
  admitted and pinned as a `desktop_parent`. Execute that conversation
  normally; do not call `hermes fleet run` to readmit or replace it.
- The CLI workflow below creates a separate `task_worker` for a bounded child
  task. It never changes or substitutes the active parent session.

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

Parent admission happens before Desktop agent construction. Child admission
happens once per new substantive bounded task. Neither path wraps every LLM
call or reroutes an active session; resume and compression continuations retain
the original lineage pin.
