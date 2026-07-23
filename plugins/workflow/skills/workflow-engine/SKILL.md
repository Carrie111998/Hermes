---
name: workflow-engine
description: "Run DAG-based pipelines via workflow_start — fire-and-forget with kanban notification."
version: 2.0.0
author: Newton
license: MIT
metadata:
  hermes:
    tags: [Workflow, Pipeline, DAG, Kanban, Orchestration]
    related_skills: [kanban-notification-system, plan, systematic-debugging]
---

# Workflow Engine

## Overview

The workflow engine is a **plugin** that registers `workflow_start` and related tools. It runs DAG-based pipelines — multi-step processes where nodes depend on each other and execute in parallel where possible.

**Key design:** fire-and-forget. You call `workflow_start`, it creates kanban cards for every node, subscribes the final-layer card(s) for notification, and returns immediately. The kanban dispatcher picks up ready cards and spawns workers. When the workflow finishes (or blocks, or fails), the gateway notifier pushes a terminal event back to your session — same as any kanban task.

No monitoring loop, no `delegate_task` wrapper, no daemon thread. The engine does not block.

## When to Use

- You need a multi-step pipeline: research → spec → build → review → deliver
- You need parallel work across multiple agents with dependency ordering
- You want to fire off a workflow and keep working — you'll be notified when it's done
- You need a workflow that pauses for user input and resumes when you unblock the card

**Don't use for:** single tool calls, simple sequential tasks a single agent can handle, or anything that needs real-time interaction mid-workflow (use `delegate_task` for that).

## Prerequisites

- The `workflow` plugin must be enabled in `~/.hermes/config.yaml`:
  ```yaml
  plugins:
    enabled:
      - workflow
  ```
