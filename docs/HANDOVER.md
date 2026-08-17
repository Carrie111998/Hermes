# Project Handover - hermes-agent

**Plan key:** hermes-agent  
**Last verified:** 2026-08-17 (Asia/Taipei)
**Handover owner/session:** Codex  
**Authoritative project log:** `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI with memory, skills, scheduled jobs, delegated agents, and messaging-platform adapters.
- **Repository:** <https://github.com/cwliao/hermes-agent>
- **Merged runtime code baseline:** `e2f94e26b0a8b1db71c00e1607bca8f89f02aaea` (PR #33, ARCH-004; ARCH-003 remains the prior runtime-state baseline).
- **DGX runtime:** host `140.96.58.171`, hostname `55-0940189-03`, user service `hermes-gateway.service`.
- **In scope:** Hermes CLI, gateway, runtime state, platform adapters, CI, skills, documentation, and explicitly ticketed deployment work.
- **Out of scope by default:** laptop files as handover sources, unrelated DGX services, credentials/tokens, and external marketplace or SkillClaw changes without a separate reviewed ticket.

The DGX live source checkout is deployment input only. The active service uses an immutable release snapshot selected by a systemd user drop-in. The primary laptop checkout is not authoritative for runtime state.

The local audit checkout is `D:/PROJECT/Hermes`, branch `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8`; it remains dirty with pre-existing changes and untracked files. They were preserved and were not staged, reset, or cleaned.

## 2. Goal and roadmap

- **Current goal:** preserve the merged ARCH-002/ARCH-003/ARCH-004 runtime-state contracts, keep Telegram service/inbound/outbound gates separate, and verify real user-visible Telegram delivery.
- **Completed and deployed:** ARCH-002, PR #29, merge `3e9fd48dc28b7df186c780992351ef01febaa070`; ARCH-003, PR #30, merge `e8cdfd1e65191b68423afd7e12248d3c6e728e00`; and ARCH-004, PR #33, merge `e2f94e26b0a8b1db71c00e1607bca8f89f02aaea`. Each has separate review, CI, immutable-release, rollback, and post-deploy evidence.
- **Telegram runtime:** the gateway recovered from a transient startup timeout through its bounded retry path. Post-recovery logs show repeated qualifying empty `telegram_polling_progress` events; user-visible delivery remains a separate gate.
- **Deferred/blocked:** HERMES-MONITORING-001 remains BLOCKED. Telegram user-visible delivery is not claimed from service or polling health.
- **Next gate:** Telegram user-visible delivery verification. Service health and inbound polling remain supporting evidence only; neither closes the delivery gate.

## 3. Verified runtime and deployment state

- **DGX host identity:** `cwliao@55-0940189-03`.
- **Active release:** `/home/cwliao/.hermes/releases/v2026.8.17-hermes-arch-004-e2f94e26`.
- **Release marker:** `HERMES_RELEASE_SHA=e2f94e26b0a8b1db71c00e1607bca8f89f02aaea`.
- **Effective service:** user `hermes-gateway.service`, `ActiveState=active`, `SubState=running`, MainPID `1654068`, `NRestarts=0`, `ExecMainStatus=0` after the ARCH-004 restart and bounded post-start check.
- **Effective WorkingDirectory/PYTHONPATH:** the ARCH-004 immutable release above.
- **Rollback:** ARCH-003 release `v2026.8.17-hermes-arch-003-e8cdfd1e`, ARCH-002 release `v2026.8.16-hermes-arch-002-3e9fd48dc2`, and prior Telegram releases/drop-ins remain available.
- **Deployment manifest:** ARCH-004 rollback metadata is preserved under `/home/cwliao/.hermes/deploy-backups/hermes-arch-004-e2f94e26`; the prior ARCH-003 drop-in remains intact.

### Telegram evidence boundary

- At restart, the service recorded transient Telegram connect timeouts and queued bounded retry.
- At `17:18:22`, the gateway logged `Connected to Telegram (polling mode)`.
- From `17:18:33` onward, the gateway recorded repeated `telegram_polling_progress` with `result_class=empty`; no post-recovery degraded event was observed in the bounded log window.
- Read-only token-protected `getMe` and `getWebhookInfo` probes returned HTTP 200; `pending_update_count=0), no webhook URL was present.
- This proves current inbound polling progress, not user-visible response/delivery. Do not expose message content or credentials.

