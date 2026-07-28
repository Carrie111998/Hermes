---
name: hermes-ngit
description: "Use this skill when working with Nostr-based Git repositories (ngit / gitworkshop.dev) for decentralized, permissionless code hosting — `ngit init`, `ngit sync`, `ngit send`, `ngit issue`, `ngit pr`, and native `git push`/`git clone` against nostr:// remotes."
version: 1.0.1
author: Joey Stanford
license: CC-BY-SA-4.0
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

**Credit / Attribution.** This skill is built on **Dan Conway's** ngit command
reference (`DanConwayDev/ngit-cli`, `skills/ngit/SKILL.md`), incorporated under the
**CC-BY-SA-4.0** share-alike license. Dan's work supplies the command reference, PR/issues
lifecycle, account management, and flag tables below. Joey Stanford added the multi-mirror
packaging, the `ngit init` remote-rewrite gotcha, and session repo detection. Attribution is
also recorded in the `NOTICE` file.

## Requirements

- `ngit` installed (https://gitworkshop.dev/ngit — `curl -Ls https://ngit.dev/install.sh | bash`
  installs both `ngit` and `git-remote-nostr`).
- `git-remote-nostr` on PATH (so `git push`/`git clone` work with `nostr://` URLs).
- An identity configured. Either:
  - raw nsec: `git config --global nostr.nsec "<nsec1...>"`, or
  - a bunker reference (preferred when available): `nostrsec://...` / `--bunker-url bunker://...`
    — keeps the private key off disk.
- Optional: `gh` CLI for GitHub mirroring workflows.

**No daemon required.** Unlike Radicle, ngit/relays need no local node running.

## This Repository

- **GitHub:** https://github.com/rinchen/hermes-ngit
- **Nostr:** published via `ngit init`; view on https://gitworkshop.dev/npub1wwaq5gyk7yljly3cwl3wleuk79nz63ukpp2a6lq5x4q9s9r4nrgqjk3dlv/relay.ngit.dev/hermes-ngit
- **Radicle:** `rad:z2fGx8T85zCue2efEmSHL8NxKCvqd/z6MkncGT6ghwtmF9utKMvuqxtK93WHj56dB3GmrDyFQoFWcj`
- **Triple Push:** `origin` pushurls = GitHub + Radicle + Nostr, so `git push` updates all three.

## How ngit Works (mental model)

Nostr is a decentralised protocol where users publish signed events to relays (simple servers
anyone can run). There is no central authority — identity is a keypair, and data is replicated
across many relays.

Git has two distinct layers that ngit separates:

- **Git state (refs)** — which commit each branch/tag points to — is published as signed events
  on Nostr relays. This is the source of truth for the repository.
- **Git data (objects)** — the actual commits, trees, and blobs — is stored on ordinary git
  servers (any server that speaks the git protocol).

When you `git fetch`, `git-remote-nostr` reads the current ref state from Nostr relays, then
fetches the corresponding objects from the git server(s) listed in the repository announcement.
Because the state lives on Nostr and the data can live anywhere, git servers are interchangeable.

**Grasp servers** combine a Nostr relay and a git server into one hosted service
(e.g. `relay.ngit.dev`). When `ngit init` publishes an announcement listing a grasp server, the
grasp server automatically creates the git repository — no prior setup or account needed.

- `ngit init` **announces** the repo to Nostr: publishes the NIP-34 repository event (pointing at
  current HEAD) and sets the `nostr://` origin URL. Run once, or re-run to re-point the
  announcement at a new HEAD.
- `ngit sync` only pushes **git refs** to the grasp git servers. It does **NOT** update the
  NIP-34 announcement that gitworkshop.dev reads — so gitworkshop can show a stale commit if you
  only sync.
- The correct, native way to keep gitworkshop current is to add the `nostr://` URL as a
  **pushurl** on `origin`, so a normal `git push` drives `git-remote-nostr`, which updates the
  announcement on every push. No `ngit init` per push, no hook.
- `ngit send` **opens a PR/proposal** (commits -> proposal). Do NOT run it on every push.

## Key Rules

- **`pr/` prefix is MANDATORY for PRs** — branch names for pull requests MUST start with `pr/`
  (e.g. `pr/my-feature`). A branch without this prefix is a plain git push and will never create a PR.
- **Always use `--json`** on `ngit` commands when reading output — far easier to parse than
  human-readable text. (`git` commands do not support `--json`.)
