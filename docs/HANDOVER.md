# Project Handover - hermes-agent

**Plan key:** hermes-agent
**Last verified:** 2026-08-14
**Handover owner/session:** Codex
**Authoritative project log:** `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI with memory,
  skills, scheduled jobs, delegated agents, and messaging-platform adapters.
- **Repository:** <https://github.com/cwliao/hermes-agent>
- **Working checkout:** `D:/PROJECT/Hermes`.
- **Canonical remote:** `origin` -> `git@github.com:cwliao/hermes-agent.git`.
- **Canonical mainline:** `origin/main` at
  `7018f93aaee7aa0319ee342ea860ad90da206c9b`.
- **Current checkout branch:** `ticket/hermes-reliability-002`.
- **Current checkout HEAD:** `9f3755c42` (`fix: register Claude recovery
  builtin`) on the historical ticket branch. The canonical integrated state is
  `origin/main=7018f93aa`; do not infer mainline status from the ticket branch.
- **DGX runtime:** host `140.96.58.171`, checkout
  `/home/cwliao/.hermes/hermes-agent`, service `hermes-gateway.service`.
- **In scope:** Hermes CLI, gateway, runtime state, platform adapters, CI,
  skills, documentation, and explicitly ticketed deployment work.
- **Out of scope by default:** laptop files as handover sources, unrelated DGX
  services, credentials/tokens, and external marketplace or SkillClaw changes
  without a separate reviewed ticket.

## 2. Goal and roadmap

- **Current goal:** Preserve the verified merged/deployed state of
  `HERMES-CLAUDE-RECOVERY-001` and move to the next independently reviewed
  ticket, without repurposing the existing KLIB Claude session.
- **Completed and verified:** CI baseline and earlier ARCH-001 work are on the
  recorded mainline history; Telegram controlled execution reached PR #10 and
  passed CI run `31571694814`; HERMES-AUTH-001's bounded DGX SSH probe reached
  the host without mutating runtime data.
- **Completed and deployed:** HERMES-CLAUDE-RECOVERY-001 merged as PR #12 at
  `7018f93aa` after CI run `31768361031` passed. DGX now runs the immutable
  release snapshot `v2026.8.14-hermes-claude-recovery-7018f93aa`; no
  Hermes-scoped scheduled task was enabled.
- **Deferred or pending:** HERMES-MONITORING-001 remains `BLOCKED`;
  HERMES-AUTH-001 and HERMES-RELIABILITY-002 remain
  `IMPLEMENTED_PENDING_REVIEW`. Live skill synchronization, SkillClaw work,
  and unrelated DGX service changes remain separate work.
- **Next candidates:** complete the required independent reviews and
  reconciliation for pending tickets, then advance `ARCH-002`. Do not promote
  a candidate to active work without its ticket and acceptance gates.

## 3. Verified runtime and deployment state

- **Mainline versus deployed code:** mainline is `7018f93aa`; DGX uses
  `/home/cwliao/.hermes/releases/v2026.8.14-hermes-claude-recovery-7018f93aaee`
  selected through `25-hermes-claude-recovery-7018f93aa.conf`. The live
  checkout remains untouched.
- **DGX service evidence:** `hermes-gateway.service` is active/running with
  MainPID `1969858`, exit status `0`, and `NRestarts=0`; cwd and `PYTHONPATH`
  match the release snapshot and the new PID has no Traceback/ERROR logs.
- **Telegram evidence:** the gateway was active or attempting connection, but
  end-to-end Telegram readiness/polling was not confirmed in the observed
  window.
- **DGX SSH evidence:** a bounded WSL probe returned `SSH_OK`, hostname
  `55-0940189-03`, user `cwliao`. The requested WSL key path was absent; an
  already available identity/agent was used. No credentials were stored.
- **Storage and safety:** this handover refresh does not mutate Hermes memory,
  user data, credentials, scheduled tasks, or DGX runtime state.

## 4. Ticket and gate state

### Current ticket: HERMES-CLAUDE-RECOVERY-001

- **Plan:** `docs/plans/2026-08-14-hermes-claude-recovery-001-auto.md`
- **Status:** `MERGED_DEPLOYED`.
- **Scope:** opt-in native-Windows `hermes claude-recovery status|repair`;
  disabled and empty by default; no task creation, OAuth, kill/restart,
  DGX mutation, Telegram watchdog, or LLM watchdog.
- **Evidence:** 45 local tests passed; GitHub CI run `31768361031` passed all
  required checks; Claude and AGY independent review consensus is `PASS/PASS`.
  DGX snapshot, systemd drop-in, compile, service state, cwd/PYTHONPATH, and
  new-PID log evidence are verified. Live Telegram readiness remains
  unverified and no Hermes-scoped task is configured or enabled.
- **Required next action:** select the next ticket; do not create a new
  headless session, repurpose the KLIB-scoped session, inject a guessed TTY, or
  bypass approval and permission boundaries.
- **Repository state:** PR #12 is merged; the current checkout contains only
  the follow-up documentation changes plus unrelated pre-existing dirty work
  that must be preserved.

### Other ticket state

- `HERMES-MONITORING-001`: `BLOCKED`; DGX SSH and agentmemory health reporting
  is not cleared for merge or deployment.
- `HERMES-AUTH-001`: `IMPLEMENTED_PENDING_REVIEW`; review and merge gates remain.
- `HERMES-RELIABILITY-002`: `IMPLEMENTED_PENDING_REVIEW`; do not infer
  completion from the current checkout branch name.
- `ARCH-002`: proposed next core ticket; not started here.

### Gate rule

Ticket implementation, local tests, independent cross-review, reconciliation,
CI, merge, DGX deployment, runtime health, and Telegram delivery are separate
gates. A pass at one gate cannot be reported as a pass at another. The current
ticket is complete for this implementation/deployment scope; Telegram
readiness and future ticket gates remain separate.

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
