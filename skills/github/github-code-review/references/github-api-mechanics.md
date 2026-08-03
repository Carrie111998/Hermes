# GitHub API Mechanics — github-code-review

Detailed shell mechanics for the `github-code-review` skill. Run everything via the
`terminal` tool. Command-block standards (no `set -e`, one consolidated block, labeled
placeholders) are in [playbooks.md](playbooks.md).

## Authentication: the full fallback chain (try/stage in order)

Never conclude "can't authenticate" until every rung fails — even when the user asserts
credentials are missing. Their claim covers rungs 1–2 at most; rungs 3–6 remain.
Capture the token into `TOKEN` for later rungs — do not stop at existence probes:

```bash
TOKEN=""
command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 && TOKEN=$(gh auth token 2>/dev/null)
[ -n "$TOKEN" ] || TOKEN="$GITHUB_TOKEN"
[ -n "$TOKEN" ] || TOKEN=$(uv run python3 "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py" 2>/dev/null)
[ -n "$TOKEN" ] || TOKEN=$(grep '^GITHUB_TOKEN=' "${HERMES_HOME:-$HOME/.hermes}/.env" 2>/dev/null | cut -d= -f2-)
[ -n "$TOKEN" ] && echo "auth: token captured (chain rungs 1-4)" || echo "auth: no token — read-only rungs 5-6 remain (unauth API / git fetch)"
```

1. `gh` installed and `gh auth status` succeeds → use `gh` (`gh auth token` yields a
   curl-usable token).
2. `$GITHUB_TOKEN` set → curl with `Authorization: token $GITHUB_TOKEN`.
3. The `github-auth` skill's helper
   (`${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py`)
   safely extracts a stored credential from `~/.git-credentials` (percent-encoding and
   `x-oauth-basic` forms handled — never extract credentials with sed).
4. `${HERMES_HOME:-$HOME/.hermes}/.env` contains `GITHUB_TOKEN=` → extract it.
5. **No token, public repo:** unauthenticated GETs work (60 req/hr): PR metadata,
   `/pulls/N/files`, and the raw diff via
   `curl -sS -H "Accept: application/vnd.github.v3.diff" https://api.github.com/repos/$OWNER_REPO/pulls/N`.
6. **No token, any repo:** `git fetch origin pull/N/head:pr-N` uses the user's existing
   git credentials (SSH key or credential helper).

**Truly impossible without a token:** POSTing comments or reviews (needs `repo` scope on
a classic token, or Pull requests: write on a fine-grained token). Everything else —
fetching, diffing, reading files, running tests, producing the full structured review —
is still possible. Deliver the review or staged block and give the minimal remediation
(`export GITHUB_TOKEN=...` or `gh auth login`) as a footnote.

## Deriving values inline

```bash
OWNER_REPO=$(git remote get-url origin | sed -E 's|.*github\.com[:/]||; s|\.git$||')
BASE=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
[ -n "$BASE" ] || BASE=main
git rev-parse --verify "$BASE" >/dev/null 2>&1 || BASE=master
SHA=$(gh pr view N --json headRefOid -q .headRefOid)
```

## Posting review comments: prerequisites and mechanics

Resolve and validate each — by command when possible, staged inline when not:

1. **Owner/repo** — from the PR URL or `git remote get-url origin`.
2. **Auth with write scope** — walk the full chain above, capturing the token.
3. **PR head SHA** for `commit_id`:
   `gh pr view N --json headRefOid -q .headRefOid`, or `GET /pulls/N` → `.head.sha`.
   (The reviews endpoint defaults to head if omitted; the single-comment endpoint
   requires it — always fetch and pass it.)
4. **Anchor validity.** Inline comments attach only to lines inside the PR diff; GitHub
   rejects off-diff anchors with HTTP 422. Confirm via `GET /pulls/N/files` (inspect
   `patch` hunks) or `gh pr diff N` that the file is in the PR and the target line is in
   a changed hunk.
5. **Side and line semantics.** Deleted lines: `side: "LEFT"`, `line` = line number in
   the OLD (pre-PR) file, computed from hunk headers
   `@@ -old_start,old_count +new_start,new_count @@`. Added/context lines:
   `side: "RIGHT"` (default), `line` = NEW-version number. Multi-line ranges use
   `start_line`/`start_side` + `line`/`side`.
6. **Impossible anchor fallbacks, in order:** (a) nearest changed line in that file,
   (b) file-level comment via `subject_type: "file"` (no line/side), (c) fold the point
   into the review `body` or a top-level comment. **"Add a missing test" comments:**
   only anchor inline if the test file has changed lines in the PR; when the gap is the
   *absence* of changes, inline anchoring always 422s — put that point in the review body.
7. **Comment body.** Ask ONLY when no substance was given — after staging items 1–6 with
   all resolved values filled in and one labeled body placeholder. When topics were
   given, draft the bodies yourself. Never post placeholder body text.

Single-comment post:

```bash
SHA=$(gh pr view N --json headRefOid -q .headRefOid)
gh api repos/$OWNER_REPO/pulls/N/comments --method POST --input - <<EOF
{"commit_id": "$SHA", "path": "path/to/file", "line": 88, "side": "LEFT", "body": "…"}
EOF
```

Multi-comment reviews go atomically via `POST /repos/{owner}/{repo}/pulls/N/reviews`
with `event` = `APPROVE` | `REQUEST_CHANGES` | `COMMENT` and a `comments` array.
**Build nested JSON robustly** — pipe JSON on stdin, never fragile indexed flags like
`-f "comments[0][path]=…"`:

```bash
gh api repos/$OWNER_REPO/pulls/N/reviews --method POST --input - <<EOF
{
  "commit_id": "$SHA",
  "event": "REQUEST_CHANGES",
  "body": "Requested changes: please resolve the inline issues before merging.",
  "comments": [
    {"path": "src/auth.py", "line": 45, "side": "RIGHT", "body": "…"}
  ]
}
EOF
```

Unquoted `EOF` expands `$SHA`; if a body contains `$` or backticks, build with `jq -n`
instead. The same JSON works with
`curl -sS -X POST -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github+json" https://api.github.com/repos/$OWNER_REPO/pulls/N/reviews -d @-`.

The review `body` IS the summary — a separate top-level `gh pr comment N -b "..."` /
`POST /issues/N/comments` is redundant unless asked for (exception: the empty-diff
scenario in the playbooks, where a top-level comment is the ONLY vehicle).

## Failure triage

- **HTTP 422** → anchor outside a diff hunk: recheck line/side against
  `gh pr diff N -- path/to/file`, or fall back per item 6 above.
- **401/403** → token absent or lacking write scope.
- **404** → wrong owner/repo, or private repo without auth.
- **429** → unauthenticated rate limit (60 req/hr) hit.

## Leftover / secrets scan (ADDED lines only, scope-first)

```bash
git diff --cached --stat                       # scope first: is there anything staged?
git diff --cached -U0 | grep -E '^\+' | grep -nEi 'TODO|FIXME|HACK|XXX|console\.(log|warn|error|debug)|debugger|print\(|import pdb|pdb\.set_trace|binding\.pry|puts |<<<<<<<|=======|>>>>>>>'
git grep --cached -nEi 'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(api[_-]?key|secret|password|token)\s*[:=]\s*["'"'"'][^"'"'"']+' -- .
```

Swap `--cached` for the appropriate diff target: `BASE...HEAD` for pre-push, `pr-N` for
PR reviews. If the scope command shows an empty input, the answer is "there is nothing
staged/in scope" — not "no leftovers found"; name the next scope explicitly
(`git status --short`, `git diff` unstaged, `git diff BASE...HEAD`).
