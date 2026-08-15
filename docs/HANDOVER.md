# Project Handover - hermes-agent

**Plan key:** hermes-agent
**Last verified:** 2026-08-15
**Handover owner/session:** Codex
**Authoritative project log:** `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI with memory,
  skills, scheduled jobs, delegated agents, and messaging-platform adapters.
- **Repository:** <https://github.com/cwliao/hermes-agent>
- **Working checkout:** `D:/PROJECT/Hermes/.worktrees/hermes-auth-002`.
- **Canonical remote:** `origin` -> `git@github.com:cwliao/hermes-agent.git`.
- **Canonical mainline:** `origin/main` at
  `1b3d4449553433100038f38e7b58f2f2dc489fa7`.
- **Current checkout branch:** `ticket/hermes-auth-002-target-config`.
- **Current checkout HEAD at implementation-review handoff:** `5e8df81b6`
  (`test: isolate DGX wrapper resolver harness`).
- **DGX runtime:** configured target, checkout
  `/home/cwliao/.hermes/hermes-agent`, service `hermes-gateway.service`.
- **In scope:** Hermes CLI, gateway, runtime state, platform adapters, CI,
  skills, documentation, and explicitly ticketed deployment work.
- **Out of scope by default:** laptop files as handover sources, unrelated DGX
  services, credentials/tokens, and external marketplace or SkillClaw changes
  without a separate reviewed ticket.

## 2. Goal and roadmap

- **Current goal:** Merge the independently reviewed and CI-green
  `HERMES-AUTH-002` implementation when the merge authorization is exercised;
  keep deployment separately authorized.
- **Completed and verified:** HERMES-AUTH-001 merged as PR #14 at
  `63bcd7ac` after main CI run `31791195033` passed all required checks,
  including the Windows wrapper job.
- **Completed and deployed:** HERMES-CALENDAR-GUARD-001 merged through PR #17
  and correction PR #18; `main` is `1b3d444955...`, and DGX runs immutable
  release snapshot
  `v2026.8.15-hermes-calendar-guard-1b3d444955`.
- **Telegram evidence:** direct outbound verification through the merged
  release returned `success=true`, `message_id=1919`, and `mirrored=true` for
  the configured SPARK target. Gateway polling still reports network timeout /
  reconnect warnings, so inbound polling is not claimed as healthy.
- **Deferred or pending:** HERMES-MONITORING-001 remains `BLOCKED`;
  HERMES-AUTH-002 is `READY_FOR_MERGE` after CI and independent
  implementation-review consensus. Live skill synchronization,
  SkillClaw work, and unrelated DGX service changes remain separate work.

## 3. Verified runtime and deployment state

- **Mainline versus deployed code:** mainline and deployed code are
  `1b3d4449553433100038f38e7b58f2f2dc489fa7`; DGX uses
  `/home/cwliao/.hermes/releases/v2026.8.15-hermes-calendar-guard-1b3d444955`
  selected through `29-hermes-calendar-guard-1b3d444955.conf`. The live source
  checkout remains untouched.
- **DGX service evidence:** the configured DGX target;
  `hermes-gateway.service` is active/running with MainPID `3161529`, exit
  status `0`, and `NRestarts=0`; cwd and release identity match the merged
  release. `hermes-gateway-recovery.timer` is active/waiting and its oneshot
  has `Result=success`.
- **DGX SSH evidence:** a bounded WSL probe returned `SSH_OK` through the
  authenticated route. No credentials were stored.
- **Storage and safety:** this handover refresh does not mutate Hermes memory,
  user data, credentials, or scheduled tasks.

## 4. Ticket and gate state

### Current ticket: HERMES-AUTH-002

- **Plan:** `docs/plans/2026-08-14-hermes-auth-002-target-config.md`
- **Status:** `READY_FOR_MERGE`.
- **Scope:** parameterize public DGX SSH host/user metadata through existing
  Hermes user configuration; preserve strict host-key and fail-closed auth
  behavior.
- **Required next action:** merge PR #16 after verifying its current
  ready-for-merge state. Keep deployment separately authorized.

### Other ticket state

- `HERMES-MONITORING-001`: `BLOCKED`; DGX SSH and agentmemory health reporting
  is not cleared for merge or deployment.
- `HERMES-AUTH-001`: `MERGED_DEPLOYED`; main CI, DGX runtime, and outbound
  Telegram evidence are recorded above.
- `HERMES-CALENDAR-GUARD-001`: `MERGED_DEPLOYED`; PR #17 introduced the
  recovery path and PR #18 corrected the release-wrapper/venv deployment
  integration. DGX timer and recovery oneshot are verified healthy.
- `HERMES-RELIABILITY-002`: implementation remains separate; do not infer
  completion from historical checkout files.
- `ARCH-002`: proposed next core ticket; not started here.

### Gate rule

Ticket implementation, local tests, independent cross-review, reconciliation,
CI, merge, DGX deployment, runtime health, and Telegram delivery are separate
gates. A pass at one gate cannot be reported as a pass at another. The
AUTH-002 implementation-review and CI gates are complete; merge and deployment
are not complete. Telegram readiness and future ticket gates remain separate.

## 5. Safe continuation instructions

1. Read this handover, `docs/ROADMAP-HERMES-DGX.md`, and the current ticket
   plan before acting.
2. Verify the exact repository root, remote, branch, HEAD, and worktree before
   using any project fact.
3. Preserve every pre-existing dirty and untracked file; do not reset, clean,
   discard, or overwrite unrelated work.
4. Use the repository's managed `.venv` for tests and CLI checks.
5. Route Claude/Codex/AGY only to a uniquely addressable, real authenticated
   session: DGX Spark first, then local WSL, then native Windows. A headless
   fallback is allowed only after those candidates are unavailable and a
   bounded preflight proves it is authenticated and usable.
6. Do not change DGX runtime state without a reviewed deployment gate and
   rollback evidence; the current release already has both.
7. Before ending the next session, refresh this handover with verified facts
   only, including the exact next action and any remaining gate.
