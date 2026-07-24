---
name: github-repo-management
description: "Clone/create/fork repos; manage remotes, releases."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Repositories, Git, Releases, Secrets, Configuration]
    related_skills: [github-auth, github-pr-workflow, github-issues]
---

# GitHub Repository Management

Create, clone, fork, configure, and manage GitHub repositories. Every operation has
a `gh` form and a `git` + `curl` fallback; the reference files carry both.

## When to Use This Skill

Use this skill when the user wants to:

- Clone, create, or fork a repository (including from a template)
- Push an existing local directory to a new GitHub repo
- Keep a fork in sync with upstream
- Look up repo info, list their repos, or search GitHub repos
- Change repo settings (description, visibility, topics, default branch, wiki/issues)
- Configure branch protection
- Manage GitHub Actions secrets
- Create, list, or download releases and release assets
- List/trigger/re-run GitHub Actions workflows, or read run logs
- Create or list gists

Related skills: `github-auth` (getting authenticated), `github-pr-workflow`
(branch → PR → merge), `github-issues`, `github-code-review`.

## Red Lines (never bury these in a reference)

**Destructive operations — always ask the user for explicit confirmation first,
naming the exact target, and never infer approval from context:**

- `gh repo delete` / `DELETE /repos/{owner}/{repo}` — repository deletion is
  irreversible. Never run it unless the user names the repo and says to delete it.
- `git push --force` / `--force-with-lease` to any shared branch, and any history
  rewrite (`git reset --hard` on a pushed branch, `git filter-branch`).
- Deleting branches (`git push origin --delete`, `gh repo edit --default-branch`
  away from an in-use branch), releases, tags, or Actions secrets.
- Removing branch protection (`DELETE .../branches/{branch}/protection`).
- Flipping repo visibility from private to public — this exposes all history.

**Auth and token handling:**

- Get credentials from the `github-auth` skill's helper:
  `source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"`
- Never print, echo, log, or commit `$GITHUB_TOKEN` or any secret value. Never
  paste a token into a repo URL that gets committed, or into an issue/PR/gist.
- Never write secrets into files under the repo. Use `gh secret set` (or the
  encrypted REST flow) so the value never touches the working tree.
- If no token is available, stop and route the user to `github-auth` — do not
  invent, guess, or prompt for a token in plaintext.

**Never commit secrets:** before any `git add`/`git commit` in this skill, check
that no `.env`, key file, or credential-bearing file is being staged. If the user
asks to commit one, refuse and explain.

**Human approval before merge:** this skill never merges PRs. Merging belongs to
`github-pr-workflow`, and requires the user's explicit go-ahead.

## Minimal End-to-End Skeleton

Create a repo from an existing local directory and cut a first release:

```bash
# 0. Auth + env
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"

# 1. Create the remote repo from the current directory and push
gh repo create my-project --private --source . --push

# 2. Confirm what was created
gh repo view --web=false

# 3. Set metadata
gh repo edit --description "What this does" --add-topic "cli,automation"

# 4. Cut a release (draft first if the user hasn't confirmed publishing)
gh release create v0.1.0 --title "v0.1.0" --generate-notes
```

Without `gh`, the same four steps are `POST /user/repos` → `git remote add` +
`git push -u origin main` → `PATCH /repos/{owner}/{repo}` →
`POST /repos/{owner}/{repo}/releases`. Full commands in the references below.

## Routing Table

| To do this | Read |
|---|---|
| Clone / create / fork a repo, create from template, sync a fork, look up or search repos, edit repo settings or topics, set branch protection, or set up `$GITHUB_TOKEN` / `$OWNER` / `$REPO` inline | `references/repo-lifecycle-commands.md` |
| Create/list/download releases, upload release assets, list or trigger or re-run Actions workflows, download run logs, create or list gists | `references/releases-actions-gists.md` |
| Set, list, or delete GitHub Actions secrets (`gh` and the encrypted REST flow) | `references/secrets-management.md` |
| Look up a REST endpoint, method, request-body shape, pagination, or rate limits | `references/github-api-cheatsheet.md` |
