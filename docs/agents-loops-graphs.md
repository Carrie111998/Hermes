# Agents, Loops, and Graphs — Playbook for This Stack

A mapping of the three orchestration patterns (autonomous agents, self-checking
loops, dependency-graph fan-outs) onto what this stack actually runs today, and
a prioritized list of what to build next. Written against the fork at
`dizhaky/hermes-agent`, the Claude Code environment, and the live fleet
(mfc1 gateway, memory-gateway, vault daemons, Linear-driven ops).

The short version: **all three patterns already exist here.** The work is not
building them — it is (1) using the right layer for the right job, (2) closing
four specific reliability gaps, and (3) adopting two conventions that make
loops and graphs trustworthy: real pass/fail checks and fresh-context
verifiers.

---

## 1. What already exists (do not rebuild)

### Agents — this stack is already at "Level 4"

| Capability | Where it lives |
|---|---|
| Tool-calling agent runtime | Hermes core (`run_agent.py`, `agent/`) and Claude Code |
| Cross-session memory | Memory providers (`plugins/memory/` — memgw, honcho, etc.), FTS5 session search (`tools/session_search_tool.py`), Obsidian vault, Linear |
| Learning loop | Background review (`agent/background_review.py`) auto-creates/patches skills; Curator (`agent/curator.py`) archives stale ones |
| Triggered/scheduled autonomy | `hermes cron`, webhook subscriptions, gateway delivery to Telegram/Slack/etc. |

The "paste a checkpoint prompt" advice for the memory problem is mostly
obsolete here. The durable-state conventions already in use are better:
Linear issues for work state, `docs/system-log/` for daily operational
history, and the vault for knowledge. **Convention to adopt:** any agent task
expected to outlive one session gets a Linear issue at start (the checkpoint)
and a system-log line at end. That is the checkpoint pattern, made durable
and searchable instead of pasted.

Similarly, "reusable system prompts" are skills in this stack. A recurring
agent role (researcher, data analyst, code agent) should be codified as a
Hermes skill or Claude Code skill — not a prompt kept in a note. Skills are
versioned, curator-managed, loadable per cron job (`--skill`), and
progressively disclosed instead of burning context.

### Loops — several already in production

- **Cron jobs** (`cron/`): three schedule shapes (`once`/`interval`/`cron`), per-job model pinning,
  script pre-injection, `no_agent` script-only watchdog mode, `context_from`
  chaining, model-policy guard for legal/finance workloads, prompt-injection
  scan of assembled prompts.
- **verify-on-stop** (`agent/verification_stop.py`): a *real* evidence
  ledger — exit-code-0 verification results are recorded in SQLite, and when
  the model tries to finish right after editing code without fresh passing
  evidence it gets a **bounded (≤2) policy-only nudge** to verify first. It's
  the closest thing here to the article's "check that can actually fail" —
  but note the honest limits: it's a nudge, not a hard completion block, it
  never runs the checks itself, and it's off on messaging surfaces and for
  doc-only edits.
- **CI auto-healer / auto-fix / escalation detector** (`.github/workflows/`):
  a production fix-until-green loop on this fork.
- **Fleet loops**: vault healer, env-guard watchdog, config-integrity
  watchdog, ugw-health-check — all existing loop-shaped automation.

### Graphs — Kanban is the DAG scheduler

The under-advertised finding: **Hermes already has a dependency-graph
scheduler.** The Kanban subsystem (`tools/kanban_tools.py`,
`hermes_cli/kanban_db.py`) supports:

- `parents: [...]` edges; a task stays in `todo` until every parent is `done`,
  then auto-promotes to `ready` (fan-in for free)
- cycle rejection via topological sort
- a dispatcher inside the gateway that spawns one OS subprocess per task,
  with `max_in_progress`, per-profile caps, per-task `max_runtime_seconds`,
  and auto-block after `failure_limit` consecutive failures
- per-task model/provider overrides plus per-task `skills` (cheap models for
  cheap nodes) — but **toolsets** are resolved from the assignee *profile*, not
  per task (there is no `--toolset` on `kanban create`)
