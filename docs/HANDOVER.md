# Project Handover - hermes-agent

Plan key: `hermes-agent`
Last verified: 2026-08-18 (Asia/Taipei), Claude continuation session
Authoritative roadmap: `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

Verified in this session unless marked carried forward.

- Repository: `https://github.com/cwliao/hermes-agent`
- `origin` = `git@github.com:cwliao/hermes-agent.git`; `upstream` fetch is
  `NousResearch/hermes-agent` with push set to `DISABLE`.
- PR #48's merge commit `a5bebea8f8af78ea6996fbc10d2ea9e77d23b286` landed on
  `main`; the pre-merge base was `2b5eb8437a...`. This handover update is the
  docs-only commit sitting directly on top of that merge commit, so the current
  `origin/main` is this commit. Re-read `origin/main` rather than trusting a
  SHA quoted inside the file it names.
- Local primary checkout in this environment: `~/.hermes/hermes-agent`,
  branch `main`, HEAD `b7b362b2f1bcf494ba7f0acf2236452caacb05b6`, clean
  (`git status --porcelain` empty). It is behind `origin/main`.
- The Windows primary checkout `D:/PROJECT/Hermes` recorded in earlier
  handovers is not present on this host. Its dirty state is carried forward
  and unverified here. On whichever host it exists, preserve it: do not
  reset, clean, overwrite, stage, or commit it.
- Isolated worktree used for this session:
  `~/.hermes/worktrees/pr48-review-claude001`, detached at the PR #48 head,
  clean. No files were changed outside it.
- PR #48 is **merged and closed**. Merged at `2026-08-18T07:30:25Z` with
  `merge_commit_sha=a5bebea8f8...`, head `f97f3402f38bb735430c3296fbb19fc387dc7348`,
  base `2b5eb8437a...`. The head is confirmed an ancestor of `origin/main`.
- DGX is reached only from WSL Ubuntu. Verify hostname `55-0940189-03`, user
  `cwliao`, and `hermes-gateway.service` before any remote mutation. Carried
  forward; not verified in this session.

This handover covers the four-lane Kanban worker correction ticket. It does
not claim that the current PR is merged, deployed, restarted, or Telegram
user-visible.

## 2. Current goal and roadmap

The current goal is to finish the correction ticket's CI and review gates for
the four explicit lanes: native Hermes, Claude, Grok, and AGY. The intended
test is a disposable, metadata-only joke-brainstorm goal created through the
existing Kanban graph, with all four worker lanes, a fail-closed verifier, a
Hermes synthesizer, and a separately verified Telegram user-visible response.

Completed for this ticket (review items carried forward from the prior
session; CI and test items verified in this session):

- Design packet reached the required two-of-three review quorum.
- Implementation commit: `af3034993fae20b6d6a552cd014fa0646cbf7af2`.
- Claude: `PASS`; Grok: `PASS`; AGY: `BLOCKED` because the DGX CLI required an
  interactive TTY. AGY is not counted and was not duplicated.
- Focused swarm suite: 6 passed, re-run and re-confirmed in this session.
- Branch pushed and PR #48 opened.

CI status change verified in this session:

- The recorded blocker was CI run `32102234859`, whose `head_sha` is
  `af303...` and whose `run_attempt` is `2`. Its conclusion is `failure`,
  the failing job is `Python tests / Run tests slice 3/8`, and the aggregate
  `All required checks pass` job is `failure`. This confirms the previously
  recorded blocker for `af303...`.
- CI run `32103854648` ran on the current PR head
  `f97f3402f3...` and concluded `success`. All eight Python test slices,
  including `slice 3/8`, are `success`, and `All required checks pass` is
  `success`. `osv-scanner` is `neutral`, not a failure.
- The commit between the two runs is docs-only and changes no Python code, so
  the same code that failed `slice 3/8` later passed it. The gateway
  fresh-final failures therefore behave as flaky or infrastructure-dependent,
  not as a deterministic baseline regression.
- PR #48 REST state: `state=open`, `draft=false`, `merged=false`,
  `mergeable=true`, `mergeable_state=clean`.

Consequence: the CI gate passed on head `f97f3402f3...`, the user explicitly
authorized the merge, and PR #48 was merged by the user through the GitHub web
UI. Deployment, restart, relay/timer enablement, and Telegram testing remain
separate, unexecuted gates and still require explicit authorization; none were
performed in this session.

Merge mechanics worth recording: this environment's `gh` credentials are
unauthenticated (`gh api user` returns 401, rate limit 60/hr), so all GitHub
reads were anonymous public reads and no API write was possible. A local
git merge plus SSH push was also unavailable in the session. The user
performed the merge in the browser.

The gateway flakiness is still worth a separately authorized follow-up, but it
is now a flake investigation, not a confirmed baseline break. GitHub Issues are
disabled, so any follow-up needs an authorized repo-local plan or another
approved ticket path. Do not fold it into PR #48 without explicit scope.

## 3. Verified implementation state

PR #48 changed only these existing surfaces. Verified after merge by diffing
`2b5eb8437a...` against `origin/main`: exactly nine files, no gateway files and
no gateway tests.

- `hermes_cli/kanban.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban_swarm.py`
- `optional-skills/devops/kanban-worker/SKILL.md`
- `tests/hermes_cli/test_kanban_swarm.py`
- `tools/kanban_tools.py`
- `docs/HANDOVER.md`, `docs/ROADMAP-HERMES-DGX.md`, and the ticket design
  under `docs/plans/`

The implementation provides four explicit lane identities, expected
lane/preflight-skill binding, bounded goal and worker runtime settings,
parser-safe worker construction, required worker completion metadata, a
fail-closed verifier, and a synthesizer result boundary in the existing task
`result` field. Dashboard and Telegram correlation is metadata-only and uses
opaque IDs and status fields.

