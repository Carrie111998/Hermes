---
name: todoist-kanban
description: "Use Todoist as the personal task system of record and Hermes Kanban as the agent execution layer."
version: 0.1.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  env_vars: [TODOIST_API_TOKEN]
metadata:
  hermes:
    tags: [Todoist, Kanban, Productivity, Tasks, Agents]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [hermes-agent]
---

# Todoist Kanban Bridge

Use this skill when Neil wants Todoist to stay authoritative for personal tasks while Hermes Kanban executes agent-suitable work.

The bridge is intentionally an edge integration:

- Todoist remains the system of record for commitments, due dates, and human completion.
- Hermes Kanban receives only agent-capable tasks.
- Handoffs are idempotent through Kanban `idempotency_key=todoist:<task_id>`.
- Kanban completion evidence is posted back to Todoist as a comment.
- No core Hermes model tools are added.

## Setup

Store the Todoist API token as a secret in `${HERMES_HOME:-~/.hermes}/.env`:

```bash
TODOIST_API_TOKEN=your_todoist_token
```

Put behavior in `~/.hermes/config.yaml`:

```yaml
todoist_kanban:
  board: personal
  tenant: todoist
  default_assignee: codex
  include_labels: [hermes, agent]
  agent_labels: [hermes, agent, codex, automatable]
  human_labels: [human, manual, errand, call, waiting]
  exclude_labels: [human, manual, waiting]
  workspace: scratch
  goal_mode: true
  goal_max_turns: 20
  skills: []
```

The same block is available in `config.example.yaml`.

Create the Kanban board once if you set a non-default board:

```bash
hermes kanban boards create personal --name "Personal Execution"
```

Define a helper:

```bash
TK="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/todoist-kanban/scripts/todoist_kanban.py"
```

## Classification

Agent-capable tasks are tasks with an agent label such as `hermes`, `agent`, `codex`, or `automatable`.

Human commitments are left in Todoist only when they have human/manual labels, excluded labels, or no configured agent label. This is deliberate: absence of an automation label means Todoist remains purely human-owned.

Check a fixture or webhook payload:

```bash
$TK classify --source todoist-task.json
```

## Sync

Dry-run first:

```bash
$TK sync --dry-run
```

Create or reuse Kanban handoffs:

```bash
$TK sync
```

Every handoff uses `todoist:<task_id>` as the Kanban idempotency key. Re-running sync for the same active Todoist task returns the same Kanban card instead of creating a duplicate.

For local testing:

```bash
$TK sync --source todoist-tasks.json --dry-run
$TK sync --source todoist-tasks.json
```

## Webhooks

Use the generic Hermes webhook platform and this skill's filter script to avoid waking an agent for non-agent tasks:

```bash
hermes webhook subscribe todoist-kanban \
  --script "todoist-kanban.py" \
  --prompt "{payload.prompt}" \
  --skills "todoist-kanban"
```

Install a tiny wrapper under `~/.hermes/scripts/todoist-kanban.py` that execs:

```python
import runpy, sys
sys.argv = ["todoist_kanban.py", "webhook-filter"]
runpy.run_path("/path/to/todoist_kanban.py", run_name="__main__")
```

Empty stdout means the webhook is ignored. JSON stdout contains the Todoist task, classification, and idempotency key for an agent-capable task.

## Postback

After Kanban workers finish, post evidence back to Todoist comments:

```bash
$TK postback --dry-run
$TK postback
```

Postback records completed Kanban card IDs in `${HERMES_HOME:-~/.hermes}/todoist-kanban/ledger.json`, so comments are not duplicated on retries.

## Reviews

Daily and weekly reviews summarize bridge activity from the local ledger:

```bash
$TK review daily
$TK review weekly --format json
```

Use these in cron or manual review flows after `sync` and `postback`.
