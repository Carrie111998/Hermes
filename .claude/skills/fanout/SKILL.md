---
name: fanout
description: Run a complex task as parallel subagents — plan, fan out N independent workers at once, attack the combined findings with a skeptic, then synthesize what survives. Diamond specialization of graph-engineering. Use after the graph-engineering fake-edge test says a graph exists, when a task splits into 3+ independent work units (research, analysis, writing, design, audits) and serial work would exceed ~15 minutes. Do NOT use for a single work unit, for work under ~15 minutes, or when steps are sequentially dependent (each needs the previous one's output).
---

# Fanout — plan → parallel workers → skeptic → synthesis

Four phases. The order is the whole point: **checking is a separate job from
writing**, so the agent that produces the answer is never the only one grading it.

```
plan  ──▶  N workers in parallel  ──▶  skeptic (refutes)  ──▶  synthesis
 1              2                          3                      4
Fable          Sonnet xN                  Opus                  Opus
```

Wall-clock ≈ the slowest single worker, not the sum.

## Model routing

The routing is half the value — cheap parallel breadth, expensive serial
judgment. Running every phase on one tier is not a simplification, it is a
different (worse, costlier) algorithm.

| Phase | Model | Why |
|-------|-------|-----|
| 1 Plan | **Fable** | Main loop, not a subagent. Decomposition gates all downstream spend. |
| 2 Workers | **Sonnet** | Bulk of tokens; breadth over depth. Pass `model: "sonnet"` explicitly. |
| 3 Skeptic | **Opus** | Refutation is the hardest reasoning in the pipeline. Fresh context. |
| 4 Synthesis | **Opus** | Must weigh conflicting evidence and honor verdicts. |

Canonical reference: `~/.claude/refs/FABLE-SONNET-OPUS-PLAYBOOK.md`.
Copy-paste `Agent({...})` calls: `~/.claude/refs/plan-execution-template.md`.

On tools without these named tiers, map by role — strongest for planning,
fast-and-capable for workers, strongest for skeptic and synthesis.

> [!warning] Do not genericize this table
> This skill is PROTECTED (no `[AUTO]` tag): automated rewriters must post a
> Modification Request and wait for approval, per `self-improvement-loop`.
> On 2026-08-27 it was genericized twice in one session anyway — named tiers
> replaced with "your strongest model" prose and the `model:` parameters
> dropped from the example calls. The result still reads as correct guidance
> while silently running a different, costlier algorithm.
> Keep the role-mapping sentence above for tool-neutrality; keep the concrete
> `model:` values too. They are not redundant.

Parent discipline: `graph-engineering` (fake-edge test, skip-graph rules,
silent-node counting, layered fan-in, anchors, cost caps). This skill is
the diamond once that test says a graph exists. Grok's code-owned diamond
is `/workflow graph-run`.

This skill is tool-neutral. For the exact spawn syntax in your tool, read
`references/PLATFORMS.md`. If your tool has no parallel-subagent primitive,
read the degradation rules in that file — do not silently run it serially and
call it a fan-out.

## When to trigger

Trigger when **all three** hold:

- The task decomposes into **3+ independent work units**
- Estimated serial work is **more than ~15 minutes** (the cost floor)
- Units have **no hard sequential dependencies** and no shared mutable state

Do NOT trigger when:

- It's a single unit, or under the ~15 min floor — just do it
- Each step needs the previous step's output — use sequential calls
- Decomposition would cost more coordination than it saves

If it looks parallelizable but doesn't clearly clear the bar, say so and proceed
directly. Forcing a fan-out onto a task that doesn't fit is the most common way
this skill wastes money.

## Phase 1 — Plan

Run the fake-edge test first (`graph-engineering`). Decompose into **3–5
discrete, parallelizable units** that do not consume each other's output.
For each: the question it answers, its scope boundary, the output contract,
and what "done" looks like. Also record dependencies (there should be none),
per-unit effort, and risks. If two units share a file, API, or worktree,
that is a hidden edge — isolate or serialize.

**Model: Fable.** The planner is the *main loop*, not a spawned subagent —
switch the session model (`/model fable`) and plan in-session. The plan is
leverage over every downstream token, so it gets the strongest planning tier.
On a tool without named tiers, substitute your strongest planning model.

**Approval gate.** In an interactive session, present the plan and **wait for
approval** before spawning anything. This gate is mandatory.

**Unattended sessions** (cron, loops, scheduled runs — nobody available to
approve):

- **All units read-only** (research, analysis, lookups — nothing writes files,
  sends messages, or mutates external state) → skip the gate, proceed.
- **Any unit writes** → abort the fan-out, fall back to serial execution.

All-or-nothing by design. Partial gating is unpredictable, and a half-approved
parallel writer is the worst failure mode available.

This waives **only** the human approval gate. The skeptic in Phase 3 still runs —
unattended output has no human reading it before it lands, which is exactly when
unchallenged findings do the most damage.

## Phase 2 — Execute in parallel

Spawn one worker per unit, **all in a single message**, so they run
concurrently. Sequential spawns forfeit the entire benefit. Writers get
an isolated worktree; do not fan out mutating jobs onto a shared workspace.

Each worker prompt must carry:

- The unit's specific question and scope boundary
- Enough context to work alone — workers start with a clean context window and
  cannot see your conversation or each other
- The output shape you want back (findings, not narration)
- An explicit instruction to search the web if the unit needs current data

**Model: Sonnet.** Workers are the bulk of the token spend and the cheapest
place to be wrong. Reserve Opus for Phases 3 and 4, where judgment beats
throughput.

The `model` parameter is not optional. Omit it and every worker inherits the
session model, silently collapsing the cost structure — a fan-out planned on
Fable and run on Opus workers costs multiples of the intended budget for no
quality gain. In Claude Code:

```js
// All N in ONE message — sequential spawns forfeit the entire benefit.
Agent({
  description: "<unit name>",
  subagent_type: "claude",
  model: "sonnet",
  prompt: "<the unit's question, scope boundary, output contract, and enough " +
          "standalone context to work with no view of this conversation. " +
          "Add an explicit WebSearch instruction if the unit needs current data.>"
})
```

On a tool without named tiers, use its fast-but-capable tier and keep the
strongest one for the skeptic and synthesis.

## Phase 3 — Skeptic (required)

### Completeness gate first — it's mechanical, do it before anything else

Confirm each unit returned **findings**, not narration. A unit reporting
"waiting on results" or restating its plan has produced nothing to refute, and a
skeptic prompted to attack *claims* will not flag their *absence*. Re-dispatch
any such unit.

Then count **expected vs actual** returns. A dead worker among many is a
silent node failure — name the gap; do not synthesize as if the set were
complete. If N is large, layer fan-in (batch, summarize, combine summaries)
instead of dumping every raw output into the skeptic.

This is not hypothetical: in the run that produced this guidance, 2 of 4 units
initially returned confident, well-formed narration and no actual work.

### Then spawn one skeptic over the combined output

**Model: Opus** — `Agent({subagent_type: "claude", model: "opus", ...})`, on a
fresh context. Prompt it to **refute, not summarize**:

> You are a skeptic. Your job is to REFUTE, not to summarize.
> Here are findings from N parallel research units: <combined output>.
> For each material claim: is it actually supported by evidence, or asserted
> confidently without proof? Flag (a) unsupported claims, (b) stale or undated
> evidence, (c) sources that don't say what the finding claims, (d) conflicts
> between units, (e) verification weaker than it looks — a unit that checked a
> file EXISTS rather than that it WORKS, or that re-read the author's own
> comment as independent proof, (f) anything mistaking correlation, popularity,
> or pain for willingness to pay.
> Return SURVIVES / WEAK / REFUTED per claim, with a one-line reason.
> Default to WEAK when evidence is thin — do not be agreeable.

This phase is **required**, not conditional. Fan-out has a ~15-minute cost
floor, so anything reaching it is substantial enough to be worth checking; a
per-run judgment call would just reintroduce the failure mode.

The honest exception: work with no falsifiable claims — parallel creative
drafts, independent mechanical passes — has nothing to refute. Parallelizability
and claim-verifiability are different axes. Run the skeptic whenever the output
carries factual claims, which is nearly always.

### What this stage does NOT do

- The skeptic sees worker **output**, not sources. It judges plausibility and
  internal consistency; it cannot re-verify a citation it was never given.
- Nothing structurally forces synthesis to honor the verdicts — that stays a
  judgment call. Read the verdicts against the final deliverable when stakes
  warrant.
- **Check the skeptic too.** In the run that produced this guidance it refuted
  three findings — two correctly, one wrongly (it counted the wrong directory).
  Verdicts are input to judgment, not a substitute for it.
- Treat a unit's clean sweep with **more** suspicion when it read code rather
  than ran it. Static reading cannot fail a claim the way execution can.

## Phase 4 — Synthesize

**Model: Opus.** With the verdicts in hand:

- Build on **surviving** evidence; drop or explicitly caveat REFUTED claims
- Resolve conflicts and overlaps across units
- Cite which unit each finding came from
- Carry the verdict on anything WEAK or REFUTED
- Flag gaps and uncertain areas rather than smoothing them over

## Phase 5 — Save the audit trail

- `plan.md` — the decomposition
- `review.md` — the skeptic's verdicts. What was challenged and what survived is
  as useful later as the findings themselves.
- The final deliverable, linked back to `plan.md`

## Checklist

- [ ] 3+ independent units and >~15 min serial work (else do it directly)
- [ ] Interactive: plan approved before any worker spawns
- [ ] Unattended: all units read-only (gate skipped) OR fan-out aborted (any writes)
- [ ] All workers dispatched in ONE message
- [ ] Model routing applied: Fable plan, `model: "sonnet"` workers, Opus skeptic + synthesis
- [ ] Every unit returned findings, not narration (re-dispatch if not)
- [ ] Expected vs actual worker count recorded; gaps named
- [ ] Skeptic ran over combined output **before** synthesis, on a fresh context
- [ ] Skeptic's own verdicts spot-checked — it can be wrong too
- [ ] Synthesis builds on survivors; REFUTED claims dropped or caveated
- [ ] Sources cited per unit
- [ ] `plan.md` + `review.md` + deliverable saved