- **Use `--offline`** on all but the first `ngit` command in a session — reads from local cache
  instantly. (`git fetch origin` also refreshes the cache.)
- **Never construct NIP-05 addresses** (`user@domain`). Use the `npub1...` form unless a NIP-05
  address was explicitly provided.
- **`<ID|nevent>`** accepts a 64-char hex event ID or a `nevent1...` bech32 string. Get IDs from
  `ngit pr list --json` or `ngit issue list --json`.
- **`--json` output uses `nevent1…` bech32** for all `id` and `reply_to` fields (not raw hex). Use
  these values directly as `<ID|nevent>` arguments and in `nostr:` URI references.
- **Reference other issues/PRs/comments in `--body` using `nostr:` URIs** — e.g.
  `nostr:nevent1abc…` or `nostr:naddr1abc…`. Never paste raw hex IDs into body text. The `id` field
  from `--json` output is already a valid `nevent1…` string; prefix it with `nostr:` to form the URI.

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

PITFALL — triple-push mirrors can silently DIVERGE and `git pull` will NOT warn you.
When `origin` has only a `nostr://` FETCH url (set by `ngit init`) but three PUSH urls
(GitHub + Radicle + Nostr), `git pull` fetches from Nostr only. If GitHub gains a commit
(e.g. a merged PR) your local `main` lacks, `git pull` still reports "Already up to date"
because Nostr already had your tip. The divergence only surfaces as a non-fast-forward on
`git push` to GitHub — while the Nostr/Radicle pushurls print "Everything up-to-date" (they
already had your commit), making it look like only GitHub is the problem.
Diagnosis BEFORE any reset: fetch each remote into a throwaway ref and check ancestry —
`git fetch -q git@github.com:rinchen/<repo>.git +refs/heads/main:refs/remotes/ghprobe/main`,
then `git merge-base --is-ancestor main ghprobe/main` (is local an ancestor of GitHub?) and
the reverse. If NEITHER is an ancestor, the histories diverged (parallel duplicate commits).
Confirm the content is actually identical before assuming a reset is safe:
`git diff <local> <remote> --stat` and compare trees with `git rev-parse <c>^{tree}`.
Resolution: prefer `git merge --no-ff` of the GitHub tip into local. The merge commit descends
from BOTH sides, so a plain `git push` fast-forwards all three mirrors with no force. A
`git reset --hard` to GitHub LOOKS simpler but will NON-fast-forward Nostr/Radicle (they sit at
the local tip, which is not an ancestor of GitHub) — it trades one rejected pushurl for two.
Only force-push if you intend to rewrite GitHub history.

## Links format (radicle-links hub convention)
When adding a repo to `~/repos/radicle-links/README.md` (the hub):
- GitHub cell: `[GitHub](https://github.com/rinchen/<repo>)`
- **Radicle cell uses the SHORT `[rad]` text** linking to `app.radicle.xyz/.../rad:<rid>` — NOT the full `rad:z…` ID as the display text.
- Nostr cell: `[ngit](https://gitworkshop.dev/npub…/relay.ngit.dev/<repo>)`
- All three columns per repo.

PITFALL — recurring correction (broken 3× across sessions, including this one): do NOT write
`[rad:z3Y6…]` as the link text. The visible label must be literally `rad`; the full RID stays
inside the URL: `[rad](https://app.radicle.xyz/nodes/rosa.radicle.network/rad:z3Y6…)`. Apply
this to EVERY row you add or move in the hub README — if a pre-existing row violates it (e.g.
the Hermes Radicle Skill row did), fix that too, not just the new one. Verify with
`grep -cE '\[rad:z' README.md` → expect 0.

## Recommended multi-mirror workflow (kill the blind `git pull`)

The trap above happens because `ngit init` sets `origin` to FETCH from Nostr but PUSH to
GitHub+Radicle+Nostr. PRs merge on **GitHub**, which `git pull` never consults — so divergence
is invisible until `git push` rejects. Fix it by splitting the two directions with plain git
config (no hook, no source edits):

```bash
# 1. Add GitHub as a FETCH-ONLY remote (PRs merge here)
git remote add github git@github.com:rinchen/<repo>.git

# 2. Make `git pull` sync from GitHub (where PRs land)
git config branch.<main>.remote github
git config branch.<main>.merge refs/heads/<main>

# 3. Keep `git push` -> origin (the triple-mirror pushurls)
git config remote.pushDefault origin
git config push.default current   # per-repo: no-arg `git push` always hits origin (the triple mirror)
```

