# HERMES-INTAKE-001: Reconcile DGX HermesHub and Drive-watch artifacts

**Status:** DRAFT_PENDING_REVIEW; documentation and boundary reconciliation
only. No external skill installation, Drive mutation, gateway configuration
change, or runtime deployment is authorized by this ticket.

## Objective

Research two Markdown artifacts found in the DGX Hermes checkout, preserve the
useful evidence in a repository-owned ticket, and keep Hermes mainline work
separate from external projects and unverified runtime state.

## Source evidence

The source artifacts were read from the DGX checkout, not from a laptop copy:

- `docs/hermeshub-skill-tickets.md` — HermesHub skills intake notes pinned to
  upstream revision `7bd1fb508799cc536c767caf99edbc3e3d97ebd3`.
- `docs/plans/2026-08-10-004-drive-watch-checkpoint-recovery-plan.md` —
  `T0156`, a Drive-watch checkpoint recovery record marked `DONE`.

These files were untracked in the Hermes checkout and are not source-of-truth
for Hermes mainline until reviewed and merged as repository documentation.

## Disposition

### A. HermesHub skills

The artifact describes five pinned external skills and their safety gates:

| Item | Source-reported state | Hermes disposition |
|---|---|---|
| HH-001 `agent-hardening` | Blocked: bounded formal install exceeded 90 seconds; no install remained. | Create a follow-up scanner-diagnosis ticket; do not retry or use `--force`. |
| HH-002 `api-builder` | Deferred: terminal-capable, medium/high risk. | Keep deferred; any future test must use its dedicated disposable sandbox. |
| HH-003 `arxiv-watcher` | Deferred: external network and possible cron mutation. | Keep deferred; no cron mutation or network smoke in this intake. |
| HH-004 `data-analyst` | Deferred: high-risk local-data and terminal access. | Keep deferred; synthetic fixtures only in a future isolated ticket. |
| HH-005 `diagram-maker` | Source artifact reported `SAFE` installation; Telegram acceptance was pending. | Treat as live evidence requiring revalidation, not repository proof. |

Current DGX read-only verification found `diagram-maker` enabled and present at
`/home/cwliao/.hermes/skills/diagram-maker/SKILL.md` with SHA-256
`6572f2c6d96da1372ced69225d99137e37f94a40a97bf9c6e4f35a4c739798f0`.
The source artifact reported a different bundle hash
(`c8e5014bc536167ddb32c1fa7fedc048384f673de400ab48b0454c5974a5d50`). Until
the pinned upstream content, installed bytes, scanner result, and acceptance
evidence are reconciled, HH-005 must not be promoted as trusted current
mainline evidence.

The Hermes follow-up is **HERMES-SKILLS-004 — HermesHub pinned-skill intake and
scanner reconciliation**. It must remain separate from ARCH-002 and must not
install HH-001 through HH-004 as part of this intake.

### B. Drive-watch checkpoint recovery

The artifact describes `T0156`, owned by the DGX Hermes Drive-watch system. Its
scope is reconciliation of older unprocessed Google Drive files against
`processed_file_ids`, with dry-run, idempotency, focused tests, AGY review, and
release evidence. The document reports a completed release and two production
runs (`processed=1 scanned=6`, then `processed=0 scanned=6`). Those metrics are
unverified external documentation claims in this Hermes repository; they were
not independently validated against Drive-watch telemetry or Hermes core logs.

This is external operational evidence, not Hermes core source. It explicitly
keeps Gateway, Telegram, KMDaily, Postgres, Drive content, permissions, and
deletion behavior out of scope. Do not port `scripts/hermes_drive_watch.py`,
its tests, or its release into `cwliao/hermes-agent` under this ticket. Any
future synchronization belongs to the KLIB/Drive-watch project handover and
its own reviewed ticket.

## Acceptance criteria

- Both DGX artifacts and their source boundaries are recorded without copying
  unreviewed external runtime code into Hermes.
- HermesHub skill statuses, pinned revision, scanner timeout, and the HH-005
  installed-hash mismatch are explicit.
- T0156 is recorded as external completed evidence, not as a Hermes runtime
  implementation or deployment authorization.
- ARCH-002 remains the next core Hermes ticket; HERMES-SKILLS-004 is a separate
  follow-up.
- No `.env`, config, MCP, cron, Telegram session, Drive data, or gateway state
  is modified.
- Documentation checks, recursive review, independent cross-review, and
  reconciliation pass before merge.

## Review and delivery gate

Review loop: `review -> revise -> independent Codex/AGY cross-review ->
reconcile -> READY or BLOCKED`.

This ticket itself is documentation-only. Merging it to Hermes `main` records
the boundary and follow-up ticket; it does not install skills, change runtime
state, or deploy T0156.
