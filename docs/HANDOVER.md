# Project Handover - hermes-agent

Plan key: `hermes-agent`
Last verified: 2026-08-18 (Asia/Taipei)
Authoritative roadmap: `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

- Repository: `https://github.com/cwliao/hermes-agent`
- Verified `origin/main`: `2b5eb8437a0bdae0529e7f4af5b8771b5f339997`.
- Primary checkout: `D:/PROJECT/Hermes`, branch `ticket/hermes-auth-001`,
  HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8`.
- The primary checkout is intentionally dirty. Preserve all existing changes
  and untracked files; do not reset, clean, overwrite, stage, or commit it.
- Active isolated worktree: this worktree, branch
  `ticket/hermes-multi-agent-claude-worker-001`, HEAD
  `af3034993fae20b6d6a552cd014fa0646cbf7af2`.
- PR #48 is open against `main`; its required CI is currently unstable.
- DGX is reached only from WSL Ubuntu. Verify hostname `55-0940189-03`, user
  `cwliao`, and `hermes-gateway.service` before any remote mutation.

This handover covers the four-lane Kanban worker correction ticket. It does
not claim that the current PR is merged, deployed, restarted, or Telegram
user-visible.

## 2. Current goal and roadmap

The current goal is to finish the correction ticket's CI and review gates for
the four explicit lanes: native Hermes, Claude, Grok, and AGY. The intended
test is a disposable, metadata-only joke-brainstorm goal created through the
existing Kanban graph, with all four worker lanes, a fail-closed verifier, a
Hermes synthesizer, and a separately verified Telegram user-visible response.

Completed for this ticket:

- Design packet reached the required two-of-three review quorum.
- Implementation commit: `af3034993fae20b6d6a552cd014fa0646cbf7af2`.
- Claude: `PASS`; Grok: `PASS`; AGY: `BLOCKED` because the DGX CLI required an
  interactive TTY. AGY is not counted and was not duplicated.
- Focused swarm suite: 6 passed.
- Branch pushed and PR #48 opened.

Current blocker:

- CI run `32102234859` fails in Python tests slice 3/8, in existing gateway
  fresh-final tests under `tests/gateway/test_stream_consumer_fresh_final.py`.
- The failed job was rerun and reproduced the same three failures.
- PR #48 changes no gateway files or gateway tests; the aggregate required
  check remains failed and PR #48 must not be merged on this state.
- GitHub Issues are disabled, so a gateway follow-up needs a separately
  authorized repo-local plan or another explicitly approved ticket path.

Do not fix the gateway regression inside PR #48 without a new, explicitly
authorized scope. Do not merge, deploy, restart DGX, enable relay/timer, or
run the Telegram test for `af303...` while CI is blocked.

## 3. Verified implementation state

PR #48 changes only these existing surfaces:

- `hermes_cli/kanban.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban_swarm.py`
- `optional-skills/devops/kanban-worker/SKILL.md`
- `tests/hermes_cli/test_kanban_swarm.py`
- `tools/kanban_tools.py`
- the ticket design under `docs/plans/`

The implementation provides four explicit lane identities, expected
lane/preflight-skill binding, bounded goal and worker runtime settings,
parser-safe worker construction, required worker completion metadata, a
fail-closed verifier, and a synthesizer result boundary in the existing task
`result` field. Dashboard and Telegram correlation is metadata-only and uses
opaque IDs and status fields.

The focused swarm test passed 6/6 and `git diff --check` passed. CI is the
authoritative repository gate and is not replaced by local tests.

The last separately verified DGX runtime was the pre-PR release identified by
`HERMES_RELEASE_SHA=20aadf71cb4ead7db7d9590310465e90c937558b`, with the user
service active/running, zero restarts, and rollback evidence retained. This
does not prove that `af303...` is deployed. Relay remains disabled by default.

## 4. Ticket and gate state

Active ticket plan: `docs/plans/2026-08-18-hermes-multi-agent-claude-worker-001.md`.

The gates remain separate: source identity, ticket design, implementation,
tests, Claude/Grok/AGY review, reconciliation, CI, commit, push, merge, DGX
deployment, runtime health, inbound polling, outbound delivery, and Telegram
user-visible delivery.

Current state: implementation committed and pushed, review quorum PASS,
focused tests PASS, CI BLOCKED by reproduced baseline gateway failures, and
all later gates unsatisfied for `af303...`.

## 5. Safe continuation procedure

1. Read this file, `docs/ROADMAP-HERMES-DGX.md`, and the active ticket plan.
2. Verify repository root, remote, branch, HEAD, worktree, and authenticated
   GitHub `origin/main` in the exact command context.
3. Preserve `D:/PROJECT/Hermes`; use an isolated worktree for every change.
4. Treat the CI failure as a separate gateway follow-up. Do not expand PR #48
   unless the user explicitly authorizes that scope.
5. Use one authenticated reviewer per family; never duplicate Claude, Grok, or
   AGY across platforms. Reconcile any non-quorum finding.
6. Before DGX mutation, verify hostname, user, effective unit, release SHA,
   and rollback evidence.
7. Keep reports metadata-only: no message bodies, prompts, joke text, tokens,
   credentials, or sensitive absolute paths.

## Copyable Claude continuation prompt

```text
You are taking over the hermes-agent repository. Use the repo as the source of truth.
Read docs/HANDOVER.md, docs/ROADMAP-HERMES-DGX.md, and
docs/plans/2026-08-18-hermes-multi-agent-claude-worker-001.md before acting.

Verify repository root, remote, branch, HEAD, worktree, and authenticated
origin/main. The primary checkout D:/PROJECT/Hermes is intentionally dirty;
do not reset, clean, overwrite, stage, or commit it. Work only in an isolated
worktree.

The current implementation is commit af3034993fae20b6d6a552cd014fa0646cbf7af2
on PR #48. Claude and Grok returned PASS on the same metadata-only packet;
AGY was blocked by a required interactive TTY and is not counted. The focused
swarm suite passed 6 tests.

CI run 32102234859 is blocked: Python tests slice 3/8 fails three existing
gateway fresh-final tests in tests/gateway/test_stream_consumer_fresh_final.py;
the failed job rerun reproduced them. PR #48 contains no gateway files or
gateway tests. Do not fix that gateway issue inside PR #48 unless the user
explicitly authorizes a separate scope. GitHub Issues are disabled, so a
follow-up needs an authorized repo-local plan or another approved ticket path.

Do not merge, deploy, restart DGX, enable relay/timer, or run Telegram testing
for af303 while CI is blocked. Keep design, implementation, tests, review,
CI, commit, push, merge, deployment, runtime health, inbound polling,
outbound delivery, and Telegram user-visible delivery as separate gates.
Before ending, update the repo handover only with facts verified in this
session, and keep evidence metadata-only.
```
