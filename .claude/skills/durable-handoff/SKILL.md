---
name: durable-handoff
description: Save-game / load-game for long tasks — snapshot live state to a durable external store (Linear issue, Obsidian vault note, or a repo file) in a fixed schema, and cold-start from it in a new session. Trigger on "checkpoint this", "save state", "resume the X work", "continue where we left off", when a task will span sessions or an unattended run must resume, or when context usage crosses ~70%. Complements session-context / strategic-compact, which manage context only WITHIN a live session.
---

# Durable Handoff — Checkpoint & Resume Across Sessions

`session-context` and `strategic-compact` keep working state inside one live
session. When the session dies — context exhaustion, a cron boundary, a crash,
a scheduled run that picks up tomorrow — that state is gone. This skill writes a
resumable snapshot to a **durable store outside the session** and reconstructs
it on cold start. Save-game / load-game for long tasks.

## When to checkpoint
- A task will span more than one session, or exceed a context budget.
- Any unattended / cron / loop run that must "pick up where we left off."
- Context usage crosses ~**70%** (checkpoint before you're forced to compact).
- The user says "checkpoint", "save state", "resume", "continue where we left
  off."
- Proactively at the end of any session that left a multi-step task unfinished.

For long unattended runs, checkpoint every N iterations — not just at the end —
so a mid-run crash is recoverable.

## The checkpoint record (schema)
A fixed shape is what makes cold resume reliable. Fill every field:

```
GOAL:        <the durable objective, one or two sentences>
DONE:        <completed steps — enough detail to trust without re-checking>
IN-FLIGHT:   <the single step underway + which substep>
NEXT-3:      <the next 3 concrete actions, in order>
BLOCKERS:    <what's stuck and on whom/what>
KEY-FACTS:   <IDs, paths, URLs, decisions, constraints the next session needs>
ARTIFACTS:   <files/PRs/docs produced, with locations>
RESUME:      <the literal first command/prompt to run on resume>
UPDATED:     <local timestamp, labeled>
```

A copy-paste skeleton lives in `references/checkpoint-template.md`.

## Where state lives (pick per task)
- **Linear issue** — when the work is tracked / collaborative / has a ticket.
  Put the record in the description; append progress as comments.
- **Obsidian vault note** — when the work is personal / knowledge-oriented.
- **A repo file** (e.g. `.plans/<task>.md`) — when it's code work in one repo.
- When unsure, Linear + vault both; the record is small.

**Idempotency (critical):** always update the SAME record — search by a stable
marker first, never spawn a second checkpoint for the same task. A duplicate
checkpoint is worse than none: the next session can't tell which is current.

## Writing a checkpoint
1. Search the store for an existing record for this task (stable marker/title).
2. Upsert the schema above (update in place if found, else create).
3. Confirm the write echoed back — a checkpoint you didn't verify isn't one.

## Cold resume
1. Locate the record by its stable marker.
2. Reconstruct the working set: re-read KEY-FACTS and ARTIFACTS.
3. Re-state the plan from NEXT-3.
4. **Verify DONE items are actually done** before continuing — trust but
   check; a falsely-marked-done step is the classic resume failure. Then run
   RESUME.

## Verify it works
- Scripted 3-step task: checkpoint after step 1, kill the session; in a fresh
  session, resume must reconstruct the goal, mark step 1 done, and start step 2
  without redoing step 1.
- Checkpoint twice → one record updated, not two (idempotency).
- Feed a checkpoint with a falsely-marked-done item → resume catches it at the
  verify step.
- Schema lint: the written record parses back into all required fields.

## Wiring note
The schema and protocol are generic; the backends are your specific stores
(Linear workspace/team, vault paths, the MCP tool names). Set those once for
your environment; everything above is portable.

## Checklist
- [ ] Record written in the full schema (no empty required fields)
- [ ] Stored in the right backend for this task
- [ ] Same record updated in place (searched first — no duplicate)
- [ ] Write verified (echoed back)
- [ ] On resume: DONE items re-verified before continuing
