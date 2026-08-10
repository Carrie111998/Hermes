# Hermes Architecture Roadmap

> Snapshot: 2026-08-10 (Asia/Taipei). `main` is the canonical Hermes
> integration line; DGX release branches are staging inputs only.

## Source-of-truth rules

- Every `ARCH-*` ticket must be reviewed and merged into Hermes `main` before
  it is considered complete.
- A DGX release snapshot is deployable evidence, not mainline completion.
- Deployment requires separate runtime evidence and rollback evidence.
- The review loop is: review -> revise -> independent Codex/AGY cross-review ->
  reconcile findings -> READY or explicitly BLOCKED.

## Current topology

| Reference | Current evidence | Meaning |
|---|---|---|
| `origin/main` | `7fa1865f7` | Canonical Hermes mainline. |
| `origin/ticket/T0127-v2026.8.3-merged` | release staging branch | DGX release input; not the ARCH merge target. |
| live DGX checkout | `/home/cwliao/.hermes/hermes-agent` | Claude-owned; do not edit or reset. |
| ARCH-001 deployed snapshot | `v2026.8.3-arch-001-f0dd130c8` | Deployed evidence, but not mainline-complete. |
| current reconciliation worktree | `agent/arch-001-mainline-reconciliation` | Clean `origin/main` base for isolated integration. |

## Goals and ticket order

1. `ARCH-001-MAINLINE-001` — reconcile ARCH-001 onto clean Hermes `main`.
2. `ARCH-002` — extend the runtime-state contract after ARCH-001 is merged.
3. `ARCH-003` — audit/replay integration after the shared state boundary is
   stable.
4. `ARCH-004` — redaction and SQLite/WAL safeguards after the preceding
   contracts are accepted.

## Ticket status

| Ticket | Status | Gate |
|---|---|---|
| `ARCH-001` | deployed, mainline incomplete | Mainline reconciliation and independent review. |
| `ARCH-001-MAINLINE-001` | `READY_FOR_COMMIT` (Codex READY; AGY waived by owner) | Clean-main diff and tests pass; owner-approved AGY waiver recorded. |
| `ARCH-002` to `ARCH-004` | proposed | Depend on ARCH-001 mainline merge. |

The current worktree contains an isolated main-based port of ARCH-001. Codex
self-review is READY, and the owner explicitly waived the unavailable AGY
cross-review for this ticket. It is ready for commit, but is not yet pushed,
merged, or deployed from this branch.
