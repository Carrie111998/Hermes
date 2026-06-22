---
name: workflow-engine
description: 'Use when the user asks to run, invoke, start, or trigger a DAG-based pipeline or workflow. Covers YAML authoring (making workflows) and invocation (running the engine).'
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [workflow, pipeline, dag, orchestration]
    related_skills: [plan, systematic-debugging]
---

# Workflow Engine

## Overview

The workflow engine (`tools/workflow_engine.py` + the `workflow-engine` plugin) is a DAG-based pipeline runner. It resolves pipeline YAML definitions, performs topological sort, executes non-conflicting nodes in parallel, and advances layers automatically as nodes complete.

The engine resolves pipeline YAMLs from the first directory that exists:

1. `HERMES_WORKFLOWS` env var — explicit override
2. `$HERMES_HOME/workflows/` — profile-scoped default
3. Bundled defaults shipped with the agent

## When to Use

- User wants to run a multi-step pipeline (research → build → review → deliver)
- User wants to check pipeline status or validate a workflow definition
- User wants to orchestrate work across multiple agents with dependency ordering
- User wants to run a fire-and-forget workflow via `delegate_task(background=True)`

**Don't use for:** single one-off tool calls, workflows that need real-time user interaction (use `delegate_task` for async), or simple sequential tasks better handled by a single agent session.

## Making Workflows (YAML Authoring)

### YAML Structure

```yaml
name: my-workflow
description: "Multi-step pipeline"
version: "1.0.0"

defaults:
  goal_max_turns: 20
  max_retries: 1
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

### Node Fields

| Field | Required | Description |
|-------|----------|-------------|
| `agent` | Yes | Which agent executes this node (must have a Hermes profile) |
| `task` | Yes | Instruction body — supports `{upstream-node.output}` template variables |
| `depends_on` | No | List of node IDs that must complete before this node starts |
| `outputs` | No | Named outputs — available as `{node-id.output-name}` in downstream tasks |
| `timeout_minutes` | No | Max wall-clock runtime per node. Default: 10 min. |
| `timeout_seconds` | No | Wall-clock timeout per node (overrides `timeout_minutes`) |
| `max_retries` | No | Retry count on failure. Default: 0. |
| `fallback_on_timeout` | No | `skip` \| `degraded` \| `fail` (default). Controls behavior when a node times out. |
| `inputs` | No | List of upstream outputs this node consumes (e.g. `- setup.findings`) |
| `goal_max_turns` | No | Max agent turns for this node. Default: 20. |
| `channel` | No | Where status posts go |

### Node Types

- **Regular agent_task** — dispatched to an agent via kanban. The agent executes the task and reports back.
- **Synthetic** — auto-completed gate node. No agent executes it; the engine marks it done when its dependencies are met. Use for logical grouping or conditional branching.

### DAG Patterns

- **Linear chain:** A → B → C (each node depends on the previous)
- **Parallel layer:** Multiple nodes with the same `depends_on` run concurrently
- **Diamond branching:** Two nodes depend on the same prior node, then converge on a downstream node
- **Failure routing:** `fallback_on_timeout: skip` lets downstream nodes proceed; `degraded` passes partial results; `fail` (default) blocks the pipeline

### DAG Rules

- **No cycles** — a node cannot depend on itself or create circular dependencies
- **Layer assignment** — nodes with no dependencies start at Layer 0; each layer waits for the previous to complete
- **Parallel nodes** — nodes in the same layer run in parallel
- **Skip propagation** — if a dependency is skipped, downstream nodes are skipped too (unless the dependency was "blocked")
- **Blocked re-check** — after the monitoring loop, the engine re-checks blocked nodes. If their dependencies have unblocked, the blocked node is dispatched.

### Conditional Execution (`when:`)

Nodes can include a `when:` expression that gates execution:

```yaml
security-review:
  agent: security
  task: "Review the changes for security issues"
  depends_on:
    - build
  when: "{build.has_security_relevant_changes} == true"
