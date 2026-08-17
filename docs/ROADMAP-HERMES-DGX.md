# Hermes Architecture Roadmap

> Snapshot: 2026-08-17 (Asia/Taipei). `main` is the canonical Hermes
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
| `main` runtime code baseline | `e2f94e26b0a8b1db71c00e1607bca8f89f02aaea` | ARCH-004 merged via PR #33; later handover/roadmap commits are documentation-only. |
| Primary laptop checkout | `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8` | Dirty audit checkout; preserve all pre-existing changes and untracked files. |
| DGX live source checkout | `/home/cwliao/.hermes/hermes-agent`, clean HEAD `1c14d2b9df29da845fb2a56b2fbe12cf8ee507cb` | Deployment input only; do not reset or edit as active runtime source. |
| DGX active release | `/home/cwliao/.hermes/releases/v2026.8.17-hermes-arch-004-e2f94e26` | Immutable runtime snapshot selected by drop-in `36-hermes-arch-004-e2f94e26.conf`; marker matches merge `e2f94e26...`. |
| Gateway service | `active/running`, MainPID `1654068`, `NRestarts=0`, `ExecMainStatus=0` | Service/process health PASS after ARCH-004 restart and bounded post-start check; effective path matches ARCH-004 release. |
| Rollback | ARCH-003 release/drop-in and earlier Telegram/ARCH-002 releases retained | Rollback evidence remains available under `/home/cwliao/.hermes/deploy-backups/hermes-arch-004-e2f94e26`; no prior release was deleted. |

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
| HERMES-TELEGRAM-DELIVERY-VERIFICATION-001 | NEXT_GATE | Repo-local verification gate because GitHub Issues are disabled. Prove an approved user-visible Telegram response/delivery path with metadata-only evidence; do not substitute service health, polling progress, `getMe`, `getWebhookInfo`, or empty `getUpdates`. |

## Current next lane

**HERMES-TELEGRAM-DELIVERY-VERIFICATION-001** is the next lane. Establish
real user-visible Telegram response/delivery evidence through an approved test
path, recording only metadata needed to correlate the attempt and outcome.
Polling progress, gateway health, token-protected API probes, and empty update
batches remain supporting evidence only and cannot close this gate. No new
runtime implementation or DGX mutation is authorized by this roadmap entry.

## Runtime and deployment boundary

The active release is selected through the ARCH-004 drop-in while the prior
ARCH-003 and Telegram releases remain intact for rollback. The service
restarted successfully and stayed active with zero restarts. The gateway has
historical evidence of API reachability and repeated empty `getUpdates`
progress after transient startup recovery, but this is inbound polling
evidence, not user-visible delivery evidence. No Telegram credentials,
allowlists, webhook state, or TLS verification were changed during the
ARCH-004 deployment.

## Ticket inventory note

GitHub Issues are disabled. The only open PR is PR #21, the HERMES-UPDATE-001
DGX upstream update plan, with required checks passing; it remains open and is
not part of the ARCH-004 runtime deployment. Repo-local plans and this roadmap
remain the authoritative ticket inventory until that planning PR is resolved.
