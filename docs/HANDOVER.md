# Project Handover - hermes-agent

**Plan key:** `hermes-agent`  
**Last verified:** 2026-08-10 18:22:48 +08:00  
**Handover owner/session:** Codex  
**Authoritative project log:** [`docs/ROADMAP-HERMES-DGX.md`](ROADMAP-HERMES-DGX.md)

## 1. Project identity and boundary

- **Purpose:** Hermes is a private-first agent gateway and CLI for messaging,
  model routing, approvals, runtime state, and scheduled automation.
- **In scope:** Hermes CLI/gateway code, runtime-state integration, Telegram
  and other platform adapters, model catalog handling, CI reliability, and
  Hermes project documentation.
- **Out of scope:** Laptop local files/checkouts as handover objects, unrelated
  DGX services, Open WebUI/Ollama stacks, and external agent credentials or
  tokens. Claude-owned changes are not imported into this handover.
- **唯一交接对象:** Git repository `https://github.com/cwliao/hermes-agent.git`
  and DGX Spark Hermes home `/home/cwliao/.hermes`.
- **Remote/branch:** `origin` / `ticket/ci-baseline-001`.
- **Implementation commit:** `d20c481322f4a5303f4658ff1455217c0a200b7e`.
- **Handover state:** This file is the subsequent pushed handover record;
  verify the branch's latest HEAD from the remote before consuming it.
- **Local checkout rule:** A laptop checkout is only a staging workspace. Do
  not hand over while its intended worktree is dirty; finish, commit, and push
  in-scope changes first, while isolating unrelated local changes.
- **Runtime/deployment:** DGX Spark host `140.96.58.171`, Hermes checkout under
  `/home/cwliao/.hermes/hermes-agent`, user service `hermes-gateway.service`.
- **Entry points:** `hermes_cli.main`, `gateway.run`, messaging adapters,
  `docs/ROADMAP-HERMES-DGX.md`, and the ticket files under `docs/plans/`.

## 2. Goal and roadmap

- **Current goal:** Restore the blocking Python CI baseline without changing
  ARCH-001 runtime behavior or touching the live DGX checkout.
- **Completed and verified:**
  - ARCH-001 mainline reconciliation merged as `ec50a154eeb44e7206f24b7703f9032b8f97069c`.
  - ARCH-001 deployed to DGX; service was verified `active/running` after
    restart, with rollback ref
    `backup/pre-arch-001-deploy-20260810T031625Z`.
  - Deployment record merged to main as
    `5fb3d5cd1ee8358570e86d9fbce95e8e73dd584b`.
- **Active:** `CI-BASELINE-001` is implemented and pushed as `d20c48132`;
  draft PR [#3](https://github.com/cwliao/hermes-agent/pull/3) is open; GitHub
  Actions, review, merge, and deployment gates remain.
- **Deferred / not goals:** No DGX deploy or service restart for CI-BASELINE-001
  before remote CI and merge gates pass. Do not reset or edit Claude-owned
  dirty/diverged worktrees.
- **Next candidates:** `ARCH-002` runtime-state contract extension;
  `ARCH-003` audit/replay integration; `ARCH-004` redaction and SQLite/WAL
  safeguards. Start ARCH-002 only after CI-BASELINE-001 is green and merged.

## 3. Verified state

- **Tests:**
  - Five original CI failures: **5 passed** after implementation.
  - Affected modules: **184 passed** (26 adapter, 37 authorization, 84 model,
    37 startup-gating).
  - ARCH-001 regression set: **35 passed**.
  - Ruff, compileall, and `git diff --check`: passed.
- **Health:** Last DGX read-only check showed `hermes-gateway.service`
  `active/running` and no error entries in the checked recent journal window.
  Draft PR #3 is the active repository review object; the two prior main CI
  runs were red before CI-BASELINE-001 implementation.
- **UI/runtime smoke:** No new UI smoke was run for CI-BASELINE-001. The DGX
  gateway process remains the existing runtime evidence boundary.
- **Data/storage safety:** No DGX `.hermes` storage mutation was performed for
  CI-BASELINE-001. No secrets or tokens are stored in this handover.
- **Deployment:** DGX remains on `ec50a154eeb44e7206f24b7703f9032b8f97069c`.
  CI-BASELINE-001 has **not** been deployed. Rollback ref remains
  `backup/pre-arch-001-deploy-20260810T031625Z`.
- **Known risks:** GitHub Actions has recurring failures in Codex response
  normalization, unauthorized Telegram DM gating, OpenRouter model tests,
  and builtin CLI subcommand gating. The pushed implementation fixes them
  locally, but remote CI is not yet verified.

## 4. Next ticket

- **Ticket:** `CI-BASELINE-001 - Restore the blocking Python CI baseline`
- **Status:** `IMPLEMENTED_PENDING_CI` on pushed branch `d20c48132`, draft PR #3.
- **Scope:** `agent/codex_responses_adapter.py`, `gateway/run.py`,
  `hermes_cli/main.py`, affected tests, and the ticket/roadmap documents.
- **Dependencies:** Clean Hermes `main`; preserve ARCH-001 runtime-state
  behavior; no dependency on Claude or AGY for implementation.
- **Acceptance gates:** Five named failures pass; affected Python modules pass;
  ARCH-001 regression remains green; Ruff/OSV/attribution/uv/Docker/JS checks
  remain green; GitHub Actions `All required checks pass` is green; then code
  review, commit, push, merge, and only later consider deployment.
- **Open questions:** Whether remote OpenRouter catalog conditions remain
  stable after the test snapshot pin; verify in GitHub Actions rather than
  assuming local network behavior represents CI.

## 5. Safe continuation instructions

1. Read this file, `docs/ROADMAP-HERMES-DGX.md`, and
   `docs/plans/2026-08-10-ci-baseline-001.md`.
2. Verify the remote repository branch/HEAD and the DGX
   `/home/cwliao/.hermes` state before any action.
3. Treat laptop local files as staging only; do not hand over an unresolved
   dirty worktree, and never discard unrelated changes with reset/checkout.
4. Use the repository's managed environment for tests; do not use global Python
   as CI evidence.
5. Do not deploy CI-BASELINE-001 before remote CI and merge pass.
6. Never edit or reset `/home/cwliao/.hermes/hermes-agent` or Claude-owned
   runtime state without an explicit deployment gate.
7. Refresh this handover with verified remote-repository and DGX facts before
   ending the next session.
