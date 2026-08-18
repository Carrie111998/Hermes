# Hermes Architecture Roadmap

Snapshot: 2026-08-18 (Asia/Taipei), Claude continuation session. This file is the current repository-local
state summary. Git history contains older deployment snapshots; do not use
those older snapshots to infer the current PR or runtime state.

## Source-of-truth rules

- `origin/main` and the repository-local handover are authoritative.
- The primary checkout is an audit checkout and may be dirty; preserve it.
- Use an isolated worktree for new work.
- Keep design, implementation, tests, independent review, reconciliation, CI,
  commit, push, merge, DGX deployment, runtime health, inbound polling,
  outbound delivery, and Telegram user-visible delivery as separate gates.
- Service health, polling progress, empty updates, `getMe`, or webhook status
  do not prove Telegram user-visible delivery.

## Current topology

| Reference | Verified state |
|---|---|
| Repository | `https://github.com/cwliao/hermes-agent` |
| `origin/main` | `a5bebea8f8af78ea6996fbc10d2ea9e77d23b286` (merge commit for PR #48; pre-merge base was `2b5eb8437a...`) |
| Primary checkout | Recorded as `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8`; intentionally dirty. Carried forward: that path is absent on the Linux host used for the 2026-08-18 Claude continuation session, where `~/.hermes/hermes-agent` sits on `main` at `b7b362b2f1bcf494ba7f0acf2236452caacb05b6` and is clean |
| Isolated ticket worktree | `ticket/hermes-multi-agent-claude-worker-001`, implementation HEAD `af3034993fae20b6d6a552cd014fa0646cbf7af2` |
| Pull request | PR #48 **merged and closed** 2026-08-18T07:30:25Z; head `f97f3402f38bb735430c3296fbb19fc387dc7348`, merge commit `a5bebea8f8...`; required CI was green on that head before merge |
| DGX identity | WSL Ubuntu only; expected hostname `55-0940189-03`, user `cwliao`, service `hermes-gateway.service` |

## Engineering roadmap

1. Preserve the merged/deployed Hermes baseline and its separate Telegram
   delivery evidence.
2. Finish `HERMES-MULTI-AGENT-CLAUDE-WORKER-001` without weakening the
   four-lane contract: native Hermes, Claude, Grok, and AGY.
3. Investigate the independent gateway slice flakiness through a separately
   authorized follow-up; do not silently expand PR #48.
4. PR #48 is merged, which closes the code gate only. The four-lane runtime
   preflight, graph execution, verifier/synthesis, Dashboard evidence, and the
   Telegram user-visible test are the remaining work for this ticket. DGX
   deployment, restart, runtime checks, and the metadata-only Telegram
   delivery test each still require explicit user authorization and remain
   unexecuted.

## Ticket status

| Ticket | Status | Evidence / next action |
|---|---|---|
| `HERMES-MULTI-AGENT-CLAUDE-WORKER-001` | `MERGED_END_TO_END_TEST_NOT_RUN` | Implementation `af3034993...`; focused swarm tests 6/6 (re-run 2026-08-18); Claude and Grok PASS; AGY blocked by interactive TTY. CI run `32102234859` failed on `af303...` at attempt 2, but run `32103854648` on PR head `f97f3402f3...` concluded success with all eight slices and the aggregate required check passing. PR #48 merged by the user through the GitHub web UI. Code gate closed only: ticket acceptance gates 3-9 (four-lane runtime preflight, graph execution, worker completion, verifier/synthesis, Dashboard evidence, Telegram user-visible delivery, cleanup) are all unexecuted, and the AGY lane is still BLOCKED on an interactive TTY. |
| Gateway slice flakiness follow-up | `NOT_OPEN` | The three fresh-final failures did not recur on unchanged code in CI, and 28/28 passed locally at the PR head; treat as flake, not a baseline break. GitHub Issues are disabled. Create a repo-local plan only after explicit authorization; keep it separate from PR #48. |
| Previous orchestration baseline | Historical | Do not infer current PR or runtime state from older roadmap entries or releases. |

## Current ticket boundaries

The active ticket changes only existing Kanban/worker surfaces and tests. It
adds explicit lane identities, preflight-skill binding, bounded goal/worker
runtime settings, parser-safe worker construction, required metadata-only
handoffs, a fail-closed verifier, and a synthesizer result boundary. It does
not add a dispatcher, provider integration, Telegram adapter, custom
Dashboard, or alternate task store.

The intended disposable test is a Telegram-requested joke brainstorm:
Kanban goal -> four parallel workers -> verifier requiring all four lanes ->
Hermes synthesizer -> same-user Telegram response. The final response text is
not metadata evidence; only direct user confirmation closes the Telegram gate.

## CI history and current CI state

Run `32102234859` (head `af303...`, `run_attempt=2`) failed in Python tests
slice 3/8 at `tests/gateway/test_stream_consumer_fresh_final.py`, and its
aggregate `All required checks pass` job failed. PR #48 contains no gateway
files or gateway tests.

Run `32103854648` on the current PR head `f97f3402f3...` concluded `success`:
all eight Python slices, including slice 3/8, passed, and `All required checks
pass` passed. `osv-scanner` reports `neutral`, which is not a failure. The only
commit between the two runs is docs-only, so identical Python code failed and
then passed that slice. The failures are therefore flaky or
infrastructure-dependent, not a deterministic baseline regression. Locally,
that gateway file passed 28/28 at the PR head.

A green CI run is bound to an exact head SHA; re-verify on whatever head is
under consideration rather than reusing an older run. Because the flaky job is
a required check, it can randomly block future PRs the same way it blocked
this one.

GitHub Issues are disabled. No issue number exists. A gateway flake
investigation must be planned and authorized separately, then independently
reviewed and tested.

## DGX runtime boundary

The last separately verified runtime predates commit `af303...` and had
`HERMES_RELEASE_SHA=20aadf71cb4ead7db7d9590310465e90c937558b`, an active
running user service, zero restarts, and rollback evidence. It was not
re-verified in the 2026-08-18 Claude continuation session. This is historical
runtime evidence only; it does not prove `af303...` or `f97f340...` is
deployed. Relay remains
disabled by default. Before any remote mutation, reverify WSL route, hostname,
user, effective unit, release identity, and rollback evidence.

## Immediate next action

Read `docs/HANDOVER.md` and the active ticket plan, verify repository identity,
and confirm `origin/main` is `a5bebea8f8...`. Then ask the user for explicit
decisions on two separate questions: whether to authorize the four-lane
runtime preflight and the disposable end-to-end test that this ticket actually
exists to prove, and whether to open a repo-local plan for the gateway slice
flakiness. Do not deploy, restart, enable timers/relay, or run Telegram
testing without that explicit authorization.
