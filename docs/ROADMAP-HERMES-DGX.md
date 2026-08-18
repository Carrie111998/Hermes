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
| `main` runtime code baseline | `dd7a0164f45c235b6cbc5b5ef4995b6bbec036ea` | HERMES-MULTI-AGENT-ORCHESTRATION-001 merged via PR #45; deployed evidence is recorded below. |
| Primary laptop checkout | `D:/PROJECT/Hermes`, `ticket/hermes-auth-001`, HEAD `c192e863d8dc9df98c2bd9d066ce49bc4f9cb3e8` | Dirty audit checkout; preserve all pre-existing changes and untracked files. |
| DGX live source checkout | `/home/cwliao/.hermes/hermes-agent`, clean HEAD `1c14d2b9df29da845fb2a56b2fbe12cf8ee507cb` | Deployment input only; do not reset or edit as active runtime source. |
| DGX active release | `v2026.8.18-hermes-multi-agent-orchestration-dd7a0164` | Immutable runtime snapshot selected by drop-in `42-hermes-multi-agent-orchestration-dd7a0164.conf`; marker matches merge `dd7a0164...`. |
| Gateway service | `active/running`, MainPID `2919046`, `NRestarts=0`, `ExecMainStatus=0` | Service/process health PASS after authorized restart and bounded stability check; effective cwd/PYTHONPATH match the new release. |
| Summary timer | `hermes-kanban-summary.timer` enabled/active | Ten-minute metadata-only summary service is installed; first run returned `success`/`sent`, which is outbound evidence only. |
| Rollback | Multi-agent rollback manifest plus prior ARCH/Telegram releases retained | Rollback evidence remains available under the corresponding deployment-backups entry; no prior release was deleted. |

## Core engineering order

1. CI-BASELINE-001 — restore blocking Python CI while preserving behavior. **Complete.**
2. ARCH-002 — extend the runtime-state contract. **Merged, deployed, and runtime marker verified.**
3. ARCH-003 — audit/replay integration after the shared state boundary is stable. **Merged, deployed, and runtime marker verified.**
4. ARCH-004 — redaction and SQLite/WAL safeguards. **Merged, deployed, and runtime marker verified.**

5. HERMES-MULTI-AGENT-ORCHESTRATION-001: official worker skills, metadata-only Kanban summary, and existing dashboard/notifier integration. **Merged, deployed, timer enabled; Grok/AGY executable authentication and user-visible summary receipt remain separate gates.**

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
| HERMES-TELEGRAM-DELIVERY-VERIFICATION-001 | PASS | PR #43 merged as `3bf3f210...`; required CI run `32075681273` passed; authenticated Claude and AGY reviewed the same metadata-only packet and returned PASS. One authorized test produced correlated inbound accepted and outbound delivered audit records, plus direct user-visible confirmation. No retry was issued. |
| HERMES-MULTI-AGENT-ORCHESTRATION-001 | MERGED_DEPLOYED_TIMER_ENABLED | PR #45 merged as `dd7a0164...`; required CI run `32084903909` passed after the failed slice was rerun; Claude and AGY implementation review both PASS on the same correction set; immutable release, gateway restart, official skill parity, enabled summary timer, and first metadata-only outbound send audit are verified. Grok/AGY executable authentication and direct Telegram user-visible receipt remain separate. |

## Current next lane

**HERMES-MULTI-AGENT-ORCHESTRATION-001** is merged, deployed, and timer-enabled.
The first summary run returned `success`/`sent`, proving only the existing
outbound send path was invoked successfully. Skill parity and gateway health
are verified. Grok OAuth, AGY executable smoke, and direct Telegram
user-visible receipt for this summary remain separate gates.

## Runtime and deployment boundary

The active release is selected through the multi-agent orchestration drop-in
while the prior ARCH-004, ARCH-003, and Telegram releases remain intact for
rollback. The service restarted successfully and stayed active with zero
restarts. The metadata-only summary timer is enabled and its first oneshot
returned `success`/`sent`; this is not direct user-visible delivery evidence.
No Telegram credentials, allowlists, webhook state, or TLS verification were
changed. The non-secret summary target uses the existing Telegram home-channel
configuration.

## Ticket inventory note

GitHub Issues are disabled. The only open PR is PR #21, the HERMES-UPDATE-001
DGX upstream update plan, with required checks passing; it remains open and is
not part of the ARCH-004 runtime deployment. Repo-local plans and this roadmap
remain the authoritative ticket inventory until that planning PR is resolved.
