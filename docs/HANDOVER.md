# Project Handover - hermes-agent

**Plan key:** hermes-agent  
**Last verified:** 2026-08-16 (Asia/Taipei)  
**Handover owner/session:** Codex  
**Authoritative project log:** `docs/ROADMAP-HERMES-DGX.md`

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI with memory, skills, scheduled jobs, delegated agents, and messaging-platform adapters.
- **Repository:** <https://github.com/cwliao/hermes-agent>
- **Merged runtime code baseline:** `3e9fd48dc28b7df186c780992351ef01febaa070` (PR #29, ARCH-002).
- **DGX runtime:** host `140.96.58.171`, hostname `55-0940189-03`, user service `hermes-gateway.service`.
- **In scope:** Hermes CLI, gateway, runtime state, platform adapters, CI, skills, documentation, and explicitly ticketed deployment work.
- **Out of scope by default:** laptop files as handover sources, unrelated DGX services, credentials/tokens, and external marketplace or SkillClaw changes without a separate reviewed ticket.

The DGX live source checkout is deployment input only. The active service uses an immutable release snapshot selected by a systemd user drop-in. The primary laptop checkout is not authoritative for runtime state.

The local audit checkout is `D:/PROJECT/Hermes`, branch `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8`; it remains dirty with pre-existing changes and untracked files. They were preserved and were not staged, reset, or cleaned.

## 2. Goal and roadmap

- **Current goal:** preserve the merged ARCH-002 runtime-state contract, keep Telegram service/inbound/outbound gates separate, and prepare the next architecture ticket.
- **Completed and deployed:** ARCH-002, PR #29, merge `3e9fd48dc28b7df186c780992351ef01febaa070`; implementation review consensus PASS, latest-head CI PASS, immutable DGX deployment complete.
- **Telegram runtime:** the gateway recovered from a transient startup timeout through its bounded retry path. Post-recovery logs show repeated qualifying empty `telegram_polling_progress` events; user-visible delivery remains a separate gate.
- **Deferred/blocked:** HERMES-MONITORING-001 remains BLOCKED. Telegram user-visible delivery is not claimed from service or polling health.
- **Next candidate:** ARCH-003 audit/replay integration after the shared runtime-state boundary is stable. No ARCH-003 plan has been written or approved yet.

## 3. Verified runtime and deployment state

- **DGX host identity:** `cwliao@55-0940189-03`.
- **Active release:** `/home/cwliao/.hermes/releases/v2026.8.16-hermes-arch-002-3e9fd48dc2`.
- **Release marker:** `HERMES_RELEASE_SHA=3e9fd48dc28b7df186c780992351ef01febaa070`.
- **Effective service:** `hermes-gateway.service`, `ActiveState=active`, `SubState=running`, MainPID `467772`, `NRestarts=0`.
- **Effective WorkingDirectory/PYTHONPATH:** the ARCH-002 immutable release above.
- **Rollback:** prior Telegram-inbound release `v2026.8.16-hermes-telegram-inbound-178c9be1` and its drop-in remain available.
- **Deployment manifest:** the release records source commit `3e9fd48d...`, base release `178c9be1...`, and the runtime-state overlay hashes.

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
- **ARCH-003:** proposed next architecture ticket; design scope and plan are pending approval. No implementation is authorized.

### Gate rule

Ticket design, implementation, tests, independent review, reconciliation, CI, merge, deployment, service health, inbound polling, outbound delivery, and rollback are separate gates. A pass at one gate cannot be reported as a pass at another.

## 5. Safe continuation instructions

1. Read this handover, `docs/ROADMAP-HERMES-DGX.md`, and the active ticket plan before acting.
2. Verify repository root, remote, branch, HEAD, GitHub main code baseline, DGX hostname, service, release marker, and rollback release.
3. Preserve the primary dirty worktree and DGX live source checkout; do not reset, clean, or overwrite either.
4. Keep reviewer packets metadata-only; never send source, secrets, tokens, absolute paths, message bodies, or generated evidence text.
5. For ARCH-003, write and review the ticket plan before implementation. The proposed direction is an append-only runtime-state audit journal plus a read-only replay verifier; this proposal is not yet approved.
6. Keep ARCH-002 and Telegram delivery evidence separate from ARCH-003.
7. Refresh this handover only with verified facts and the exact next action.
