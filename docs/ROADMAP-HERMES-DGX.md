# Hermes Architecture Roadmap

Snapshot: 2026-08-18 (Asia/Taipei). This file is the current repository-local
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
| `origin/main` | `2b5eb8437a0bdae0529e7f4af5b8771b5f339997` |
| Primary checkout | `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8`; intentionally dirty |
| Isolated ticket worktree | `ticket/hermes-multi-agent-claude-worker-001`, HEAD `af3034993fae20b6d6a552cd014fa0646cbf7af2` |
| Pull request | PR #48 open against `main`; required CI currently unstable |
| DGX identity | WSL Ubuntu only; expected hostname `55-0940189-03`, user `cwliao`, service `hermes-gateway.service` |

## Engineering roadmap

1. Preserve the merged/deployed Hermes baseline and its separate Telegram
   delivery evidence.
2. Finish `HERMES-MULTI-AGENT-CLAUDE-WORKER-001` without weakening the
   four-lane contract: native Hermes, Claude, Grok, and AGY.
3. Resolve the independent CI gateway regression through a separately
   authorized follow-up; do not silently expand PR #48.
4. Only after CI and the required review gates pass, consider merge, DGX
   deployment, restart, runtime checks, and the separately authorized
   metadata-only Telegram delivery test.

## Ticket status

| Ticket | Status | Evidence / next action |
|---|---|---|
| `HERMES-MULTI-AGENT-CLAUDE-WORKER-001` | `CI_BLOCKED_BASELINE_GATEWAY_REGRESSION` | Implementation `af3034993...`; focused swarm tests 6/6; Claude and Grok PASS; AGY blocked by interactive TTY; CI run `32102234859` reproduces three existing gateway fresh-final failures. Keep PR #48 open and do not deploy. |
| Gateway follow-up | `NOT_OPEN` | GitHub Issues are disabled. Create a repo-local plan only after explicit authorization; keep it separate from PR #48. |
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

## CI blocker

Run `32102234859` fails in Python tests slice 3/8 at
`tests/gateway/test_stream_consumer_fresh_final.py`. The failed job rerun
reproduced the same three failures. PR #48 contains no gateway files or
gateway tests. Other required checks passed, but the aggregate required check
is failed. This is a baseline gateway blocker, not evidence that the current
Kanban implementation is safe to merge.

GitHub Issues are disabled. No issue number exists. A gateway correction must
be planned and authorized separately, then independently reviewed and tested.

## DGX runtime boundary

The last separately verified runtime predates commit `af303...` and had
`HERMES_RELEASE_SHA=20aadf71cb4ead7db7d9590310465e90c937558b`, an active
running user service, zero restarts, and rollback evidence. This is historical
runtime evidence only; it does not prove `af303...` is deployed. Relay remains
disabled by default. Before any remote mutation, reverify WSL route, hostname,
user, effective unit, release identity, and rollback evidence.

## Immediate next action

Read `docs/HANDOVER.md` and the active ticket plan, verify repository identity,
then decide whether the user explicitly authorizes a separate repo-local
gateway follow-up. Until that decision and a passing CI gate, do not merge,
deploy, restart, enable timers/relay, or run Telegram testing for `af303...`.
