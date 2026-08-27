# Platform adapters for graph engineering

The discipline in `SKILL.md` is tool-neutral. Only spawn syntax differs.
Fan-out spawn details live in `fanout/references/PLATFORMS.md` — do not
duplicate them here.

## Grok Build

Preferred: the saved workflow, because **coordination is code**.

```
/workflow graph-run
```

Args:

```json
{
  "objective": "one sentence job",
  "units": [
    {"label": "angle-a", "question": "bounded question with defined output"},
    {"label": "angle-b", "question": "independent of angle-a"}
  ]
}
```

`units` is optional. If omitted, a planner node runs the fake-edge test
and emits independent contracts. The script caps at 8, counts expected vs
actual workers, and verifies each finding on a fresh context.

Interactive diamond without a workflow: spawn `fanout-worker` nodes in
**one** `spawn_subagent` batch, then `fanout-skeptic`, then synthesize
yourself. Writers take `isolation: "worktree"`.

Source of the workflow: `~/Dev/dotfiles/grok/workflows/graph-run.rhai`
(user copy: `~/.grok/workflows/graph-run.rhai`, an independent regular
file — Grok rejects a symlink for named `/workflow` lookup; a hardlink
into the git tree is also refused).

## Claude Code

`fanout` skill. `Agent` / Task calls in **one message**. Workers
`fanout-worker`, skeptic `fanout-skeptic`. Writer isolation: worktrees.

Paste-ready prompts: [templates.md](templates.md).

## Cursor / Antigravity

Same diamond. Spawn syntax in `fanout/references/PLATFORMS.md`.

## Hermes

`delegate_task` for bounded parallel subtasks. Spawn `hermes -w` (worktree)
for writers. Do not share one workspace across mutating delegates.

## Degradation

If the tool has no parallel primitive, keep the **quality** half: still
run a fresh-context verifier over combined output. Do not call a serial
run a fan-out. Say which mode you used.