## 4. Ticket and gate state

### ARCH-002

- **Status:** `MERGED_DEPLOYED`.
- **PR/commit:** PR #29, head `d705c977...`, merge `3e9fd48d...`.
- **Implementation evidence:** focused runtime-state and gateway integration tests `24 passed`.
- **Review evidence:** identical metadata-only v3 packet SHA-256 `6d55a0944efd858ab63d5af809f81e6c5fab09d1c02c2f293fd11fae4ed213c1`; authenticated DGX AGY PASS and Claude Opus 4.8 PASS, correction set none.
- **CI evidence:** latest-head CI run `31937692260` / #127 completed successfully, including required checks.
- **Deployment evidence:** immutable release and effective service state are recorded above. Telegram delivery remains separate.

### Other ticket state

- **HERMES-TELEGRAM-TRANSPORT-001:** merged/deployed; current runtime recovered from the observed transient startup timeout, but user-visible delivery remains separate.
- **HERMES-TELEGRAM-INBOUND-001:** merged/deployed; qualifying empty polling progress is currently observed.
- **HERMES-AUTH-001 / AUTH-002:** merged/deployed separately; do not conflate them with ARCH-003.
- **HERMES-CALENDAR-GUARD-001:** merged/deployed; actual DGX guard naming/effective unit remains a separate documentation hygiene concern.
- **HERMES-MONITORING-001:** BLOCKED; do not infer readiness from gateway health.
- **ARCH-003:** `MERGED_DEPLOYED`; PR #30, implementation commit `9b777c0b1`, merge `e8cdfd1e...`; focused Windows dependency test `29 passed`; canonical runner ARCH-003 tests `24 passed` with its gateway integration collection limited by the borrowed KLIB venv missing PyYAML; authenticated AGY and fresh authenticated WSL Claude implementation reviews both `PASS`; CI run `31981532693` passed after retrying an unrelated pre-existing stream-consumer slice; immutable release and runtime health verified on DGX.
- **ARCH-004:** `MERGED_DEPLOYED`; PR #33, implementation commit `650a34808`, merge `e2f94e26b0a8b1db71c00e1607bca8f89f02aaea`; focused runtime-state tests `32 passed`, compileall PASS; required CI checks PASS in run `31990198398`; Claude and active DGX `.hermes` AGY implementation reviews both `PASS` with no correction set; immutable release and user-service health verified above.

### Repository inventory

- GitHub Issues are disabled for `cwliao/hermes-agent`; repo-local `docs/plans`, this handover, and the roadmap are the ticket source of truth.
- The only open PR is PR #21, `docs: open HERMES-UPDATE-001 DGX upstream update plan`; its required checks PASS, but it remains an open planning/documentation PR and is not the ARCH-004 runtime deployment.
- Merged/deployed lanes: HERMES-UPDATE-001 runtime work, HERMES-TELEGRAM-TRANSPORT-001, HERMES-TELEGRAM-INBOUND-001, HERMES-AUTH-001, HERMES-AUTH-002, HERMES-CALENDAR-GUARD-001, ARCH-002, ARCH-003, and ARCH-004.
- Blocked/separate gates: HERMES-MONITORING-001 remains BLOCKED; Telegram user-visible delivery is the next independent verification gate.

### Gate rule

Ticket design, implementation, tests, independent review, reconciliation, CI, merge, deployment, service health, inbound polling, outbound delivery, and rollback are separate gates. A pass at one gate cannot be reported as a pass at another.

## 5. Safe continuation instructions

1. Read this handover, `docs/ROADMAP-HERMES-DGX.md`, and the active ticket plan before acting.
2. Verify repository root, remote, branch, HEAD, GitHub main code baseline, DGX hostname, service, release marker, and rollback release.
3. Preserve the primary dirty worktree and DGX live source checkout; do not reset, clean, or overwrite either.
4. Keep reviewer packets metadata-only; never send source, secrets, tokens, absolute paths, message bodies, or generated evidence text.
5. ARCH-004 is merged and deployed with separate implementation, review, CI, merge, release, rollback, and runtime evidence; do not repeat its implementation lane.
6. Keep ARCH-002, ARCH-003, and Telegram delivery evidence separate.
7. The exact next action is Telegram user-visible delivery verification; record metadata-only evidence and keep it separate from polling/service health.