Now after a PR merges: `git pull` pulls it into local directly (FF, or a merge commit if you
also have local commits). No silent "Already up to date", no surprise non-fast-forward.
`git push` still propagates to GitHub+Radicle+Nostr via `origin` pushurls.

Mechanics: `branch.<name>.remote` controls the `git pull` source; `remote.pushDefault` controls
the `git push` target — two independent knobs. `git pull --dry-run` should show `From
github.com:rinchen/<repo>`; `git push --dry-run` should target `origin`.

Optional: scope GitHub's fetch to the main branch only (otherwise the first `git pull` registers
all upstream branches as `github/*` tracking refs — harmless but noisy):
```bash
git config remote.github.fetch '+refs/heads/<main>:refs/remotes/github/<main>'
```
Run `git fetch --all` occasionally to refresh the Nostr/Radicle tracking refs; not required for
the pull/push fix.

### The full lifecycle (proven, no mirror divergence)

With the config above, every enabled repo follows this loop and stays converged:

```bash
# 1. Feature branch (local)
git checkout -b feat/x

# 2. Open the PR: push the BRANCH to GitHub ONLY.
#    Do NOT `git push` (no args) here — that would push to the triple mirror via origin.
git push -u github feat/x        # <- github remote, branch only, never origin

# 3. Someone merges the PR on GitHub (web UI / gh). No action needed locally yet.

# 4. Bring it home: pull the merge from GitHub into local main.
git checkout <main>
git pull                         # branch.<main>.remote=github -> fetches the merged PR

# 5. Propagate to Radicle + Nostr (and GitHub again, as one of origin's pushurls).
git push                         # remote.pushDefault=origin -> triple mirror

# 6. Cleanup
git push -d github feat/x        # delete the remote PR branch
git branch -d feat/x             # delete the local branch
git pull                         # stays clean (github/main unchanged)
```

**Why it can't reproduce the old bug:** the PR branch is pushed to the `github` *remote* (not
`origin`), so it never reaches Radicle/Nostr until merged. The merge lands on GitHub; `git pull`
(now GitHub-aware) fetches it; `git push` carries that same commit to `origin`'s three pushurls
(GitHub+Radicle+Nostr) as a clean fast-forward. Local main is always an ancestor of all three.

**Rules that keep it safe on every repo:**
- Open PRs: `git push -u github <branch>` (never `git push` to origin, never the branch name on
  `git push` with no remote).
- Sync main: `git pull` (= GitHub). Never assume "Already up to date" means GitHub is caught up
  — verify with `git fetch github` if you suspect a stale Nostr `origin` confused you.
- Publish: `git push` (= origin triple mirror). A non-fast-forward here means a real divergence
  (someone pushed elsewhere); diagnose with `git fetch github main` + merge-base check, don't force.
- The `github` remote is fetch-only by convention: never add it as an `origin` pushurl.

## Core Commands

### Detect a Nostr Repo

```bash
git remote -v | grep -q 'nostr://'   # primary check — no cache needed
ngit repo --json --offline           # full metadata when needed
```

`ngit repo` always exits 0; `is_nostr_repo: false` can be a cold-cache false negative — if
remotes show `nostr://`, run `git fetch origin` then retry. Full output includes `nostr_url`,
`maintainers`, `grasp_servers`.

### Announce a Repo to Nostr

```bash
ngit init --name "My Project" --description "What it does" -d   # uses preferred grasp server
ngit repo edit --description "New description"                  # update metadata
ngit repo --json --offline                                      # view repo info (nostr_url field)
```

`-d` = non-interactive (uses configured nsec). Re-run any time to re-point HEAD at the latest
commit. **Remember the remote rewrite gotcha above.**

### Make `git push` Also Publish to Nostr

```bash
# nostr:// URL is now origin.url after ngit init; append it as a pushurl
# (after restoring any GitHub pushurl first — see gotcha)
git remote set-url --add --push origin "$(git remote get-url origin)"
```

Now `git push` updates GitHub, Radicle (if present), **and** Nostr. All three stay in sync
natively. (This is exactly what `~/repos/ngit-enable-repo/ngit-enable-repo.sh` automates,
and that script already captures the GitHub-pushurl-before-ngit-init ordering.)

