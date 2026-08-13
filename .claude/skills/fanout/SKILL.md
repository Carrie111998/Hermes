---
name: fanout
description: Run a complex task through the plan → parallel-workers → skeptic → synthesize playbook. Auto-invoke PROACTIVELY whenever a task decomposes into 3+ independent, parallelizable work units (research, analysis, writing, design) AND the estimated serial work exceeds ~15 minutes — especially when time-sensitive. Do NOT trigger for a single work unit, for tasks under ~15 min of serial work, or when the parts are highly interdependent (shared state, sequential dependencies, one step blocks the next). In unattended sessions (cron, loops, scheduled runs with no human to approve a plan), the plan-approval gate is skipped only when every work unit is read-only (research/analysis); if any unit would write (files, external state, sends), abort the fan-out and fall back to serial execution instead.
---

# Fanout — Parallel Execution Playbook

Run a complex task through the parallel-execution playbook: **plan → parallel subagents execute → a fresh-context skeptic attacks the findings → synthesize what survives.**

In Claude Code, spawn workers with the Agent/Task tool — **all in one message** so they run concurrently. Where the Workflow tool is available (web/Cowork sessions with orchestration enabled), a Workflow with a verify stage is the equivalent for larger fan-outs.

## When to trigger (auto-invoke)

Trigger proactively — without waiting to be asked — when a task meets ALL of:

- Decomposes into **3+ independent work units** (research, analysis, writing, design)
- Estimated serial work is **more than ~15 minutes** (the cost floor — below this, fan-out overhead isn't worth it)
- Units don't have hard sequential dependencies or shared mutable state

Do NOT trigger when:
- The task is a single unit or under the ~15 min cost floor — do it directly
- Parts are highly interdependent (each step needs the prior step's output) — use sequential agents instead
- The task is simple enough that decomposition would add overhead without saving wall-clock time

If a task looks parallelizable but doesn't clearly meet the bar, say so and proceed directly rather than forcing a fan-out.

## Unattended-session gate

The plan-approval gate (below) is mandatory in interactive sessions. In **unattended** sessions — cron, loops, scheduled runs, or anywhere there's no human able to approve a plan in real time — apply this rule instead:

- **All work units read-only** (research, analysis, lookups — nothing writes files, calls send-type tools, or mutates external state): skip the approval gate, proceed straight to execution.
- **Any work unit writes** (files, external systems, messages, commits): abort the fan-out entirely and fall back to serial execution of the task. Do not partially gate — an all-or-nothing rule keeps this predictable.

This waives **only the human plan-approval gate**. The skeptic (step 3) still runs — unattended output has no human reading it before it lands, which is exactly when unchallenged findings are most likely to go unnoticed.

## Procedure

### 1. Plan (gated in interactive sessions)
Enter plan mode:
- Decompose into **3–5 discrete, parallelizable work units**, plus dependencies, per-unit effort, and risks.
- Present the plan. In an interactive session, **wait for the user's approval** — this gate is mandatory; do not spawn agents before it. Exit plan mode only after approval.
- In an unattended session, apply the unattended-session gate above instead of waiting for approval.

If the task turns out NOT to parallelize cleanly (fewer than 3 independent units, or hard dependencies), say so and recommend doing it directly instead of forcing a fan-out.

### 2. Execute (parallel workers)
For each approved work unit, spawn one subagent — **all in a single message** so they run concurrently. Give each a self-contained prompt carrying the plan's context for its unit (add a web-search instruction if the unit needs current data).

No dependencies between agents. Wall-clock time ≈ the slowest single unit, not the sum.

### 3. Check (skeptic — required)

**Checking is its own job.** Do not let the agent that writes the answer be the only one that grades it — a synthesizer asked to both merge and critique will reliably rate its own inputs as sound. Spawn one fresh-context skeptic over the combined worker output *before* synthesis:

> You are a skeptic. Your job is to REFUTE, not to summarize. Here are findings from N parallel work units. For each material claim: is it actually supported by evidence, or asserted confidently without proof? Flag (a) unsupported claims, (b) stale or undated evidence, (c) sources that don't say what the finding claims, (d) conflicts between units, (e) anything mistaking correlation, popularity, or pain for significance. Return SURVIVES / WEAK / REFUTED per claim, with a one-line reason. Default to WEAK when evidence is thin — do not be agreeable.

This stage is **required**, not conditional. Fan-out already has a ~15-minute cost floor, so anything reaching it is substantial enough to be worth checking. (The honest exception: work with no falsifiable claims — parallel creative drafts, independent mechanical passes — has nothing to refute. Run the skeptic whenever the output carries factual claims, which is nearly always.)

**First, a mechanical completeness gate.** Before spawning the skeptic, confirm each unit returned *findings*, not narration. A unit that reports "waiting on results" or restates its plan has produced nothing to refute, and a skeptic prompted to attack claims will not flag their *absence*. Re-dispatch it instead.

What this stage does NOT do:
- The skeptic sees worker **output**, not sources. It judges plausibility and internal consistency; it cannot re-verify a citation it was never given.
- Nothing structurally forces synthesis to honor the verdicts — re-read the review against the final deliverable if the stakes warrant it.
- **Check the skeptic too.** It can refute wrongly.
- Treat a unit's clean sweep with *more* suspicion when it read code rather than running it. Static reading cannot fail a claim the way execution can.

Pass the skeptic's verdicts into synthesis.

### 4. Synthesize
After the skeptic returns:
- Build on **surviving** evidence; drop or explicitly caveat REFUTED claims.
- Resolve conflicts and overlaps across unit results.
- Produce the final deliverable (report / summary / decision matrix).
- Cite which unit each finding came from, and carry the skeptic's verdict on any claim that was WEAK or REFUTED.
- Flag gaps and uncertain areas.

### 5. Save
- Write the plan to `plan.md` (audit trail).
- Write the skeptic's verdicts to `review.md` — the paper trail for *what was challenged and what survived* is as useful later as the findings themselves.
- Save the synthesis as the final deliverable, linked back to `plan.md`.

## Checklist
- [ ] Task has 3+ independent units and >~15 min estimated serial work (else do it directly)
- [ ] Interactive session: plan approved before any worker spawns
- [ ] Unattended session: all units read-only (gate skipped) OR fan-out aborted (any unit writes)
- [ ] All workers dispatched in one message
- [ ] Every unit returned actual findings, not narration (re-dispatch if not)
- [ ] Skeptic ran over the combined output **before** synthesis
- [ ] Skeptic's own verdicts spot-checked — it can be wrong too
- [ ] Synthesis builds on surviving evidence; REFUTED claims dropped or caveated
- [ ] `plan.md` + `review.md` + final deliverable saved
