---
sidebar_position: 12
title: "Standardized Task Prompt: Autonomous Issue Resolution"
description: "A reusable, fill-in-the-blanks prompt template for driving Hermes to resolve a scoped code issue end-to-end — read source, write the change, self-verify, and open a draft PR."
---

# Standardized Task Prompt: Autonomous Issue Resolution

**The problem:** You want Hermes to pick up a well-scoped issue — a bug fix, a test-gap, a
small refactor — and carry it all the way to a reviewable draft PR, unattended
(`--yolo`), without babysitting. Free-form prompts get inconsistent results: the agent
asserts behavior the code doesn't have, forgets to self-verify, or opens a PR that skips
your conventions.

**The solution:** A standardized prompt with the sections that reliably produce a correct,
convention-compliant PR. Fill in the blanks, pass it with `hermes chat -q "..." --yolo`,
and let it run.

This template has been validated on real test-gap issues where the agent located the
production code, wrote a regression test that genuinely fails when the behavior is broken,
self-verified with the project's test runner, and opened a draft PR — in a single
unattended run.

## When to use it

- The task is **well-scoped** and verifiable (one function/behavior, one or two files).
- There is a **command that proves success** (a test file, a build, a lint).
- You want a **draft PR** at the end, not a merge.

For open-ended design work, prefer an interactive session.

## The template

Copy this, replace every `<...>`, and pass it as the query. Keep it explicit — the agent
does best when the deliverable and the definition of done are unambiguous.

```text
You are a developer resolving <ISSUE REF, e.g. GitHub issue #123> in the repo <ORG/REPO>.
Your current working directory is <PACKAGE/SUBDIR>. The repo is cloned, dependencies are
installed, and you are on branch `<BRANCH>` (already created and checked out). git, gh
(authenticated), and the toolchain are available.

## The issue (<test-only | bugfix | refactor> — <NO production change | scope note>)
<1-3 sentence summary of the requirement / defect.>

The relevant production code is:
  <path> — <function/symbol name> (around line <N>; LINE NUMBERS MAY BE APPROXIMATE —
  locate by symbol name). Behavior:
  <describe the behavior in prose: inputs, branches, return values, edge cases.>

## What to do
Edit <ONLY this file | these files>:
  <path to the file to change>
Follow the conventions already used in that file (imports, mocking style, helpers).

<Precisely state the deliverable and the assertions/behavior required. Frame the
acceptance test as: "a <broken variant> must fail this" so the change is a real guard,
not a tautology.>

FIRST read the production source to confirm the exact behavior before you assert it — do
not assert behavior the code does not have. Do NOT modify unrelated code.

## Self-verify (must pass before you commit)
Run: <exact command, e.g. `npx jest --config jest.config.js <file>`>
Everything must pass, including what you added.

## Commit, push, open a DRAFT PR
- Stage only the intended file(s). Commit message: `<type(scope): imperative summary>`
  with a short body noting `<Refs/Closes #123>`.
- Push branch `<BRANCH>`.
- Open a DRAFT PR: base `<BASE BRANCH>`; title EXACTLY: `<exact title>`.
  Body must contain:
        ## Summary
        <one or two real sentences — do NOT leave placeholder text>

        <Closes #123>

        ## Risk
        <LOW/MED/HIGH — one line of justification>

        <any required review-checklist markers your repo expects>
  Use: gh pr create --draft --base <BASE> --title "..." --body-file <file>.

When done, print the PR URL and a one-line summary. If you get stuck, explain exactly where.
```

## Invocation

```bash
hermes chat -q "$(cat task.txt)" --yolo --max-turns 60 -Q
```

- `--yolo` runs unattended (bypasses command approvals — use on a scoped, sandboxed clone).
- `--max-turns 60` bounds the run.
- `-Q` (quiet) prints only the final response and session info — ideal for logging.

## Tips learned from real runs

- **Describe behavior in prose, not just line numbers.** Source drifts; line refs go
  stale. Symbol names are stable — tell the agent to *locate by name* and treat line
  numbers as approximate.
- **Make "done" a command.** A single self-verify command the agent can run and re-run is
  the difference between "I think it works" and proof.
- **Frame the acceptance test adversarially.** "A batched/parallel refactor must fail this
  test" produces a real regression guard; "add a test" produces a tautology that passes
  against a mock.
- **Ask for a real Summary.** Explicitly say *do not leave placeholder text* in the PR
  body — agents will otherwise sometimes echo the template instructions verbatim.
- **Use a unique branch name per run** to avoid non-fast-forward collisions with a prior
  attempt's push.
- **Keep scratch files in the workspace.** If the agent writes temporary verification
  scripts, tell it to use the repo/workspace scratch dir, not a system temp path — some
  environments guard `/tmp`-like locations and the agent will thrash working around it.
- **Rebase onto the latest base branch before opening the PR** so the PR diff is only your
  change, even if the base moved during the run.
