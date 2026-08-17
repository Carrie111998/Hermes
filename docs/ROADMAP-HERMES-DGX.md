# Hermes Architecture Roadmap

> Snapshot: 2026-08-18 (Asia/Taipei). `main` is the canonical Hermes
> integration line; DGX release snapshots are deployable evidence only.
> Runtime service health, Telegram inbound polling, outbound delivery, and
> rollback remain separate gates.

## Source-of-truth rules

- Every architecture or long-term integration ticket must be reviewed and merged into Hermes `main` before it is complete.
- A DGX release snapshot is deployable evidence, not mainline completion.
- Deployment requires immutable identity, effective user-unit evidence, runtime evidence, and rollback evidence.
- The review loop is review -> revise -> independent cross-review -> reconcile -> READY or BLOCKED.
- A running systemd service does not prove Telegram inbound readiness or user-visible delivery.

## Current topology

| Reference | Current evidence | Meaning |
|---|---|---|
| `main` runtime code baseline | `36c2d243461e7e9f9be7c8b98e8a9063eef8fd1c` | Telegram delivery audit correlation correction merged via PR #38; deployed evidence is recorded below. |
| Primary laptop checkout | `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8` | Dirty audit checkout; preserve all pre-existing changes and untracked files. |
| DGX live source checkout | `/home/cwliao/.hermes/hermes-agent`, clean HEAD `1c14d2b9df29da845fb2a56b2fbe12cf8ee507cb` | Deployment input only; do not reset or edit as active runtime source. |
| DGX active release | `/home/cwliao/.hermes/releases/v2026.8.18-hermes-telegram-delivery-audit-fix-36c2d243` | Immutable runtime snapshot selected by drop-in `39-hermes-telegram-delivery-audit-fix-36c2d243.conf`; marker matches merge `36c2d243...`. |
| Gateway service | `active/running`, MainPID `2601915`, `NRestarts=0`, `ExecMainStatus=0` | Service/process health PASS after authorized correction restart and bounded ten-second stability check; effective cwd/PYTHONPATH match the correction release. |
| Rollback | Correction rollback manifest plus prior ARCH/Telegram releases retained | Rollback evidence remains available under the corresponding deployment-backups entry; no prior release was deleted. |

## Core engineering order

1. CI-BASELINE-001 — restore blocking Python CI while preserving behavior. **Complete.**
2. ARCH-002 — extend the runtime-state contract. **Merged, deployed, and runtime marker verified.**
3. ARCH-003 — audit/replay integration after the shared state boundary is stable. **Merged, deployed, and runtime marker verified.**
4. ARCH-004 — redaction and SQLite/WAL safeguards. **Merged, deployed, and runtime marker verified.**

## Product priority

1. Verify the private Telegram baseline: DM, one allowlisted user, /status, and restart recovery.
2. Gateway plus cron/job health monitoring and failure/recovery alerts.
3. Mobile HITL for destructive Hermes operations.
4. Verify voice and file handoff in the real deployment.
5. Safe remote coding-agent workflow on Spark, including Claude/Codex/AGY, TaskRouter, worktrees, runner supervision, and external CLI HITL.
6. Team Telegram bot with pairing, per-user sessions, groups, and isolation.
7. Scheduled briefings and knowledge-base workflows.
8. Multi-bot or bot-to-bot collaboration last.

## Ticket status

| Ticket | Status | Current evidence / next action |
|---|---|---|
| HERMES-UPDATE-001 | MERGED_DEPLOYED | PR #22 and immutable DGX release remain historical verified evidence. |
| HERMES-TELEGRAM-TRANSPORT-001 | MERGED_DEPLOYED_RUNTIME_RECOVERED | Prior startup timeout/reconnect degradation recovered through bounded retry; current service has repeated qualifying empty polling progress. User-visible delivery remains separate. |
| HERMES-TELEGRAM-INBOUND-001 | MERGED_DEPLOYED_INBOUND_POLLING_PASS | PR #26 merged as `178c9be1...`; current release records repeated `telegram_polling_progress` metadata with empty batches. Accepted-update/user-visible response remains a separate gate. |
| HERMES-AUTH-001 | MERGED_DEPLOYED | Separate auth ticket; do not conflate it with ARCH-003. |
| HERMES-AUTH-002 | MERGED_DEPLOYED | Separate target-config ticket; do not conflate it with ARCH-003. |
| HERMES-CALENDAR-GUARD-001 | MERGED_DEPLOYED | Actual DGX guard naming/effective unit remains separate documentation hygiene. |
| HERMES-MONITORING-001 | BLOCKED | No merge/deployment/readiness inference from current gateway evidence. |
| ARCH-002 | MERGED_DEPLOYED | PR #29 merge `3e9fd48d...`; 24 focused tests passed; CI `31937692260` PASS; AGY + Claude Opus implementation review PASS; immutable DGX release active. |
| ARCH-003 | MERGED_DEPLOYED | PR #30; merge `e8cdfd1e...`; implementation review consensus PASS; CI run `31981532693` PASS after retrying an unrelated pre-existing stream-consumer slice; immutable DGX release active with rollback metadata. |
| ARCH-004 | MERGED_DEPLOYED | PR #33; implementation commit `650a34808`; merge `e2f94e26b0a8b1db71c00e1607bca8f89f02aaea`; focused tests `32 passed`, compileall PASS; required CI run `31990198398` PASS; Claude + active DGX `.hermes` AGY implementation review PASS with no corrections; immutable DGX release active with rollback metadata. |
| HERMES-TELEGRAM-DELIVERY-VERIFICATION-001 | PARTIAL_USER_VISIBLE_DELIVERY_PASS_OUTBOUND_UNPROVEN | PR #38 merged as `36c2d243...`; final CI run `32056603324` had no failures; authenticated Claude and AGY final review PASS. PR #40 records one authorized test with direct user-visible confirmation; the matching outbound runtime audit is still missing. Do not substitute service health, polling progress, `getMe`, `getWebhookInfo`, or empty `getUpdates`. |

## Current next lane

**HERMES-TELEGRAM-DELIVERY-VERIFICATION-001** has a partial result. The
authorized test produced direct user-visible confirmation and an accepted
inbound metadata record, but the matching outbound runtime audit was absent.
Polling progress, gateway health, token-protected API probes, and empty update
batches remain supporting evidence only. Do not retry the conversation merely
to manufacture the missing outbound evidence.

## Runtime and deployment boundary

The active release is selected through the correction drop-in while the prior
ARCH-004, ARCH-003, and Telegram releases remain intact for rollback. The
service restarted successfully and stayed active with zero restarts. The
gateway has historical evidence of API reachability and repeated empty
`getUpdates` progress after transient startup recovery, but this is inbound
polling evidence, not user-visible delivery evidence. No Telegram credentials,
allowlists, webhook state, or TLS verification were changed during the
correction deployment; one authorized Telegram test was performed, and no
retry or second action was issued.

## Ticket inventory note

GitHub Issues are disabled. The only open PR is PR #21, the HERMES-UPDATE-001
DGX upstream update plan, with required checks passing; it remains open and is
not part of the ARCH-004 runtime deployment. Repo-local plans and this roadmap
remain the authoritative ticket inventory until that planning PR is resolved.
