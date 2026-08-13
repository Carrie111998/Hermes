---
name: red-team
description: Attack a claim set you did NOT just produce — a fresh-context skeptic returns SURVIVES/WEAK/REFUTED per claim. Trigger when handed a finished doc, plan, memo, or set of findings to poke holes in ("red-team this", "what's wrong with this", "is this actually supported", "steelman the opposite"). NOT for your own in-session output (use verification-loop) and NOT as part of a parallel fan-out (fanout's own step 3 already does this).
---

# Red-Team — Standalone Adversarial Skeptic

Run the fresh-context skeptic over **any** claim set someone points you at — a
document, a plan, a set of findings, a memo — and return a per-claim verdict.
This is the "checking is its own job" primitive, unbundled from `fanout`.

## When to use (and when not)

Use when ALL of these hold:
- The material was produced by **someone else** (or by you in a *different*
  session) — you're auditing foreign claims, not your own fresh work.
- There is **no parallel work to fan out** — you just want the claims attacked.
- The claims are **falsifiable** (facts, forecasts, recommendations with
  stated evidence) — not pure creative/opinion content.

Disambiguation:

| Situation | Skill |
|---|---|
| Attack a foreign doc/plan/findings | **red-team** (this skill) |
| Iterate your own output until it clears a rubric | `verification-loop` |
| Verify findings inside a parallel fan-out | `fanout` (step 3) |

## Procedure

1. **Extract atomic claims.** Break the material into discrete, individually
   checkable claims. A claim bundle ("X is true, so we should do Y") splits
   into the factual claim and the recommendation.
2. **Spawn the skeptic in fresh context.** Use the shared prompt at
   `../fanout/references/skeptic-prompt.md` (single source of truth — `fanout`
   uses the same file). Give it ONLY the claims, not your reasoning about them.
   If the underlying sources are available (files, URLs), give the skeptic
   access and instruct it to verify against them — that is strictly stronger
   than judging plausibility alone.
3. **Collect verdicts.** SURVIVES / WEAK / REFUTED per claim, each with a
   one-line reason and (for WEAK) what evidence would upgrade it.
4. **Spot-check the skeptic.** It can refute wrongly — sanity-check any verdict
   that would change a decision, especially a REFUTED on a load-bearing claim.

## Output format

A verdict table (claim · verdict · reason), then a short "top 3 must-fix" list
of the REFUTED/WEAK claims that most affect the conclusion. If every claim
survives, say so plainly — but treat an all-SURVIVES sweep of read-only
material with suspicion and note it.

## Limits
- The skeptic judges the claims and whatever sources it was given; it cannot
  re-verify a citation it never saw.
- One skeptic is one perspective. For high-stakes material, run 2–3 with
  distinct lenses (correctness / currency / does-the-source-say-this) and keep
  a claim only if a majority let it survive.

## Checklist
- [ ] Material is foreign (not your own in-session output)
- [ ] Claims extracted atomically
- [ ] Skeptic ran in fresh context on the claims (+ sources if available)
- [ ] Verdicts spot-checked — the skeptic can be wrong
- [ ] Top must-fix claims surfaced; all-survive sweeps flagged as suspicious
