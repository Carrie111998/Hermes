# Project Handover - hermes-agent

**Plan key:** hermes-agent  
**Last verified:** 2026-08-16 09:44 CST  
**Handover owner/session:** Codex  
**Authoritative project log:** `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI with memory, skills, scheduled jobs, delegated agents, and messaging-platform adapters.
- **Repository:** <https://github.com/cwliao/hermes-agent>
- **Canonical mainline:** merged code commit `29d4663bb94cf2d9603d2de9d437a431b5101f14` (PR #25); handover refreshes are documentation-only commits on `main` after deployment.
- **DGX runtime:** configured target, live source checkout `/home/cwliao/.hermes/hermes-agent`, user service `hermes-gateway.service`.
- **In scope:** Hermes CLI, gateway, runtime state, platform adapters, CI, skills, documentation, and explicitly ticketed deployment work.
- **Out of scope by default:** laptop files as handover sources, unrelated DGX services, credentials/tokens, and external marketplace or SkillClaw changes without a separate reviewed ticket.

The DGX source checkout is a deployment input and remains separate from the immutable release selected by systemd. No laptop checkout is authoritative for runtime state.

The local audit checkout observed during this refresh is `D:/PROJECT/Hermes`, branch `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8`; it is dirty with pre-existing changes and is not the canonical mainline or runtime source. Those files were preserved and were not staged, committed, reset, or cleaned.

## 2. Goal and roadmap

- **Current goal:** diagnose the remaining Telegram inbound polling degradation after the merged transport implementation; do not claim inbound readiness from process health or a returned `start_polling()`.
- **Completed and deployed:** HERMES-UPDATE-001, PR #22, merged SHA `0fe3773ccfbec860984d0dc93adc4875ca2d5d4b`; immutable DGX release is active.
- **Completed and corrected:** Calendar Guard wrapper correction, PR #24, merged SHA `91ae4a7f7a73a4c331e2f5dd018b7ce2ca5c03a9`; the valid immutable release path is no longer mistaken for the fallback sentinel.
- **Completed and deployed:** managed-CA trust correction, PR #25, squash merge `29d4663bb94cf2d9603d2de9d437a431b5101f14`; authenticated Claude and AGY implementation reviews both PASS, CI run `31918804987` passed, and the new immutable DGX release is active.
- **E2E state:** gateway process/service is healthy and outbound Telegram delivery passed, but inbound polling has no qualifying `getUpdates` progress evidence. The 2026-08-16 bounded DGX window recorded `Connected to Telegram (polling mode)` and gateway startup completion, but no explicit `getUpdates` success or accepted-update event.
- **Deferred:** ARCH-002 remains the next core candidate after the active Telegram transport gate; HERMES-MONITORING-001 remains blocked and does not absorb this transport diagnosis.

## 3. Verified runtime and deployment state

- **DGX release:** `/home/cwliao/.hermes/releases/v2026.8.16-hermes-ca-29d4663bb9`.
- **Release identity:** `.hermes-release-sha` is `29d4663bb94cf2d9603d2de9d437a431b5101f14`.
- **Effective gateway drop-in:** `32-hermes-ca-29d4663bb9.conf`; prior release/drop-ins, including `31-hermes-update-001-0fe3773ccf.conf` and `ssl-ca.conf`, remain available for rollback.
- **Gateway evidence:** `active/running`, MainPID `27416`, `ExecMainStatus=0`, `NRestarts=0`; effective WorkingDirectory and PYTHONPATH point to the new release after a bounded stability check.
- **Calendar guard evidence:** recovery timer `active/waiting`; the deployed wrapper now preserves the valid release path. Direct wrapper execution exited 0, and cron job `85dcd4b817d6` completed successfully when manually triggered. The old release and drop-in were not deleted.
- **Source checkout evidence:** the DGX live checkout remains separate from the active immutable release and was not reset or overwritten.

### Telegram E2E boundary

- **Outbound:** the last verified outbound command returned `success=true`, `message_id=1967`, `mirrored=true`.
- **Inbound:** after the latest bounded restart window, no qualifying successful `getUpdates` progress was observed. The inbound state remains `DEGRADED/UNPROVEN`.
- **Interpretation:** service health PASS and outbound delivery PASS do not upgrade Telegram inbound readiness.

## 4. Ticket and gate state

### Current lane: HERMES-TELEGRAM-INBOUND-001

- **Plan:** `docs/plans/2026-08-16-hermes-telegram-inbound-001.md`.
- **Status:** `IMPLEMENTATION_REVIEW_BLOCKED_CLAUDE_UNAVAILABLE`; design consensus, bounded DGX diagnosis, and the failed Claude/AGY correction-set review are recorded in the plan.
- **Parent lane:** HERMES-TELEGRAM-TRANSPORT-001 is `MERGED_DEPLOYED_RUNTIME_DEGRADED`; its service and outbound gates passed, but inbound readiness remains unproven.
- **Next action:** obtain one authenticated Claude PASS on the exact correction packet and reconcile it with the recorded AGY PASS. Implementation remains blocked until both reviewer-family gates pass.
- **Do not:** implement, restart, deploy, change credentials/allowlists/webhook state, or claim inbound readiness before the ticket review and direct polling evidence gates pass.

### Other ticket state

- `HERMES-UPDATE-001`: `MERGED_DEPLOYED`; PR #22, SHA `0fe3773c...`, Lane 2/3 review PASS and targeted tests passed.
- `HERMES-AUTH-001`: merged/deployed separately.
- `HERMES-AUTH-002`: `MERGED_DEPLOYED`; PR #16 merged as `826349ccbfe165ef9f2f7f47f72ed53226c13603`; implementation commit `5e8df81b6`, targeted tests/CI/Windows wrapper evidence recorded. The active release SHA `0fe3773ccfbec860984d0dc93adc4875ca2d5d4b` is four commits ahead of the AUTH-002 merge, so no separate AUTH-002 runtime restart is required.
- `HERMES-CALENDAR-GUARD-001`: `MERGED_DEPLOYED`; PR #17/#18 plus correction PR #24; timer, recovery unit, wrapper, and cron execution are verified.
- `Managed CA trust correction`: `MERGED_DEPLOYED`; PR #25, merge `29d4663b...`, CI `31918804987`, release `v2026.8.16-hermes-ca-29d4663bb9`; focused release tests `6 passed`, source/release hashes matched, and service health is active/running. Telegram inbound remains a separate `DEGRADED/UNPROVEN` gate.
- `HERMES-MONITORING-001`: `BLOCKED`; do not infer readiness from this deployment.
- `ARCH-002`: proposed next core ticket, deferred until the current Telegram transport gate is resolved or explicitly re-sequenced.

### Gate rule

Implementation, tests, independent review, reconciliation, CI, merge, deployment, service health, inbound polling, outbound delivery, and rollback are separate gates. The current lane has merge/deployment/service/outbound evidence but remains blocked on inbound polling evidence.

## 5. Safe continuation instructions

1. Read this handover, `docs/ROADMAP-HERMES-DGX.md`, and the current ticket plan before acting.
2. Verify repository identity, current GitHub `main` SHA, DGX hostname, service, effective drop-ins, release marker, and rollback release.
3. Preserve the dirty laptop worktree and the DGX live source checkout; do not reset, clean, or overwrite either.
4. Use bounded, read-only DGX diagnostics for the Telegram network/polling diagnosis before implementation.
5. Keep the current release and prior release/drop-in as rollback evidence.
6. Record direct inbound and outbound evidence separately; service active alone is insufficient.
7. Refresh this handover only with verified facts and the exact next action.
