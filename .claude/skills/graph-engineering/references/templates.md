# Ready graphs

Swap the bracketed parts. Keep yourself as the last yes before anything
ships. The word **workflow** (Grok) or **fan out N workers** (Claude/
Cursor) is what turns a line into a diamond.

Cap first runs. 8 units unless a prior run earned more.

## Research desk

Independent angles, skeptic, one report.

Grok:

```
/workflow graph-run
```

```json
{
  "objective": "Decision-grade research on [QUESTION]",
  "units": [
    {"label": "what-exists", "question": "What is the current public evidence on [QUESTION]? Cite dated sources."},
    {"label": "who-disagrees", "question": "What are the strongest contrary positions and their evidence?"},
    {"label": "what-breaks", "question": "Where does this fail, get expensive, or depend on an unstated assumption?"},
    {"label": "what-to-do", "question": "What would a decision-maker do next week with only surviving evidence?"}
  ]
}
```

Claude / Cursor: load `fanout`. Units = the four questions above. Skeptic
required. Synthesis is the report.

## Repo review (read-only)

```json
{
  "objective": "Review [PATH] for correctness, error handling, and silent failure",
  "units": [
    {"label": "correctness", "question": "Read the shipped code under [PATH]. List concrete bugs with file and line."},
    {"label": "errors", "question": "Read the shipped code under [PATH]. List error-handling gaps with file and line."},
    {"label": "silent", "question": "Read the shipped code under [PATH]. List swallowed errors, bad fallbacks, and missing propagation."}
  ]
}
```

These three have **no data edge**. Do not run them as A-then-B-then-C.

## Refactor sweep (writes — isolate)

Only after a read-only pass. Each worker gets `isolation="worktree"` and
owns a disjoint path. Merge counts expected vs actual worktrees before
any apply-back.

## Discovery of unknown size

Do **not** graph this first. Run a loop (`loop-keep-rate`) until the
work list is known, then graph the independent leftovers.
