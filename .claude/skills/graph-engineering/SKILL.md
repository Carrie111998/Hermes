---
name: graph-engineering
description: >
  [AUTO] Fake-edge test, then diamond: independent jobs in parallel,
  reduce in code, verify on a fresh context, synthesize. MUST use on any
  task with 2+ steps that might be independent, and whenever work splits
  into 3+ units, parallel agents, fan-out, or a workflow of agents. Do
  not serialize independent jobs. Do NOT use for a single sequential
  job, a tight human-approval loop, or exploratory work you cannot yet
  name. Slash: /graph-engineering. Grok: /workflow graph-run. Parent of
  fanout.
---

# Graph engineering

Always run the fake-edge test before serializing 2+ steps. Load this
skill; do not reimplement it from memory. Grok: `/workflow graph-run`
when a graph exists. `fanout` is the diamond after the test says yes.

> `[AUTO]` means the fake-edge test is always-on, not that every task
> is a diamond. Skip-graph cases below still apply. Do not genericize
> this into "always fan out."

A graph is a plan: which jobs happen, and which job must wait for which.
Nodes think. Edges carry results. A chain of "then" is a graph with no
width — it runs correctly and slowly, and dies at the first stall.

`fanout` is the diamond specialization of this skill. Load it after the
fake-edge test says a graph exists. Related: `unlazy` (anchors),
`red-team` (fresh-context attack on a finished artifact),
`loop-keep-rate` (stop a loop that is no longer paying).

Platform spawn syntax: [references/platforms.md](references/platforms.md).
Paste-ready graphs: [references/templates.md](references/templates.md).
Grok's runnable diamond: `/workflow graph-run`.

## 1. Fake-edge test (do this first)

Walk the current workflow. At each step ask: **does this step consume
the previous step's output?**

- Yes → real edge. Keep the order.
- No → fake edge. Cut it. Those jobs run at the same time.

If you cannot find two jobs with no edge between them, there is no
graph to build. It is a loop. Run the loop.

## 2. Node contract

A usable node has one bounded job, a defined input, and a defined
output shape. Free-text walls are nodes only a human can read. The next
node must consume the output without guessing.

## 3. The diamond (the one pattern)

```
fan out  →  reduce in code  →  verify (fresh context)  →  synthesize
```

- **Fan out** for breadth: independent workers, one contract each, all
  spawned in one turn.
- **Reduce** with plain code / script, not another chat. Count expected
  vs actual inputs here.
- **Verify** on a **fresh context**. The worker that did the work never
  grades the work. Split the check three ways: correct, current, source
  real.
- **Synthesize** once, from survivors. Cheap models on boring nodes;
  the strong model only where judgment lives.

Wall-clock ≈ the slowest layer, not the sum of every node.

## 4. Skip the graph when

- The task is small or isolated (one function, one bug).
- You want to approve every step.
- You do not yet know what you are looking for.
- The steps genuinely depend on each other.
- You cannot find two jobs with no edge between them.

A graph buys **width**, not judgment. Forcing one onto a line adds cost
for zero speedup.

## 5. Where graphs break (and the fix)

1. **Context collapse** — do not dump a thousand outputs into one final
   step. Batch, summarize each batch, combine summaries.
2. **False independence** — prompts look independent but share a file,
   worktree, or rate-limited API. Isolate writers (`isolation="worktree"`
   / `isolation_worktree`). Audit shared resources, not just shared data.
3. **Silent node failure** — one dead node among many slips into a
   report that looks complete. Every merge **counts expected vs actual**
   and flags the gap. Failed `parallel()` slots are missing inputs, not
   "nothing to say."

## 6. Anchors

Topology does not buy truth. The graph needs nodes that cannot be
argued with: tests that **ran**, unlazy gates with `CHECK`/`EXPECT`
evidence, revenue in the bank. Frozen rules an optimizer would weaken
stay off-limits. Let a graph grade its own reports and it will be
confidently wrong.

## 7. Cost and caps

A fleet burns a pile of tokens. Cap first-run width (8 units is the
default in `graph-run`). Watch the bill before going wider. Coordination
gets cheaper; the work itself does not.

## 8. How to run it here

**Interactive (any tool):** draw the graph, cut fake edges, then run
`fanout` if 3+ independent units remain and serial work would exceed
~15 minutes.

**Grok:** `/workflow graph-run` with `args.objective` and optional
`args.units` (`[{label, question}, ...]`). The script owns counting,
capping, and fail-closed verification. Do not reimplement that in chat.

**Writes:** isolate each writer. Do not fan out mutating jobs onto a
shared workspace.

## Checklist

- [ ] Fake-edge test run; leftover units are actually independent
- [ ] Each node has a contract (bounded job, typed I/O)
- [ ] Workers spawned together, each with a clean context
- [ ] Merge counted expected vs actual (gaps named)
- [ ] Verifier is a separate node on a fresh context
- [ ] Three lenses: correct / current / source real
- [ ] Synthesis uses survivors; REFUTED dropped or caveated
- [ ] Anchors are execution, not self-report
- [ ] Width capped; skip-graph cases honored