```

- Operators: `==`, `!=`, `in`, `not in`, `matches` (regex)
- References: `{node-id.output-field}`, `{context.key}`, `{inputs.X}`
- Fail-closed: if the `when:` expression can't be evaluated (missing key), the node is skipped

### Template Substitution

The engine exposes upstream nodes' outputs as template variables for downstream nodes:

- **Namespaced (preferred):** `{phase1.researcher}`, `{setup.findings}`
- **Bare (legacy):** `{researcher}`, `{topic}` (context-first, then node-id)

Variables resolve from:
1. Engine-injected context: `{run_id}`, `{date}`, `{channel_id}`, `{channel_name}`, `{thread_id}`
2. Input parameters: `{inputs.pr_link}`, `{inputs.topic}`
3. Upstream node outputs: `{researcher.findings}`, `{setup.raw-context}`
4. Namespace labels: `{phase1}` (for grouping display, not substitution)

### Per-node `goal_max_turns`

The engine only passes `--goal-max-turns` when the node's YAML defines `goal_max_turns: <N>`. When unset, the CLI defaults to **20 turns**.

```yaml
deep-research:
  agent: researcher
  timeout_minutes: 5
  goal_max_turns: 30  # needs deeper context gathering
```

### Naming Conventions

- **Filenames**: lowercase-hyphenated, `.yaml` extension
- **Node IDs**: lowercase-hyphenated (`deep-research`, `security-review`)
- **Agent names**: must match a registered Hermes agent profile
- **Board names**: 1-64 chars, lowercase alphanumerics/hyphens/underscores. No leading `-` or `_`. Auto-created boards get `wf_` prefix.

### Testing New Workflows

1. `workflow_validate(workflow="my-workflow")` — catches structural issues
2. `workflow_start(workflow="my-workflow", dry_run=True)` — shows execution plan without creating cards
3. `workflow_start(workflow="my-workflow")` — live run with real cards

## Validation Rules

`workflow_validate(workflow="name")` returns:

```python
{"valid": bool, "issues": [...], "layers": int, "nodes": int}
```

| Rule | What it catches | Severity | How to fix |
|------|----------------|----------|------------|
| **YAML load failed** | File not found, parse error, or invalid YAML structure | **Fatal** | Check filename matches `{name}.yaml` in the pipelines directory. Run `yaml lint` on the file. |
| **Unknown dependency** | Node's `depends_on` references a node ID that doesn't exist | **Fatal** | Fix the node ID in `depends_on` to match an actual node key, or remove the reference. |
| **Cycle detected** | DAG has a circular dependency (e.g. A → B → A) | **Fatal** | Break the cycle. Add a new node or rewire `depends_on` so the graph is acyclic. |
| **Unknown agent** | Node's `agent:` field references a profile that doesn't exist | Warning | Register the agent profile or change the node's `agent:` to a known agent. Synthetic gate nodes are exempt. |
| **`incomplete_branch`** | Node has downstream consumers but no explicit `fallback_on_timeout` | Warning | Add `fallback_on_timeout: skip` or `fallback_on_timeout: degraded` to make failure routing intentional. |
| **`when:` references non-dependency** | Node's `when:` expression references `{node-id.field}` but that node is not in `depends_on` | Warning | Add the referenced node to `depends_on`, or use a `{context.*}` variable instead. |

**Fatal** issues block execution. **Warning** issues are surfaced but do not block execution.

## Running the Engine (Invocation)

### Quick Reference

| Action | Command |
|--------|---------|
| List available pipelines | `workflow_list()` |
| Show pipeline structure | `workflow_show(workflow="name")` |
| Validate before running | `workflow_validate(workflow="name")` |
| Start a pipeline | `workflow_start(workflow="name", context={...})` |
| Check running status | `workflow_status(workflow="name")` |

### Invocation Pattern

1. **Validate first** — always run `workflow_validate` before invoking a new or edited pipeline
2. **Detect input requirements** — use `workflow_show` to check for `{placeholder}` variables in node task bodies
3. **Start with context and inputs**:
   ```python
   workflow_start(
       workflow="my-workflow",
       context={"topic": "Should we adopt X?"},
       inputs={"detail_level": "deep"}
   )
   ```
4. **Dry-run mode** — `workflow_start(workflow="name", dry_run=True)` shows the execution plan without creating cards
5. **Resume from saved state** — `workflow_start(workflow="name", resume=True, node="specific-node")` reuses saved state

### Input Parameters vs Context

- **`context`** — merged into the template lookup as top-level keys. Use for pipeline-level values like `{topic}`, `{project}`, `{date}`.
- **`inputs`** — merged into context under `inputs.*` namespace. Available as `{inputs.<key>}` in templates. Use for workflow-specific parameters.

### Board Resolution

The engine uses three-tier board resolution:

1. **YAML `kanban_board` field** — if the pipeline declares a board, it wins
2. **Caller `board` parameter** — override at invocation time
3. **Auto-create** — if neither is set, creates `wf_<workflow-name>` board

### Fire-and-Forget Orchestration

`workflow_start` runs **synchronously** — it blocks until the pipeline completes. For async execution, wrap in `delegate_task(background=True)`:

```python
delegate_task(
    goal="Start workflow X with these inputs. Monitor it via workflow_status. "
         "When it finishes, return the final deliverable as your result.",
    background=True
)
```

The delegate spawns a subagent that calls `workflow_start` synchronously. When the subagent completes, its result re-enters the caller's session.

### Status and Monitoring

- `workflow_status(workflow="name")` returns current state: which nodes are done, running, skipped, or failed
- State files are per-run, kept for a retention period
- **Do NOT poll automatically** — the engine handles card lifecycle. Only check when asked.

### Single-Flight Opt-In

The engine supports single-flight enforcement: if a workflow is already running, subsequent `workflow_start` calls are rejected. Enable by setting `single_flight: true` in the workflow YAML.

## Common Pitfalls

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Card blocked, `consecutive_failures=2` | Agent ran out of goal-mode turns before completing | Ensure `goal_max_turns` is high enough; unblock card |
| Cards land on wrong kanban board | `kanban_board` not declared in YAML | Add `kanban_board: <slug>` to YAML |
| Nodes stuck "running" but agents completed | Engine polls wrong board | Verify card completion on the correct board, update state file, restart engine |
| Engine process dies mid-workflow, cards orphaned | Engine subprocess exits before polling detects completion | Kill zombie state, manually update state file, restart engine |
| Dependent card swept before parent completes | Card created with `parents` but `initial_status` not set to `blocked` | Use `initial_status='blocked'` + `unblock_task()` for dependent cards |
| Pipeline skips all nodes after one timeout | `degraded` status in engine's blocking list | Remove `degraded` from the blocking check in `workflow_engine.py` |
| Card auto-blocked with `heartbeat stale` | Newly created cards have NULL `last_heartbeat_at` | After `create_kanban_card()`, call `heartbeat_worker()` to set initial heartbeat |
| Template substitution failure | Context dict missing a key referenced in YAML | Ensure ALL template variables in YAML are present in the context dict |
| Gateway restart kills running pipeline | Engine runs synchronously inside agent session | Use `delegate_task(background=True)` to isolate from gateway lifecycle |

## Verification Checklist

- [ ] Pipeline name exists: `workflow_list()` shows it
- [ ] Pipeline validates: `workflow_validate(workflow="name")` returns `valid: true`
- [ ] All required inputs provided (check `workflow_show` for placeholder variables)
- [ ] All referenced context keys present in the context dict
- [ ] All `when:` references are in `depends_on`
- [ ] All nodes declare `fallback_on_timeout` or are terminal
- [ ] `workflow_start` returns `ok: true` with a `run_id`
- [ ] Status checkable via `workflow_status(workflow="name")`
