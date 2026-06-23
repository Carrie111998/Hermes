---
name: workflow-engine-fleet
description: 'Use when you need to run, reference, or extend fleet workflows — pre-defined pipelines (council, brainstorm, ideation, feature-dev, deployment-verify, deployment-revert, error-response, new-agent-onboarding) or dynamic model-authored workflows for open-ended investigation.'
version: 1.0.0
author: Sherlock (fleet orchestrator)
license: MIT
metadata:
  hermes:
    tags: [workflow, fleet, pipelines, council, ideation, kanban, dynamic]
    related_skills: [workflow-engine, kanban-orchestrator, fleet-pipeline-governance]
---

# Workflow Engine — Fleet Overlay

## Overview

This skill extends `workflow-engine` with fleet-specific content: the pipelines we ship, our kanban-specific patterns (human validation gate, todo card flow), and our operator conventions.

Load `workflow-engine` first for the generic engine reference. This overlay adds fleet-specific context on top.

## When to Reach for a Pipeline

**Rule of thumb for strategic/fleet-level questions:** if the question involves multiple stakeholders, trade-offs, risk assessment, or competing priorities — use a pipeline. Do NOT write a solo analysis.

**Council vs Brainstorm — which pipeline?**

| Situation | Pipeline | Why |
|-----------|----------|-----|
| Adversarial debate needed (tough trade-offs, competing priorities) | **Council** | Agents challenge each other's positions |
| Collaborative assessment (fleet audit, strategy, role mapping) | **Brainstorm** | Agents build on each other's ideas, cooperative framing |
| "Should we build X or Y?" | **Council** | Disagreement is the point |
| "What's the risk of Z?" | **Council** | Multiple risk lenses |
| "How do I deploy this?" | Solo answer (single-domain) | No pipeline needed |

**Exception:** if the question is purely technical (how-to, config, debug a specific error), solo analysis is correct. Pipelines are for questions where reasonable agents can disagree or where multiple perspectives produce better decisions.

### Pre-defined vs Dynamic — which mode?

| Signal | Pre-defined (`workflow_start`) | Dynamic (`workflow_dynamic_start`) |
|--------|-------------------------------|-----------------------------------|
| Pipeline shape is known upfront | ✅ | ❌ |
| Repeatable across runs | ✅ | ❌ |
| Cost must be predictable | ✅ | ❌ |
| Problem is open-ended / exploratory | ❌ | ✅ |
| Shape emerges from worker findings | ❌ | ✅ |
| One-off investigation, debug, research | ❌ | ✅ |

**Rule:** if you can draw the DAG before any worker runs, use `workflow_start`. If you can't, use `workflow_dynamic_start` and let the model author the graph as findings arrive.

## Dynamic Workflows

Dynamic workflows create ad-hoc DAGs at runtime instead of reading pre-defined YAML pipelines. The model authors the graph: start with initial nodes, then extend the DAG based on what workers report back.

**Tool:** `workflow_dynamic_start` — create a workflow with initial nodes.
**Tool:** `workflow_dynamic` — extend, record, dispatch, status, cancel (action enum).

### Workflow Lifecycle

```
1. workflow_dynamic_start(
     workflow="investigate-checkout-failure",
     context={
       "objective": "Investigate and fix the checkout flow failure on iOS Safari",
       "nodes": [{"node_id": "read-logs", "goal": "Read production error logs, summarize hypotheses"}]
     },
     scope="project"
   )

2. [Worker returns: "94% Safari iOS 17, started after deploy X on June 10. Points to polyfill regression."]

3. workflow_dynamic(action="extend", workflow_id="investigate-checkout-failure", nodes=[
     {"node_id": "diff-deploy", "goal": "Diff deploy X vs prior, flag polyfill changes", "depends_on": ["read-logs"]},
     {"node_id": "reproduce", "goal": "Reproduce on iOS 17 Safari", "depends_on": ["read-logs"]},
     {"node_id": "synthesize", "goal": "Combine findings into a fix proposal", "depends_on": ["diff-deploy", "reproduce"]}
   ])

4. workflow_dynamic(action="dispatch", workflow_id="investigate-checkout-failure")

5. [Workers complete. synthesize reports back.]
```