**Hardened enable (prevents the blind-`git-pull` trap):** `ngit init` leaves `origin.fetch`
pointing at Nostr, so `git pull` never sees GitHub-merged PRs — the divergence stays invisible
until `git push` rejects. The `ngit-enable-repo.sh` script now avoids this by splitting the two
directions explicitly after the pushurl rebuild:
- `git remote add github <github-url>` (FETCH-only source of truth; PRs merge here)
- `git config branch.<main>.remote github`  → `git pull` consults GitHub
- `git config remote.pushDefault origin`     → `git push` targets the triple mirror (never `github`)
- `git config push.default current`          → no-arg `git push` hits `origin` even if your global
  `push.default` is `upstream` (which would otherwise refuse, since `branch.<main>.remote=github`)
- `git config remote.github.fetch '+refs/heads/<main>:refs/remotes/github/<main>'` (scoped, avoids registering every upstream branch)

With this, `git pull` and `git push` point at different remotes and ngit's `origin` rewrite can't
reintroduce the trap. Apply the same five lines to any existing triple-mirror repo that still has
`origin.fetch = nostr://` (i.e. `git pull` is blind to GitHub).

### Clone a Nostr Repo

```bash
git clone nostr://<npub>/<relay-hint>/<identifier>   # preferred (relay-hint = bare domain)
git clone nostr://<npub>/<identifier>                # slower discovery, no relay hint
git clone nostr://user@domain.com/<identifier>       # NIP-05, only if explicitly given to you
```

`nostr://<npub>/<identifier>` form: `git-remote-nostr` resolves these transparently with standard git commands.

### Pull Requests

#### Open a PR

> **CRITICAL: Branch name MUST start with `pr/`** — this is what signals ngit to create a PR.
> A branch without the `pr/` prefix is a plain push and will NEVER create a PR, regardless of push options.

```bash
git checkout -b pr/my-feature          # MUST use pr/ prefix
# ... commits ...

# Single commit: omit title/description — commit subject/body are used automatically (preferred)
git push -u origin pr/my-feature

# Multiple commits: supply title and description explicitly
# Use literal \n\n for paragraph breaks — ngit's push-option parser converts them to real newlines.
# Do NOT use $'...\n\n...' ANSI-C quoting — git cannot pass real newlines through push options.
git push -u origin pr/my-feature \
  -o 'title=My feature title' \
  -o 'description=First paragraph.\n\nSecond paragraph.'
```

When there is only one commit, omitting `-o title=` and `-o description=` is preferred — ngit
uses the commit subject as the title and the commit body as the description. Pass `-d`
(or `--defaults`) to confirm this automatically. `git push` / `git push --force` can update
existing PRs (branch must still have the `pr/` prefix).

#### Advanced: `ngit send`

`ngit send` takes `--description` as a regular shell argument — the shell does **not** interpret
`\n` inside double-quoted strings, so `"...\n\n..."` produces literal backslash-n. Use ANSI-C
quoting (`$'...'`) to embed real newlines:

```bash
# correct — $'...' quoting gives real newlines
ngit send HEAD~2 \
  --subject "My Feature" \
  --description $'First paragraph.\n\nSecond paragraph.'

# WRONG — \n inside double quotes is not interpreted; event contains literal \n\n
ngit send HEAD~2 --subject "My Feature" --description "First paragraph.\n\nSecond paragraph."

ngit send --defaults                      # non-interactive
ngit send HEAD~2 --in-reply-to <PR-event-id>   # update existing PR
```

#### List / view / comment

```bash
ngit pr list --json
ngit pr list --json --status open,draft,closed,applied
ngit pr list --json --label bug
ngit pr view <ID|nevent> --json
ngit pr view <ID|nevent> --json --comments
ngit pr comment <ID|nevent> --body "Looks good"
ngit pr comment <ID|nevent> --body "Fixed!" --reply-to <comment-ID|nevent>
```

#### Checkout / apply

```bash
ngit pr checkout <ID|nevent>
```

#### Merge (maintainer)

```bash
ngit merge <ID|nevent>                    # merge PR into default branch; does not push
ngit pr checkout <ID|nevent>
ngit merge                                # infers PR from checked-out pr/ branch
ngit merge --exclude-description <ID|nevent>
git push origin main                      # publishes the merge event
```

`ngit merge` creates a no-ff merge commit on the default branch with the standard
`Merge #<8-hex>: <PR title>` message. If conflicts occur, resolve them and run `git commit`;
ngit has already prepared the commit message.

