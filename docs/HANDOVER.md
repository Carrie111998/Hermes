# Project Handover - hermes-agent

**Plan key:** hermes-agent
**Last verified:** 2026-08-10
**Handover owner/session:** Codex
**Authoritative project log:** docs/ROADMAP-HERMES-DGX.md

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI for messaging,
  model routing, approvals, runtime state, and scheduled automation.
- **In scope:** Hermes CLI/gateway code, runtime-state integration, Telegram
  and other platform adapters, model catalog handling, CI reliability,
  skills/plugin discovery, the skill catalog, and Hermes project documentation.
- **Out of scope:** Laptop local files/checkouts as handover objects, unrelated
  DGX services, Open WebUI/Ollama stacks, external agent credentials or tokens,
  and marketplace integration unless separately ticketed.
- **唯一交接对象:** Git repository https://github.com/cwliao/hermes-agent.git
  and DGX Spark Hermes home /home/cwliao/.hermes.
- **Canonical remote/branch:** origin/main; ticket work uses isolated ticket/*
  branches.
- **Current mainline:** 7a14e3fdc2f1f2dc2bcd2b14265e091582e5d71a.
- **Runtime/deployment:** DGX Spark host 140.96.58.171, Hermes checkout under
  /home/cwliao/.hermes/hermes-agent, user service hermes-gateway.service.
- **Entry points:** hermes_cli.main, gateway.run, messaging adapters,
  docs/ROADMAP-HERMES-DGX.md, and ticket files under docs/plans/.

## 2. Goal and roadmap

- **Current goal:** Advance Hermes mainline through ARCH-002 while maintaining a
  separate, reviewed skills ecosystem lane. Product priority and remote-coding
  engineering order remain recorded separately from ticket order.
- **Completed and verified:**
  - ARCH-001 mainline reconciliation merged as
    ec50a154eeb44e7206f24b7703f9032b8f97069c.
  - ARCH-001 deployed to DGX; service verified active/running after restart,
    with rollback ref backup/pre-arch-001-deploy-20260810T031625Z.
  - CI-BASELINE-001 merged to GitHub main as
    7a14e3fdc2f1f2dc2bcd2b14265e091582e5d71a; CI run 31381350666 was green
    and Codex/independent AGY review reconciled READY.
- **Active:** HERMES-SKILLS-001 consolidates the recovered skill roadmap;
  this branch changes documentation only. ARCH-002 remains the next core
  implementation ticket.
- **Deferred / not goals:** No DGX deployment or service restart for
  CI-BASELINE-001, HERMES-SKILLS-001, or any proposed SkillClaw work.
- **Next candidates:** ARCH-002, then skills inventory/catalog, teach
  durability, klib discovery evidence, HERMES-SKILLS-004, and
  HERMES-SKILLCLAW-001. HERMES-INTAKE-001 is the current documentation
  reconciliation being reviewed for merge.

## 3. Verified state

- **CI:** Required checks for CI-BASELINE-001 passed in run 31381350666; no
  new CI run is claimed for this documentation branch.
- **DGX health:** Last read-only check showed hermes-gateway.service
  active/running; DGX remains at ec50a154eeb44e7206f24b7703f9032b8f97069c.
- **Skill evidence:** klib manifest commit 674a4fb72 is repository evidence.
  teach durability commit ab1a40040 is historical GitHub evidence and is not
  current mainline documentation. The historical inventory checkpoint is not
  current until re-counted.
- **Data/storage safety:** No DGX .hermes storage mutation was performed for
  this roadmap consolidation. No secrets or tokens are stored here.
- **Deployment:** No CI or skills-roadmap change has been deployed to DGX.
- **Known limitations:** This branch adds roadmap documentation only; the
  SkillClaw client, inventory reconciliation, teach port, and klib live smoke
  remain unimplemented/proposed.

## 4. Next ticket

- **Core ticket:** ARCH-002 — extend the runtime-state contract.
- **Status:** proposed; no ARCH-002 ticket file exists yet.
- **Separate long-term lane:** HERMES-SKILLS-002 inventory/catalog
  reconciliation, followed by HERMES-SKILLS-003, HERMES-PLUGIN-001, and
  HERMES-SKILLS-004 and HERMES-SKILLCLAW-001.
- **Acceptance gates:** Each ticket requires explicit scope, focused tests or
  verifier evidence, documentation consistency, CI, recursive review,
  independent cross-review, reconciliation, and only then merge. DGX
  deployment remains a separate authorization gate.

## 5. Safe continuation instructions

1. Read this file, docs/ROADMAP-HERMES-DGX.md, and the relevant plan under
   docs/plans/.
2. Verify GitHub main, the ticket branch/HEAD, and DGX
   /home/cwliao/.hermes state before any action.
3. Treat laptop files as staging only; do not hand over an unresolved dirty
   worktree or discard unrelated changes.
4. Use the repository's managed environment for tests; do not use global Python
   as CI evidence.
5. Keep ARCH-002 runtime work separate from the skills lane.
6. Do not install SkillClaw, synchronize skills, or edit/reset/restart the live
   DGX checkout without a dedicated reviewed ticket and explicit deployment
   authorization.
7. Refresh this handover with verified remote-repository and DGX facts before
   ending the next session.
