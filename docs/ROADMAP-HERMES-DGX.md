# Hermes Architecture Roadmap

> Snapshot: 2026-08-16 (Asia/Taipei). `main` is the canonical Hermes
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
| `main` runtime code baseline | `3e9fd48dc28b7df186c780992351ef01febaa070` | ARCH-002 merged via PR #29; later handover/roadmap commits are documentation-only. |
| Primary laptop checkout | `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8` | Dirty audit checkout; preserve all pre-existing changes and untracked files. |
| DGX live source checkout | `/home/cwliao/.hermes/hermes-agent`, clean HEAD `1c14d2b9df29da845fb2a56b2fbe12cf8ee507cb` | Deployment input only; do not reset or edit as active runtime source. |
| DGX active release | `/home/cwliao/.hermes/releases/v2026.8.16-hermes-arch-002-3e9fd48dc2` | Immutable runtime snapshot selected by drop-in `34-hermes-arch-002-3e9fd48dc2.conf`. |
| Gateway service | `active/running`, MainPID `467772`, `NRestarts=0` | Service/process health PASS at verification time; effective path matches ARCH-002 release. |
| Rollback | `v2026.8.16-hermes-telegram-inbound-178c9be1` and prior drop-in retained | Rollback evidence remains available; no prior release was deleted. |

## Core engineering order

1. CI-BASELINE-001 — restore blocking Python CI while preserving behavior. **Complete.**
2. ARCH-002 — extend the runtime-state contract. **Merged, deployed, and runtime marker verified.**
3. ARCH-003 — audit/replay integration after the shared state boundary is stable. **Next design ticket; plan pending approval.**
4. ARCH-004 — redaction and SQLite/WAL safeguards after the preceding contracts are accepted.

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
| ARCH-003 | PROPOSED_DESIGN_PENDING | Draft the ticket plan and bounded design packet. Proposed direction: append-only runtime-state audit journal plus read-only replay verifier; no implementation until design review consensus. |

## Current next lane

**ARCH-003 ticket design** is the next core lane. First write the plan, define
the audit/replay boundary, non-goals, metadata-only event contract, replay
consistency states, and hermetic acceptance tests. Then route one identical
bounded packet to exactly one authenticated Claude reviewer and one authenticated
AGY reviewer. Do not implement, migrate, deploy, or modify DGX until that
design/review gate is complete.

## Runtime and deployment boundary

The active release is selected through the ARCH-002 drop-in while the prior
Telegram-inbound release remains intact for rollback. The service restarted
successfully and stayed active with zero restarts. Telegram general API
reachability and token-protected `getMe`/`getWebhookInfo` probes returned
success; the gateway later recorded repeated empty `getUpdates` progress after
a transient startup timeout. This is inbound polling evidence, not
user-visible delivery evidence. No Telegram credentials, allowlists, webhook
state, or TLS verification were changed during the ARCH-002 deployment.
