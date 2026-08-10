# Hermes Architecture Roadmap

> Snapshot: 2026-08-10 (Asia/Taipei). main is the canonical Hermes
> integration line; DGX release branches are staging inputs only.

## Source-of-truth rules

- Every ARCH-* and long-term integration ticket must be reviewed and merged
  into Hermes main before it is considered complete.
- A DGX release snapshot is deployable evidence, not mainline completion.
- Deployment requires separate runtime evidence and rollback evidence.
- The review loop is: review -> revise -> independent Codex/AGY cross-review ->
  reconcile findings -> READY or explicitly BLOCKED.

## Current topology

| Reference | Current evidence | Meaning |
|---|---|---|
| origin/main | 7a14e3fdc2f1f2dc2bcd2b14265e091582e5d71a | Canonical Hermes mainline after CI-BASELINE-001 merge. |
| origin/ticket/T0127-v2026.8.3-merged | release staging branch | DGX release input; not the ARCH merge target. |
| live DGX checkout | /home/cwliao/.hermes/hermes-agent | Claude-owned; do not edit or reset. |
| live DGX deployed commit | ec50a154eeb44e7206f24b7703f9032b8f97069c | Last verified deployed mainline; gateway was active/running. |
| rollback ref | backup/pre-arch-001-deploy-20260810T031625Z | DGX-local pre-ARCH-001 deploy ref; preserved for rollback. |
| CI-BASELINE-001 merge | 7a14e3fdc2f1f2dc2bcd2b14265e091582e5d71a | Merged to GitHub main; not deployed to DGX. |

## Core engineering order

1. CI-BASELINE-001 — restore blocking Python CI while preserving behavior. **Complete.**
2. ARCH-002 — extend the runtime-state contract after CI is green. **Next core ticket.**
3. ARCH-003 — audit/replay integration after the shared state boundary is stable.
4. ARCH-004 — redaction and SQLite/WAL safeguards after the preceding contracts are accepted.

## Long-term skills lane

The consolidated skills roadmap is
docs/plans/2026-08-10-hermes-skills-roadmap.md.

| Track | Status | Evidence / next action |
|---|---|---|
| Skills inventory/catalog | proposed | Re-count the active checkout and reconcile bundled, optional, plugin, installed, and available categories. |
| User-local skill durability | historical evidence | teach trigger note exists in commit ab1a40040; port only through a reviewed follow-up ticket. |
| Plugin skill discovery | repository evidence | klib manifest exists in commit 674a4fb72; add isolated discovery and authorized smoke evidence separately. |
| HERMES-SKILLCLAW-001 | proposed | Client + Hermes integration first; marketplace remains separate. |
| HERMES-SKILLS-001 | documentation | This consolidation; no runtime or DGX mutation. |

## Ticket status

| Ticket | Status | Gate |
|---|---|---|
| ARCH-001 | complete | Merged to Hermes main and deployed to DGX; service active after restart. |
| ARCH-001-MAINLINE-001 | complete | Merge ec50a154…; compile and 35 targeted tests passed; runtime restart verified. |
| CI-BASELINE-001 | complete | PR #3 merged as 7a14e3f…; latest required checks were green and Codex/AGY review reconciled READY. |
| ARCH-002 | proposed | Draft the runtime-state contract ticket and review it before implementation. |
| HERMES-SKILLS-001 | in progress | Consolidate the long-term skill roadmap in this branch; documentation only. |
| HERMES-SKILLS-002 to HERMES-SKILLCLAW-001 | proposed | Select only after the relevant contract and evidence gates are reviewed. |

## Runtime and deployment boundary

ARCH-001 remains the last verified DGX deployment. CI-BASELINE-001 and this
skills-roadmap consolidation have not been deployed or restarted on DGX.
No roadmap document authorizes a live skill synchronization or runtime change.
