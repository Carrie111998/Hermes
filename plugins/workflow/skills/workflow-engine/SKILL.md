---
name: workflow-engine
description: "Run DAG-based pipelines via workflow_start — fire-and-forget with kanban notification."
version: 3.1.0
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

**Key design:** fire-and-forget. You call `workflow_start`, it creates kanban cards for nodes, subscribes the final-layer card(s) for notification, and returns immediately. The kanban dispatcher picks up ready cards and spawns workers. When the workflow finishes (or blocks, or fails), the gateway notifier pushes a terminal event back to your session.

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
- Your profile must have `"workflow"` in its toolsets:
  ```yaml
  tools:
    toolsets:
      - hermes-cli
      - workflow
  ```
- `HERMES_WORKFLOW_FILES` must point to the directory containing pipeline YAMLs
- The gateway must be running (it runs the kanban dispatcher and notifier)

## How to Run

### Quick reference

| Action | Call |
|--------|------|
| List available pipelines | `workflow_list()` |
| Show pipeline structure | `workflow_show(workflow="name")` |
| Validate before running | `workflow_validate(workflow="name")` |
| Start a pipeline | `workflow_start(workflow="name", context={...})` |
| Check running jobs | `workflow_jobs()` |
| Check job status | `workflow_status(workflow="name")` |

### Starting a workflow

```python
workflow_start(
    workflow="my-pipeline",
    context={"topic": "Should we adopt X?"},
    inputs={"detail_level": "deep"},
    board="my-board",  # optional board override
)
```

Returns immediately:
```json
{
  "status": "dispatched",
  "workflow": "my-pipeline",
  "message": "Workflow 'my-pipeline' started — cards created, final node will notify on completion"
}
```

### What happens when you call it

1. **Session injection** — your session's platform, chat_id, thread_id, and profile are read from the gateway context
2. **Card creation** — the engine creates kanban cards for every node in the DAG
3. **Subscription** — the final-layer card(s) get a row in `kanban_notify_subs`, linking them to your session
4. **Return** — you get `{"status": "dispatched"}` immediately
5. **Execution** — the kanban dispatcher picks up ready cards, spawns workers
6. **Notification** — when the final node completes (or blocks, or fails), the gateway notifier pushes a terminal event back to your session

### Supervisor subprocess (loop zones)

If the workflow contains LOOP zones (verify → revision pairs), the engine spawns a **supervisor subprocess** that runs the layer-by-layer monitoring loop. The supervisor:

- Creates cards for each layer as it advances
- Detects LOOP blocks and dispatches revision nodes
- Evaluates LOOP rejections via the analyst (checks criteria vs rejection)
- Saves state at each transition for resume
- Exits when the workflow completes

The calling agent is not blocked — the supervisor runs in the background.

### Unexpected block notification

When a card blocks with a non-LOOP reason, the engine:

1. Detects the block in the monitoring loop
2. Calls the analyst to evaluate the situation
3. Pushes a structured assessment to your session:

> ⚠️ Workflow anomaly: **node-name** blocked in **pipeline-name**
> **Summary:** One-line summary of what happened
> **Detail:** Explanation of the failure and its impact
> **Action:** What you should do next

You don't need to poll or subscribe to every card — the engine handles detection and notification automatically.

### User-feedback nodes

When a workflow reaches a node that needs user input, the worker blocks the card. The notifier pushes a `blocked` event back to your session. You can then:

1. Surface the options to the user
2. Get their input
3. Call `kanban_unblock(card_id="t_abc123")` to continue

### Dry-run mode

```python
workflow_start(workflow="name", dry_run=True)
```

Shows the execution plan without creating any cards.

### Resume from saved state

```python
workflow_start(workflow="name", resume=True, node="specific-node")
```

Reuses saved state from a previous run. Skips already-completed nodes.

### Job log

```bash
hermes workflow jobs                    # last 10 runs
hermes workflow jobs --status running   # only running
hermes workflow jobs --limit 5          # last 5
```

Shows per-node progress: `8/10` means 8 of 10 nodes completed.

## YAML Authoring

### Roles

Workflows declare a `roles:` block mapping role names to profile names. Nodes reference roles via `agent: "{role_name}"`. Agent names appear only in the `roles:` block — everywhere else uses role names.

```yaml
roles:
  analyst: analyst-profile
  coder: coder-profile
  reviewer: reviewer-profile
  security: security-profile

nodes:
  analyst-spec:
    agent: "{analyst}"
    task: "Write the spec for {inputs.topic}"
  coder-implement:
    agent: "{coder}"
    task: "Implement the spec from {analyst-spec.spec_path}"
    depends_on:
      - analyst-spec
```

To change the agent for a role, edit one line in `roles:`. The workflow is portable — give it to someone and they just change the profile names in the roles block.

### Node fields

| Field | Required | Description |
|-------|----------|-------------|
| `agent` | Yes | Role placeholder: `"{role_name}"` |
| `task` | Yes | Instruction body — supports `{upstream.output}` template variables |
| `depends_on` | No | Node IDs that must complete before this one starts |
| `outputs` | No | Named outputs — available as `{node-id.output-name}` downstream |
| `timeout_minutes` | No | Max wall-clock runtime per node. Default: 10 min. |
| `fallback_on_timeout` | No | `skip` \| `degraded` \| `fail` (default) |
| `when` | No | Conditional expression — node only runs when condition is met |

### Revision loops (LOOP convention)

When a reviewer rejects work, they block the card with reason `LOOP:<target-node> | <details>`. The engine evaluates the rejection against the verify node's criteria via the analyst:

- **Analyst says "loop"** — revision node is dispatched, then verify re-runs
- **Analyst says "proceed"** — rejection doesn't match criteria, workflow advances
- **Analyst unavailable** — defaults to "loop" (conservative)

Up to 3 cycles; the 4th rejection escalates to the orchestrator.

### Template substitution

Variables resolve from:
1. Engine-injected context: `{run_id}`, `{date}`
2. Input parameters: `{inputs.topic}`, `{inputs.pr_link}`
3. Upstream node outputs: `{upstream-node.output-name}`

## Pitfalls

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `workflow_start` returns error "workflow not found" | Pipeline YAML missing or `HERMES_WORKFLOW_FILES` not set | Check `workflow_list()` shows it; verify env var |
| Cards created but no notification arrives | Session info not injected (CLI/cron path) | Ensure you're calling from a gateway session |
| Node stuck "running" but worker completed | Kanban dispatcher polls the wrong board | Verify card is on the expected board |
| Template substitution failure | Context dict missing a key referenced in YAML | Ensure ALL `{placeholders}` in YAML are in the context dict |
| Workflow doesn't resume after unblock | Engine already returned — no monitoring loop | Expected. Kanban dispatcher picks up unblocked cards on next tick |
| Unexpected block, no notification | Analyst or adapter unavailable | Check logs for errors |
| Plugin tools not visible to LLM | `"workflow"` not in agent's toolsets | Add `"workflow"` to `tools.toolsets` in agent config |

## Verification Checklist

- [ ] Plugin enabled: `plugins: enabled: [workflow]` in config
- [ ] Toolset enabled: `"workflow"` in agent's `tools.toolsets`
- [ ] Pipeline exists: `workflow_list()` shows it
- [ ] Pipeline validates: `workflow_validate(workflow="name")` returns `valid: true`
- [ ] All required inputs provided
- [ ] All referenced context keys present in the context dict
- [ ] `workflow_start` returns `{"status": "dispatched"}`
- [ ] Final node completion triggers notification in your session
- [ ] Unexpected blocks trigger analyst assessment in your session