Local evidence re-run in this session at the PR head, in the isolated
worktree, using the repository virtualenv:

- `tests/hermes_cli/test_kanban_swarm.py`: 6 passed.
- `tests/gateway/test_stream_consumer_fresh_final.py`: 28 passed. The three
  CI failures did not reproduce locally.
- Whole `tests/gateway` directory: 9276 passed, 25 failed, 11 skipped. None of
  the 25 are in `test_stream_consumer_fresh_final.py`; they are spread across
  eight unrelated gateway test files and are treated as local-environment
  noise, since CI passed every slice on this same head.

CI is the authoritative repository gate and is not replaced by local tests.

The last separately verified DGX runtime was the pre-PR release identified by
`HERMES_RELEASE_SHA=20aadf71cb4ead7db7d9590310465e90c937558b`, with the user
service active/running, zero restarts, and rollback evidence retained. This is
carried forward and was not re-verified in this session. It does not prove that
`af303...` or `f97f340...` is deployed. Relay remains disabled by default.

## 4. Ticket and gate state

Active ticket plan: `docs/plans/2026-08-18-hermes-multi-agent-claude-worker-001.md`.

The gates remain separate: source identity, ticket design, implementation,
tests, Claude/Grok/AGY review, reconciliation, CI, commit, push, merge, DGX
deployment, runtime health, inbound polling, outbound delivery, and Telegram
user-visible delivery.

Gates satisfied: source identity, ticket design, implementation, focused
tests, Claude/Grok review quorum (two of three), reconciliation, CI, commit,
push, and **merge**.

Gates NOT satisfied. Merging the code does not close this ticket; acceptance
gates 3 through 9 of the ticket plan are all still unexecuted:

- **Runtime worker gate**: the four-lane DGX preflight has not been run. The
  AGY lane remains `BLOCKED` because its CLI requires an interactive TTY.
- **Graph construction, worker completion, and verifier/synthesis gates**: the
  disposable four-lane joke-brainstorm graph has never been executed.
- **Dashboard gate**: no run evidence collected.
- **Telegram gate**: no user-visible delivery test performed or confirmed.
- **Cleanup gate**: nothing to clean yet, because no disposable cards exist.
- DGX deployment, service restart, runtime health, inbound polling, and
  outbound delivery are all unexecuted and unauthorized.

## 5. Tooling notes for the next session

- `gh auth status` reports the default token as invalid, but the GitHub REST
  API through `gh api` works. GraphQL returns `HTTP 401`, so `gh pr view` and
  other GraphQL-backed commands fail. Use `gh api repos/...` REST endpoints
  for PR, run, and check-run state.
- Test runs need the repository virtualenv; the system `python3` has no
  `pytest`.

## 6. Safe continuation procedure

1. Read this file, `docs/ROADMAP-HERMES-DGX.md`, and the active ticket plan.
2. Verify repository root, remote, branch, HEAD, worktree, and authenticated
   GitHub `origin/main` in the exact command context.
3. Preserve the primary checkout; use an isolated worktree for every change.
4. Re-verify CI on the exact head under consideration; a green run is bound to
   one SHA and any new push re-opens the CI gate.
5. Treat the gateway slice flakiness as a separate, still-unopened follow-up.
   It is a required check, so it can randomly block future PRs.
6. Use one authenticated reviewer per family; never duplicate Claude, Grok, or
   AGY across platforms. Reconcile any non-quorum finding.
7. Before DGX mutation, verify hostname, user, effective unit, release SHA,
   and rollback evidence.
8. Keep reports metadata-only: no message bodies, prompts, joke text, tokens,
   credentials, or sensitive absolute paths.

## Copyable Claude continuation prompt

```text
You are taking over the hermes-agent repository. Use the repo as the source of truth.
Read docs/HANDOVER.md, docs/ROADMAP-HERMES-DGX.md, and
docs/plans/2026-08-18-hermes-multi-agent-claude-worker-001.md before acting.

Verify repository root, remote, branch, HEAD, worktree, and origin/main. The
primary checkout recorded as D:/PROJECT/Hermes is intentionally dirty; do not
reset, clean, overwrite, stage, or commit it. Work only in an isolated
worktree.

PR #48 is merged. origin/main is a5bebea8f8af78ea6996fbc10d2ea9e77d23b286,
the merge commit; the pre-merge base was 2b5eb8437a. Implementation commit
af3034993f plus one docs-only commit f97f3402f3 are now ancestors of main.
CI passed on f97f3402f3 (run 32103854648, all eight slices and the aggregate
required check). The earlier failure on af303 (run 32102234859, attempt 2,
slice 3/8 gateway fresh-final) did not recur on unchanged code, so it is
flaky, not a baseline regression, and remains an unopened follow-up.

Merging closed the code gate only. Ticket acceptance gates 3 through 9 are
all unexecuted: four-lane DGX runtime preflight (AGY still BLOCKED on an
interactive TTY), graph construction, worker completion, verifier/synthesis,
Dashboard evidence, Telegram user-visible delivery, and cleanup. DGX
deployment, restart, relay/timer enablement, and Telegram testing are
separate gates and are not authorized.

Environment notes: gh credentials are unauthenticated here (gh api user
returns 401, rate limit 60/hr), so GitHub reads are anonymous public reads
and API writes are impossible; restoring auth needs `gh auth login` in an
interactive terminal. Test runs need the repository virtualenv, not the
system python3.

Before ending, update the repo handover only with facts verified in that
session, and keep evidence metadata-only.
```
