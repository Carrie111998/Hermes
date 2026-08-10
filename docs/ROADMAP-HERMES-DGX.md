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
| `origin/main` | `ec50a154eeb44e7206f24b7703f9032b8f97069c` | Canonical Hermes mainline with ARCH-001 merged. |
| `origin/ticket/T0127-v2026.8.3-merged` | release staging branch | DGX release input; not the ARCH merge target. |
| live DGX checkout | `/home/cwliao/.hermes/hermes-agent` | Claude-owned; do not edit or reset. |
| ARCH-001 deployed snapshot | `v2026.8.3-arch-001-f0dd130c8` | Historical release evidence; superseded on DGX by the merged mainline commit. |
| live DGX deployed commit | `ec50a154eeb44e7206f24b7703f9032b8f97069c` | Fast-forward deployed to `/home/cwliao/.hermes/hermes-agent`; gateway verified active. |
| rollback ref | `backup/pre-arch-001-deploy-20260810T031625Z` | DGX-local pre-deploy commit ref; preserved for rollback. |

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
| `ARCH-001` | complete | Merged to Hermes `main` and deployed to DGX; service active after restart. |
| `ARCH-001-MAINLINE-001` | `COMPLETE` (Codex READY; AGY waived by owner) | Merge `ec50a154…`; compile and 35 targeted tests passed; runtime restart verified. |
| `ARCH-002` to `ARCH-004` | proposed | Ready for ticket planning now that ARCH-001 is on mainline. |

ARCH-001 was merged by owner-authorized admin merge because the repository's
baseline CI still has unrelated pre-existing failures. The ARCH-001 targeted
checks passed, including compile, lint/security checks, attribution, and the
runtime-state test set. The DGX checkout was fast-forwarded without touching
Claude's unrelated untracked files; the user service is active after restart.
