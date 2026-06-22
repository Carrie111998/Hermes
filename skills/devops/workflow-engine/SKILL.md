---
name: workflow-engine
description: 'Use when the user asks to run, invoke, start, or trigger a pipeline or workflow (e.g. council, ideation, feature-dev). Covers YAML authoring (making workflows) and invocation (running the engine).'
version: 1.0.0
author: Sherlock (fleet orchestrator); Hopper (fleet tuner, initial design)
license: MIT
metadata:
  hermes:
    tags:
    - workflow
    - pipeline
    - dag
    - kanban
    - orchestration
    - fleet
    related_skills:
    - kanban-orchestrator
    - fleet-pipeline-governance
    - dispatch-followup
    - systematic-debugging
---

# Workflow Engine

## Overview

The workflow engine (`tools/workflow_engine.py` + the `workflow-engine` plugin) is the fleet's pipeline runner. It replaces manual Kanban dispatching with a **mechanical DAG** — topological sort, parallel execution of non-conflicting nodes, and automatic layer advancement as cards complete.

The engine resolves pipeline YAMLs from the first directory that exists:

1. `HERMES_FLEET_PIPELINES` env var — explicit override (used for docs-repo path)
2. `$HERMES_HOME/workflows/` — profile-scoped default
3. `hermes-agent/docs/fleet-pipelines/` — shipped defaults

## When to Use

**Rule of thumb for strategic/fleet-level questions:** if the question involves multiple stakeholders, trade-offs, risk assessment, or competing priorities — use a pipeline. Do NOT write a solo analysis.

**Council vs Brainstorm — which pipeline?**

| Situation | Pipeline | Why |
|---|---|---|
| Adversarial debate needed (tough trade-offs, competing priorities) | **Council** | Agents challenge each other's positions |
| Collaborative assessment (fleet audit, strategy, role mapping) | **Brainstorm** | Agents build on each other's ideas, cooperative framing |
| "Should we build X or Y?" | **Council** | Disagreement is the point |
| "What's the risk of Z?" | **Council** | Multiple risk lenses |
| "How do I deploy this?" | Solo answer (single-domain) | No pipeline needed |

**Exception:** if the question is purely technical (how-to, config, debug a specific error), solo analysis is correct. Pipelines are for questions where reasonable agents can disagree or where multiple perspectives produce better decisions.

### Council Limitations

The council pipeline has known failure modes:

- **Parallel execution fragility.** The council's parallel phases (position, reflect, probe) are the most failure-prone. Cards spawned before dependencies complete, timeout cascades through skip chains, stale heartbeats on pending cards.
- **Debate format vs structured deliverable.** The council produces debate artifacts (shared concerns, genuine disagreement, confidence dispersion) — NOT structured reports with overlap maps, gap matrices, or recommendation tables.

**When to NOT use the council:**
- The user wants a specific document format (overlap map, gap analysis, recommendations table)
- The question requires reading all agent files and producing a comprehensive audit
- The user explicitly says the council isn't the right shape
- The topic is too broad for a single council question — break it into smaller, focused questions

---

## Part 1: Making Workflows (YAML Authoring)

### YAML Structure

Every pipeline lives in `docs/fleet-pipelines/` (or `HERMES_FLEET_PIPELINES` dir). Filename = pipeline name: `council.yaml` → `workflow_start(workflow="council")`.