### Scope Modes

| Scope | Kanban | Persistence | Use for |
|-------|--------|-------------|---------|
| `project` (default) | Cards on `dynamic-workflows` board | No | Visible fleet investigations |
| `global` | No cards | No | Quick one-shot investigations |
| `durable` | No cards | `~/.hermes/workflow-logs/<id>/state.json` | Cron-driven workflows, restart-safe |

### Constraints

- Max 256 nodes per workflow
- Max 16 workers per dispatch call
- `max_iterations: 150` per worker (from delegation config)
- `max_concurrent_children: 3` (from delegation config)
- `max_spawn_depth: 1` (from delegation config)

### Worker Output

- Workers return summaries. The orchestrator reads them and decides next steps.
- `exit_reason: "max_iterations"` = worker ran out of turns with a partial result. Read the partial, extend with a follow-up node or accept and move on.
- No polling needed — reconciliation happens automatically on every `status` and `dispatch` call.

### Cancellation

`workflow_dynamic(action="cancel")` marks all pending nodes as cancelled and interrupts dispatched workers. Cancel when the objective is met or the cost budget is exhausted.

### Council Limitations

The council pipeline has known failure modes:

- **Parallel execution fragility.** The council's parallel phases (position, reflect, probe) are the most failure-prone. Cards spawned before dependencies complete, timeout cascades through skip chains, stale heartbeats on pending cards.
- **Debate format vs structured deliverable.** The council produces debate artifacts (shared concerns, genuine disagreement, confidence dispersion) — NOT structured reports with overlap maps, gap matrices, or recommendation tables.

**When to NOT use the council:**
- The user wants a specific document format (overlap map, gap analysis, recommendations table)
- The question requires reading all agent files and producing a comprehensive audit
- The user explicitly says the council isn't the right shape
- The topic is too broad for a single council question — break it into smaller, focused questions

## Auto-Discovery (Workflows as Skills)

When a user message matches a workflow trigger, the agent should offer to run that workflow — just like skills. The workflow registry scans two locations:

1. **Pre-defined pipelines** from `docs/fleet-pipelines/` (shipped with the repo)
2. **User-saved templates** from `~/.hermes/workflows/` (dynamic mode)

### Trigger Matching

Matching is keyword-based and case-insensitive. Each workflow declares a trigger string (pipe-separated keywords). The engine splits the trigger on `|` and checks if any keyword appears in the user message.

**Workflow trigger keywords (built-in):**

| Pipeline | Trigger keywords |
|----------|-----------------|
| `council` | `council`, `debate`, `trade-off`, `competing priorities`, `adversarial`, `perspective` |
| `ideation` | `ideation`, `spec`, `research`, `decompose`, `architecture` |
| `brainstorm` | `brainstorm`, `collaborative`, `ideation session`, `group ideation` |
| `feature-dev` | `feature`, `build`, `develop`, `implement`, `coding`, `ci`, `review`, `merge`, `pull request` |
| `deployment-verify` | `verify deploy`, `post-deploy`, `deployment check`, `smoke test` |
| `deployment-revert` | `revert`, `rollback`, `undo deploy`, `deploy failure`, `auto-rollback` |
| `error-response` | `sentry`, `error alert`, `incident`, `triage`, `fatal error`, `bug`, `crash` |
| `new-agent-onboarding` | `onboard`, `new agent`, `commission agent`, `agent setup`, `create agent` |
| `report-back` | `report back`, `deliver results`, `summary report`, `deliver summary` |

**Matching algorithm:**
1. Lowercase both the trigger keywords and the user message.
2. For each workflow, count how many of its trigger keywords appear as substrings.
3. Return the workflow with the most hits. If no workflow matches, return None.

