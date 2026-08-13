---
name: loop-keep-rate
description: Know when a loop stops paying off — track kept÷produced per iteration and stop on decay. Trigger for any iterative/generate-and-filter loop past ~3 rounds, or when asked "when do we stop?", "is this still worth continuing?", "are we still making progress?". Pairs with autonomous-loops / ralph-loop / verification-loop, which run loops but define "done" qualitatively.
---

# Loop Keep-Rate — The Loop's Fuel Gauge

A loop that can't measure its own yield either stops too early (leaving value
on the table) or grinds pointlessly (burning tokens on outputs you throw away).
Keep-rate is the missing gauge: **outputs you kept ÷ outputs the loop
produced.** It tells you when a loop is still earning its cost and when to pull
the plug.

## When to use

- Any loop skill engaged for more than ~3 iterations.
- A generate-then-filter loop (draft N variants, keep the good ones).
- An autonomous / cron / ralph loop with no natural terminator.
- The user asks "when should I stop?", "is this worth continuing?", "are we
  still improving?".

Attach it to a loop at the start — you must define what "kept" means *before*
you iterate, or the metric is post-hoc rationalization.

## Definitions

- **produced** — outputs generated this iteration.
- **kept** — outputs that survived the loop's acceptance test (passed the
  rubric, were merged, were acted on). Define the acceptance test up front.
- **instantaneous keep-rate** — kept ÷ produced for this iteration.
- **rolling keep-rate** — kept ÷ produced over the last K iterations (default
  K=3), which is what the stop rule reads (a single bad iteration shouldn't
  end a healthy loop).
- **cumulative kept** — total kept so far, measured against the goal.

## Attaching to a loop

Log one line per iteration:

```
iter=<n> produced=<p> kept=<k> rate=<k/p> cumulative_kept=<total>
```

## Stop rules

Fire STOP when ANY holds:
- **Decay:** rolling keep-rate < threshold (default **0.20**). Below this the
  article's rule bites — you're paying for more than you keep.
- **Dry:** K consecutive zero-keep iterations (default K=2).
- **Goal met:** cumulative kept reaches the target.
- **Cost ceiling:** cost-per-kept exceeds budget (hand off to
  `token-budget-advisor` for the token accounting).

## Reading the curve

- **Flat and high** → keep going; the loop is productive.
- **Sharp decay** → stop; you've harvested the easy wins.
- **Zero from the very start** → do NOT just stop — the loop is mis-specified.
  Fix the generator or the acceptance test, not the iteration count.

## Report format

A compact table (iter · produced · kept · rate · cumulative) plus a one-line
verdict: **CONTINUE**, **STOP** (with which rule fired), or **FIX-GENERATOR**
(zero-keep from the start).

## Verify it works
- Feed a decaying log `(5,4,2,1,0,0)` → STOP fires at the second zero / rolling
  crossing.
- Feed a steady-high log → CONTINUE.
- Feed all-zeros → FIX-GENERATOR, not STOP.
- Override the threshold → the new threshold is respected.

## Checklist
- [ ] "Kept" acceptance test defined before iterating
- [ ] Per-iteration line logged (produced, kept, rate, cumulative)
- [ ] Stop rule thresholds set (or defaults accepted)
- [ ] Verdict emitted each iteration (CONTINUE / STOP / FIX-GENERATOR)
- [ ] Zero-from-start treated as a generator bug, not a stop
