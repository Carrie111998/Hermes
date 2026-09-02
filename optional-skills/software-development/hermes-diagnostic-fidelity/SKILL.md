---
name: hermes-diagnostic-fidelity
description: Fix Hermes diagnostics that disagree with real runtime.
version: 0.1.0
author: Irakli (Maestro), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [diagnostics, reporting, prompt-size, regression-tests, contributor]
    related_skills: [hermes-agent, systematic-debugging, test-driven-development]
---

# Hermes Diagnostic Fidelity Skill

Hermes ships introspection commands (`hermes prompt-size`, context breakdowns,
toolset reports) that *model* what a live session would send. When one of them
reports a zero, an `(unknown)` bucket, or a duplicated row, the defect is
usually in the measuring code, not in the agent runtime. This skill is the
discipline for proving which of the two is broken and fixing only the
reporting layer.

Scope: diagnostic and reporting code inside the hermes-agent tree. Not for
debugging the agent's actual tool-calling behavior — use `systematic-debugging`
for that.

## When to Use

- A Hermes diagnostic reports `0`, `[]`, `(unknown)`, or a duplicated row.
- Diagnostic numbers disagree with what a live session demonstrably ships.
- You are about to change runtime semantics because a report looked wrong.
- Don't use for: real runtime defects, provider/model failures, config repair.

## Prerequisites

- A hermes-agent source checkout; run commands from the repo root.
- The repo venv interpreter. The launcher shim names it — read the shim with
  `read_file` rather than guessing, and prefer `./venv/bin/python` over the
  system interpreter, which lacks the project dependencies.
- No API key needed: the diagnostics build an offline agent.

## Core Principle

**The diagnostic must delegate to the runtime resolver, never re-derive it.**
Every re-derivation is a copy that drifts silently. If a report needs the
toolsets a surface receives, import the function that surface actually calls.

## How to Run

```
terminal(command="./venv/bin/hermes prompt-size --platform cli --json", timeout=300)
terminal(command="./scripts/run_tests.sh tests/hermes_cli/test_prompt_size.py -q", timeout=600)
```

## Quick Reference

| Need | Call |
|---|---|
| Compare all surfaces | `hermes prompt-size --platform <p> --json` per surface |
| Find a surface's real host | `search_files(pattern="enabled_toolsets", path="tui_gateway")` |
| Find delegation blocklists | `search_files(pattern="_blocked_toolsets_for_role", path="tools")` |
| Run one suite | `./scripts/run_tests.sh <path> -q` |
| Long suite | same, with `background=True, notify_on_complete=True` |

## Procedure

1. **Reproduce across every variant, not just the broken one.** Loop the
   diagnostic over all platforms/modes and tabulate the output. A healthy peer
   column is the control that tells you what the broken one should look like.
   Done when the table holds at least one known-good row.
   Note: a serialized tool array of `2` bytes is literally `[]` — nothing at
   all, not "small".

2. **Locate the real host of the broken variant.** Do not assume a platform
   lookup table covers it; surfaces such as the desktop app, the TUI, and
   delegated subagents may own no composite entry. Use `search_files` for the
   place that constructs a live agent for that surface.
   Done when you can name the file and line where a real session of that
   variant obtains its toolsets, and it differs from what the diagnostic calls.

3. **Prove the runtime is healthy before editing anything.** Build the object
   through the real path with `terminal` and print the numbers.
   Done when the real path yields non-zero where the diagnostic yielded zero.
   If the runtime is also zero, stop: this is a runtime defect, switch skills.

4. **Trace caused-by cascades.** One zero often manufactures another — the
   skills index is gated on the skill tools being present in the resolved tool
   names, so zero tools silently produces a zero-byte skills index.
   Done when every reported zero is attributed to a cause rather than listed
   as an independent defect.

5. **Fix by delegation, with a documented fallback.** Import the runtime
   helper; keep any local mirror minimal and label it as a mirror.
   Done when the fix adds no second copy of a runtime set.

6. **Write regression tests, then prove they bite.** Copy the fixed file aside
   with `terminal`, revert the fix in source only, keep the tests, run them,
   then restore the copy.
   Done when the reverted run fails with one cluster per defect and the
   restored run is fully green. A test that passes both ways is a no-op.

7. **Run the blast radius.** The targeted file, then every module you imported
   from. Long suites go to the background.
   Done when you have read the actual summary line — a launched run is not a
   passed run.

## Pitfalls

- **Latent defects need synthetic reproduction.** A name-collision defect can
  show zero collisions on the current machine. Build the collision under
  pytest's `tmp_path` and monkeypatch the skills-directory lookup. Label the
  proof as synthetic in the report.
- **Two-key `setdefault` in a single pass is an ordering defect.** Registering
  a declared `name` and a directory name together lets an earlier entry's
  directory alias shadow a later entry's declared name. Use two passes:
  declared names first, directory names only for keys nobody claimed.
- **Synthesised tools have no registry entry.** Bridge tools built at assembly
  time are absent from the tool-to-toolset map and fall into `(unknown)`. Give
  them their own named bucket so the cost is visible.
- **Fixed-width report columns truncate new labels.** Compute the column width
  from the rows and assert the label survives rendering.
- **A verification tool that is not the project's pinned tool is a fabricated
  result.** `npx --yes -p pkg@5 …` silently resolves a cached major (5.9.3)
  while the project pins 6.0.3, and it runs with whatever dependencies happen
  to be installed — none, if nobody ran the install. The resulting errors
  describe the harness, not the file: a missing-module diagnostic vanished
  entirely once the real toolchain ran. Invoke the pinned binary directly
  (`./node_modules/.bin/tsc`, `./venv/bin/python`) and confirm `--version`
  against the lockfile before believing any output. This is the skill's own
  Core Principle applied to your instruments.
- **Two bad runs are not a baseline.** Comparing a patched file against a
  baseline measured with the same broken harness reproduces the artifact on
  both sides and looks like proof. Re-measure the baseline after every change
  of instrument, and treat an unchanged conclusion as unproven until it comes
  from the pinned tool.
- **Foreground `terminal` is capped at 600s.** Use `background=True` with
  `notify_on_complete=True`, then read the summary.
- **Prefer `search_files` over ad-hoc shell search.** Regex escaping differs
  between engines and some are unavailable in trimmed environments.

## Verification

- [ ] Every variant reports non-zero, plausible numbers; `(unknown)` is gone
- [ ] The fix contains no re-derived copy of a runtime set
- [ ] Revert-check produced one failure cluster per defect; restored run green
- [ ] Blast-radius summary line read, not merely launched
- [ ] Diff touches only diagnostic and test files
- [ ] Every verification ran the project's pinned binary, version checked
      against the lockfile — not an `npx` fallback
- [ ] Any baseline comparison was re-measured with that same pinned tool
- [ ] Latent or synthetic proofs are labeled as such in the report