### Using `workflow_list()`

`workflow_list()` returns all registered workflows with full metadata:
- `name`: workflow identifier (pass to `workflow_start` or `workflow_dynamic_start`)
- `description`: human-readable "Use when ..." string
- `trigger`: pipe-separated keywords for matching
- `mode`: `predefined` (fleet pipeline) or `dynamic` (user template)
- `category`: `fleet`, `dynamic`, or `project-specific`
- `path`: filesystem path to the YAML definition

**Filter by trigger:** `workflow_list(trigger="deploy rollback")` returns only workflows whose triggers match.

### Auto-Discovery Flow

1. User sends a message.
2. Agent calls `match_workflow_trigger(user_message)` or `workflow_list(trigger=user_message)`.
3. If a match is found, agent offers: *"I found a matching workflow: `{name}` — {description}. Want me to run it?"*
4. User confirms → agent calls `workflow_start(workflow="{name}", context={...})`.

## Fleet Pipelines

### Pipeline Catalog

| Pipeline | Purpose | Nodes | Layers | Key context keys |
|----------|---------|-------|--------|------------------|
| `council` | Structured multi-agent debate | 13 | 8 | `{question}` |
| `ideation` | Research → spec → security → decomposition | 14 | 12 | project context |
| `brainstorm` | Collaborative multi-agent ideation | 14 | 6 | topic, goals |
| `feature-dev` | Build → CI → review → merge → post-merge | 10 | 5 | `{pr_link}` |
| `deployment-verify` | Post-deploy adversarial probe | 4 | 3 | `{env}`, `{project}` |
| `deployment-revert` | Auto-rollback on deploy failure | 4 | 3 | `{env}`, `{project}` |
| `error-response` | Sentry alert triage and dispatch | 5 | 4 | `{project}`, `{env}` |
| `new-agent-onboarding` | 7-phase DAG for commissioning a new agent profile | multi | 7 | agent name, role |

### Intent → Pipeline Mapping

| User intent | Pipeline |
|-------------|----------|
| "run a council on X" | `council` |
| "start ideation for Y" | `ideation` |
| "brainstorm this topic" | `brainstorm` |
| "build this feature" | `feature-dev` |
| "verify the deployment" | `deployment-verify` |
| "revert the deployment" | `deployment-revert` |
| "respond to this error" | `error-response` |
| "onboard a new agent" | `new-agent-onboarding` |
| "what pipelines exist" | `workflow_list()` |

### Pipeline-Specific Notes

**Council (`council`)**
- Requires: `question` in context (the question for the council to deliberate on)
- Board: `council` (declared in YAML)
- Duration: ~15-20 minutes (13 nodes, 8 layers)
- Output: artifact at `docs/fleet-research-kb/council/<date>/<run_id>/artifact.md`
- Delivery: delivery node produces voice summary

**Ideation (`ideation`)**
- Requires: project context (description, goals, constraints)
- Board: auto-created `wf_ideation`
- Duration: ~10-15 minutes
- Output: spec document at project spec directory

**Feature-dev (`feature-dev`)**
- Requires: `pr_link` or feature description in inputs
- Board: auto-created `wf_feature-dev`
- Duration: ~5-10 minutes
- Output: implemented code + PR

**New Agent Onboarding (`new-agent-onboarding`)**
- 7-phase DAG: intent capture → 3-agent parallel evaluation → human gate → profile creation + SOUL/AGENTS authoring + model chain → resource provisioning → domain education → operational confirmation
- **This is the canonical workflow for adding a new agent to the fleet** — don't hand-roll a setup script, fire this.

### Council Pipeline DAG

