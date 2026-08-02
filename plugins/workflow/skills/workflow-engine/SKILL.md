---
name: workflow-engine
description: "Use when running multi-agent DAG pipelines via workflow_start — fire-and-forget orchestration across agents. Covers review loops, template substitution, delivery routing, and workspace inheritance."
version: 5.0.0
author: Newton
license: MIT
metadata:
  hermes:
    tags: [workflow, pipeline, dag, kanban, orchestration, multi-agent]
    related_skills: [kanban-notification-system]
---

# Workflow Engine

## Overview

Run DAG-based pipelines via `workflow_start`. The engine creates kanban cards, returns immediately, and the kanban dispatcher handles execution. When the workflow finishes, you get a notification with a summary.

Fire-and-forget. No monitoring loop. The engine does not block.

## When to Use

- Multi-step pipeline: research → spec → build → review → deliver
- Parallel work across multiple agents with dependency ordering
- Sealed-envelope testing (implementer and tester work blind)
- Review loops with fail → enrich → re-work cycles

**Don't use for:** single tool calls, simple sequential tasks one agent can handle, or real-time mid-workflow interaction.

## How to Use

### Starting a Workflow

```
workflow_start(
    workflow="implementation",
    board="agent-service",
    inputs={"issue_number": "346", "repo": "agent-service"},
    attachments={"grill_artifact": "/path/to/file.md"},
)
```

**Parameters:**
- `workflow` — pipeline name (matches YAML filename without .yaml)
- `board` — kanban board name (optional, overrides YAML)
- `inputs` — key-value dict available as `{inputs.key}` in node tasks
- `attachments` — named file dict attached to cards (keys must match YAML declarations)
- `delivery_target` — optional object for cron-triggered workflows (see below)

### Progressive Disclosure

1. `workflow_list()` — names + descriptions
2. `workflow_show(workflow="ideation")` — structure, inputs, attachments, node descriptions
3. `workflow_start(...)` — trigger

### Cron Delivery Routing

Cron-triggered workflows have no Discord session context. Always pass `delivery_target`:

```
workflow_start(
    workflow="implementation",
    board="agent-service",
    inputs={"issue_number": "346"},
    delivery_target={
        "platform": "discord",
        "channel": "1500949529443303594",
        "profile": "nikola"
    },
)
```

Without this, notifications go to Sherlock (default profile).

### Review Nodes

Reviewers always complete the card — never block. Pass/fail is in the result summary:

```yaml
qa-verify:
  agent: "{qa}"
  task: >
    Run the tests. Complete this card with a summary.
    - If pass → include test results
    - If fail → include failure details

    Do NOT block this card. Always complete it with pass/fail.
```

The engine auto-injects a context header telling the reviewer which node it's reviewing and the card ID. YAML doesn't need to explain how to find the work.

### Review Workspace Inheritance

Default: reviewer runs in scratch (blind review). Set `inherit_workspace: true` to start in the same project directory:

```yaml
  qa-review:
    inherit_workspace: true  # reviewer sees the spec author's files
```

Use `true` for read-only reviews where the reviewer needs the actual files.

### Block Reason Convention

- `"pending review"` — triggers the review pipeline
- Any other block reason — treated as unrelated failure

Workers default to `kanban_block`. YAML must explicitly say "call kanban_complete with a summary" when the worker should complete.

## Template Variables

| Template | Resolves to |
|---|---|
| `{inputs.key}` | Input value |
| `{nodes.node-id.card_id}` | Kanban task ID (for cross-referencing) |
| `{nodes.node-id.name}` | Node ID + short run ID |
| `{run_id}` | Full run ID |
| `{run_short_id}` | Time-only portion |
| `{date}` | Current date |

## Common Pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| No notification arrives | Cron without `delivery_target` | Pass `delivery_target` with explicit `profile` |
| Notification goes to Sherlock | `profile` is None | Pass `delivery_target` with `profile` |
| Unknown input rejected | Input not declared in YAML | Add to YAML `inputs:` or use top-level param |
| Review not triggered | Block reason ≠ "pending review" | Use exact phrase "pending review" |
| Review loops hit triage | Reviewer blocked instead of completing | Reviewers must always complete (done) |
| Cards default to scratch | Board has no `default_workdir` | Set `default_workdir` in `board.json` |
| Body prefix not injected | Gateway running stale bytecode | Delete `.pyc` files, restart gateway |
| Template unresolved | Missing key in context/inputs | Ensure all `{placeholders}` are declared |

## Verification Checklist

- [ ] Pipeline exists: `workflow_list()` shows it
- [ ] Pipeline validates: `workflow_validate(workflow="name")` returns valid
- [ ] All required inputs provided
- [ ] `workflow_start` returns `{"status": "dispatched"}`
- [ ] Cron workflows have `delivery_target` with explicit `profile`
- [ ] Review nodes say "call kanban_complete" (not kanban_block)
- [ ] Final node completion triggers notification in your session
