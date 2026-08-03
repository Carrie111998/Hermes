# Scenario Playbooks — github-code-review

Behavioral playbooks for the `github-code-review` skill. All shell runs via the
`terminal` tool; API mechanics (auth chain, posting, triage) are in
[github-api-mechanics.md](github-api-mechanics.md).

## Command-Block Standards

- One block pasted top-to-bottom **from the repo root**. Never start with `cd /placeholder`.
- **No `set -e` / `set -euo pipefail`**: one failing probe would kill the fallback
  ladder. Run probes plainly, guard optional steps with `|| true`, branch with `if`.
- **No interactive prompts** (`read -rp …`). Derive values inline; if truly
  unobtainable, use a clearly labeled placeholder plus the exact command that reveals
  valid values. Never fill a placeholder with an invented value that looks real.
- Both `gh` and `curl` variants where applicable.
- Resolve auth FIRST in the block and CAPTURE it for later rungs (see the mechanics
  reference).

## No-Execution Playbook

When the session has no shell/API access, deliver in this order:

1. ONE consolidated copy-paste command block (per the standards above) performing the
   entire task, every derivable value resolved inline. For unidentified targets, the
   two-block pattern below.
2. An interpretation guide for each key command (each plausible output → meaning → next
   action), covering BOTH branches of any user claim being verified.
3. Fully drafted content: comment bodies, review text, applicable checklist/framework.
4. Failure triage for each step (422 / 401 / 403 / 404 / 429).
5. The single minimal artifact you still need — as a one-line footnote.

## The two-block pattern for unidentified targets

When a target (PR number, branch) must be discovered:

- **Block 1 — discovery:** resolves owner/repo, base, auth (captured), and finds
  candidates (full ladder below).
- **Block 2 — follow-on:** the complete fetch + review + scan + test + cleanup pipeline,
  parameterized on ONE clearly labeled placeholder (`N=<number printed by Block 1>`).

The user makes one paste, reads one value, fills one placeholder — never a second
round-trip for mechanics.

## Resolving what to review

- **PR number or URL given:** use it. Derive owner/repo from the URL or the remote.
- **PR described but not identified** (e.g., "this PR adds /login"): run or stage this
  FULL discovery ladder before asking:
  1. `gh pr status` / `gh pr view` — PR attached to the current branch
  2. `gh pr list --state open --search "login" --json number,title,headRefName,url` and
     `gh pr list --state open --json number,title,headRefName`
  3. curl fallback: `curl -sS "https://api.github.com/repos/$OWNER_REPO/pulls?state=open" | jq -r '.[] | "\(.number)\t\(.title)\t\(.head.ref)"'`
  4. Inspect each candidate's file list for the feature:
     `gh pr view N --json files -q '.files[].path'` (paths containing "login"/"auth")
  5. Local fallback: `git branch --all --list '*login*'`,
     `git log --all --oneline -i --grep='login'`; if the feature branch is checked out,
     review `git diff BASE...HEAD` directly.
  6. Only then ask — presenting the candidates found, plus the staged follow-on block
     with the single labeled placeholder.
- **"Review before pushing" / "check my changes":** local review of
  `git diff BASE...HEAD` plus staged/unstaged diffs; no GitHub API needed.

## Playbook: leftover / debug-statement scan

- **With execution:** run the scope command and the added-lines scan (mechanics
  reference); report real output. If `git diff --cached --stat` is empty, the answer is
  **"there are no staged changes"** — not "no leftovers found". Offer the next scope
  concretely: `git status --short`, `git diff` (unstaged), `git diff BASE...HEAD`.
- **Without execution:** staged block + interpretation guide: `--stat` empty → nothing
  staged → re-scan other scopes; `--stat` non-empty, greps silent → clean; greps hit →
  each hit is file:line to fix.
- One block, then results (executed) or interpretation (staged). Never both.

## Playbook: user claims tools/credentials are missing

Treat it as a claim about rungs 1–2 of the auth chain only. Stage or run the FULL
ladder: capture-token auth block, then `git fetch origin pull/N/head:pr-N`, then — if
fetch fails — the unauthenticated public-API diff. Include scope, full diff, test
detection run against the PR code via worktree, and cleanup. Deliver the checklist and
failure triage. Single minimal artifact if all rungs fail: pasted diff or PR URL.

## Playbook: security-focused review of a login/auth endpoint

If the PR isn't identified, use the TWO-BLOCK pattern — do not ask first. Block 1 is the
discovery ladder looking for login/auth paths; Block 2 is the full review pipeline
parameterized on `N`. Interpretation guide: each discovery hit → that's the PR, fill
`N`; all empty → fork/merged/unpushed → paste URL or diff.