```yaml
name: council                    # Display name (shown in workflow_list)
description: "Multi-agent council for strategic decisions"
version: "1.0.0"

defaults:
  goal_max_turns: 20             # Per-node timeout (agent turns)
  max_retries: 1                 # Retry count on node failure
  timeout_seconds: 3600          # Wall-clock timeout per node

kanban_board: council            # Optional. Auto-creates wf_council if absent.

inputs:
  - name: question               # Required input
    required: true
    description: "The question the council should deliberate on"
  - name: channel_id             # Optional input (also auto-injected by engine)
    required: false
    description: "Override delivery channel"

nodes:
  # ── Layer 0: Setup ──
  orchestrator-setup:
    description: "Capture raw inputs and set up context"
    agent: orchestrator
    task: >
      You have access to the project's Discord channel, voice transcripts, and
      research notes. Write ALL raw inputs verbatim into
      docs/fleet-research-kb/council/{date}/{run_id}/raw-context.md.
      Every subsequent node reads from {raw-context}.
    outputs:
      - raw-context
    max_retries: 1

  # ── Layer 1: Positions ──
  position-strategist:
    description: "Strategic analysis"
    agent: strategist
    task: >
      Read {raw-context}. Analyze the question: "{question}".
      Provide a strategic assessment with thesis, evidence, and confidence level.
    inputs:
      - orchestrator-setup.raw-context
    outputs:
      - assessment
    timeout_seconds: 1800
    fallback_on_timeout: degraded

  position-researcher:
    description: "Research and documentation"
    agent: researcher
    task: >
      Read {raw-context}. Research "{question}" thoroughly.
      Document findings with citations and evidence chains.
    inputs:
      - orchestrator-setup.raw-context
    outputs:
      - findings

  # ── Layer 2: Probes ──
  probe-orchestrator:
    description: "Critical challenge"
    agent: orchestrator
    task: >
      Review the position assessments. Identify assumptions, blind spots,
      and weaknesses. Challenge each position with specific evidence.
    inputs:
      - position-strategist.assessment
      - position-researcher.findings

  # ── Layer 3: Reflection ──
  reflect-synthesizer:
    description: "Synthesis and reflection"
    agent: synthesizer
    task: >
      Given all positions and challenges, reflect on the strongest arguments.
      Identify areas of agreement and unresolved disagreement.
    inputs:
      - probe-orchestrator.challenges
      - position-strategist.assessment
      - position-researcher.findings

  # ── Layer 4: Synthesis ──
  synthesize-researcher:
    description: "Final synthesis"
    agent: researcher
    task: >
      Synthesize all analyses into a single coherent recommendation.
      Write to docs/fleet-research-kb/council/{date}/{run_id}/artifact.md.
    inputs:
      - reflect-synthesizer.synthesis
      - position-strategist.assessment
      - position-researcher.findings
    outputs:
      - artifact_path

  # ── Layer 5: Delivery ──
  orchestrator-deliver:
    description: "Report back to the human"
    agent: orchestrator
    task: >
      Report the council's finding to the user. Keep it under 200 words.
      If delivery channel is {channel_id}, post there. Otherwise post to the originating channel.
    inputs:
      - synthesize-researcher.artifact_path
```

### Node Fields

| Field | Required | Description |
|-------|----------|-------------|
| `agent` | Yes | Which agent executes this node (must have a Hermes profile) |
| `task` | Yes | Instruction body — supports `{upstream-node.output}` template variables |
| `depends_on` | No | List of node IDs that must complete before this node starts |
| `outputs` | No | Named outputs — available as `{node-id.output-name}` in downstream tasks |
| `timeout_minutes` | No | Max runtime per worker. Default: 10 min. |
| `timeout_seconds` | No | Wall-clock timeout per node (overrides `timeout_minutes`) |
| `max_retries` | No | Retry count on failure. Default: 0. |
| `fallback_on_timeout` | No | `skip` \| `degraded` \| `fail` (default). Controls behavior when a node times out. |
| `inputs` | No | List of upstream outputs this node consumes (e.g. `- position-strategist.assessment`) |
| `channel` | No | Where status posts go (e.g. `orchestration`) |

### DAG Rules

