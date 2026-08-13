---
name: decision-tournament
description: Pick among 3+ options defensibly — a weighted decision matrix or a pairwise tournament, with a sensitivity check that reports what would flip the winner. Trigger on "which should we pick", "compare these options", "decision matrix", "score these", "run a bracket", or as the reduce-step after a fanout produces competing proposals. NOT for binary yes/no or when one option obviously dominates.
---

# Decision Tournament — Defensible N-way Reduction

The reusable "fan-in / reduce" primitive: take N candidate options and reduce
them to a **ranked, defensible** decision — with the reasoning shown, so the
choice survives a "why not the other one?" challenge. This is the disciplined
version of a `fanout`'s free-form synthesize step.

## When to use
- More than 2 serious options and the choice must be justified.
- Choosing a vendor, design, approach, model, candidate, architecture.
- The natural downstream of a fan-out that produced competing proposals.
- User says "which should we pick", "compare these", "score these", "decision
  matrix", "run a bracket".

Do NOT use for a binary yes/no (that's a different analysis) or when one option
clearly dominates on every axis (just say so).

## Choosing the method
- **Weighted matrix** — when options are absolutely scorable on shared criteria
  (most decisions). Best default.
- **Pairwise tournament** — when options are hard to score in the abstract but
  easy to compare head-to-head (e.g. writing samples, designs). Round-robin or
  single-elimination with a stated judging rubric.

## Method A — Weighted matrix
1. **Fix the criteria** — the axes that matter for THIS decision.
2. **Fix the weights** — must sum to 1.0 — **before seeing any scores.** This
   is the anti-gaming rule: weights set after scores are visible are just a
   rationalization of a pre-picked winner.
3. **Score** each option on each criterion (say 1–5), each with a one-line
   justification.
4. **Compute** weighted totals.
5. **Rank**, and state the margin between #1 and #2.

## Method B — Pairwise tournament
1. State the **judging rubric** (what makes one option beat another).
2. Run the bracket (round-robin for ≤5 options; single-elim for more), one
   pairwise verdict at a time, each with a reason.
3. **Tally** wins; the winner is the option that beats the field.

## Sensitivity check (required)
Find the **smallest weight or criterion change that flips the winner**. If a
tiny change flips it, declare a **near-tie** and say so — don't manufacture
false confidence. If the winner is robust to large weight shifts, say that too;
it strengthens the recommendation.

## Anti-gaming rules
- No editing weights/criteria after scores are visible. If you must revise
  them, re-score every option from scratch.
- For high-stakes calls, run the scoring pass in **fresh context** (borrow the
  `red-team` skeptic) so the scorer isn't the option's author.

## Output format
The matrix (or bracket), the **winner + margin**, the **runner-up**, the
**flip-point** from the sensitivity check, and a one-paragraph recommendation
that names the runner-up's best idea worth grafting on.

## Verify it works
- Rigged matrix where B dominates on every criterion → B wins regardless of
  weights.
- Near-tie case → sensitivity reports a small flip-point and the verdict says
  "near-tie".
- Change weights after scoring → the skill refuses / re-scores clean.
- Transitive tournament (A>B>C) → A wins the bracket.
- Arithmetic audit: weighted totals recompute by hand.

## Checklist
- [ ] 3+ genuine options, no single dominant one
- [ ] Method chosen (matrix vs tournament) with reason
- [ ] Criteria + weights fixed BEFORE scores (weights sum to 1.0)
- [ ] Every score/verdict carries a one-line justification
- [ ] Sensitivity check done; near-ties declared honestly
- [ ] Output shows winner, margin, runner-up, flip-point, recommendation