- git-worktree workspaces for parallel code mutation without conflicts
- **`hermes kanban swarm`** — a prebuilt diamond: parallel workers → verifier
  → synthesizer, with a shared blackboard on the root task

That last item is exactly the article's fan-out/converge diamond *plus* its
fresh-context checker, as one command.

On the Claude Code side, the Workflow tool provides the same shape for
repo-scale work (parallel finders → adversarial verifiers → synthesis), and
subagents cover flat fan-out.

---

## 2. The decision matrix — which layer for which job

The stack has four orchestration layers that do not share a scheduler. Route
work by durability and shape:

| Job shape | Use | Why |
|---|---|---|
| Multi-angle work inside a chat, results needed this turn | `delegate_task` batch mode | Flat parallel fan-out, isolated contexts, summaries return as one message |
| Mechanical multi-step pipeline (loop over 40 files, retry-with-backoff, filter/aggregate) | `execute_code` (PTC) | Intermediate results never enter context; plain code instead of tokens — the article's "REDUCE — no model, no tokens" step |
| Durable DAG, overnight or unattended, mixed models, code mutation | Kanban (`hermes kanban swarm`, `kanban_create` with `parents`) | Survives restarts, real dependency edges, failure auto-block, worktree isolation |
| Scheduled monitoring/reporting | `hermes cron` — with `no_agent` + script whenever the check is mechanical | LLM-free watchdogs cost nothing and cannot hallucinate a pass |
| Repo-scale code review/migration/audit in Claude Code | Workflow tool / subagent fan-out | Worktree isolation, schema-validated agent outputs, adversarial verify built into the pattern |

Known seams to respect (verified in code, not guesses):

- `delegate_task` children have the kanban toolset **stripped** — a subagent
  cannot enqueue DAG work. Orchestration across the seam must be done by the
  parent or via `execute_code`/CLI.
- `delegate_task` has **no dependency edges** — it is flat fan-out that joins
  on all children. Anything with stage-2-needs-stage-1 structure belongs in
  Kanban or a PTC script.
- `execute_code` RPC calls are serialized (global lock) — no tool-call
  parallelism inside a script, and it cannot spawn subagents.
- Cron sessions **skip memory providers by design** (intrinsic — cron system
  prompts would corrupt user representations); they also get a hard interrupt,
  but only on the conditional timeout/shutdown path, not as an intrinsic
  property. Do not put memory-dependent reasoning in cron prompts.

---

## 3. The four gaps worth closing (prioritized)

### Gap 1 — Cron has no retry and no consecutive-failure auto-pause ⚠ highest leverage

`cron/executions.py` states it plainly: "not a retry queue." A failed run
delivers a one-line failure and waits for the next tick; nothing counts
consecutive failures; a job broken by a rotated credential fails quietly
forever. This is the root of the standing "Infrastructure Health & Alerts"
noise and the open cron-cleanup/health-check Linear projects.

**Built (this branch):** `mark_job_run` in `cron/jobs.py` now tracks
`consecutive_failures` per job; after N in a row (per-job `failure_limit` >
`HERMES_CRON_FAILURE_LIMIT` env > default 3, `0` disables) a recurring job
is auto-paused, and the scheduler folds the auto-pause notice into the final
failure delivery — one escalation instead of per-tick spam. `resume`,
`trigger`, and any success reset the streak; one-shots are exempt. Mirrors
Kanban's existing `failure_limit` semantics.

### Gap 2 — Cron outputs have no pass/fail gate

The `[SILENT]` sentinel is self-reported by the model; nothing validates that
a "weekly digest" contains a digest. Two mitigations, no new infrastructure:

- **Prefer `no_agent` script jobs for every check that is mechanical.**
  Script exit semantics are the gate. Reserve the LLM for jobs that need
  reasoning, fed by `--script` output.
- **For LLM jobs that matter, chain a checker**: a second cron job with
  `context_from` pointing at the producer, running a *different* (cheap)
  model, whose only task is "does this output meet the criteria — reply
  PASS or a one-line failure." The worker never grades its own homework.

### Gap 3 — verify-on-stop covers code only

