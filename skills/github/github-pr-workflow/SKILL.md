---
name: github-pr-workflow
description: "GitHub PR lifecycle: branch, commit, open, CI, merge."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Pull-Requests, CI/CD, Git, Automation, Merge]
    related_skills: [github-auth, github-code-review]
---

# GitHub Pull Request Workflow

Manage the full PR lifecycle: branch → commit → push → open PR → CI → merge. Every
step has a `gh` form and a `git` + `curl` fallback; the reference files carry both.

## When to Use This Skill

Use this skill when the user wants to:

- Start a branch for a change, or commit work with a conventional commit message
- Push a branch and open a pull request (including drafts, reviewers, labels)
- Check or watch CI status on a branch or PR
- Diagnose a CI failure and iterate until checks are green
- Merge a PR, enable auto-merge, or clean up the branch afterwards
- List, comment on, request review on, or close a PR

Prerequisites: authenticated with GitHub (see `github-auth`), and inside a git
repository with a GitHub remote.

Related skills: `github-auth`, `github-code-review` (reviewing a PR's contents),
`github-issues` (linking `Closes #N`).

## Red Lines (never bury these in a reference)

**Human approval before merge — absolute:**

- Never run `gh pr merge`, `PUT /pulls/{n}/merge`, or enable auto-merge unless the
  user has explicitly told you to merge *this* PR. "Open a PR and get CI green" is
  not approval to merge.
- Never merge with CI red, with unresolved review comments, or with a
  `REQUEST_CHANGES` review outstanding. Report the state and stop.
- Never merge a PR you authored on the user's behalf into a protected or default
  branch without the user saying so, even if you have permission.

**Destructive operations — confirm first, naming the exact target:**

- `git push --force` / `--force-with-lease` to a shared branch, `git commit --amend`
  or `git rebase` on already-pushed commits, `git reset --hard` on a pushed branch.
- Deleting branches: `git push origin --delete <branch>`, `gh pr merge
  --delete-branch`, `git branch -D`. Only delete a branch that was just merged, and
  only when the user asked for cleanup.
- Closing someone else's PR, or force-pushing to a PR branch you do not own.
- Check `git status` before any checkout or stash — never discard uncommitted user
  work.

**Auth and token handling:**

- Get credentials from the `github-auth` helper:
  `source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"`
- Never echo, log, or commit `$GITHUB_TOKEN`. Never put it in a remote URL that gets
  committed, or in a PR title, body, or comment.
- Never paste CI log excerpts into a PR comment without scanning them for tokens and
  credentials first.
- If no token is available, stop and route the user to `github-auth`.

**Never commit secrets:** before every `git add`/`git commit`, verify no `.env`,
key file, or credential-bearing file is staged. If a secret was already pushed,
tell the user it must be rotated — removing the line is not enough.

**Test integrity:** when fixing CI, fix the production code. Never weaken an
assertion, mark a test skipped, or edit test infrastructure to make a check pass.
After 3 failed fix attempts on the same failure, stop and ask the user.

## Minimal End-to-End Skeleton

```bash
# 0. Auth + env
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"

# 1. Start from clean main
git status                                   # stop if the tree is dirty
git checkout main && git pull origin main

# 2. Branch
git checkout -b fix/login-redirect-bug

# 3. (Agent makes code changes with file tools)

# 4. Commit — conventional commit message
git add src/auth/login.py tests/test_login.py
git commit -m "fix: correct redirect URL after login

Preserves the ?next= parameter instead of always redirecting to /dashboard."

# 5. Push
git push -u origin HEAD

# 6. Open the PR
gh pr create --title "fix: correct redirect URL after login" \
  --body-file templates/pr-body-bugfix.md

# 7. Watch CI; fix and re-push until green
gh pr checks --watch

# 8. Report the green PR to the user and STOP.
#    Merge only after they explicitly say to:
#    gh pr merge --squash --delete-branch
```

Without `gh`: `git push -u origin HEAD` → `POST /repos/{owner}/{repo}/pulls` →
poll `GET /repos/{owner}/{repo}/commits/{sha}/status` →
`PUT /repos/{owner}/{repo}/pulls/{n}/merge`. Full commands in the references below.

## Routing Table

| To do this | Read |
|---|---|
| Any concrete `gh`/`curl` command: env setup and owner/repo extraction, branch creation and naming, committing, pushing, creating a PR (body fields, draft, response fields), checking and polling CI status, downloading failure logs, the auto-fix loop, merging, auto-merge via GraphQL, plus the gh↔REST quick reference table | `references/pr-commands.md` |
| Diagnose a specific CI failure — log signatures and fixes for test, lint, type-check, build, permission, timeout and Docker failures, plus the auto-fix decision tree | `references/ci-troubleshooting.md` |
| Write the commit message: type table with examples, scopes, breaking-change syntax, multi-line bodies, `Closes`/`Fixes`/`Refs` footers, quick decision guide | `references/conventional-commits.md` |
| Fill in a feature PR body | `templates/pr-body-feature.md` |
| Fill in a bugfix PR body | `templates/pr-body-bugfix.md` |