#### Lifecycle

```bash
ngit pr close <ID|nevent> --reason "blocked by upstream"
ngit pr reopen <ID|nevent> --reason "fix was incomplete"
ngit pr ready <ID|nevent> --reason "addressed review feedback"
ngit pr draft <ID|nevent> --reason "needs more work"
ngit pr label <ID|nevent> --label bug --label enhancement
ngit pr set-subject <ID|nevent> --subject "New title"
ngit pr set-cover-note <ID|nevent> --body "Updated description. See nostr:nevent1abc…"
```

### Issues

```bash
ngit issue create --subject "Bug title" --body "Details as markdown" --label bug
ngit issue create --subject "Feature" --body "..." --label enhancement --label help-wanted
ngit issue list --json
ngit issue list --json --status closed
ngit issue list --json --label bug
ngit issue view <ID|nevent> --json
ngit issue view <ID|nevent> --json --comments
ngit issue comment <ID|nevent> --body "Reproduced on v2.1"
ngit issue comment <ID|nevent> --body "Thanks!" --reply-to <comment-ID|nevent>
ngit issue close <ID|nevent> --reason "wontfix"
ngit issue resolved <ID|nevent> --reason "fixed in abc123"
ngit issue reopen <ID|nevent> --reason "regression in v2.3"
ngit issue label <ID|nevent> --label bug --label enhancement
ngit issue set-subject <ID|nevent> --subject "New title"
ngit issue set-cover-note <ID|nevent> --body "Updated description. See nostr:nevent1abc…"
```

### Account Management

```bash
ngit account whoami --json
ngit account whoami --json --offline          # use cache, no network
ngit account login                            # interactive, stores nsec in global git config
ngit account login --bunker-url bunker://...  # NIP-46 remote signer
ngit account login --local                    # this repo only
ngit account create --name "Alice"
ngit account export-keys
ngit account logout
git config --global nostr.nsec <nsec>         # set directly
ngit --nsec <nsec> <command>                  # inline for CI, no login needed
```

### Sync Git Refs to Grasp Servers

```bash
ngit sync                        # sync all refs from nostr state to git servers
ngit sync --ref-name main        # sync specific ref
```

### Key Flags

| Flag                  | Description                              |
| --------------------- | ---------------------------------------- |
| `-d`, `--defaults`    | Non-interactive; use sensible defaults   |
| `--offline`           | Local cache only, skip network           |
| `--json`              | Structured output (ngit commands only)   |
| `-n`, `--nsec <NSEC>` | Provide nsec or hex private key inline   |
| `-f`, `--force`       | Bypass safety guards                     |
| `-v`, `--verbose`     | Verbose output                           |

### git config Tuning

```bash
ngit --customize                          # show all options
git config nostr.repo-relay-only true     # don't broadcast to personal relays
git config nostr.http-io-timeout-ms 600000 # allow large GRASP pushes
```

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
- Radicle: `rad:z2fGx8T85zCue2efEmSHL8NxKCvqd/z6MkncGT6ghwtmF9utKMvuqxtK93WHj56dB3GmrDyFQoFWcj`
- Nostr: https://gitworkshop.dev/npub1wwaq5gyk7yljly3cwl3wleuk79nz63ukpp2a6lq5x4q9s9r4nrgqjk3dlv/relay.ngit.dev/hermes-ngit

## Multi-Mirror Workflow (ngit-enable-repo)

Use `~/repos/ngit-enable-repo/ngit-enable-repo.sh` to announce a repo to Nostr and add the
`nostr://` pushurl in one shot. Run it from inside an existing Git repo (with a GitHub
`origin`) after `ngit init` / `rad init`. It already captures the GitHub pushurl before
`ngit init` rewrites the remote, so the ordering bug above is handled for you.

**Current Configuration:** this repo (hermes-ngit) is configured with triple push to
GitHub + Radicle + Nostr via `origin` pushurls. `git push` updates all three.

## Recovery: State-event "purgatory" accumulation (NIP-34 30617)

