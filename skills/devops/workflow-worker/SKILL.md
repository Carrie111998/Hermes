---
name: workflow-worker
description: Execute one workflow stage through wf tools only.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [workflow, stage-turn, worker]
    related_skills: [kanban-worker]
---

# Workflow Worker Skill

You are running one bounded stage turn for an existing workflow instance.
You do not manage the underlying Kanban card and you do not choose a broader
workflow shape.

## When to Use

This skill is force-loaded by the workflow-aware dispatcher. Use it only when
`$HERMES_WORKFLOW_TASK` is set.

## Prerequisites

- The dispatcher supplied a workflow task id and current step key.
- The `wf_*` tools are present.
- Tenant-specific briefs and bridge tools, if any, were force-loaded separately.

## How to Run

1. Call `wf_context` without a task id. Treat its current `step`, structured
   `event`, `vars`, and `corr` as the entire stage scope.
2. Perform only the work allowed by the step's `turn` and `actions`.
3. Finish with exactly one terminal workflow tool:
   `wf_advance`, `wf_propose`, `wf_review`, or `wf_exception`.
4. Stop after that terminal tool succeeds.

## Quick Reference

| Need | Tool |
|---|---|
| Read current step and structured input | `wf_context` |
| Enter the declared next step | `wf_advance` |
| Park a complete action for approval | `wf_propose` |
| Ask for a bounded human choice | `wf_review` |
| Record a resumable execution failure | `wf_exception` |
| Ledger a structured signal | `wf_signal` |

## Procedure

### 1. Orient

Call `wf_context`. Never infer the stage from the task title or prompt alone.
The context is step-scoped deliberately: do not search for or load the full
workflow template.

### 2. Execute the stage

Follow the tenant brief named by `step.turn.brief`. Use only actions listed in
`step.actions`. Raw inbound bodies must remain behind references; work from the
validated structured event fields returned by `wf_context`.

### 3. Settle once

- `wf_advance`: the stage work is complete and evidence is structured.
- `wf_propose`: an allowlisted side effect requires approval; supply the
  complete payload.
- `wf_review`: the stage cannot choose safely; provide the bounded options.
- `wf_exception`: execution failed or the stage contract cannot be satisfied.

Exactly one of these closes the turn.

## Pitfalls

- Never call `kanban_complete`, `kanban_block`, `kanban_unblock`,
  `kanban_create`, `kanban_comment`, or any other raw `kanban_*` write.
  Workflow workers do not own Kanban lifecycle state; the engine translates
  workflow outcomes into chassis transitions.
- Never advance to a step not declared by the current step.
- Never place raw message bodies, secrets, or client data in prompts, argv,
  evidence, comments, or exception text.
- Never answer conversationally and exit without a terminal `wf_*` call.
  The dispatcher treats that as a protocol violation and maps it to an
  exception.

## Verification

A valid turn leaves the instance in the state implied by its single terminal
tool, closes or parks the active run, and records the run's current step key.
