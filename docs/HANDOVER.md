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
  `6a225b43502463e0e21e305a96b456444b82017c`.
- **Current checkout branch:** `ticket/hermes-reliability-002`.
- **Current checkout HEAD:**
  `7e78039c97173d59413826f0cc09fe88b461ce99` (`feat: add safe Claude remote
  recovery`). The branch name is historical and must not be treated as proof
  that HERMES-RELIABILITY-002 is complete.
- **DGX runtime:** host `140.96.58.171`, checkout
  `/home/cwliao/.hermes/hermes-agent`, service `hermes-gateway.service`.
- **In scope:** Hermes CLI, gateway, runtime state, platform adapters, CI,
  skills, documentation, and explicitly ticketed deployment work.
- **Out of scope by default:** laptop files as handover sources, unrelated DGX
  services, credentials/tokens, and external marketplace or SkillClaw changes
  without a separate reviewed ticket.

## 2. Goal and roadmap

- **Current goal:** Advance `HERMES-CLAUDE-RECOVERY-001` through its CI/PR gate
  after resolving the external review blocker, without repurposing the existing
  KLIB Claude session; then reconcile older pending tickets before `ARCH-002`.
- **Completed and verified:** CI baseline and earlier ARCH-001 work are on the
  recorded mainline history; Telegram controlled execution reached PR #10 and
  passed CI run `31571694814`; HERMES-AUTH-001's bounded DGX SSH probe reached
  the host without mutating runtime data.
- **Active, pending CI:** HERMES-CLAUDE-RECOVERY-001 has the scoped commit
  `7e78039c` plus an uncommitted polling-status correction. Independent Claude
  and AGY re-review both returned `PASS` on the same bounded packet, so the
  external-review blocker is resolved. It has no PR and is not merged,
  deployed, or enabled as a scheduled task.
- **Deferred or pending:** HERMES-MONITORING-001 remains `BLOCKED`;
  HERMES-AUTH-001 and HERMES-RELIABILITY-002 remain
  `IMPLEMENTED_PENDING_REVIEW`. Live skill synchronization, SkillClaw work,
  and unrelated DGX service changes remain separate work.
- **Next candidates after this ticket's remaining CI/merge gates:** complete the
  required independent reviews and reconciliation for the pending tickets,
  then advance `ARCH-002`. Do not promote a candidate to active work without
  its ticket and acceptance gates.

## 3. Verified runtime and deployment state

- **Mainline versus deployed code:** mainline is `6a225b4`; the recorded DGX
  release is the older Telegram-controlled snapshot
  `/home/cwliao/.hermes/releases/v2026.8.12-telegram-controlled-af99f0f1ad`,
  selected through `24-telegram-controlled-execution.conf`. A running DGX
  service is not evidence that the current ticket is deployed.
- **DGX service evidence:** `hermes-gateway.service` was recorded active with
  MainPID `4109761`, exit status `0`, and `NRestarts=0`.
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
- **Status:** `IMPLEMENTED_PENDING_CI`; the external-review blocker is resolved.
- **Scope:** opt-in native-Windows `hermes claude-recovery status|repair`;
  disabled and empty by default; no task creation, OAuth, kill/restart,
  DGX mutation, Telegram watchdog, or LLM watchdog.
- **Evidence:** 44 original focused/regression tests plus 1 correction test
  passed in the managed environment. Ruff, `py_compile`, diff checks, isolated
  CLI smoke, local security review, and local operations review passed.
  Independent Claude and AGY re-review both returned `PASS` on the same bounded
  read-only packet after the correction; consensus was reached. Live readiness
  remains unverified because no Hermes-scoped task is configured or enabled.
- **Required next action:** run the CI/PR gate for the corrected implementation.
  Do not create a new headless session, repurpose the KLIB-scoped session,
  inject a guessed TTY, or bypass approval and permission boundaries.
- **Repository state:** commit `7e78039c` is pushed, while the polling
  correction and its test are currently uncommitted. The worktree also contains
  unrelated dirty changes that must be preserved.

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
ticket cannot be called complete while its external review is blocked.

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
6. Do not merge or deploy HERMES-CLAUDE-RECOVERY-001 until its CI, PR review,
   merge, and deployment gates are separately passed. Do not change DGX runtime
   state without a reviewed deployment gate and rollback evidence.
7. Before ending the next session, refresh this handover with verified facts
   only, including the exact next action and any remaining gate.