```
L0: premortem-researcher           (Researcher: failure imagination — PRIVATE)
L1: council-ready                  (synthetic gate — premortem privacy)
L2: position-strategist/implementer/researcher  (parallel: architecture/execution/skepticism)
L3: probe-orchestrator/tester      (parallel: systemic cost / boundary analysis)
L4: reflect-strategist/implementer/researcher   (parallel: concede, update positions)
L5: assumption-map-strategist      (solo: map assumptions per position)
L6: synthesize-researcher          (solo: confidence dispersion, artifact, git push)
L7: orchestrator-deliver           (solo: TTS voice summary via DM)
```

## Fleet-Specific Patterns

### Human Validation Gate (Todo Card Pattern)

For workflows that need human approval between phases (e.g. ideation → execution), use `todo` cards as approval gates. **The dispatcher only picks up `ready` cards.** Cards in `todo` sit until manually promoted.

**How It Works**

1. Ideation node finishes → creates a `todo` card with the research document attached
2. Card sits in `todo` — orchestrator watchdog ignores it
3. Human reviews the document at their pace
4. Human approves by moving card to `ready` (or adding an "approved" comment)
5. Next node picks it up automatically via `recompute_ready()`

**Implementation**

```yaml
approval-gate:
  description: "Human reviews ideation output before execution"
  agent: orchestrator
  task: >
    Review the ideation output. Create a todo card with the research document
    attached. Wait for human approval before proceeding.
  outputs:
    - approved_document
```

The engine creates this card as `todo` (not `ready`). The `recompute_ready()` function only promotes `todo` → `ready` when ALL parent dependencies are met. Since this card has no parents (it IS the gate), it stays in `todo` until the human acts.

**Re-ideate Loop**

When the human is not satisfied with the ideation output:
1. Human tells the orchestrator: "re-ideate" / "researcher missed X" / "this doesn't match what I meant"
2. Orchestrator dispatches researcher to produce new artifacts
3. New `todo` card is created with updated documents
4. Human reviews again
5. When satisfied → move to `ready` → execution starts automatically

**Key:** the loop is human-driven. The engine doesn't loop — it creates a new `todo` card each time. The human controls iteration count and exit condition.

**Card Ownership:** Assign the approval card to the agent who acts on it, not the orchestrator. The system cannot assign cards to humans — only to agents. The human triggers by manually moving the card from `todo` to `ready`.

**Approval Mechanisms:**
- **Kanban CLI:** `hermes kanban update <card-id> --status ready`
- **Comment trigger:** Human adds "approved" comment, engine watches for this

**Why This Works Without Schema Changes:** The kanban status flow is `triage → todo → ready → running → done`. `todo` = waiting (dispatcher ignores). `ready` = all deps met, dispatcher claims. No new statuses needed.

**Delivery Notifications:** When a human gate is reached, the pipeline should notify the originating channel BEFORE the todo card is created. Add a `notify-*` node between evaluation and the human gate.

### Workflow Ownership Model

**Workflows are org resources, not agent responsibilities.** Only the workflow operator needs to understand the full pipeline. Individual agents don't need to own, teach, or even know about workflows — they get dispatched via kanban, execute their part, and report back.

- **Workflows** = coordination infrastructure. Multi-agent pipelines with phases. The operator defines, runs, and maintains them.
- **Agents** = role-based workers. They receive dispatch context and acceptance criteria. They don't need to know the workflow exists.
- **Individual agents** run their own processes for role-based work. Those aren't workflows — they're just job execution.

**Fleet operator convention:** Sherlock operates workflows; agents don't need to know they exist.

### Fleet-Pipelines YAML Standard

All workflows in `fleet-pipelines/` should follow these conventions:

**Required fields per node:**
```yaml
node-name:
  agent: <agent>
  task: >
    Instruction text...
  timeout_minutes: 30
  fallback_on_timeout: degraded
  channel: orchestration
```

**`fallback_on_timeout` convention (MANDATORY on all nodes):**
- Use `degraded` on research/analysis nodes (timeout = partial results, continue)
- Use `skip` on setup/CI/monitoring nodes (timeout = not critical, skip)
- Omit only on critical path nodes where timeout = pipeline failure (merge, security gate)

