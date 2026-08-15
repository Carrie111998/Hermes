# Project Handover - hermes-agent

**Plan key:** hermes-agent  
**Last verified:** 2026-08-16  
**Handover owner/session:** Codex  
**Authoritative project log:** `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI with memory, skills, scheduled jobs, delegated agents, and messaging-platform adapters.
- **Repository:** <https://github.com/cwliao/hermes-agent>
- **Canonical mainline:** `main` at `0fe3773ccfbec860984d0dc93adc4875ca2d5d4b` (PR #22).
- **DGX runtime:** configured target, live source checkout `/home/cwliao/.hermes/hermes-agent`, user service `hermes-gateway.service`.
- **In scope:** Hermes CLI, gateway, runtime state, platform adapters, CI, skills, documentation, and explicitly ticketed deployment work.
- **Out of scope by default:** laptop files as handover sources, unrelated DGX services, credentials/tokens, and external marketplace or SkillClaw changes without a separate reviewed ticket.

The DGX source checkout is a deployment input and remains separate from the immutable release selected by systemd. No laptop checkout is authoritative for runtime state.

## 2. Goal and roadmap

- **Current goal:** diagnose the remaining Telegram inbound polling degradation after the merged transport implementation; do not claim inbound readiness from process health or a returned `start_polling()`.
- **Completed and deployed:** HERMES-UPDATE-001 Lane 2/3 correction set, PR #22, merged SHA `0fe3773ccfbec860984d0dc93adc4875ca2d5d4b`; immutable DGX release is active.
- **E2E state:** gateway process/service is healthy and outbound Telegram delivery passed, but inbound polling has no qualifying `getUpdates` progress evidence.
- **Deferred:** ARCH-002 remains the next core candidate after the active Telegram transport gate; HERMES-MONITORING-001 remains blocked and does not absorb this transport diagnosis.

## 3. Verified runtime and deployment state

- **DGX release:** `/home/cwliao/.hermes/releases/v2026.8.16-hermes-update-001-0fe3773ccf`.
- **Release identity:** `.hermes-release-sha` is `0fe3773ccfbec860984d0dc93adc4875ca2d5d4b`.
- **Effective gateway drop-in:** `31-hermes-update-001-0fe3773ccf.conf`; the prior `30-hermes-telegram-transport-77bcb5d0717e.conf` and release remain available for rollback.
- **Gateway evidence:** `active/running`, MainPID `3992364`, `ExecMainStatus=0`, `NRestarts=0`; effective WorkingDirectory and PYTHONPATH point to the new release.
- **Calendar guard evidence:** recovery timer `active/waiting`; its service and wrapper now point to the new release. The old release and drop-in were not deleted.
- **Source checkout evidence:** live checkout remains clean at HEAD `1c14d2b9df29da845fb2a56b2fbe12cf8ee507cb`; it was not reset, overwritten, or used as the active runtime source.

### Telegram E2E boundary

- **Outbound:** `hermes send --to telegram:SPARK --json` returned `success=true`, `message_id=1967`, `mirrored=true`.
- **Inbound:** after restart at approximately `06:31:43` CST, logs showed connection and menu registration through approximately `06:31:52`, but no qualifying successful `getUpdates` progress was observed through `06:37:25` CST. The inbound state remains `DEGRADED/UNPROVEN`.
- **Interpretation:** service health PASS and outbound delivery PASS do not upgrade Telegram inbound readiness. The CLI also reports an installed-service-definition-outdated warning; the effective systemd drop-in was separately verified and is running the intended release.

## 4. Ticket and gate state

### Current lane: HERMES-TELEGRAM-TRANSPORT-001 diagnosis

- **Plan:** `docs/plans/2026-08-15-hermes-telegram-transport-001.md`.
- **Status:** `MERGED_DEPLOYED_RUNTIME_DEGRADED`.
- **Next action:** map the current DGX primary/fallback Telegram network path and polling-progress ownership, then prepare a narrow correction only if the root cause is confirmed.
- **Do not:** claim inbound readiness, fold ARCH-002 or monitoring work into this lane, or change credentials/allowlists/webhook state.

### Other ticket state

- `HERMES-UPDATE-001`: `MERGED_DEPLOYED`; PR #22, SHA `0fe3773c...`, Lane 2/3 review PASS and 33 targeted tests passed.
- `HERMES-AUTH-001`: merged/deployed separately.
- `HERMES-CALENDAR-GUARD-001`: merged/deployed; timer and recovery unit remain active.
- `HERMES-MONITORING-001`: BLOCKED; do not infer readiness from this deployment.
- `ARCH-002`: proposed next core ticket, deferred until the current Telegram transport gate is resolved or explicitly re-sequenced.

### Gate rule

Implementation, tests, independent review, reconciliation, CI, merge, deployment, service health, inbound polling, outbound delivery, and rollback are separate gates. The current lane has merge/deployment/service/outbound evidence but remains blocked on inbound polling evidence.

## 5. Safe continuation instructions

1. Read this handover, `docs/ROADMAP-HERMES-DGX.md`, and the current ticket plan before acting.
2. Verify repository identity, current GitHub `main` SHA, DGX hostname, service, effective drop-ins, release marker, and rollback release.
3. Preserve the dirty laptop worktree and the DGX live source checkout; do not reset, clean, or overwrite either.
4. Use bounded, read-only DGX diagnostics for the Telegram network/polling diagnosis before implementation.
5. Keep the current release and prior `30-...` release/drop-in as rollback evidence.
6. Record direct inbound and outbound evidence separately; service active alone is insufficient.
7. Refresh this handover only with verified facts and exact next action.
