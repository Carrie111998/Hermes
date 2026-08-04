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

Reviewers **always complete the card — never block**. Pass/fail is parsed from the completion summary by the engine, so the reviewer must lead with a verdict on the FIRST LINE:

```yaml
qa-verify:
  agent: "{qa}"
  task: >
    Run the tests. Complete this card with a summary.
    - First line: PASS or FAIL (verdict must be the first word)
    - If PASS → include test results
    - If FAIL → include the specific blockers the author must fix

    Do NOT block this card. Always complete it with a verdict.
```

**Verdict contract (mandatory):** the completion summary's first line must start with `PASS` or `FAIL` (the engine also recognizes `CHANGES REQUIRED`, `BLOCKED`, `REJECTED`, ✅, ❌ in the first 300 chars). Without a verdict the engine treats the review as passed by default — a reviewer that finds problems but forgets the `FAIL` verdict lets broken work through.

The engine auto-injects a context header telling the reviewer which node it's reviewing and the card ID. YAML doesn't need to explain how to find the work.

### What happens on FAIL (automatic — no manual card resets)

1. Engine comments the upstream card with the review results (`Review Failed (reviewer)`)
2. Upstream resets to `ready` — the author re-does the work
3. **All** reviewers of that upstream are re-dispatched automatically when the revised work completes
4. Each reviewer has a retry budget (per-review `max_retries` > node > workflow > default 3). Once exhausted, the reviewer stays terminal and the FAIL is documented for operator escalation

Do NOT manually reset reviewer cards with SQL after a FAIL — the engine handles re-dispatch. Manual resets are only needed if a supervisor crashed mid-loop (see `workflow_status` / resume).

### Review Workspace Inheritance

Default: reviewer runs in scratch (blind review). Set `inherit_workspace: true` to start in the same project directory:

```yaml
  qa-review:
    inherit_workspace: true  # reviewer sees the spec author's files
```

Use `true` for read-only reviews where the reviewer needs the actual files.

### Block Reason Convention

The **only** block reason that matters to the engine is `"pending review"` — used by the legacy auto-resume path when a completed run's card is re-requested for review. All other block reasons are treated as unrelated failures and do NOT trigger the review pipeline.

In current review flows, reviewers never block: they complete with a PASS/FAIL verdict (see Review Nodes above).

## Template Variables

| Template | Resolves to |
|---|---|
| `{inputs.key}` | Input value |
| `{nodes.node-id.card_id}` | Kanban task ID (for cross-referencing) |
| `{nodes.node-id.name}` | Node ID + short run ID |
| `{run_id}` | Full run ID |
| `{run_short_id}` | Time-only portion |
| `{date}` | Current date |

## Workflow Templates

Bundled YAML templates in `templates/` map the wiki's graph-engineering patterns (see `docs/wiki/concepts/dynamic-workflow-engineering.md`) onto the engine's schema. Each file is a working pipeline with comments explaining what every construct achieves. Copy → rename → edit roles and tasks.

The template files ship inside the plugin bundle at `<plugin install>/skills/workflow-engine/templates/` — read them directly with `read_file` when you need the exact YAML (the table below captures the pattern so you can also reproduce it inline).

| Pattern | Template | Key constructs |
|---|---|---|
| Sequential pipeline | `templates/sequential-pipeline.yaml` | `depends_on` chains, `fallback_on_timeout` |
| Parallel fan-out + synthesize | `templates/parallel-fanout.yaml` | implicit same-layer parallelism, barrier via `depends_on` on all tracks |
| Review loop (adversarial verification) | `templates/review-loop.yaml` | `reviews:` list on producer, PASS/FAIL verdict contract, `max_retries`, `inherit_workspace` |
| Conditional branch (classify-and-act) | `templates/conditional-branch.yaml` | `when:` expressions on branches, `{node-id.result} contains ...` |
| Multi-phase with privacy barriers | `templates/multi-phase-gates.yaml` | `synthetic: true` gates, `privacy_gate: true` on sealed producers |
| Orchestrator-workers (generate-and-filter) | `templates/orchestrator-workers.yaml` | parallel candidates, barrier, rubric filter |
| Loop-until-done (bounded) | `templates/loop-until-done.yaml` | review loop as bounded loop, `max_retries` as ceiling, `when:` stop-condition follow-up |

Engine support note: the wiki's unbounded loop-until-done and mid-run dynamic graph extension are **not** representable in static YAML — the engine's loop is the bounded review loop, and dynamic extension lives in (paused) dynamic mode.

## Common Pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| No notification arrives | Cron without `delivery_target` | Pass `delivery_target` with explicit `profile` |
| Notification goes to Sherlock | `profile` is None | Pass `delivery_target` with `profile` |
| Unknown input rejected | Input not declared in YAML | Add to YAML `inputs:` or use top-level param |
| Review not triggered | Reviewers block instead of completing | Reviewers must always complete (done) with PASS/FAIL verdict |
| Broken work passes review | Reviewer summary lacks a verdict word | First line must be `PASS` or `FAIL` (see Verdict contract) |
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
- [ ] Reviewers lead with `PASS`/`FAIL` verdict on first line
- [ ] Final node completion triggers notification in your session
