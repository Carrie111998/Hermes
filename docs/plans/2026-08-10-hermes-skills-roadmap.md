# HERMES-SKILLS-001: Consolidate the long-term skills roadmap

**Status:** ROADMAP_CONSOLIDATED; documentation only. No SkillClaw
installation, skill synchronization, runtime mutation, or DGX deployment was
performed by this ticket.

**Parent project:** Hermes long-term product and engineering roadmap.

## Objective

Bring the previously recovered Hermes skill plans and verified skill-related
commit evidence into one repository-owned roadmap. Keep implemented behavior,
historical evidence, proposed work, and deferred work separate so a future
session can select a ticket without reconstructing the plan from memory.

## Verified repository evidence

- Current canonical mainline after CI-BASELINE-001 merge:
  7a14e3fdc2f1f2dc2bcd2b14265e091582e5d71a.
- 674a4fb7276624e280830e6918107d2ee7e5e422 adds the klib plugin manifest at
  plugins/klib/plugin.yaml; this is committed repository evidence, not proof
  of live DGX discovery.
- ab1a40040b28159eb9e63f17e00425c3586fe75f records the teach natural language
  trigger durability note. The commit exists in GitHub history, but its
  documentation file is not currently present on main; treat it as historical
  evidence requiring a deliberate follow-up port/review.
- The recovered inventory checkpoint associated with 674a4fb72 reported
  72 bundled skills, 103 optional skill files, 1 plugin skill, and 100 optional
  catalog rows. This is a historical checkpoint; re-count the active checkout
  before using it as current inventory evidence.

## Long-term tracks

### Track A — Skills inventory and catalog integrity

Maintain an explicit distinction between bundled, optional, plugin-provided,
installed, and merely available skills. Reconcile filesystem counts with the
optional catalog and record discrepancies rather than silently normalizing them.

### Track B — Durable user-local skills

Preserve user-local skills across release bakes and updates. The teach trigger
is the reference case: a skill-local trigger must remain intact after reinstall
or release-slot changes, with a same-release CLI smoke test and timestamped
backup evidence.

### Track C — Plugin skill discovery

Keep plugin manifests and discovery behavior aligned. klib manifest presence is
repository evidence; a future ticket must add an isolated discovery check and,
where authorized, a real gateway/plugin smoke without treating unit tests alone
as live proof.

### Track D — HERMES-SKILLCLAW-001

First target the AMAP-ML/SkillClaw client plus Hermes integration. The
skillclaw.org marketplace is a separate future scope.

The implementation contract remains:

- Windows Python 3.11.x only; do not auto-try unsupported Python versions.
- Pin and record an immutable upstream Git ref/SHA.
- Install client core only; do not add evolve, sharing, or server extras.
- Use explicit same-user paths: %USERPROFILE%\\.hermes\\skills and
  %USERPROFILE%\\.skillclaw\\config.yaml.
- Require loopback-only proxy binding at 127.0.0.1:30000, health evidence,
  no non-loopback listener, and request evidence through the proxy.
- Use timestamped backups, SHA-256 hashes, deterministic before/after
  manifests, and a conflict report. Same-name/different-hash conflicts are
  partial; never overwrite, rename, or silently skip.
- Restore only through the recorded, hash-gated skillclaw restore hermes flow
  after verifying the installed CLI syntax.
- Deliver a relative-path runbook and non-mutating verifier; do not add a
  Hermes production dependency.

## Relationship to the broader Hermes roadmap

The product priority and engineering implementation order remain separate:

Product priority:
1. Verify the private Telegram baseline: DM, one allowlisted user, /status,
   restart recovery.
2. Gateway plus cron/job health monitoring and failure/recovery alerts.
3. Mobile HITL for destructive Hermes operations.
4. Verify voice and file handoff in the real deployment.
5. Safe remote coding-agent workflow on Spark, including Claude/Codex/AGY,
   TaskRouter, worktrees, runner supervision, and external CLI HITL.
6. Team Telegram bot with pairing, per-user sessions, groups, and isolation.
7. Scheduled briefings and knowledge-base workflows.
8. Multi-bot or bot-to-bot collaboration last.

Remote-coding engineering order:
1. Capability probes and shared interfaces.
2. TaskRouter with git worktree isolation and SQLite path leases.
3. Runner Supervisor.
4. Claude/Codex/Antigravity AGY adapters.
5. Approval integration.
6. A thin Hermes /goal hook without changing the Goal loop.
7. Telegram/Spark end-to-end validation and health monitoring.

The skills lane supports these goals but does not reorder them, change the Goal
loop, or turn a product priority into an implementation ticket without review.

## Boundaries

- This roadmap does not modify the Hermes Goal loop, TaskRouter, runner
  supervisor, provider implementations, or ARCH-002 runtime-state contract.
- No live /home/cwliao/.hermes edit, reset, pull, restart, or skill sync is
  authorized by this documentation ticket.
- No local laptop skill directory is treated as repository source of truth.
- Marketplace work, broad skill migration, and automatic conflict resolution
  remain out of scope until separately ticketed.

## Ticket order

1. Keep ARCH-002 as the next core runtime-state ticket.
2. Create and review HERMES-SKILLS-002 for current inventory/catalog
   reconciliation.
3. Create and review HERMES-SKILLS-003 for the teach durability port and
   release-sync evidence.
4. Create and review HERMES-PLUGIN-001 for klib discovery evidence.
5. Implement HERMES-SKILLCLAW-001 after the client contract and environment
   preflight are independently reviewed.
6. Keep marketplace integration as a later, separate ticket.

Every ticket follows: review -> revise -> independent Codex/AGY cross-review ->
reconcile -> READY or explicitly BLOCKED. A roadmap entry is not an
implementation or deployment authorization.

## Acceptance criteria for this consolidation

- The four skill tracks and their evidence states are recorded in-repo.
- Historical commits are linked to their actual status and are not presented as
  current runtime evidence.
- SkillClaw constraints are preserved without importing an external dependency.
- ARCH-002 and the skills lane remain separate.
- The broader product and engineering order remains recorded separately from
  the skills lane.
- Handover and roadmap documents agree on current mainline, CI merge status,
  next ticket, and DGX deployment state.
- CI and documentation checks pass for the resulting pull request.