**`outputs` convention (MANDATORY on terminal and referenced nodes):**
- Declare on terminal nodes (final reports, artifacts pushed to docs repo)
- Declare on nodes whose output is referenced by downstream nodes
- Format: `outputs: [raw-context]`
- Template variables in output paths: `{date}` and `{run_id}` resolve correctly. Use `docs/{project}/brainstorm/{date}/{run_id}/artifact.md`.

**Channel convention:**
- All nodes use `channel: orchestration` unless they need to post to a project channel
- The delivery node uses the originating channel for voice DM delivery

**Validation after changes:**
```bash
cd ~/.hermes/hermes-agent
.venv/bin/python3 -m tools.workflow_engine validate <workflow-name>
```

### Agent Selection Guide

| Agent | Use for |
|-------|---------|
| `orchestrator` | Setup, orchestration, delivery, git operations |
| `implementer` | Implementation, code changes, feature building |
| `strategist` | Design, spec writing, architecture decisions |
| `researcher` | Research, analysis, documentation |
| `tester` | Testing, QA, verification |
| `security` | Security review, compliance auditing |
| `writer` | Writing, content, copywriting |
| `tuner` | Behavioral tuning of agents — prompt patterns, performance measurement |
| `operator` | Operations, infrastructure maintenance |

## Fleet Recovery Patterns

### Stalled Pipeline Nodes

When `workflow_status` returns "no saved state" and the engine process is dead, the pipeline may have stalled at layer 0. Query the kanban board for the run's cards:

```bash
sqlite3 ~/.hermes/kanban/boards/fleet-workflow/kanban.db \
  "SELECT id, title, status, assignee, datetime(created_at,'unixepoch') as dt \
   FROM tasks ORDER BY created_at DESC LIMIT 5;"
```

Look for the layer-0 card (e.g. `premortem-researcher`) in `blocked` status.

### `workflow_start(node=...)` Creates Duplicate Runs

Each call to `workflow_start` with `node: "ci-green"` creates a NEW `run_id`, new kanban cards, and fresh state — it does NOT advance the existing workflow run.

**Correct pattern for advancing an existing run:**
1. Check if a workflow run is already in progress: `workflow_status()`
2. If yes, the run is monitoring kanban cards — CI green is detected by the engine's polling loop, NOT by calling `workflow_start` again
3. Only call `workflow_start(node=...)` when NO existing run covers this work
4. When you must re-dispatch, use `resume=True` to reuse saved state: `workflow_start(workflow="feature-dev", resume=True, node="ci-green")`

### Template Substitution Failure

Common blocked reason: `Template substitution failure: {question} was not resolved`. This means the context dict passed to `workflow_start` didn't carry through.

**Fix:** inject the variable and reset the card:
```sql
UPDATE tasks SET
  body = replace(body, '{question}', '<the full question text>'),
  status = 'ready',
  claim_lock = NULL,
  claim_expires = NULL,
  current_run_id = NULL,
  worker_pid = NULL
WHERE id = '<card-id>';
```

**Prevention:** when calling `workflow_start`, ensure ALL template variables referenced in the pipeline YAML are present in the context dict.

### Per-pipeline Kanban Board Routing

`hermes kanban create` has **no `--board` flag**. Board resolves via `HERMES_KANBAN_BOARD` env var. Three-part fix:

1. YAML: `kanban_board: council`
2. Engine: read in `load_workflow()`, override in `execute()`
3. Subprocess: inject `HERMES_KANBAN_BOARD` env var in `create_kanban_card`

**CRITICAL:** the polling side (`get_card_status`) must ALSO inject the env var.

### Loop Convention

When a pipeline node needs to iterate, use the convention: `"LOOP:<target> | <instruction>"` in the task body. The agent executing the node recognizes this prefix and loops until the target condition is met or `goal_max_turns` is exhausted.
