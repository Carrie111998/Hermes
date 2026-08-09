---
name: adaptive-pr-review
description: "Review a GitHub pull request with risk-proportional depth."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [github, pull-request, code-review, ci, infrastructure, adaptive]
    related_skills: [github-code-review, github-pr-workflow, github-auth]
---

# Adaptive Pull Request Review

Review a pull request from one simple command:

```text
/adaptive-pr-review <PR_NUMBER>
```

The caller should not need to choose review tools, lenses, or depth. Inspect the current PR and announce one of `LIGHT`, `MEDIUM`, or `DEEP` before doing substantive work. Spend effort in proportion to risk: do not run expensive workflows for trivial changes, and do not use a light review for a small but security-sensitive change.

## Operating contract

- Verify current GitHub and repository state; do not rely on stale summaries.
- Review the complete PR diff against its actual base branch.
- Use an isolated worktree for checkout, tests, conflict resolution, and edits.
- Read `AGENTS.md`, `REVIEW.md`, nested instructions, and applicable governance policies before editing.
- Never approve or merge a PR.
- Never modify `develop`, `staging`, `main`, or production infrastructure.
- Never bypass hooks or use `--no-verify`.
- Never stage with `git add .` or `git add -A`; stage explicit paths only.
- Do not edit non-English locale files.
- Re-fetch before every final disposition.
- Make bounded fixes only when the active user/repository policy authorizes repair. Otherwise report findings without mutation.
- If a safe disposition requires ambiguous human judgment, mark `BLOCKED` and stop work on that PR.

## State first

Before selecting a tier, fetch and record:

- PR number, title, body, author, draft state, base branch, head branch, and head SHA
- changed files and diff size
- GitHub mergeability and merge-state status
- current CI checks
- human and automated reviews and unresolved threads
- repository working-tree and worktree state

If the PR number is missing or invalid, stop and ask for it. If authentication or repository access is unavailable, stop rather than guessing.

## Select the review tier

Choose the **highest** matching tier. Report the selected tier and the reason in one sentence.

### LIGHT

Use for documentation, comments, formatting, generated metadata, or narrowly scoped test-only changes with no production behavior change.

Do:

1. Inspect the complete diff and description.
2. Check for accidental files, secrets, conflict markers, and description mismatch.
3. Inspect current CI and review threads once.
4. Run the smallest relevant validation, such as formatting or the changed test.
5. Report concise findings and a final disposition.

Do not load broad infrastructure, security, or CI-debugging workflows unless the diff reveals a risk signal.

### MEDIUM

Use for ordinary application changes with localized production behavior and no detected auth, tenancy, policy, migration, deployment, binding, secret, or workflow risk.

Do everything in LIGHT, plus:

1. Inspect surrounding call sites, error paths, and relevant tests.
2. Run focused tests plus relevant typecheck/lint/format validation.
3. Check API and data-contract compatibility.
4. Load the standard GitHub code-review workflow and respond to actionable review threads.
5. Add a focused regression test and make the smallest safe fix when a valid bounded issue is found and repair is authorized.

### DEEP

Use for any change involving authentication, authorization, tenancy, policy enforcement, privacy, data access, concurrency, caching, retries, background jobs, migrations, schemas, workflows, deployment, Terraform, Cloudflare bindings, environment variables, secrets, release tooling, production paths, rollback behavior, or cross-cutting changes. Also use DEEP when the PR is large, conflicted, unclear, has failed checks, or contains substantive review findings.

Do everything in MEDIUM, plus only the relevant deep lenses:

- **Security/policy:** trace positive and negative paths, fail-open/fail-closed behavior, tenant isolation, authorization provenance, and sensitive-data handling.
- **Data/migrations:** inspect schema compatibility, migration ordering, reads/writes, rollback limits, and test coverage.
- **Infrastructure/deployment:** inspect workflows, bindings, resource identity, environment mapping, secrets, staging/production readiness, scheduled behavior, and rollback controls. Do not mutate infrastructure.
- **Concurrency/reliability:** inspect ordering, races, retries, partial failures, idempotency, timeouts, and observability.
- **CI:** diagnose failures before changing code; never weaken a gate or test to make CI pass.
- **Conflicts:** resolve each conflict semantically in the PR worktree. Never use blanket ours/theirs resolution. Mark `BLOCKED` for ambiguous `wrangler.toml`, binding/resource, migration, environment, or locale conflicts.

Load the corresponding repository skill only when its trigger is present. Do not load every review skill for every PR.

## Review and repair loop

For the selected tier:

1. Compare the description with the actual diff. Classify it `MATCH`, `PARTIAL`, or `MISMATCH`; identify omitted production impact, unsupported claims, and unrelated changes.
2. Review findings by severity. Report only actionable findings or material risks; avoid style-only noise unless repository policy requires it.
3. For each valid finding, choose one disposition:
   - `FIXED` — code exists and relevant validation passed.
   - `DECLINED` — concrete technical reason.
   - `BLOCKED` — prohibited action or unresolved human judgment.
   - `FOLLOW_UP_RECOMMENDED` — real concern outside this PR with no verified tracking issue; do not invent a ticket.
4. If repair is authorized, batch bounded fixes, validate them, commit with normal hooks, and push once to the PR branch. Never push protected branches.
5. Re-fetch the PR after any push and verify the new head SHA, CI state, review threads, and mergeability.
6. Ensure automated review is requested at most once for the current head. Do not duplicate bot-review comments or manually request tools that are already automatic.

Do not claim a finding is fixed before the relevant test or validation has passed. Do not resolve human-owned review threads unless explicitly authorized by repository policy.

## Final disposition

Use exactly one:

- `READY_FOR_HUMAN_MERGE` — current head reviewed, no actionable unresolved findings, required checks passed, mergeability verified, and no governance/infrastructure blocker. Human approval and merge are still required.
- `REVIEW_COMPLETE_CHECKS_PENDING` — review is complete but required CI is still queued or running.
- `NOT_READY` — actionable findings, failed checks, missing validation, or merge conflicts remain.
- `BLOCKED` — safe continuation requires prohibited action, unavailable credentials, ambiguous infrastructure/human judgment, or another hard stop.

A local test pass alone is never sufficient for `READY_FOR_HUMAN_MERGE`. GitHub's mergeability state may be stale; if it has not recomputed, report that explicitly and do not claim readiness.

## Output format

Start with:

```text
Proceeding with a LIGHT|MEDIUM|DEEP review because <one-sentence reason>.
```

Then report concisely:

```text
PR #<number> — <title>
Head: <sha>  Base: <base>  Worktree: <path>
Tier: LIGHT|MEDIUM|DEEP
Description: MATCH|PARTIAL|MISMATCH

Findings:
- <severity> <file:line> — <finding and evidence>

Actions:
- <fixed, declined, blocked, or follow-up item with validation>

Validation:
- <command> — PASS|FAIL|PENDING
CI: <passed> passed, <failed> failed, <pending> pending
Mergeability: <current GitHub state, including stale/pending if applicable>

Disposition: READY_FOR_HUMAN_MERGE|REVIEW_COMPLETE_CHECKS_PENDING|NOT_READY|BLOCKED
Human action: <exact next action, or none>
```

For LIGHT reviews, omit empty sections and keep the report short. For DEEP reviews, include the evidence needed to reproduce each material finding and the relevant infrastructure/security analysis.
