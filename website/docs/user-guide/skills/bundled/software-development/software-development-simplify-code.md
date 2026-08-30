---
title: "Simplify Code — Simplify recent code with bounded independent review"
sidebar_label: "Simplify Code"
description: "Simplify recent code with bounded independent review"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Simplify Code

Simplify recent code with bounded independent review.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/simplify-code` |
| Version | `1.2.0` |
| Author | Hermes Agent (inspired by Claude Code /simplify) |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `code-review`, `cleanup`, `refactor`, `delegation`, `simplify` |
| Related skills | [`requesting-code-review`](/docs/user-guide/skills/bundled/software-development/software-development-requesting-code-review), [`test-driven-development`](/docs/user-guide/skills/bundled/software-development/software-development-test-driven-development) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Simplify Code

Simplify a known-working diff without turning cleanup into a broad correctness
rewrite. Use up to three independent review lenses when parallel search adds
value; the owning agent reconciles findings and verifies every applied change.

## When to Use

Use only when the user explicitly asks to simplify, clean up, or review recent
changes for maintainability. Honor explicit focus, scope, and dry-run requests.

Do not auto-run after every edit. Do not use this skill as the primary bug,
security, or acceptance review; use `requesting-code-review` for that. If there
is no identifiable diff or named code scope, report that there is nothing to
simplify.

## Procedure

### 1. Capture a coherent candidate

Use the comparison scope the user requested: working tree, staged index, last
commit, branch, or named paths. Capture the full diff and relevant repository
instructions.

For a very large diff, partition by subsystem or contract before delegating.
Do not send several reviewers an oversized candidate they cannot inspect
completely.

Completion criterion: the exact diff and all files in cleanup scope are known.

### 2. Choose one to three useful review lenses

Select only lenses that fit the candidate:

1. **Reuse and clarity** — duplicated utilities, redundant state, parameter
   sprawl, deep nesting, leaky abstractions, and inconsistent local patterns.
2. **Efficiency and lifecycle** — repeated I/O, N+1 work, missed safe
   concurrency, unbounded state, leaked handles/listeners, and silent failures.
3. **Altitude and boundaries** — call-site band-aids that should be fixed in a
   shared mechanism, unnecessary wrappers, or configuration that routes around
   a broken default.

A small diff may need only one inline pass. Use multiple reviewers only when the
lenses are independent and the expected findings justify the context cost.

Completion criterion: each selected lens has a distinct question and no two
writers own overlapping files.

### 3. Dispatch read-only reviewers when useful

Run at most three reviewers in parallel. Give each reviewer:

- absolute repository path and exact comparison scope;
- acceptance intent and relevant project conventions;
- one lens, not a generic duplicate brief;
- permission to inspect callers, history, and tests;
- a read-only boundary unless the user separately authorized edits.

Require findings in this shape:

```text
file:line -> problem -> concrete cost -> suggested change
confidence: high|medium|low; risk: safe|careful|risky
```

Before recommending removal or a deeper rewrite, inspect surrounding code and
history. A compatibility shim, staged migration, or vendored boundary may be
intentional.

Completion criterion: reviewers return evidence-backed findings or explicitly
report that no material issue was found.

### 4. Reconcile instead of voting

The owning agent:

- deduplicates findings by underlying mechanism;
- verifies pointers against the current candidate;
- drops unsupported nits and style churn;
- resolves conflicts by correctness, user intent, maintainability, then proven
  hot-path performance;
- separates a discovered correctness bug into the appropriate review/debugging
  workflow.

Several reviewers repeating the same claim do not make it true. One
well-evidenced finding can be sufficient.

Completion criterion: each retained finding has a verified cost and chosen
resolution.

### 5. Apply only scoped, behavior-preserving improvements

Apply safe cleanup first, then careful refactors in small coherent groups.
Examples include removing dead imports, using an existing helper, flattening a
conditional, consolidating duplicate logic, or reducing an unnecessary
allocation.

Do not auto-apply risky public API, concurrency, persistence, error-semantics,
or architectural changes. Present them as separate follow-up work unless the
user expands scope.

If the user requested a dry run, apply nothing.

Completion criterion: every edit traces to a retained finding and stays within
the authorized scope.

### 6. Verify the final candidate

Run focused tests for touched behavior plus the repository's relevant lint,
type, and build checks. Re-read the final diff for accidental behavior changes,
unrelated churn, and reviewer-suggested complexity that outweighed its benefit.

Report applied improvements, skipped findings and reasons, commands and outcomes,
and any risky follow-up. Say whether review was inline or delegated.

Completion criterion: final tests ran after the last production edit and the
result remains a cleanup, not an undeclared feature or bug-fix expansion.

## Pitfalls

- **Four or more reviewers by habit:** cost and conflicting noise exceed coverage.
- **Identical briefs:** correlated reviewers rediscover the same surface issue.
- **Whole huge diff per reviewer:** context truncates and findings become partial.
- **Reviewers writing concurrently:** overlapping edits race and invalidate review.
- **Style-only churn:** a larger diff is created without reducing real cost.
- **Micro-performance theater:** clarity is traded for an unmeasured hot path.
- **Public contract cleanup:** exported names or schemas are changed as if local.
- **Band-aid overreach:** a deliberate compatibility boundary is removed.

## Verification

Before completing, confirm:

- [ ] The user explicitly requested cleanup or simplification.
- [ ] Candidate scope was captured without omitting relevant files.
- [ ] No more than three independent lenses ran in parallel.
- [ ] Delegated reviewers were read-only unless writes were explicitly authorized.
- [ ] Retained findings were verified against code and history.
- [ ] Risky behavior or contract changes were not silently auto-applied.
- [ ] Focused checks ran after the final edit.
- [ ] The final report distinguishes applied cleanup from follow-up findings.
