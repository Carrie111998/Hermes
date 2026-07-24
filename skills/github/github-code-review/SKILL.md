---
name: github-code-review
description: "Review PRs: diffs, inline comments via gh or REST."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Code-Review, Pull-Requests, Git, Quality]
    related_skills: [github-auth, github-pr-workflow]
---

# GitHub Code Review

Review local changes before pushing, or review open PRs on GitHub. Most of this
skill is plain `git` — the `gh`/`curl` split only matters for PR-level interactions.

## When to Use This Skill

Use this skill when the user asks to:

- "Review the code" / "check this before I push" — review of uncommitted or
  unpushed local changes
- "Review PR #N" / "look at this PR" / pastes a PR URL
- Leave a general or inline comment on a PR
- Submit a formal review verdict (approve / request changes / comment)
- Apply a systematic quality/security checklist to a diff

Do **not** use this skill to create, update, or merge PRs — that is
`github-pr-workflow`. Authentication setup is `github-auth`.

Prerequisites: authenticated with GitHub (`github-auth`), and inside a git repo.

## Red Lines (never bury these in a reference)

**Human approval before merge — absolute:**

- This skill never merges a PR. Reviewing is not merging. Even after `--approve`,
  the merge decision belongs to a human; do not run `gh pr merge` here.
- Approving a PR is a real, attributable action taken as the user. Never
  `--approve` unless the user explicitly asked you to approve. When in doubt, use
  `--comment` and report findings back to the user instead.
- Never post a review verdict, comment, or inline comment to GitHub without the
  user having asked you to post to GitHub. Default to reporting findings in chat.

**Destructive operations:**

- Never `git push`, `git push --force`, `git commit --amend`, or rebase while on a
  checked-out PR branch that belongs to someone else. Review is read-only.
- The only branch you may delete is the local `pr-<N>` scratch branch you created
  yourself (`git branch -D pr-<N>`). Never delete remote branches from this skill.
- Never `git checkout`/`git stash` over uncommitted user work — check
  `git status` first and stop if the tree is dirty.

**Auth and token handling:**

- Obtain credentials via
  `source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"`.
- Never echo, log, or commit `$GITHUB_TOKEN`. Never paste it into a PR comment,
  review body, or issue.
- If no token is available, stop and route the user to `github-auth`.

**Never commit secrets — and flag them when found:**

- Scan every diff you review for credentials (`password`, `secret`, `api_key`,
  `token =`, `private_key`, `.env` files, key material). Any hit is a **Critical**
  finding, not a suggestion.
- If a secret is already committed in the diff, tell the user it must be rotated —
  removing the line is not sufficient. Do not paste the secret value into a GitHub
  comment.

## Minimal End-to-End Skeleton

Local pre-push review:

```bash
git diff main...HEAD --stat     # 1. scope
git diff main...HEAD            # 2. read the full diff
# 3. read_file on changed files for surrounding context
# 4. apply references/review-checklist.md
# 5. report Critical / Warnings / Suggestions / Looks Good to the user
```

PR review:

```bash
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"

PR_NUMBER=123
gh pr view $PR_NUMBER                    # 1. context: title, author, description
gh pr diff $PR_NUMBER --name-only        #    scope
gh pr checkout $PR_NUMBER                # 2. or: git fetch origin pull/N/head:pr-N
# 3. read the diff + files, run tests/linters locally
# 4. apply references/review-checklist.md
# 5. report findings to the user; post only if they asked you to:
#    gh pr review $PR_NUMBER --comment --body "..."   (approve only on request)
git checkout main && git branch -D pr-$PR_NUMBER   # 6. clean up local scratch branch
```

## Routing Table

| To do this | Read |
|---|---|
| Review uncommitted/unpushed local changes: diff commands, grep heuristics for debug statements, big files, secrets and conflict markers, the pre-push workflow, and checking out a PR branch locally | `references/local-diff-review.md` |
| Interact with a PR via `gh`/`curl`: env setup, fetch PR metadata and changed files, post general comments, post inline comments (field table), submit a formal review with multiple atomic comments, post a summary comment | `references/pr-review-commands.md` |
| Work through the review categories — correctness, security, code quality, testing, performance, documentation — plus running tests/linters and mapping findings to a verdict | `references/review-checklist.md` |
| Format the review output: summary comment structure, severity icons and blocking rules, verdict wording, inline comment phrasing, local-review example | `references/review-output-template.md` |
