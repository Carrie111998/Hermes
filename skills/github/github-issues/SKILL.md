---
name: github-issues
description: "Create, triage, label, assign GitHub issues via gh or REST."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Issues, Project-Management, Bug-Tracking, Triage]
    related_skills: [github-auth, github-pr-workflow]
---

# GitHub Issues Management

Create, search, triage, and manage GitHub issues. Every operation has a `gh` form
and a `curl` fallback; the reference files carry both.

## When to Use This Skill

Use this skill when the user wants to:

- List, search, or view issues (by label, assignee, state, milestone)
- File a new issue — bug report or feature request
- Add or remove labels, assign or unassign people, set a milestone
- Comment on an issue
- Close or reopen an issue, or link an issue to a PR / branch
- Triage a backlog (categorize, label, prioritize `needs-triage` issues)
- Run a bulk operation across many issues

Prerequisites: authenticated with GitHub (see `github-auth`), and either inside a
git repo with a GitHub remote or with the repo specified explicitly.

Related skills: `github-auth`, `github-pr-workflow` (branch → PR → merge),
`github-code-review`.

Note: the REST Issues API also returns pull requests. When parsing, skip items that
have a `pull_request` key.

## Red Lines (never bury these in a reference)

**Destructive / irreversible-in-practice operations — confirm with the user first,
naming the exact issue numbers:**

- Bulk closing, bulk labelling, or bulk commenting. Always do a dry run that prints
  the affected issue numbers, show it to the user, and only then run the mutating
  pass.
- Closing an issue you did not open, or closing as `not_planned` — that is a
  maintainer's judgement call.
- Deleting or renaming repo-wide labels or milestones; this changes the project's
  conventions for everyone.
- Editing or deleting other people's issue bodies and comments.

**Comments and issues are public and permanent:** anything you post is attributed to
the user and visible to the world. Never post speculation as fact, and never paste
log output, stack traces, internal hostnames, customer data, or file contents into
an issue without checking them first.

**Auth and token handling:**

- Get credentials from the `github-auth` helper:
  `source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"`
- Never echo, log, or commit `$GITHUB_TOKEN`, and never paste a token into an issue
  title, body, or comment.
- If no token is available, stop and route the user to `github-auth` — do not guess
  or ask for a token in plaintext.

**Never commit secrets:** if you create a branch from an issue and make changes,
check that no `.env`, key file, or credential is being staged. A secret found in an
issue or a diff must be reported and rotated, not just deleted.

**Human approval before merge:** this skill never merges anything. Closing an issue
via a PR merge happens in `github-pr-workflow`, and merging always needs the user's
explicit go-ahead.

## Minimal End-to-End Skeleton

File a bug, work it, close it:

```bash
# 0. Auth + env
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"

# 1. Check it isn't already filed
gh issue list --search "login redirect" --state all

# 2. File it (body from a template file avoids shell-quoting pain)
gh issue create --title "Login redirect ignores ?next= parameter" \
  --body-file templates/bug-report.md --label "bug"

# 3. Take it and branch from it
gh issue edit 42 --add-assignee @me
gh issue develop 42 --checkout

# 4. Fix, then link the fix — "Closes #42" in the PR body auto-closes on merge
#    (see github-pr-workflow)

# 5. Or close directly with a reason
gh issue close 42 --reason completed
```

Without `gh`: `GET /repos/{owner}/{repo}/issues` → `POST .../issues` →
`POST .../issues/42/assignees` → `PATCH .../issues/42`. Full commands in the
references below.

## Routing Table

| To do this | Read |
|---|---|
| Any concrete `gh`/`curl` command: env setup, list/search/view, create, labels, assignees, comments, close/reopen, link to PRs, milestones, bulk operations, plus list query params, issue body fields, and the gh↔REST quick reference table | `references/issue-commands.md` |
| Run a triage pass: find untriaged issues, categorization label table, priority heuristics, and how to report the pass back to the user | `references/triage-workflow.md` |
| Fill in a bug report body | `templates/bug-report.md` |
| Fill in a feature request body | `templates/feature-request.md` |