- `HERMES_FLEET_PIPELINES` must point to the directory containing pipeline YAMLs (typically `~/.hermes/workspace/docs/fleet-pipelines/`)
- The gateway must be running (it runs the kanban dispatcher and notifier)
- Your profile must have the `workflow` toolset available (it's registered as a standalone plugin toolset)

## How to Run

### Quick reference

| Action | Call |
|--------|------|
| List available pipelines | `workflow_list()` |
| Show pipeline structure | `workflow_show(workflow="name")` |
| Validate before running | `workflow_validate(workflow="name")` |
| Start a pipeline | `workflow_start(workflow="name", context={...})` |
| Check running status | `workflow_status(workflow="name")` |

### Starting a workflow

```python
workflow_start(
    workflow="ideation",
    context={"topic": "Should we adopt X?"},
    inputs={"detail_level": "deep"},
    board="my-board",  # optional board override
    attachments=["/path/to/design.png"],  # files to attach to first-layer cards
)
```

Returns immediately:
```json
{
  "status": "dispatched",
  "workflow": "ideation",
  "message": "Workflow 'ideation' started — cards created, final node will notify on completion"
}
```

### What happens when you call it

1. **Session injection** — your session's platform, chat_id, thread_id, and profile are read from the gateway context (same mechanism as `kanban_create`)
2. **Card creation** — the engine creates kanban cards for every node in the DAG, across all layers
3. **Subscription** — the final-layer card(s) get a row in `kanban_notify_subs`, linking them to your session
4. **Return** — you get `{"status": "dispatched"}` immediately
5. **Execution** — the kanban dispatcher picks up ready cards, spawns workers, workers complete nodes and update card statuses
6. **Notification** — when the final node completes (or blocks, or fails), the gateway notifier pushes a terminal event back to your session

You are notified of these terminal events:
- **completed** — workflow finished successfully
- **blocked** — a node needs user input or is waiting on something
- **crashed** — a worker process died unexpectedly
- **timed_out** — a node exceeded its max runtime
- **gave_up** — the dispatcher exhausted retries

### User-feedback nodes

When a workflow reaches a node that needs user input, the worker blocks the card. The notifier pushes a `blocked` event back to your session. You see something like:

> ⏸ [wf_ideation] @newton Kanban t_abc123 blocked: Needs user feedback — which option should we pursue? (A, B, or C)

You can then:
1. Surface the options to the user in your conversation
2. Get their input
3. Call `kanban_unblock(card_id="t_abc123")` to continue the workflow

The workflow resumes from where it paused — no restart needed.

### Dry-run mode

```python
workflow_start(workflow="name", dry_run=True)
```

Shows the execution plan (layers, nodes, dependencies) without creating any cards. Always synchronous since it's instant.

### File attachments

Pass file paths to attach to the first layer's kanban cards:

```python
workflow_start(
    workflow="feature-dev",
    inputs={"artifact": "/tmp/design.png"},
    attachments=["/tmp/design.png"],
)
```

Attachments are stored in the kanban attachment store. Agents that pick up the cards can read them via `list_attachments`. Only the first layer gets the files — downstream nodes consume upstream outputs instead.

### Resume from saved state

```python
workflow_start(workflow="name", resume=True, node="specific-node")
```

Reuses saved state from a previous run. Skips already-completed nodes and starts from the specified node.

## Available Pipelines

These live in `$HERMES_FLEET_PIPELINES` (typically `~/.hermes/workspace/docs/fleet-pipelines/`):

| Pipeline | Nodes | Layers | Purpose |
|----------|-------|--------|---------|
| `council` | 13 | 8 | Structured multi-agent debate (adversarial) |
| `ideation` | 14 | 12 | Research → spec → security → decompose |
| `brainstorm` | 14 | 6 | Collaborative multi-agent ideation (cooperative) |
| `feature-dev` | 10 | 5 | Build → CI → review → merge → post-merge |
| `deployment-verify` | 4 | 3 | Post-deploy adversarial probe |
| `deployment-revert` | 4 | 3 | Auto-rollback on deploy failure |
| `error-response` | 5 | 4 | Sentry alert triage and dispatch |
| `research` | 1 | 1 | Single-agent deep research |
| `fleet-health` | — | — | Fleet health check |
| `agent-service-e2e` | — | — | Agent-service end-to-end test |

Use `workflow_list()` for the full up-to-date list.

## Making Workflows (YAML Authoring)

### Structure

```yaml
name: my-workflow
description: "Multi-step pipeline"
version: "1.0.0"

defaults:
  goal_max_turns: 20
  timeout_minutes: 60

inputs:
  - name: topic
    required: true
    description: "The topic to analyze"

nodes:
  setup:
    description: "Capture inputs and set up context"
    agent: researcher
    task: >
      Analyze the topic: "{topic}".
      Write findings to the shared context.
    outputs:
      - findings

  review:
    description: "Review and synthesize"
    agent: reviewer
    task: >
      Read {setup.findings}. Synthesize a final recommendation.
    depends_on:
      - setup
    outputs:
      - recommendation
    fallback_on_timeout: degraded
```

### Node fields

| Field | Required | Description |
|-------|----------|-------------|
| `agent` | Yes | Which agent profile executes this node |
| `task` | Yes | Instruction body — supports `{upstream.output}` template variables |
| `depends_on` | No | Node IDs that must complete before this one starts |
| `outputs` | No | Named outputs — available as `{node-id.output-name}` downstream |
| `timeout_minutes` | No | Max wall-clock runtime per node. Default: 10 min. |
| `fallback_on_timeout` | No | `skip` \| `degraded` \| `fail` (default). Controls behavior on timeout. |
| `goal_max_turns` | No | Max agent turns for this node. Default: 20. |
| `when` | No | Conditional expression — node only runs when condition is met |

### DAG patterns

- **Linear chain:** A → B → C (each depends on the previous)
- **Parallel layer:** Multiple nodes with the same `depends_on` run concurrently
- **Diamond:** Two nodes depend on the same prior node, then converge
- **Failure routing:** `fallback_on_timeout: skip` lets downstream proceed; `degraded` passes partial results; `fail` blocks the pipeline

### Template substitution

Variables resolve from:
1. Engine-injected context: `{run_id}`, `{date}`
2. Input parameters: `{inputs.topic}`, `{inputs.pr_link}`
3. Upstream node outputs: `{researcher.findings}`, `{setup.raw-context}`

### Roles

Workflows can declare a `roles:` block mapping role names to profile names. Nodes reference roles via `agent: "{role_name}"`. To swap an agent, edit one line in `roles:`.

```yaml
roles:
  architect: edison
  executor: newton
  skeptic: nikola
nodes:
  position-architect:
    agent: "{architect}"
    task: "Analyze the proposal from an architecture perspective"
```

### Revision loops (LOOP convention)

When a reviewer rejects work, they block the card with reason `LOOP:<target-node> | <details>`. The engine re-runs the target node automatically. Up to 3 cycles; the 4th rejection escalates to the orchestrator.

A `blocked` without the `LOOP:` prefix is treated as a genuine blocker and escalates immediately.

## Pitfalls

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `workflow_start` returns error "workflow not found" | Pipeline YAML missing or `HERMES_FLEET_PIPELINES` not set | Check `workflow_list()` shows it; verify env var |
| Cards created but no notification arrives | Session info not injected (CLI/cron path) | Ensure you're calling from a gateway session, not a cron or CLI |
| Node stuck "running" but worker completed | Kanban dispatcher polls the wrong board | Verify card is on the expected board |
| Template substitution failure | Context dict missing a key referenced in YAML | Ensure ALL `{placeholders}` in YAML are in the context dict |
| Card auto-blocked "heartbeat stale" | New card needs initial heartbeat | Engine handles this automatically in fire-and-forget mode |
| Workflow doesn't resume after unblock | Engine already returned — no monitoring loop | This is expected. The kanban dispatcher picks up unblocked cards on its next tick. No engine restart needed. |

## Verification Checklist

- [ ] Pipeline exists: `workflow_list()` shows it
- [ ] Pipeline validates: `workflow_validate(workflow="name")` returns `valid: true`
- [ ] All required inputs provided (check `workflow_show` for placeholder variables)
- [ ] All referenced context keys present in the context dict
- [ ] `workflow_start` returns `{"status": "dispatched"}`
- [ ] Status checkable via `workflow_status(workflow="name")`
- [ ] Final node completion triggers notification in your session