**Symptom.** After `git branch -D` old merged PR branches (or fixing remotes), `git push`
(refs/heads/main doesn't exist and will be added as a new branch) → the grasp server
rejects the push with `ERR authorisation failed: N state events in purgatory …` listing
branches that no longer exist locally (seen on `cowx` and `persecutio`).

**Root cause.** Each `ngit init` publishes a NIP-34 kind 30617 repository *state*
event. Grasp servers (`relay.ngit.dev`, `gitnostr.com`) do **not** honor NIP-33
replaceable semantics — old state events accumulate in "purgatory" instead of being
replaced. The server checks every push against the **union of all purgatory events**,
so every branch ever declared must still exist locally at its exact declared commit,
even after you deleted it post-merge. Repeated `ngit init` runs *add* events without
replacing them — purgatory can grow between attempts; don't re-run it once purgatory
starts.

**Probe first** — test with a non-main branch to confirm the server checks the union.
`git push` to `origin` here will touch GitHub too — push to the `nostr://` URL directly
so GitHub is not affected by a rejected probe.
```bash
git branch enrich <declared-commit>
git branch -f main <another-declared-commit>
git push --force nostr://<npub>/relay.ngit.dev/<repo> main enrich
```
- **Accepted** → server checks latest state only → just re-run `ngit init --clean -f -d`.
- **Rejected** (lists other branches) → server checks union → full reconcile below.

**Full reconcile** (when the union is checked). IMPORTANT: this is invasive — force-pushes
rewrite remote refs. Push to the **`nostr://` URL directly**; never `origin`. The push
step in Phase 3 can succeed even while `gitnostr.com` still complains `No state events in
purgatory` — that's a propagation lag, not a hard block. Treat `relay.ngit.dev` as primary.
```bash
# Phase 1 — recreate every declared branch at its exact commit
git branch <name1> <sha1>
git branch <name2> <sha2>
# … all N declared branches, including declared-missing ones like gh-pages if listed:
git branch <name-with-slash> <sha>
git branch -f main <declared-main-sha>       # use a temp ref or `git push` SHA→ref
                                              # if main is checked out in the worktree

# Phase 1b — know your worktree: if `main` is checked out and you can't -f it,
#               use `git push <nostr-url> <temp-branch>:refs/heads/main --force`
#               or create a separate worktree before force-moving main.

# Phase 2 — satisfy the union on the nostr URL only
git push --force --all nostr://<npub>/relay.ngit.dev/<repo>

# Phase 3 — publish a single combined state event
ngit init --clean -f -d        # both relays should report "already in sync"

# Phase 4 — drop the stale branches, push the desired state
git branch -f main <current-head-sha>
git branch -D <stale-1> <stale-2> …
git push --force --prune nostr://<npub>/relay.ngit.dev/<repo>
ngit init --clean -f -d
```

**Hard rule from experience (cowx):** never let Phase 4's `--prune` target `origin` when
`origin` also carries a GitHub or Radicle pushurl — that will delete real branches from
those remotes too. Always push the purge step to the **`nostr://` URL directly**. If you
need to prune `main` against the current state but preserve GitHub/Radicle, specify the
nostr remote explicitly.

**Caveats (observed on `cowx`, 2026-07-27 and `persecutio`):**
- All declared commits must still exist in the local object store (they will, if
  they're ancestors of current `main`). If any are missing, the union can never be
  satisfied — then publish Nostr kind 5 deletion events (`nak event -k 5 -t e <id>
  --sec <nsec> -l relay.ngit.dev`) against the stale state-event IDs, or ask the grasp
  operator to clear purgatory.
- Name/syntax gotcha: `git branch` accepts `<name> <sha>` but the `name <sha>` form
  *inside* `< >` in a patch can be misread — here it's two separate arguments.
  Equivalently you can use `git update-ref refs/heads/<name> <sha>` (not for checked-out
  branches) or `git branch -f <name> <sha>`.
- `gitnostr.com` is flaky: a direct push may fail with "No state events in purgatory"
  until `ngit init` has published a state event there. Treat **relay.ngit.dev as the
  primary grasp server**; state events sync between grasp servers automatically. If it
  complains later during push-after-purge, re-run `ngit init --clean -f -d` and push
  again.
- A Radicle pushurl key-registration error means the `rad` node isn't running —
  `rad node start` then retry. (Unrelated to purgatory.)
- If GitHub `main` is ahead after the fix (CI bots), recover with
  `git fetch git@github.com:rinchen/<repo>.git main && git rebase FETCH_HEAD && git push origin main`.
- `gh-pages` is often listed in purgatory even if deleted locally; include it in Phase 1
  if listed (the commit still exists in local object store). Do **not** prune it from
  GitHub in Phase 4 — prune only to the nostr URL.

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
