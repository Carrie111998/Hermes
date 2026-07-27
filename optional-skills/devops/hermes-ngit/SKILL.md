---
name: hermes-ngit
description: "Use this skill when working with Nostr-based Git repositories (ngit / gitworkshop.dev) for decentralized, permissionless code hosting — `ngit init`, `ngit sync`, `ngit send`, `ngit issue`, `ngit pr`, and native `git push`/`git clone` against nostr:// remotes."
version: 1.0.1
author: Joey Stanford
license: MIT
compatibility: "ngit installed (gitworkshop.dev/ngit) with git-remote-nostr on PATH; an identity configured via `git config --global nostr.nsec` or a bunker reference. No daemon required. Optional: `gh` CLI for GitHub mirroring."
triggers:
  - ngit
  - nostr git
  - gitworkshop
  - nostr:// remote
  - ngit init
  - ngit sync
  - ngit send
  - ngit issue
  - ngit pr
metadata:
  hermes:
    tags: [Nostr, Git, ngit, Decentralized, gitworkshop]
    related_skills: [hermes-radicle]
---

# ngit Skill

Use when working with Nostr-based Git repositories (`ngit` / gitworkshop.dev) for
decentralized, permissionless code hosting over Nostr.

## Requirements

- `ngit` installed (https://gitworkshop.dev/ngit) — includes the `git-remote-nostr` helper.
- `git-remote-nostr` on PATH (so `git push`/`git clone` work with `nostr://` URLs).
- An identity configured. Either:
  - raw nsec: `git config --global nostr.nsec "<nsec1...>"`, or
  - a bunker reference (preferred when available): `nostrsec://...` — keeps the private key off disk.
- Optional: `gh` CLI for GitHub mirroring workflows.

**No daemon required.** Unlike Radicle, ngit/relays need no local node running.

## This Repository

- **GitHub:** https://github.com/rinchen/hermes-ngit
- **Nostr:** published via `ngit init`; view on gitworkshop.dev under your npub.
- **Radicle RID:** set after `rad init` (see Multi-Mirror below).
- **Triple Push:** `origin` pushurls = GitHub + Radicle + Nostr, so `git push` updates all three.

## How ngit Works (mental model)

- `ngit init` **announces** the repo to Nostr: publishes the NIP-34 repository event
  (pointing at current HEAD) and sets the `nostr://` origin URL. Run once, or re-run
  to re-point the announcement at a new HEAD.
- `ngit sync` only pushes **git refs** to the grasp git servers. It does **NOT** update
  the NIP-34 announcement that gitworkshop.dev reads — so gitworkshop can show a stale
  commit if you only sync.
- The correct, native way to keep gitworkshop current is to add the `nostr://` URL as a
  **pushurl** on `origin`, so a normal `git push` drives `git-remote-nostr`, which updates
  the announcement on every push. No `ngit init` per push, no hook.
- `ngit send` **opens a PR/proposal** (commits -> proposal). Do NOT run it on every push.

## ⚠️ GOTCHA: `ngit init` rewrites the `origin` remote

`ngit init` sets `remote.origin.url` to the `nostr://` URL and **drops any
existing `remote.origin.pushurl` entries** (it replaced our GitHub pushurl on this
very repo). So:

- **Always capture GitHub (or other) pushurls BEFORE running `ngit init`.**
- After `ngit init`, re-add every pushurl you need, in this order:
  1. FIRST `git remote set-url --push origin <github-url>` (restores the primary push target)
  2. THEN `git remote set-url --add --push origin <nostr://url>` (appends Nostr)
  3. Then add Radicle if present: `git remote set-url --add --push origin $(git remote get-url --push rad)`

Doing `set-url --add --push` before `set-url --push` leaves `origin` with only the
Nostr pushurl and no GitHub pushurl — exactly the bug we hit.

Correct sequence for a repo that already has a GitHub `origin`:

```bash
# 1. capture existing GitHub pushurl (BEFORE ngit init rewrites origin)
GH_PUSH=$(git remote get-url --push origin 2>/dev/null | grep -v '^nostr://' | head -1)

# 2. announce to Nostr (rewrites origin.url -> nostr://, drops pushurls)
ngit init -d --name <repo-name>

# 3. restore GitHub pushurl FIRST, then append Nostr
[ -n "$GH_PUSH" ] && git remote set-url --push origin "$GH_PUSH"
git remote set-url --add --push origin "$(git remote get-url origin)"

# 4. (optional) add Radicle if the repo is rad-enabled
[ -n "$(git remote get-url --push rad 2>/dev/null)" ] && \
  git remote set-url --add --push origin "$(git remote get-url --push rad)"

git push   # now updates GitHub + Nostr (+ Radicle) natively
```

**Caveat:** git stops on the first failed pushurl. If Nostr relays are unreachable, the
whole `git push` (including GitHub) can abort. If that bites you, drop the `nostr://`
pushurl and run `ngit sync` as a separate step instead.

## Core Commands

### Announce a Repo to Nostr

```bash
ngit init -d --name <repo-name>
```

Publishes the NIP-34 event and sets `nostr://npub.../relay.ngit.dev/<repo-name>` as the
origin URL. `-d` = non-interactive (uses configured nsec). Re-run any time to re-point
HEAD at the latest commit. **Remember the remote rewrite gotcha above.**

### Make `git push` Also Publish to Nostr

```bash
# nostr:// URL is now origin.url after ngit init; append it as a pushurl
# (after restoring any GitHub pushurl first — see gotcha)
git remote set-url --add --push origin "$(git remote get-url origin)"
```

Now `git push` updates GitHub, Radicle (if present), **and** Nostr. All three stay in sync
natively. (This is exactly what `~/repos/ngit-enable-repo/ngit-enable-repo.sh` automates,
and that script already captures the GitHub-pushurl-before-ngit-init ordering.)

### Clone a Nostr Repo

```bash
git clone nostr://npub.../relay.ngit.dev/<repo-name>
cd <repo>
git remote -v   # shows nostr:// origin
```

### Push a Branch / PR via ngit

```bash
# Simple: push a branch with the pr/ prefix
git push -o 'title=My PR' -o 'description=details' -u origin pr/my-branch

# Advanced: ngit send (commits -> proposal)
ngit send -d --subject "My change" HEAD~2
```

### List / Create Issues

```bash
ngit issue list --json -d          # open issues (JSON)
ngit issue list                     # human-readable
ngit issue create --title "..." --description "..."   # if supported
```

### List / Create PRs

```bash
ngit pr list --json -d              # open + draft proposals
ngit pr list
```

### Sync Git Refs to Grasp Servers

```bash
ngit sync -d
```

Updates the git servers (relay.ngit.dev, gitnostr.com) to match nostr state. Does not
re-announce HEAD — use `ngit init` (or the pushurl approach) for that.

## Session Repo Detection

Whenever the user opens a chat inside a Git repository, detect Nostr (ngit) remotes:

```bash
git remote -v 2>/dev/null | grep '^origin' | grep 'nostr://'
```

If a `nostr://` origin URL is present, announce the repo is Nostr-enabled and show the
gitworkshop link derived from it:

```
https://gitworkshop.dev/<nostr-url-without-scheme>
```

## Nostr Links Format

When sharing repo locations, use markdown with GitHub + Radicle + Nostr URLs:

- GitHub: https://github.com/rinchen/hermes-ngit
- Radicle: `rad:<rid>`
- Nostr: https://gitworkshop.dev/npub1.../relay.ngit.dev/hermes-ngit

## Multi-Mirror Workflow (ngit-enable-repo)

Use `~/repos/ngit-enable-repo/ngit-enable-repo.sh` to announce a repo to Nostr and add the
`nostr://` pushurl in one shot. Run it from inside an existing Git repo (with a GitHub
`origin`) after `ngit init` / `rad init`. It already captures the GitHub pushurl before
`ngit init` rewrites the remote, so the ordering bug above is handled for you.

**Current Configuration:** this repo (hermes-ngit) is configured with triple push to
GitHub + Radicle + Nostr via `origin` pushurls. `git push` updates all three.

## Rationale

ngit provides a permissionless, Nostr-native Git hosting layer with no central forge and
no daemon. Use it for:
- Censorship-resistant mirrors
- Distributed hosting (multiple grasp git servers listed in the announcement = redundancy)
- Keeping GitHub for CI/PR convenience while owning a sovereign copy

**Key safety:** never store a raw nsec inside a repository (committed file or repo-local
git config). Keep the nsec in **global** git config only, or use a bunker
(`nostrsec://`). If a bunker relay is down, a raw global nsec is the fallback — but it
must never be copied into per-repo config or committed.