- **No cycles** — a node cannot depend on itself or create circular dependencies
- **Layer assignment** — nodes with no dependencies start at Layer 0; each layer waits for the previous to complete
- **Parallel nodes** — nodes in the same layer run in parallel (separate kanban cards)
- **Skip propagation** — if a dependency is skipped, downstream nodes are skipped too (unless the dependency was "blocked" — blocked nodes don't cascade)
- **Blocked re-check** — after the monitoring loop, the engine re-checks blocked nodes. If their dependencies have unblocked (completed or degraded), the blocked node is dispatched.

### Template Substitution

The engine exposes upstream nodes' captured card bodies as template variables for downstream nodes. Two forms:

- **Namespaced (preferred):** `{phase1.position-strategist}`, `{phase2a.all}`
- **Bare (legacy):** `{position-strategist}`, `{question}` (context-first, then node-id)

Variables resolve from:
1. Engine-injected context: `{run_id}`, `{date}`, `{channel_id}`, `{channel_name}`, `{thread_id}`
2. Input parameters: `{inputs.pr_link}`, `{inputs.question}`
3. Upstream node outputs: `{position-strategist.assessment}`, `{orchestrator-setup.raw-context}`
4. Namespace labels: `{phase1}` (for grouping display, not substitution)

### Per-node `goal_max_turns`

The engine only passes `--goal-max-turns` when the node's YAML defines `goal_max_turns: <N>`. When unset, the CLI defaults to **20 turns** — plenty for research-heavy tasks.

Add to a node when you know it needs deeper context gathering:

```yaml
position-strategist:
  agent: strategist
  timeout_minutes: 5
  goal_max_turns: 20  # needs to read 5 YAMLs + session history
```

Do NOT set `goal_max_turns` to the CLI default (20) — the point is to only customize when you need more or less than the default. Unset = use default.

### Fleet-Pipelines YAML Standard

All workflows in `fleet-pipelines/` should follow these conventions:

**Required fields per node:**
```yaml
node-name:
  agent: <agent>                    # Required
  task: >                           # Required — supports {template} vars
    Instruction text...
  timeout_minutes: 30               # Required — max wall-clock minutes
  fallback_on_timeout: degraded     # Recommended — 'skip' | 'degraded' | 'retry'
  channel: orchestration            # Recommended — where status posts go
```

**`fallback_on_timeout` convention (MANDATORY on all nodes):**
- Use `degraded` on research/analysis nodes (timeout = partial results, continue)
- Use `skip` on setup/CI/monitoring nodes (timeout = not critical, skip)
- Omit only on critical path nodes where timeout = pipeline failure (merge, security gate)
- After adding/moving/consolidating workflows, validate: `workflow_validate(name)` for each.

**`outputs` convention (MANDATORY on terminal and referenced nodes):**
- Declare on terminal nodes (final reports, artifacts pushed to docs repo)
- Declare on nodes whose output is referenced by downstream nodes
- Format: `outputs: [raw-context]` (just a path string)
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

### Naming Conventions

- **Filenames**: lowercase-hyphenated, `.yaml` extension (`council.yaml`, `pull-request-review.yaml`)
- **Node IDs**: lowercase-hyphenated (`position-strategist`, `probe-orchestrator`, `synthesize-researcher`)
- **Agent names**: must match a registered Hermes agent profile
- **Board names**: 1-64 chars, lowercase alphanumerics/hyphens/underscores. No leading `-` or `_`. Auto-created boards get `wf_` prefix.

### Testing New Workflows

1. `workflow_validate(workflow="my-workflow")` — catches structural issues
2. `workflow_start(workflow="my-workflow", dry_run=True)` — shows execution plan without creating cards
3. `workflow_start(workflow="my-workflow")` — live run with real cards

---

## Validation Rules

`workflow_validate(workflow="name")` returns:

```python
{"valid": bool, "issues": [...], "layers": int, "nodes": int}
```

- **`valid`** — `False` if any **fatal** issues exist. Warnings do not flip `valid`.
- **`issues`** — list of strings describing each problem found.
- **`layers`** / **`nodes`** — structural summary (only populated when YAML loads and DAG sorts cleanly).

### Rule Reference

| Rule | What it catches | Severity | How to fix |
|------|----------------|----------|------------|
| **YAML load failed** | File not found, parse error, or invalid YAML structure | **Fatal** | Check filename matches `{name}.yaml` in the pipelines directory. Run `yaml lint` on the file. |
| **Unknown dependency** | Node's `depends_on` references a node ID that doesn't exist in the workflow | **Fatal** | Fix the node ID in `depends_on` to match an actual node key, or remove the reference. |
| **Cycle detected** | DAG has a circular dependency (e.g. A → B → A) | **Fatal** | Break the cycle. Add a new node or rewire `depends_on` so the graph is acyclic. |
| **Unknown agent** | Node's `agent:` field references a profile that doesn't exist under `~/.hermes/profiles/` | Warning | Register the agent profile or change the node's `agent:` to a known agent. Synthetic gate nodes are exempt. |
| **Revision without gate** | A node named `revise-*` doesn't depend on a gate node (verify/security/review) | Warning | Add the gate node to `depends_on` on the revision node, or rename the node if it isn't a revision loop. |
| **Orphaned gate** | A gate node is referenced by a revision node's `depends_on` but has no dependents of its own | Warning | Ensure the gate node has at least one downstream consumer, or remove the revision reference. |
| **`incomplete_branch`** | Node has downstream consumers but no explicit `fallback_on_timeout` in YAML | Warning | Add `fallback_on_timeout: skip` or `fallback_on_timeout: degraded` to make failure routing intentional. |
| **`when:` references non-dependency** | Node's `when:` expression references `{node-id.field}` but that node is not in `depends_on` | Warning | Add the referenced node to `depends_on`, or use a `{context.*}` variable instead. |

### Fatal vs Warning

- **Fatal** issues (YAML load, unknown dep, cycle) set `valid: False` and **block execution**. The engine refuses to start.
- **Warning** issues (unknown agent, incomplete_branch, when: refs, revision/gate mismatches) are surfaced in the `issues` array but do **not** block execution. They flag likely mistakes that won't crash the engine but may cause surprising behavior (silent skip cascades, stale when: evaluations, missing agent profiles).

### Example Output

```python
# Fatal: cycle detected
{"valid": False, "issues": [
    "Cycle detected: position-strategist -> probe-orchestrator -> position-strategist"
], "layers": 0, "nodes": 5}

# Warnings only (workflow still runs)
{"valid": True, "issues": [
    "Node 'position-strategist' has downstream consumers but no explicit fallback_on_timeout in YAML. Add one of: skip | degraded | retry to make failure routing intentional, not implicit.",
    "Node 'probe-orchestrator' has when: referencing 'position-strategist' but does not declare it in depends_on. Add 'position-strategist' to depends_on or use a context variable instead."
], "layers": 4, "nodes": 8}
```

### CLI Invocation

```bash
cd ~/.hermes/hermes-agent
.venv/bin/python3 -m tools.workflow_engine validate <workflow-name>
```

---

## Part 2: Running the Engine (Invocation)

### Quick Reference

| Action | Command |
|--------|---------|
| List available pipelines | `workflow_list()` |
| Show pipeline structure | `workflow_show(workflow="council")` |
| Validate before running | `workflow_validate(workflow="council")` |
| Start a pipeline | `workflow_start(workflow="council", context={"question": "..."})` |
| Check running status | `workflow_status(workflow="council")` |

### Step 1: Identify the Pipeline

If the user names a pipeline, use it. If they describe what they want, map to the closest pipeline:

| User intent | Pipeline |
|-------------|----------|
| "run a council on X" | `council` |
| "start ideation for Y" | `ideation` |
| "build this feature" | `feature-dev` |
| "what pipelines exist" | `workflow_list()` |

### Step 2: Detect Input Requirements

Use `workflow_show(workflow="name")` to check the pipeline's structure. Then determine:

**Pre-populated workflows** — all template variables are resolved from upstream node outputs. No user inputs needed. Just invoke:
```
workflow_start(workflow="council", context={"question": "..."})
```

**Input-required workflows** — have `{placeholder}` variables in node task bodies that aren't filled by upstream outputs. These need `inputs`:
```
workflow_start(
    workflow="pull-request-review",
    inputs={"pr_link": "https://github.com/org/repo/pull/42"}
)
```

**How to tell the difference:** Run `workflow_show`. Check each node's `task` field for `{...}` variables. Variables that match upstream node IDs (e.g. `{phase1.position-strategist}`) are auto-resolved. Variables that don't match any node (e.g. `{inputs.pr_link}`, `{question}`) need to be supplied via `context` or `inputs`.

### Step 3: Validate (Recommended)

Always validate before invoking a new or recently edited pipeline:
```
workflow_validate(workflow="council")
```
Returns `{valid: bool, nodes: int, layers: int, issues: [...]}`. Fix any issues before starting.

### Step 4: Invoke

```
workflow_start(
    workflow="council",
    context={"question": "Should we ship X or defer?"},
    inputs={"topic": "council debate on X"}
)
```

**Response:** The tool returns immediately with `{ok: true, run_id: "...", status: "started"}`. The engine runs synchronously within the agent session — it creates kanban cards, monitors completion, and advances the DAG layer by layer.

**Tell the user:** "Pipeline started — run `council-20260611-143000`. Cards are being created. I'll check when you ask, or you can ask me for a status update."

### Step 5: Monitor (Optional)

The engine runs autonomously. Check status when the user asks:
```
workflow_status(workflow="council")
```

Returns current state: which nodes are done, running, skipped, or failed.

**Do NOT poll automatically.** The engine handles card lifecycle. Only check when asked.

### Input Parameters vs Context

- **`context`** — merged into the template lookup as top-level keys. Use for pipeline-level values like `{question}`, `{project}`, `{date}`.
- **`inputs`** — merged into context under `inputs.*` namespace. Available as `{inputs.<key>}` in templates. Use for workflow-specific parameters.

Both are available in all node task bodies via template substitution.

### Board Resolution

The engine uses three-tier board resolution:

1. **YAML `kanban_board` field** — if the pipeline declares a board, it wins
2. **Caller `board` parameter** — override at invocation time
3. **Auto-create** — if neither is set, creates `wf_<workflow-name>` board

For fleet pipelines (council, ideation, feature-dev), the YAML typically declares `kanban_board`. You don't need to set `board` unless overriding.

### Available Pipelines

- `council` — structured multi-agent debate (13 nodes, 8 layers)
- `ideation` — research → spec → security → decomposition (14 nodes, 12 layers, includes human-approval gate)
- `brainstorm` — collaborative multi-agent ideation (14 nodes, 6 layers, cooperative framing vs council's adversarial)
- `feature-dev` — build → CI → review → merge → post-merge (10 nodes, 5 layers)
- `deployment-verify` — post-deploy adversarial probe (4 nodes, 3 layers, context: `{env}`, `{project}`)
- `deployment-revert` — auto-rollback on deploy failure (4 nodes, 3 layers, context: `{env}`, `{project}`)
- `error-response` — Sentry alert triage and dispatch (5 nodes, 4 layers, context: `{project}`, `{env}`)
- `new-agent-onboarding` — 7-phase DAG for commissioning a new agent profile (intent capture → 3-agent parallel evaluation → human gate → profile creation + SOUL/AGENTS authoring + model chain → resource provisioning → domain education → operational confirmation). **This is the canonical workflow for adding a new agent to the fleet** — don't hand-roll a setup script, fire this.

### Pipeline-Specific Notes

**Council (`council`)**
- Requires: `question` in context (the question for the council to deliberate on)
- Board: `council` (declared in YAML)
- Duration: ~15-20 minutes (13 nodes, 8 layers)
- Output: artifact at `docs/fleet-research-kb/council/<date>/<run_id>/artifact.md`
- Delivery: delivery node produces voice summary

**Ideation (`ideation`)**
- Requires: project context (description, goals, constraints)
- Board: auto-created `wf_ideation` (unless YAML declares one)
- Duration: ~10-15 minutes
- Output: spec document at project spec directory

**Feature-dev (`feature-dev`)**
- Requires: `pr_link` or feature description in inputs
- Board: auto-created `wf_feature-dev`
- Duration: ~5-10 minutes
- Output: implemented code + PR

---

## Fire-and-Forget Orchestration via `delegate_task(background=True)`

The workflow engine runs **synchronously** within the agent session — `workflow_start` blocks until the pipeline completes. To achieve fire-and-forget behavior, wrap the invocation in `delegate_task(background=True)`:

```python
# In any agent's session (DM, channel, anywhere)
delegate_task(
    goal="Start workflow X with these inputs. Monitor it via workflow_status. "
         "When it finishes, return the final deliverable as your result.",
    background=True
)
# Caller is free immediately. Delegate runs in background.
# When the workflow ends, the delegate's async-return drops the result
# into the caller's conversation as a new message.
```

**How it works:**
1. `delegate_task(background=True)` spawns a subagent (a copy of the caller)
2. The subagent calls `workflow_start(...)` synchronously — it blocks until the pipeline finishes
3. When the subagent completes, its result re-enters the caller's session natively via `process_registry.completion_queue`
4. The caller sees the result as a new message in their conversation

**Why this is the correct pattern:**
- The delegate has the caller's `send_message` access, can call `workflow_start`, can poll, can post to the originating channel
- Native async return is the bridge — no wrapper workflows, no kanban-card-as-watcher
- Inner workflow YAML is unchanged — the job is still a job

**Reliability tradeoff:** kanban cards had heartbeats and `fallback_on_timeout`; delegates can die silently. If the inner workflow is critical, the delegate should poll `workflow_status` periodically and have an internal timeout. This is a watcher's responsibility, not the engine's.

---

## Council Pipeline DAG

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

---

## Human Validation Gate (Todo Card Pattern)

For workflows that need human approval between phases (e.g. ideation → execution), use `todo` cards as approval gates. **The dispatcher only picks up `ready` cards.** Cards in `todo` sit until manually promoted.

### How It Works

1. Ideation node finishes → creates a `todo` card with the research document attached
2. Card sits in `todo` — orchestrator watchdog ignores it
3. Human reviews the document at their pace
4. Human approves by moving card to `ready` (or adding an "approved" comment)
5. Next node picks it up automatically via `recompute_ready()`

### Implementation

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

### Re-ideate Loop

When the human is not satisfied with the ideation output:
1. Human tells the orchestrator: "re-ideate" / "researcher missed X" / "this doesn't match what I meant"
2. Orchestrator dispatches researcher to produce new artifacts
3. New `todo` card is created with updated documents
4. Human reviews again
5. When satisfied → move to `ready` → execution starts automatically

**Key:** the loop is human-driven. The engine doesn't loop — it creates a new `todo` card each time. The human controls iteration count and exit condition.

### Card Ownership

**Assign the approval card to the agent who acts on it, not the orchestrator.** The system cannot assign cards to humans — only to agents. The human triggers by manually moving the card from `todo` to `ready`. The card should be assigned to the agent who will consume the approved output and proceed with execution.

### Approval Mechanisms

- **Kanban CLI:** `hermes kanban update <card-id> --status ready`
- **Comment trigger:** Human adds "approved" comment, engine watches for this

### Why This Works Without Schema Changes

The kanban status flow is: `triage → todo → ready → running → done`

- `todo` = waiting (dispatcher ignores)
- `ready` = all deps met, dispatcher claims
- `recompute_ready()` only promotes when ALL parents are done

No new statuses needed. The existing `todo` status IS the approval gate.

### Delivery Notifications

When a human gate is reached, the pipeline should notify the originating channel BEFORE the todo card is created. Add a `notify-*` node between evaluation and the human gate:

```yaml
notify-operator:
  agent: orchestrator
  task: >
    Notify the operator that the pipeline is waiting for approval.
    Send a Discord message to the originating channel with:
    "Pipeline '{pipeline_name}' — Human review required.
     Agent: {inputs.agent_name}
     Recommendation: [summary]
     Approve by moving the kanban card to ready."
  depends_on:
    - eval-node-1  # after evaluation, before gate
  fallback_on_timeout: skip  # notification failure shouldn't block pipeline
  outputs: []  # notification-only node
```

---

## Workflow Ownership Model

**Workflows are org resources, not agent responsibilities.** Only the workflow operator needs to understand the full pipeline. Individual agents don't need to own, teach, or even know about workflows — they get dispatched via kanban, execute their part, and report back.

- **Workflows** = coordination infrastructure. Multi-agent pipelines with phases. The operator defines, runs, and maintains them.
- **Agents** = role-based workers. They receive dispatch context and acceptance criteria. They don't need to know the workflow exists.
- **Individual agents** run their own processes for role-based work. Those aren't workflows — they're just job execution.

---

## Common Pitfalls

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

Common blocked reason: `Template substitution failure: {question} was not resolved`. This means the context dict passed to `workflow_start` didn't carry through — the question key was not in the card's embedded context.

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

### Common Pitfalls Table

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Card blocked, `consecutive_failures=2` | Agent ran out of goal-mode turns before calling `kanban_complete` | Ensure `goal_max_turns` is high enough; unblock card |
| Cards land on wrong kanban board | `kanban_board` not declared in YAML | Add `kanban_board: <slug>` to YAML + env var injection |
| `--board council` unrecognized arg | `hermes kanban create` has no `--board` flag | Use `HERMES_KANBAN_BOARD` env var instead |
| Nodes stuck "running" but agents completed | Cards created on `wf_<name>` board but engine polls `fleet-workflow` | Kill the stuck engine, verify card completion on the correct board, manually update the state file, restart engine |
| Engine process dies mid-workflow, cards orphaned | Engine subprocess exits after dispatching layer-0 cards but before polling detects completion | Kill zombie state, manually update state file, restart engine |
| Dependent card swept before parent completes | Card created with `parents` but `initial_status` not set to `blocked` | Use `initial_status='blocked'` + `unblock_task()` for dependent cards |
| Pipeline skips all nodes after one timeout | `degraded` status in engine's blocking list | Remove `degraded` from the blocking check in `workflow_engine.py` |
| Card auto-blocked with `heartbeat stale` | Newly created cards have NULL `last_heartbeat_at` and `started_at` | After `create_kanban_card()`, call `heartbeat_worker()` to set initial heartbeat |
| Approval card picked up prematurely | Card created as `ready` instead of `todo` | Ensure approval nodes create cards with `initial_status: todo` |
| Duplicate human-approval cards | Engine re-dispatches because `get_card_status` polls wrong board | Check for duplicates, archive extras, keep most recent |
| Workflow YAML in wrong directory | Stale nested copies | Consolidate all YAMLs into the single `fleet-pipelines/` directory. Validate all after moving. |
| Gateway restart kills running pipeline | Engine runs synchronously inside agent session | Use `delegate_task(background=True)` to isolate the pipeline from gateway lifecycle |

### Per-pipeline Kanban Board Routing

`hermes kanban create` has **no `--board` flag**. Board resolves via `HERMES_KANBAN_BOARD` env var. Three-part fix:

1. YAML: `kanban_board: council`
2. Engine: read in `load_workflow()`, override in `execute()`
3. Subprocess: inject `HERMES_KANBAN_BOARD` env var in `create_kanban_card`

**CRITICAL:** the polling side (`get_card_status`) must ALSO inject the env var.

---

## Verification Checklist

- [ ] Pipeline name exists: `workflow_list()` shows it
- [ ] Pipeline validates: `workflow_validate(workflow="name")` returns `valid: true` (see [Validation Rules](#validation-rules) for rule reference)
- [ ] All required inputs provided (check `workflow_show` for placeholder variables)
- [ ] `workflow_start` returns `ok: true` with a `run_id`
- [ ] Status checkable via `workflow_status(workflow="name")`
