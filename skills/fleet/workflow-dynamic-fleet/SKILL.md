---
name: workflow-dynamic-fleet
description: 'Use when you need to investigate, research, or debug something that doesn''t fit a pre-defined pipeline — author a DAG at runtime instead of using workflow_start'
version: 1.0.0
category: fleet
author: Sherlock (fleet orchestrator)
license: MIT
metadata:
  hermes:
    tags: [workflow, dynamic, fleet, investigation, research, debug]
    related_skills: [workflow-engine-fleet, workflow-engine]
---

# Dynamic Workflow — Fleet Overlay

## When to Use This Skill

**Use `workflow_dynamic_start` (not `workflow_start`) when:**

- The problem is open-ended and the shape of work isn't known upfront
- You need to investigate, research, or debug something
- The work is exploratory, not a repeatable pipeline

**Use `workflow_start` (not this) when:**

- The pipeline has a known, fixed shape (e.g. ideation, feature-dev, council review)
- You need auditability and repeatability
- The cost must be predictable

## How the Tool Works

**Tool name:** `workflow_dynamic_start`

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `workflow` | string | Workflow ID (e.g. `investigate-checkout-failure`) |
| `context` | object | `{ objective, nodes, context }` — the work description and initial DAG nodes |
| `scope` | string | `project` / `global` / `durable` (default: `project`) |
| `single_flight` | boolean | Prevent duplicate runs for the same workflow ID |
| `delivery_target` | string | Where to deliver results (voice DM, channel, etc.) |
| `dry_run` | boolean | Validate the DAG without executing |

**How it works:**

1. The model authors the DAG: start with a small set of initial nodes
2. Workers execute nodes and return summaries
3. The orchestrator uses `workflow_dynamic(action="extend")` to add follow-up nodes based on what the workers report
4. The engine handles: dependency validation, ready-set computation, async reconciliation, dispatch, cancellation

You never need to define the full graph upfront. Start small, extend as findings emerge.

## Workflow Lifecycle

### Concrete Example: Dynamic Investigation

```
1. workflow_dynamic_start(
     workflow="investigate-checkout-failure",
     context={
       "objective": "Investigate and fix the checkout flow failure on iOS Safari",
       "nodes": [
         {"node_id": "read-logs", "goal": "Read production error logs for checkout failures on iOS Safari, summarize hypotheses"}
       ]
     },
     scope="project"
   )

2. [Worker returns with findings: "94% Safari iOS 17, started after deploy X on June 10. Points to polyfill regression."]

3. workflow_dynamic(action="extend", workflow_id="investigate-checkout-failure", nodes=[
     {"node_id": "diff-deploy", "goal": "Diff deploy X vs prior, flag polyfill changes", "depends_on": ["read-logs"]},
     {"node_id": "reproduce", "goal": "Reproduce the issue on iOS 17 Safari", "depends_on": ["read-logs"]},
     {"node_id": "synthesize", "goal": "Combine findings into a fix proposal", "depends_on": ["diff-deploy", "reproduce"]}
   ])

4. workflow_dynamic(action="dispatch", workflow_id="investigate-checkout-failure")

5. [Workers complete. synthesize reports back.]
```

**Key pattern:** start with one exploratory node, read the result, then fan out based on what you learn.

## Scope Modes

| Scope | Behavior | Use case |
|-------|----------|----------|
| `project` (default) | Creates kanban cards on the `dynamic-workflows` board, visible to the fleet | Standard investigations |
| `global` | In-memory only, no kanban, no persistence | Quick one-shot investigations |
| `durable` | Persists state to `~/.hermes/workflow-logs/<workflow_id>/state.json`. Survives restarts | Cron-driven workflows |

## Constraints

- **Max 256 nodes** per workflow
- **Max 16 workers** per dispatch call
- **`max_iterations: 150`** per worker (inherited from delegation config)
- **`max_concurrent_children: 3`** (inherited from delegation config)
- **`max_spawn_depth: 1`** (inherited from delegation config)

## How to Read Worker Output

Workers return summaries. The orchestrator reads them and decides next steps.

**`exit_reason: "max_iterations"`** means the worker ran out of turns with a partial result. The orchestrator should:

1. Read the partial summary
2. Decide: extend with a follow-up node, or accept the partial and move on

**No polling needed.** Reconciliation happens automatically on every `status` and `dispatch` call.

## Cancellation

```
workflow_dynamic(action="cancel", workflow_id="investigate-checkout-failure")
```

This marks all pending nodes as cancelled and interrupts dispatched workers.

**Cancel when:**

- The objective is met (you have the answer)
- The cost budget is exhausted
- The investigation is no longer worth pursuing

## Naming Conventions

**Do NOT use bbopen's naming.** Use the action names our engine supports:

| Action | Purpose |
|--------|---------|
| `extend` | Add new nodes to the DAG |
| `record` | Record a worker's result for a node |
| `dispatch` | Send ready nodes to workers |
| `status` | Check workflow state |
| `cancel` | Cancel the workflow |
