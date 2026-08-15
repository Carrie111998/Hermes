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
- A running systemd service does not prove Telegram inbound readiness.

## Current topology

| Reference | Current evidence | Meaning |
|---|---|---|
| `main` | `0fe3773ccfbec860984d0dc93adc4875ca2d5d4b` | Canonical mainline after PR #22, HERMES-UPDATE-001 Lane 2/3 correction set. |
| DGX live source checkout | `/home/cwliao/.hermes/hermes-agent`, clean HEAD `1c14d2b9df29da845fb2a56b2fbe12cf8ee507cb` | Deployment input only; do not reset or edit as the active runtime source. |
| DGX active release | `/home/cwliao/.hermes/releases/v2026.8.16-hermes-update-001-0fe3773ccf` | Immutable runtime snapshot selected by drop-in `31-hermes-update-001-0fe3773ccf.conf`. |
| Gateway service | `active/running`, MainPID `3992364`, `ExecMainStatus=0`, `NRestarts=0` | Process/service health PASS at verification time. |
| Calendar guard | timer `active/waiting`; service and wrapper point to the active release | Recovery path is installed and enabled; prior release remains available. |
| Rollback | drop-in `30-hermes-telegram-transport-77bcb5d0717e.conf` and its release retained | Rollback metadata remains available; no prior release was deleted. |

## Core engineering order

1. CI-BASELINE-001 — restore blocking Python CI while preserving behavior. **Complete.**
2. ARCH-002 — extend the runtime-state contract after the active transport gate is cleared. **Proposed next core ticket.**
3. ARCH-003 — audit/replay integration after the shared state boundary is stable.
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
| HERMES-UPDATE-001 | MERGED_DEPLOYED | PR #22, SHA `0fe3773c...`; Lane 2/3 review PASS, 33 targeted tests passed, immutable DGX release active. |
| HERMES-TELEGRAM-TRANSPORT-001 | MERGED_DEPLOYED_RUNTIME_DEGRADED | Outbound E2E passed with `success=true`, `message_id=1967`, `mirrored=true`; inbound has no qualifying `getUpdates` progress after the bounded post-restart window. Diagnose network/polling ownership next. |
| HERMES-AUTH-001 | MERGED_DEPLOYED | Separate auth ticket; merged and deployed evidence remains historical. |
| HERMES-AUTH-002 | READY_FOR_MERGE | Separate target-config ticket; do not conflate it with the update/deploy lane. |
| HERMES-CALENDAR-GUARD-001 | MERGED_DEPLOYED | Recovery timer and oneshot remain active; release selection is rollback-ready. |
| HERMES-MONITORING-001 | BLOCKED | No merge or deployment inference from current gateway evidence. |
| ARCH-002 | PROPOSED | Select after the current Telegram transport gate is resolved or explicitly re-sequenced. |

## Current next lane

**HERMES-TELEGRAM-TRANSPORT-001 diagnosis** is the active next lane. First map the DGX primary/fallback Telegram network path and the exact `getUpdates` progress signal. Only then open a narrow correction set. Do not start ARCH-002 implementation or alter Telegram credentials, allowlists, webhook state, or unrelated services as part of this diagnosis.

## Runtime and deployment boundary

The active release is selected through the new drop-in while the previous transport release remains intact for rollback. The service restarted successfully and stayed active with zero restarts. Telegram outbound delivery is separately proven through Hermes `send`; inbound polling remains `DEGRADED/UNPROVEN` because the logs show connection/menu registration but no qualifying `getUpdates` progress. The CLI warning about an outdated installed service definition is recorded as deployment hygiene; effective systemd state is verified independently.