The evidence gate keys on file mutations and detected verify commands, and
caps at 2 attempts. Research/writing/analysis turns have no equivalent. The
`pre_verify` hook surface (`agent/verify_hooks.py`) ships empty — it is the
designed extension point.

**Built (this branch):** a rubric verify-on-stop gate for non-code
deliverables. `agent/verify_hooks.py` gains `build_rubric_verify_nudge` — a
*mechanical* check (marker/regex presence per criterion, "a check that can
fail," not the model grading itself), and `agent/conversation_loop.py` gains a
stop-guard block (modeled on the existing kanban stop guard) that nudges the
agent to complete missing rubric elements before finishing. It is **off by
default**: a deployment activates it by naming one `agent.verify_rubrics` entry
in `agent.verify_rubric`. Coding turns and any turn that mutated files are never
gated — the code evidence gate above owns those. This mechanizes what
accounting workpapers already do culturally via the audit-QC skill. Per-turn
nudges are bounded (`max_nudges`, default 2) so the gate can never trap the loop.

### Gap 4 — Delegation defaults are conservative for this hardware

`delegation.max_concurrent_children` defaults to 3 and
`max_spawn_depth` to 1, config-only. For mfc1-class hardware, set in
`~/.hermes/config.yaml`:

```yaml
delegation:
  max_concurrent_children: 5   # from 3
  max_spawn_depth: 1           # keep flat; raise to 2 only for a real orchestrator need
```

Leave depth at 1 until a concrete task needs nesting — depth is where
runaway costs live.

---

## 4. Two conventions that make all of it trustworthy

**1. A check that can fail, or it is not a loop.** Every recurring automation
must name its gate: a script exit code (`no_agent`), a verify command
(verify-on-stop), CI status (auto-healer), or a chained checker job. If the
gate is "the model says it's done," it is a draft generator, not a loop.
Track the **keep rate** — outputs acted on ÷ outputs produced. Below ~50%,
the automation costs more than doing the task by hand; fix the gate or kill
the job.

**2. Worker and checker never share a context.** Kanban swarm's verifier,
Claude Code's adversarial-verify stage, and the chained-checker cron pattern
all enforce this structurally. When composing ad hoc (e.g. `delegate_task`),
spawn the checker as a *separate* child that receives only the finding —
never the worker's conversation.

---

## 5. Applying it to live projects

- **Infra health (System Health Remediator, cron cleanup):** convert
  LLM-based health checks to `no_agent` scripts; land Gap 1 so broken jobs
  pause-and-escalate once instead of alerting forever. This directly
  retires standing alert noise.
- **Month-end close (3 entities):** a Kanban swarm — three parallel
  per-entity reconciliation workers → audit-QC verifier (fresh context) →
  synthesis into the close package. The entities are genuinely independent;
  the convergence genuinely needs all three. Textbook diamond.
- **OneDrive reorg loop:** already loop-shaped; add the keep-rate metric and
  a failure ceiling per run so it self-reports when classification quality
  drops instead of grinding on.
- **M&A / diligence work:** fan out the existing skill suite (commercial DD,
  legal DD, valuation) as parallel Kanban workers or Claude Code subagents;
  converge through a verifier before the synthesis memo.
- **Morning brief / digests:** cron `context_from` chains — collectors feed
  a composer; composer output optionally gated by a cheap checker (Gap 2
  pattern).

---

## 6. Suggested build order

1. **Gap 1** — cron consecutive-failure auto-pause + single escalation
   (small patch, `cron/scheduler.py` + `cron/jobs.py`, mirrors Kanban's
   `failure_limit`; closes real open Linear work).
2. **Gap 2 conventions** — sweep existing cron jobs: mechanical checks →
   `no_agent` scripts; add chained checkers to the LLM jobs that feed
   decisions.
3. **Gap 4** — one-line config bump on the gateway host.
4. **First Kanban swarm in anger** — run one real diamond (month-end close
   or a diligence sprint) end-to-end; capture what worked as a skill so the
   background-review loop compounds it.
5. **Gap 3** — `pre_verify` rubric hook, only if non-code loop quality
   becomes a felt problem.
