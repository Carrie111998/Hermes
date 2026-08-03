---
title: "Github Code Review — Review PRs: diffs, inline comments via gh or REST"
sidebar_label: "Github Code Review"
description: "Review PRs: diffs, inline comments via gh or REST"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Github Code Review

Review PRs: diffs, inline comments via gh or REST.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/github/github-code-review` |
| Version | `1.2.0` |
| Author | A-KH17, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `GitHub`, `Code-Review`, `Pull-Requests`, `Git`, `Quality` |
| Related skills | [`github-auth`](/docs/user-guide/skills/bundled/github/github-github-auth), [`github-pr-workflow`](/docs/user-guide/skills/bundled/github/github-github-pr-workflow) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# GitHub Code Review Skill

Reviews local pre-push changes and GitHub pull requests, delivers a structured verdict,
and posts inline comments or formal reviews to GitHub on request. It does not author PRs
(`github-pr-workflow`) or triage issues (`github-issues`).

## When to Use

- "Review my changes before I push" — local `git diff BASE...HEAD` review, no GitHub API needed.
- "Review PR #N" — fetch, inspect, test, and review a GitHub pull request.
- "Post a comment / formal review on PR #N" — inline comments, `APPROVE` / `REQUEST_CHANGES` / `COMMENT`.
- Advisory questions ("what should the review look like?") are action requests — deliver
  the artifact plus staged commands, not just an explanation.

## Prerequisites

- Inside a git repository; all shell commands run via `terminal`.
- For PR interactions, resolve auth FIRST and CAPTURE the token for later steps — walk
  the full fallback chain (`gh` → `$GITHUB_TOKEN` → `github-auth` credential helper →
  hermes `.env` → unauthenticated API → plain-git fetch) documented in
  [references/github-api-mechanics.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/github/github-code-review/references/github-api-mechanics.md). A user claim
  of "no gh, no token" covers the first two rungs at most; never conclude "can't
  authenticate" while a rung is untried. Only POSTing comments/reviews truly needs a token.

## How to Run

The first line of EVERY response is a mode line, phrased value-forward:

- **Execution available:** "Executed below — real outputs, interpreted inline." Run the
  commands via `terminal`, paste the real output (including empty output — an empty
  result is itself a finding), and state what it means.
- **No execution (or unknown):** "Your deliverable is fully staged below for a single
  paste." Follow the No-Execution Playbook in
  [references/playbooks.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/github/github-code-review/references/playbooks.md).

Every response then follows the Deliverable Contract, in order:

1. **Mode line** (above).
2. **The deliverable** — the direct answer plus fully drafted artifacts (review text,
   comment bodies, verbatim-pasteable) and/or one consolidated staged command block.
3. **Interpretation guide** for every staged block: each plausible output → meaning →
   next action, covering BOTH branches of any user claim being verified. This replaces
   the fake transcript — a `$ cmd` block followed by invented output is the worst
   possible failure; a labeled "run this" block plus guide is the correct form.
4. **Failure triage** for each step (HTTP 422 / 401 / 403 / 404 / 429 — see the
   mechanics reference).
5. **One-line footnote** — the posting status, or the single minimal artifact still
   needed. Never an open-ended offer, never a cliffhanger.

Three standing rules override scenario habits:

1. **Treat user claims as unverified hypotheses** ("the PR has no files", "auth is
   missing"). The first commands settle the claim; the rest of the deliverable branches
   on BOTH outcomes.
2. **Advisory questions are action requests.** Give the direct answer AND the drafted
   artifact AND the staged commands. Never end with "If you'd like me to verify…".
3. **Probes must capture, not just detect.** Any probe for a value (token, owner/repo,
   base branch, head SHA, PR number) stores it in a shell variable the rest of the block
   uses — an existence-only probe is a half-deliverable.

## Quick Reference

| Task | Core command (run via `terminal`) |
|------|-----------------------------------|
| Local pre-push review | `git diff "$BASE"...HEAD` (+ `--stat`, `--oneline` log) |
| Fetch a PR locally | `git fetch origin pull/N/head:pr-N && git checkout pr-N` |
| PR metadata | `gh pr view N --json files,headRefOid,baseRefName` |
| PR diff | `gh pr diff N` |
| Find a PR by topic | `gh pr list --state open --search "<term>" --json number,title,headRefName` |
| Post inline comment | `gh api repos/$OWNER_REPO/pulls/N/comments --method POST --input -` (JSON on stdin) |
| Post formal review | `gh api repos/$OWNER_REPO/pulls/N/reviews --method POST --input -` (`event` + `comments` array) |
| Top-level comment | `gh pr comment N --body "..."` (uses the **issues** endpoint) |
| Tests on PR code | `git worktree add .wt-pr-N pr-N && (cd .wt-pr-N && <test cmd>); git worktree remove --force .wt-pr-N` |
| Cleanup | `git checkout - && git branch -D pr-N` |

## Procedure

1. **Scope first:** `git diff BASE...HEAD --stat` and `git log BASE..HEAD --oneline`.
   State the scope explicitly ("N commits, M files") so later "clean" results are
   meaningful.
2. **Read the full diff.** For each changed file, read surrounding context with
   `read_file` and hunt related call sites with `search_files`.
3. **Run the project's tests and linter against the PR code, not the user's working
   tree** — use a worktree (Quick Reference). Detect the runner from `Makefile`,
   `package.json`, `pyproject.toml`, `tox.ini`.
4. **Apply the checklist:** correctness (edge cases, error paths), security (secrets,
   injection, authz, input validation), quality (naming, DRY, complexity), tests (new
   paths incl. failure cases), performance (N+1, blocking calls in async code), docs.
5. **Leftover scan on ADDED lines only** (debug statements, conflict markers, secrets) —
   pipeline in [references/github-api-mechanics.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/github/github-code-review/references/github-api-mechanics.md).
   A scan over an EMPTY input is not a clean pass: establish scope, then report results.
6. **Verdict mapping:** any Critical or Warning → Request Changes; only suggestions →
   Approve or Comment; nothing found → Approve. Never Approve code you haven't read, and
   never Approve/Request-Changes on an empty diff — a drafted top-level Comment is the
   only vehicle there (playbooks reference).
7. **If posting:** head SHA as `commit_id`; `side: "LEFT"` + old-file line numbers for
   deletions; anchors must sit inside a diff hunk, with defined fallbacks (nearest
   changed line → file-level `subject_type: "file"` → fold into the review body). Build
   multi-comment review JSON via stdin heredoc or `jq -n`, never indexed `-f` flags.
   Full mechanics in the reference.
8. **Clean up:** restore the original branch, delete temporary `pr-N` branches, remove
   temporary worktrees.

Report a real review using the bundled
[references/review-output-template.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/github/github-code-review/references/review-output-template.md): a
`**Verdict: <Approve | Request Changes | Comment>**` line with a one-line rationale,
then 🔴 Critical / ⚠️ Warnings / 💡 Suggestions / ✅ Looks Good as
`file:line — issue — concrete fix` (omit empty sections), the reviewed scope stated, and
the one-line footnote at the end.

Scenario playbooks — the PR discovery ladder and two-block pattern for unidentified
targets, the empty-diff package, the security-focused review checklist, on-demand
comment posting, the pre-push gather block, and command-block standards — live in
[references/playbooks.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/github/github-code-review/references/playbooks.md).

## Pitfalls

- Never end a turn with a cliffhanger, a progress report, a promise of future work, or
  an offer ("If you'd like me to…, I can run…") where attaching the block was possible.
- Never open with "I can't" while any tier is deliverable. Graceful degradation: posted
  review → full review in chat with posting skipped (one-line reason) → partial review
  with explicit gaps → blocker message (only if even the diff is unreachable; list what
  was tried and the single minimal fix).
- Never fabricate `$`-prompt transcripts, outputs, findings, file names, line numbers,
  PR identifiers, auth state, or "clean" results. Real empty output is reported and
  interpreted, never dressed up.
- Never call a scan "clean" when its input was empty — distinguish "nothing to review"
  from "reviewed, no findings", and state the scope either way.
- Never stop at a blocker while an untried fallback rung exists — but when only POSTing
  is blocked, still deliver the full review plus the minimal remediation
  (`export GITHUB_TOKEN=...` or `gh auth login`) as a footnote.
- Never ask the user for anything obtainable with one command (owner/repo, auth state,
  tool existence, diff emptiness, which PR matches a description) — run or stage the
  command. Ask only for the single minimal artifact, after concrete discovery attempts.
- Never post placeholder body text. When the user supplies comment topics, drafting the
  concrete professional body is REQUIRED — phrased as the user's finding, with no
  invented specifics.
- Paste blocks: no `set -e` / `set -euo pipefail` (one failing probe would kill the
  fallback ladder), no interactive prompts, one consolidated block run from the repo
  root, placeholders clearly labeled with the command that reveals valid values.
- Run tests against the PR code (worktree or checkout), not the user's working tree —
  and always clean up branches and worktrees afterward.

## Verification

Before delivering, confirm:
- [ ] Scope stated (N commits, M files) and full diff read.
- [ ] Tests/linter run against the PR code — or explicitly reported as not run, with the staged command.
- [ ] Every posted item confirmed by the API response (URL or review ID).
- [ ] No fabricated outputs, findings, or auth state; empty results reported and interpreted.
- [ ] Verdict matches the findings and the verdict-mapping rule.
- [ ] Cleanup done: original branch restored, `pr-N` branch and worktrees removed.