Checklist — framed as "what the review will cover", report on each explicitly:
passwords hashed with bcrypt/argon2/PBKDF2 (never plaintext/MD5/bare SHA), timing-safe
comparison; parameterized queries/ORM (no string-built SQL); input validation with
bounded lengths; rate limiting / lockout / backoff; generic "invalid credentials"
errors (no user enumeration); session/JWT entropy, expiry, fixation prevention, cookies
`HttpOnly; Secure; SameSite`; HTTPS only, CSRF protection, validated post-login
redirects; no credentials/tokens logged, no hardcoded secrets, no leftover debug output;
tests for wrong password, nonexistent user, expired/invalid token, lockout, injection
payloads — not just the happy path.

## Playbook: PR has no changed files (empty diff)

Even when asked only *what the review should look like*, deliver the FULL package
(mode line → artifact → verification → posting → footnote), not just an explanation:

1. **Verdict: Comment** — Approve/Request-Changes on an empty diff is meaningless, and
   inline comments are impossible (no diff lines to anchor). Never submit a formal
   Approve/Request-Changes here.
2. **Draft the verbatim top-level comment** (the actual deliverable): the finding (0
   changed files / no diff against base); the likely causes — branch and base now
   identical (commits already merged or branch reset/rebased to base), a later commit
   reverted the changes, wrong base branch, intended commits never pushed, PR opened by
   mistake; and the ask — push the missing commits, retarget the base branch, or close
   the PR. Keep it short — NO four-section template of "None".
3. **Stage (or run) independent verification FIRST**, with both branches covered:
   ```bash
   OWNER_REPO=$(git remote get-url origin | sed -E 's|.*github\.com[:/]||; s|\.git$||')
   gh pr diff 9 --name-only
   gh pr view 9 --json files -q '.files[].path'
   curl -sS "https://api.github.com/repos/$OWNER_REPO/pulls/9/files" | jq length
   # plain-git fallback:
   git fetch origin pull/9/head:pr-9 && git diff "$BASE"...pr-9 --stat
   ```
   Interpretation: all empty / `0` → claim confirmed → post the drafted comment.
   Anything non-empty → the claim was wrong → switch to the full review procedure
   (scope, diff, tests, verdict) for the diff you just found.
4. **Stage the posting command** (auth chain first, then `gh pr comment 9 --body "..."`
   or the `POST /issues/9/comments` curl variant — top-level PR comments use the
   **issues** comments endpoint; posting needs a token with `repo` / Pull requests:
   write — without one, the comment text is the deliverable and the token is the
   footnote).
5. **Never end with "If you'd like me to verify…"** — the verification block IS
   attached; that closes the turn.

## Playbook: on-demand comment posting ("post a comment on the deleted line X of PR #N")

- Immediately resolve (or stage): owner/repo, auth chain, head SHA, anchor validity —
  file in `/pulls/N/files`, line inside a diff hunk, `side=LEFT` + old-file line number
  for deletions. Verification inline: `gh pr diff N -- path/to/file`.
- Body given → post/stage and confirm with the comment URL. Only topics given → draft
  the body and post/stage. No substance → stage everything else, one labeled body
  placeholder, ask for the body alone.
- Include 422 triage (line not in a deletion hunk → recheck the old-file number from the
  hunk header, or fall back per the mechanics reference).
- Auth missing after the full chain → exact blocked step (POST needs a token with
  `repo` / Pull requests: write) + minimal remediation. Don't also ask for derivables.

## Playbook: local pre-push review ("review my changes before I push")

- **With execution:** detect BASE (origin/HEAD symref, else `main`, else `master`), then
  `git status --short --branch`, `git log BASE..HEAD --oneline`, `git diff BASE...HEAD`,
  staged and unstaged diffs, tests/lint, full structured review.
- **Without execution:** deliver this gather block plus the checklist, and ask the user
  to paste the output:
  ```bash
  git status --short --branch
  BASE=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||'); [ -n "$BASE" ] || BASE=main
  git rev-parse --verify "$BASE" >/dev/null 2>&1 || BASE=master
  echo "== commits ahead of $BASE =="; git log "$BASE"..HEAD --oneline
  echo "== diffstat =="; git diff "$BASE"...HEAD --stat
  echo "== full diff =="; git diff "$BASE"...HEAD
  echo "== staged (uncommitted) =="; git diff --staged
  echo "== unstaged =="; git diff
  ls Makefile package.json pyproject.toml tox.ini setup.cfg 2>/dev/null
  ```
